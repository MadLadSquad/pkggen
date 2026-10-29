#!/usr/bin/env python3
# `pkggen test` (stage 5): build and install every package in a container per
# distribution release, then copy the finished repositories to pkggen-build/test/.
#
# The testing generator of a distribution (generators/testing/<dist>.py)
# describes the containers and the steps to run in them; this module starts
# them and runs the steps. Containers start and run their setup while the
# packages are still being fetched and generated on the host.
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import containers, generate, utils
from .generate import GREEN, RED, RESET, YELLOW, plural, say

TEST_KEYS = {"engine", "connection", "cpus", "memory", "jobs", "vm", "pull", "setup", "post-setup", "distributions"}
DISTRIBUTION_KEYS = {"versions", "setup", "post-setup"}
VM_KEYS = {"disk"}
STEP_KEYS = {"name", "run", "user", "workdir"}
ENGINES = ("podman", "docker")
PULL_POLICIES = ("always", "missing", "never", "newer")
TEST_DIR = "test"
# The outcome of every target's last run, read by `pkggen deploy`
RESULTS_FILE = "results.json"
NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
# Replaced in the values of a target's `env` by the number of jobs each container gets
JOBS_PLACEHOLDER = "${PKGGEN_JOBS}"
PUBLISH_PATTERNS = ("indexes", "metadata")


class Cancelled(Exception):
    pass


# --- Configuration ---------------------------------------------------------------

def step_name(script):
    first = script.strip().splitlines()[0] if script.strip() else "step"
    return first if len(first) <= 60 else first[:57] + "..."


def load_steps(value, where, errors):
    """A list of shell commands (strings) or {name, run, user, workdir} mappings."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"'{where}' must be a list of commands")
        return []
    steps = []
    for i, entry in enumerate(value):
        if isinstance(entry, str) and entry.strip():
            steps.append({"name": step_name(entry), "run": entry, "user": None, "workdir": None})
        elif isinstance(entry, dict) and isinstance(entry.get("run"), str) and entry["run"].strip():
            unknown = sorted(set(entry) - STEP_KEYS)
            if unknown:
                errors.append(f"'{where}[{i}]' has unknown keys: {', '.join(unknown)} "
                              f"(allowed: {', '.join(sorted(STEP_KEYS))})")
            for key in ("name", "user", "workdir"):
                if entry.get(key) is not None and not isinstance(entry[key], str):
                    errors.append(f"'{where}[{i}].{key}' must be a string")
            steps.append({"name": entry.get("name") or step_name(entry["run"]), "run": entry["run"],
                          "user": entry.get("user"), "workdir": entry.get("workdir")})
        else:
            errors.append(f"'{where}[{i}]' must be a command, or a mapping with a 'run' command")
    return steps


def load_test_config(config, distributions):
    """The `test` block of pkggen.yaml, checked and with defaults. Raises one error listing every problem."""
    block = config.get("test")
    if block is None:
        block = {}
    errors = []
    if not isinstance(block, dict):
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - 'test' must be a mapping")

    unknown = sorted(set(block) - TEST_KEYS)
    if unknown:
        errors.append(f"'test' has unknown keys: {', '.join(unknown)} (allowed: {', '.join(sorted(TEST_KEYS))})")

    settings = {"engine": None, "connection": None, "cpus": None, "memory": None, "jobs": None,
                "disk": None, "pull": None, "distributions": {}}

    engine = block.get("engine")
    if engine is not None and engine not in ENGINES:
        errors.append(f"'test.engine' must be one of {', '.join(ENGINES)}")
    settings["engine"] = engine

    connection = block.get("connection")
    if connection is not None and not (isinstance(connection, str) and connection.strip()):
        errors.append("'test.connection' must be the name of a podman connection or docker context")
    settings["connection"] = connection

    cpus = block.get("cpus")
    if cpus is not None:
        if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or cpus <= 0:
            errors.append("'test.cpus' must be a positive number")
        else:
            settings["cpus"] = cpus

    vm = block.get("vm") if isinstance(block.get("vm"), dict) else {}
    for where, value in (("test.memory", block.get("memory")), ("test.vm.disk", vm.get("disk"))):
        if value is None:
            continue
        try:
            mib = containers.parse_size(value)
        except ValueError:
            errors.append(f"'{where}' must be a size like 16G or 512M, got {value!r}")
            continue
        if where == "test.memory":
            settings["memory"] = mib
        else:
            settings["disk"] = max(1, mib // 1024)  # GiB, for podman machine --disk-size

    jobs = block.get("jobs")
    if jobs is not None:
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
            errors.append("'test.jobs' must be a whole number of at least 1")
        else:
            settings["jobs"] = jobs

    if block.get("vm") is not None:
        if not isinstance(block["vm"], dict):
            errors.append("'test.vm' must be a mapping")
        elif set(vm) - VM_KEYS:
            errors.append(f"'test.vm' has unknown keys: {', '.join(sorted(set(vm) - VM_KEYS))} "
                          f"(allowed: {', '.join(sorted(VM_KEYS))}). The VM's CPUs and RAM are 'test.cpus' and 'test.memory'")

    pull = block.get("pull")
    if pull is not None and pull not in PULL_POLICIES:
        errors.append(f"'test.pull' must be one of {', '.join(PULL_POLICIES)}")
    settings["pull"] = pull

    setup = load_steps(block.get("setup"), "test.setup", errors)
    post_setup = load_steps(block.get("post-setup"), "test.post-setup", errors)

    per_dist = block.get("distributions") or {}
    if not isinstance(per_dist, dict):
        errors.append("'test.distributions' must be a mapping of distribution names")
        per_dist = {}
    for dist in sorted(set(per_dist) - set(distributions)):
        errors.append(f"'test.distributions.{dist}' isn't a distribution pkggen can test "
                      f"(available: {', '.join(distributions) or 'none'})")

    # The release names of every distribution, to check `versions` against
    known = {dist["name"]: utils.release_names(dist) for dist in utils.load_distributions()}
    for dist in distributions:
        entry = per_dist.get(dist) or {}
        if not isinstance(entry, dict):
            errors.append(f"'test.distributions.{dist}' must be a mapping")
            entry = {}
        unknown = sorted(set(entry) - DISTRIBUTION_KEYS)
        if unknown:
            errors.append(f"'test.distributions.{dist}' has unknown keys: {', '.join(unknown)} "
                          f"(allowed: {', '.join(sorted(DISTRIBUTION_KEYS))})")
        versions = entry.get("versions")
        problem = generate.release_list_problem(dist, versions, known.get(dist))
        if problem:
            errors.append(problem)
            versions = None
        settings["distributions"][dist] = {
            "versions": versions,
            "setup": setup + load_steps(entry.get("setup"), f"test.distributions.{dist}.setup", errors),
            "post-setup": post_setup + load_steps(entry.get("post-setup"), f"test.distributions.{dist}.post-setup",
                                                  errors),
        }

    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))
    return settings


def release_of(target, known):
    """The release a chosen target is asked for by: its rp-name, or its name when the rp-name has several
    targets (Void's glibc and musl builds are both void_x86_64)."""
    rp = target.get("rp_name")
    shared = [t for t in known if t["distribution"] == target["distribution"] and t.get("rp_name") == rp]
    return rp if rp and len(shared) == 1 else target["name"]


def testing_dir():
    return os.path.join(utils.get_generators_path(), "testing")


def testable_distributions():
    """{name: rp-names} of the distributions with both a packaging and a testing generator."""
    generators = utils.get_generators_path()
    return {dist["name"]: list(dist.get("rp-names") or []) for dist in utils.load_distributions()
            if os.path.isfile(os.path.join(generators, "testing", f"{dist['name']}.py"))
            and os.path.isfile(os.path.join(generators, "packaging", f"{dist['name']}.py"))}


def select_distributions(config, available, distribution_filter):
    repositories = config.get("repositories") or {}
    if distribution_filter:
        unknown = sorted(set(distribution_filter) - set(available))
        if unknown:
            raise utils.GenericError(f"No testing generator for: {', '.join(unknown)} "
                                     f"(available: {', '.join(available) or 'none'})")
        missing = [d for d in distribution_filter if d not in repositories]
        if missing:
            raise utils.GenericError(f"pkggen.yaml has no 'repositories.{missing[0]}' block, so there's nothing to test "
                                     f"for {missing[0]}")
        return [d for d in available if d in distribution_filter]
    dists = [d for d in available if d in repositories]
    if not dists:
        raise utils.GenericError("Nothing to test: pkggen.yaml has no 'repositories' block for any of "
                                 f"{', '.join(available) or 'the distributions'}")
    return dists


# --- Testing generators ------------------------------------------------------------

def describe(dist, payload):
    """Run a testing generator and return its {targets, skipped, warnings}."""
    script = os.path.join(testing_dir(), f"{dist}.py")
    # The child writes UTF-8 even where the locale's code page isn't (Windows)
    process = subprocess.run([sys.executable, script], input=json.dumps(payload), text=True,
                             capture_output=True, encoding="utf-8", errors="replace",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if process.returncode != 0:
        raise RuntimeError(utils.ANSI_RE.sub("", process.stderr).strip()
                           or f"the testing generator exited with code {process.returncode}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{os.path.basename(script)} printed invalid JSON ({e}): {process.stdout.strip()[:200]!r}")
    targets = result.get("targets") if isinstance(result, dict) else None
    if not isinstance(targets, list) or not all(isinstance(t, dict) and t.get("name") and t.get("image")
                                                for t in targets):
        raise RuntimeError(f"{os.path.basename(script)} returned no 'targets' list, or a target without a "
                           "'name' and 'image'")
    skipped = result.get("skipped") or []
    if not isinstance(skipped, list) or not all(isinstance(t, dict) and t.get("name") for t in skipped):
        raise RuntimeError(f"{os.path.basename(script)} returned a 'skipped' entry without a 'name'")
    for t in targets:
        problem = publish_problem(t.get("publish"))
        if problem:
            raise RuntimeError(f"{os.path.basename(script)} returned a target ({t['name']}) whose 'publish' {problem}")
    for t in targets + skipped:
        t["distribution"] = dist
    result["skipped"] = skipped
    return result


def publish_problem(publish):
    """Why a target's `publish` (how its repository is uploaded) is malformed, or None.

    {indexes: [regexes], metadata: [regexes], content_types: {suffix: media type}}, all optional.
    """
    if publish is None:
        return None
    if not isinstance(publish, dict):
        return "isn't a mapping"
    unknown = sorted(set(publish) - set(PUBLISH_PATTERNS) - {"content_types"})
    if unknown:
        return f"has unknown keys: {', '.join(unknown)}"
    for key in PUBLISH_PATTERNS:
        patterns = publish.get(key) or []
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            return f"'{key}' isn't a list of regular expressions"
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                return f"'{key}' has an invalid regular expression {pattern!r}: {e}"
    types = publish.get("content_types") or {}
    if not isinstance(types, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in types.items()):
        return "'content_types' isn't a mapping of file name suffixes to media types"
    return None


def payload(dist, config, settings, rp_names, host_arch, packages=()):
    repositories = config.get("repositories") or {}
    dist_settings = settings["distributions"][dist]
    return {
        "distribution": dist,
        "repository": repositories.get(dist) or {},
        "test": {"setup": dist_settings["setup"], "post-setup": dist_settings["post-setup"]},
        "rp_names": rp_names,
        "versions": dist_settings["versions"],
        "host_arch": host_arch,
        "packages": list(packages),
    }


def limit_packages(target, contexts):
    """Drop the packages whose `distributions` don't include this release. Returns their names."""
    kept, dropped = [], []
    for package in target.get("packages") or []:
        # A malformed entry is kept, so the worker reports it for this target only
        context = contexts.get(package.get("name")) if isinstance(package, dict) else None
        if context is None or generate.tested_on(context, target["distribution"], target["name"], target.get("rp_name") or ""):
            kept.append(package)
        else:
            dropped.append(package["name"])
    target["packages"] = kept
    target["excluded"] = dropped
    return dropped


def package_entries(dist, pipeline):
    """The packages that were fetched and packaged for dist, in pkggen.yaml order."""
    distribution = next((d for d in pipeline.distributions if d.name == dist), None)
    if distribution is None:
        return None
    entries = []
    for package, _ in pipeline.packages:
        name = package["name"]
        result = pipeline.packaged.get((dist, name))
        if result is None:
            continue
        output = result.get("output") or ""
        if output:
            absolute = os.path.join(utils.get_run_path(), output)
            output = os.path.relpath(absolute, distribution.repo_path).replace(os.sep, "/")
        entries.append({"context": pipeline.contexts[name], "output": output})
    return entries


# --- Running a target --------------------------------------------------------------

class TargetResult:
    def __init__(self, target, log_path):
        self.name = target["name"]
        self.distribution = target["distribution"]
        self.log_path = log_path
        self.failures = []       # generate.Failure
        self.built = 0
        self.packages = 0
        self.skipped = None      # reason, when the target never ran
        self.output = None
        self.executables = None   # files of the output that are executable in the container, relative to it
        self.package_status = {}  # package name -> True once it's built and installed
        self.excluded = []        # packages whose `distributions` leave out this release
        self.publish = target.get("publish") or {}  # how deployment generators upload its repository

    @property
    def ok(self):
        return not self.failures and not self.skipped

    def fail(self, package, message):
        self.failures.append(generate.Failure("test", package, message, self.name))


class Runner:
    """Runs every target of a `pkggen test` run."""

    def __init__(self, engine, session, resources, settings, test_dir, keep):
        self.engine = engine
        self.session = session
        self.resources = resources
        self.settings = settings
        self.test_dir = test_dir
        self.keep = keep
        self.cancelled = threading.Event()
        self.ready = {}      # distribution -> Event, set once its packages are known
        self.plans = {}      # distribution -> {target name: target}, or a reason to skip
        self.repo_paths = {}

    def env(self, target):
        """The container's environment: the job count, then the target's `env`.

        Testing generators set their build tools' variables in `env`, with
        ${PKGGEN_JOBS} for the job count (e.g. Gentoo's MAKEOPTS: -j${PKGGEN_JOBS}).
        """
        threads = str(self.resources["threads"])
        env = {"PKGGEN_JOBS": threads, "MAKEFLAGS": f"-j{threads}"}
        env.update({str(k): str(v).replace(JOBS_PLACEHOLDER, threads) for k, v in (target.get("env") or {}).items()})
        return env

    def run_steps(self, container, steps, log, result, package, what):
        """Run steps in order. Returns True when they all succeeded."""
        for step in steps:
            if self.cancelled.is_set():
                raise Cancelled()
            log.write(f"\n==> {what}: {step.get('name') or 'step'}\n".encode("utf-8"))
            log.flush()
            code, tail = self.engine.exec(container, step, log)
            if code != 0:
                lines = "\n".join(tail[-5:])
                result.fail(package, f"{what} failed: '{step.get('name')}' exited with code {code}\n{lines}".rstrip())
                return False
        return True

    def run_target(self, target):
        safe = NAME_RE.sub("-", target["name"])
        log_path = os.path.join(self.test_dir, f"{safe}.log")
        result = TargetResult(target, log_path)
        container = f"pkggen-{self.session}-{safe}"
        created = []
        try:
            with open(log_path, "wb") as log:
                try:
                    self._run(target, container, created, log, result)
                except Cancelled:
                    result.skipped = "cancelled"
                except containers.EngineError as e:
                    result.fail("*", str(e))
                except Exception as e:  # a bug, or a malformed target: only this target fails
                    result.fail("*", f"unexpected {type(e).__name__}: {e}")
                finally:
                    if not self.keep:
                        self.engine.remove(*reversed(created))
        except OSError as e:
            result.fail("*", f"couldn't write {utils.display_path(log_path)}: {e}")
        self.report(result)
        return result

    def _run(self, target, container, created, log, result):
        name = target["name"]
        dist = target["distribution"]
        pull = self.settings.get("pull")
        volumes = []
        for i, data in enumerate(target.get("data_images") or []):
            data_name = f"{container}-data{i}"
            say(f"[{name}] creating the data container for {data['image']}")
            created.append(data_name)
            self.engine.create_data(data_name, data["image"], session=self.session,
                                    platform=target.get("platform"), pull=pull)
            volumes.append(data_name)

        say(f"[{name}] starting {target['image']}")
        created.append(container)
        self.engine.start(container, target["image"], session=self.session, env=self.env(target),
                          cpus=self.resources["cpus"], memory=self.resources["memory"],
                          platform=target.get("platform"), volumes_from=volumes, pull=pull)
        if not self.run_steps(container, target.get("setup") or [], log, result, "*", "setup"):
            return

        if not self.ready[dist].is_set():
            say(f"[{name}] set up, waiting for the packages to be generated")
        self.ready[dist].wait()
        if self.cancelled.is_set():
            raise Cancelled()
        plan = self.plans.get(dist)
        if not isinstance(plan, dict) or name not in plan:
            result.skipped = plan if isinstance(plan, str) else f"the testing generator for {dist} dropped {name}"
            return
        full = plan[name]

        self.engine.copy_in(container, self.repo_paths[dist], full.get("repo") or "/pkggen/repo")
        if not self.run_steps(container, full.get("prepare") or [], log, result, "*", "prepare"):
            return

        for pname in full.get("excluded") or []:
            result.excluded.append(pname)
            say(f"{YELLOW}-{RESET} [{name}] {pname} isn't built on {name} (see its 'distributions')")
        result.packages = len(full.get("packages") or [])
        for package in full.get("packages") or []:
            pname = package["name"]
            result.package_status[pname] = False
            if not self.run_steps(container, package.get("build") or [], log, result, pname, "build"):
                say(f"{RED}✗{RESET} [{name}] {pname} failed to build")
                continue
            if not self.run_steps(container, package.get("install") or [], log, result, pname, "install"):
                say(f"{RED}✗{RESET} [{name}] {pname} failed to install")
                continue
            result.built += 1
            result.package_status[pname] = True
            say(f"{GREEN}✓{RESET} [{name}] {pname} built and installed")

        if not self.run_steps(container, full.get("finalize") or [], log, result, "*", "finalize"):
            return
        if not result.failures:
            self.run_steps(container, full.get("post_setup") or [], log, result, "*", "post-setup")

        if full.get("collect"):
            output = os.path.join(self.test_dir, NAME_RE.sub("-", name))
            self.engine.copy_out(container, full["collect"], output)
            result.output = output
            result.executables = self.engine.list_executables(container, full["collect"])
            if result.executables is None:
                log.write(b"pkggen: couldn't list the executable files of the output (find failed), so the "
                          b"deployment can't restore their executable bits where the file system has none\n")

    def report(self, result):
        log = utils.display_path(result.log_path)
        if result.skipped:
            say(f"{YELLOW}-{RESET} [{result.name}] skipped: {result.skipped}")
        elif result.ok:
            where = f" → {utils.display_path(result.output)}" if result.output else ""
            say(f"{GREEN}✓{RESET} [{result.name}] passed: {plural(result.built, 'package')} built and installed{where}")
        else:
            say(f"{RED}✗{RESET} [{result.name}] failed, see {log}")


def print_summary(results, pipeline_failures):
    passed = sum(1 for r in results if r.ok)
    built = sum(r.built for r in results)
    total = sum(r.packages for r in results)
    line = f"{passed}/{len(results)} targets passed, {built}/{total} package builds succeeded"
    failures = [f for r in results for f in r.failures]
    skipped = [r for r in results if r.skipped]
    say("")
    if not failures and not skipped and not pipeline_failures:
        say(f"{GREEN}Done:{RESET} {line}.")
        return
    problems = len(failures) + len(skipped) + len(pipeline_failures)
    say(f"{RED}Finished with {plural(problems, 'problem')}:{RESET} {line}.")
    if pipeline_failures:
        # generate.print_summary() listed them already, before the targets finished
        say(f"  {RED}✗{RESET} {plural(len(pipeline_failures), 'fetch or packaging problem')}, listed above")
    for r in results:
        if r.skipped:
            say(f"  {YELLOW}-{RESET} [{r.name}] skipped: {r.skipped}")
        for failure in r.failures:
            lines = failure.message.splitlines() or ["unknown error"]
            who = f"[{r.name}] {failure.package}" if failure.package != "*" else f"[{r.name}]"
            say(f"  {RED}✗{RESET} {who}: {lines[0]}")
            for extra in lines[1:]:
                say(f"      {extra}")
            say(f"      log: {utils.display_path(r.log_path)}")


# --- Results file --------------------------------------------------------------------

def test_output_dir():
    return os.path.join(utils.get_run_path(), utils.DEFAULT_BUILD_DIR, TEST_DIR)


def results_path():
    return os.path.join(test_output_dir(), RESULTS_FILE)


def load_results():
    """The results file as {"targets": {name: entry}}, or None when there is none yet."""
    path = results_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise utils.GenericError(f"Couldn't read {utils.display_path(path)}: {e}. Run `pkggen test` again")
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise utils.GenericError(f"{utils.display_path(path)} isn't a pkggen test results file. Run `pkggen test` again")
    return data


def write_results(results, host_skipped, contexts, host_arch):
    """Record every target of this run in the results file, keeping the other targets' entries.

    host_skipped are the releases with no image for this host's architecture:
    they're recorded as not passed, so an older result can't be deployed.
    """
    try:
        data = load_results() or {"targets": {}}
    except utils.GenericError:
        data = {"targets": {}}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in results:
        entry = {
            "distribution": r.distribution,
            "ok": r.ok,
            "finished": now,
            "output": os.path.basename(r.output) if r.output else None,
            "packages": {name: {"version": str((contexts.get(name) or {}).get("version", "")), "ok": ok}
                         for name, ok in r.package_status.items()},
            "publish": r.publish,
        }
        if r.output and r.executables is not None:
            entry["executables"] = r.executables
        # Not built here on purpose: not a failure, but nothing to deploy from this target either
        for name in r.excluded:
            entry["packages"][name] = {"version": str((contexts.get(name) or {}).get("version", "")), "ok": False,
                                       "skipped": f"its 'distributions' leave out {r.name}"}
        if r.skipped:
            entry["skipped"] = r.skipped
        elif r.failures:
            entry["failed"] = [f.message.splitlines()[0] if f.message else "unknown error" for f in r.failures]
        data["targets"][r.name] = entry
    for t in host_skipped:
        data["targets"][t["name"]] = {"distribution": t["distribution"], "ok": False, "finished": now,
                                      "output": None, "packages": {},
                                      "skipped": f"it has no {host_arch} container image"}
    path = results_path()
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temporary, path)


# --- Entry point -------------------------------------------------------------------

def test(package_filter=None, distribution_filter=None, target_filter=None, jobs=None, engine_name=None, keep=False):
    """Run stage 5. Returns the process exit code."""
    config = generate.load_config()
    available = testable_distributions()
    dists = select_distributions(config, list(available), distribution_filter)
    settings = load_test_config(config, list(available))
    # Check the packages before any container starts
    if not generate.load_packages(config, package_filter):
        raise utils.GenericError("pkggen.yaml defines no packages!")

    engine = containers.Engine.detect(engine_name or settings["engine"], settings["connection"])
    warnings, whole_machine = containers.prepare_host(engine, settings, say)
    try:
        info = engine.info()
    except containers.EngineError as e:
        raise utils.GenericError(f"The container engine isn't usable: {e}")

    def describe_all(names):
        matrix, skipped, found = [], [], []
        for dist in names:
            try:
                described = describe(dist, payload(dist, config, settings, available[dist], info["arch"]))
            except RuntimeError as e:
                raise utils.GenericError(f"The {dist} testing generator failed: {e}")
            found += [f"{dist}: {w}" for w in described.get("warnings") or []]
            matrix += described["targets"]
            skipped += described["skipped"]
        return matrix, skipped, found

    # Ask every testing generator for its targets
    matrix, skipped, found = describe_all(dists)
    if target_filter:
        # Releases that can't run on this host are known: they're skipped with a warning
        known = matrix + skipped
        names = {t["name"] for t in known} | {t.get("rp_name") for t in known}
        unknown = sorted(set(target_filter) - names)
        if unknown:
            raise utils.GenericError(f"Unknown test target(s): {', '.join(unknown)} "
                                     f"(available: {', '.join(t['name'] for t in matrix) or 'none'})")
        chosen = [t for t in known if t["name"] in target_filter or t.get("rp_name") in target_filter]
        # Ask again for only these releases, so the warnings are about them too
        dists = [d for d in dists if any(t["distribution"] == d for t in chosen)]
        for dist in dists:
            settings["distributions"][dist]["versions"] = [release_of(t, known) for t in chosen
                                                           if t["distribution"] == dist]
        skipped = [t for t in skipped if t in chosen]
        matrix, _, found = describe_all(dists)
    warnings += found
    for warning in warnings:
        say(f"{YELLOW}Warning: {warning}{RESET}")
    if not matrix:
        if skipped:  # so an older result for them can't be deployed
            os.makedirs(test_output_dir(), exist_ok=True)
            write_results([], skipped, {}, info["arch"])
        raise utils.GenericError("Nothing to test: no target can run on this machine")

    resources = containers.plan_resources(settings, len(matrix), info, whole_machine, jobs)
    for warning in resources["warnings"]:
        say(f"{YELLOW}Warning: {warning}{RESET}")
    limits = []
    if resources["cpus"]:
        limits.append(f"{resources['cpus']:g} CPUs")
    if resources["memory"]:
        limits.append(f"{resources['memory']} MiB RAM")
    say(f"Testing {plural(len(matrix), 'target')} with {engine.kind}, {resources['jobs']} at a time"
        + (f" ({' and '.join(limits)} each)" if limits else "") + ": " + ", ".join(t["name"] for t in matrix))

    output_dir = test_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    runner = Runner(engine, secrets.token_hex(4), resources, settings, output_dir, keep)
    tested = sorted({t["distribution"] for t in matrix}, key=dists.index)
    for dist in tested:
        runner.ready[dist] = threading.Event()
        runner.repo_paths[dist] = utils.get_repo_path(dist, config)

    pool = ThreadPoolExecutor(max_workers=resources["jobs"])
    try:
        futures = [pool.submit(runner.run_target, t) for t in matrix]

        # Fetch and package on the host while the containers start
        pipeline = generate.run_pipeline(config, package_filter, tested)
        generate.print_summary(pipeline.stats, len(pipeline.packages), pipeline.distributions, pipeline.failures)
        for dist in tested:
            entries = package_entries(dist, pipeline)
            if entries is None:
                runner.plans[dist] = f"the {dist} repository wasn't generated"
            elif not entries:
                runner.plans[dist] = f"no package was generated for {dist}"
            else:
                try:
                    described = describe(dist, payload(dist, config, settings, available[dist], info["arch"], entries))
                    for warning in described.get("warnings") or []:
                        if f"{dist}: {warning}" not in warnings:
                            say(f"{YELLOW}Warning: {dist}: {warning}{RESET}")
                    for t in described["targets"]:
                        limit_packages(t, pipeline.contexts)
                    runner.plans[dist] = {t["name"]: t for t in described["targets"]}
                except RuntimeError as e:
                    runner.plans[dist] = f"the testing generator failed: {e}"
            runner.ready[dist].set()

        results = [f.result() for f in futures]
    except BaseException:
        runner.cancelled.set()
        for event in runner.ready.values():
            event.set()
        if not keep:
            engine.remove_session(runner.session)
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    print_summary(results, pipeline.failures)
    try:
        write_results(results, skipped, pipeline.contexts, info["arch"])
    except OSError as e:
        say(f"{YELLOW}Warning: couldn't write {utils.display_path(results_path())}: {e}. "
            f"`pkggen deploy` needs it{RESET}")
    if keep:
        # The machine's connection may not be the default one (macOS, Windows)
        cli = " ".join([engine.kind] + engine.command(remote=True)[len(engine.argv):])
        say(f"Containers were kept: remove them with `{cli} rm -f -v $({cli} ps -aq "
            f"--filter label={containers.SESSION_LABEL}={runner.session})`")
    return 1 if pipeline.failures or any(not r.ok for r in results) else 0
