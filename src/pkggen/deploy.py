#!/usr/bin/env python3
# `pkggen deploy` (stage 6): push the repositories `pkggen test` built and
# checked (pkggen-build/test/<target>/) to production repositories.
#
# `deployment.<distribution>` in pkggen.yaml holds one deployment, or a list of
# them, each naming a deployment generator (generators/deployment/<name>.py).
# Only targets whose last test passed are deployed, unless --skip-tests is given.
# Every generator first checks its configuration, so problems are reported
# before anything is pushed; then the deployments run one after the other.
import json
import os
import subprocess
import sys
import threading

import generate
import testing
import utils
from generate import GREEN, RED, RESET, YELLOW, plural, say

DEPLOY_DIR = "deploy"
SCRIPT_ENV = {"PYTHONIOENCODING": "utf-8"}


# Deployment generators that push with git


class Deployment:
    def __init__(self, distribution, where, generator, config, ident):
        self.distribution = distribution
        self.where = where
        self.generator = generator
        self.config = config
        self.id = ident
        self.targets = []           # payloads of the targets with test output
        self.skipped_targets = []   # names of the selected targets without
        self.skipped = []           # [(name, reason)]
        self.result = None
        self.error = None

    @property
    def script(self):
        return os.path.join(deployment_dir(), f"{self.generator}.py")


def deployment_dir():
    return os.path.join(utils.get_generators_path(), "deployment")


def available_generators():
    directory = deployment_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(entry.name[:-3] for entry in os.scandir(directory)
                  if entry.is_file() and entry.name.endswith(".py") and entry.name != "lib.py")


# --- Configuration -----------------------------------------------------------------

def load_deployments(config, distribution_filter):
    """Every deployment in pkggen.yaml, in order. Raises one error listing every problem."""
    block = config.get("deployment")
    if block is None or block == {}:
        raise utils.GenericError("Nothing to deploy: pkggen.yaml has no 'deployment' block")
    if not isinstance(block, dict):
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - 'deployment' must be a mapping of "
                                 "distribution names to deployments")
    distributions = [d["name"] for d in utils.load_distributions()]
    generators = available_generators()
    errors = []
    deployments = []
    ids = {}

    if distribution_filter:
        unknown = sorted(set(distribution_filter) - set(block))
        if unknown:
            errors.append(f"pkggen.yaml has no deployment for: {', '.join(unknown)} "
                          f"(deployed: {', '.join(block) or 'none'})")

    for dist, value in block.items():
        if dist not in distributions:
            errors.append(f"'deployment.{dist}' isn't a distribution pkggen knows "
                          f"(available: {', '.join(distributions)})")
            continue
        entries = value if isinstance(value, list) else [value]
        for i, entry in enumerate(entries):
            where = f"deployment.{dist}" + (f"[{i}]" if isinstance(value, list) else "")
            if not isinstance(entry, dict):
                errors.append(f"'{where}' must be a mapping with a 'generator' key")
                continue
            name = entry.get("generator")
            if not isinstance(name, str) or name not in generators:
                errors.append(f"'{where}.generator' must name a deployment generator in {deployment_dir()} "
                              f"(available: {', '.join(generators) or 'none'})")
                continue
            targets = entry.get("targets")
            if targets is not None and not (isinstance(targets, list) and targets
                                            and all(isinstance(t, str) and t for t in targets)):
                errors.append(f"'{where}.targets' must be a list of test target names like fedora-44")
                continue
            base = f"{dist}-{name}"
            ids[base] = ids.get(base, 0) + 1
            ident = base if ids[base] == 1 else f"{base}-{ids[base]}"
            if not distribution_filter or dist in distribution_filter:
                deployments.append(Deployment(dist, where, name, entry, ident))

    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))
    return deployments


# --- The test gate -----------------------------------------------------------------

def not_passed_reason(entry):
    if entry.get("skipped"):
        reason = f"its last test was skipped: {entry['skipped']}"
        if "container image" in entry["skipped"]:
            reason += ". Test it on a machine with that architecture, or with a remote 'test.connection'"
        return reason
    failed = entry.get("failed") or []
    return f"its last test failed ({failed[0]})" if failed else "its last test failed"


def select_targets(deployments, results, target_filter, skip_tests):
    """Attach the test output of every deployment's targets. Returns the deployments that have something to deploy."""
    targets = (results or {}).get("targets") or {}
    errors = []
    if target_filter:
        unknown = sorted(set(target_filter) - set(targets))
        if unknown:
            errors.append(f"never tested: {', '.join(unknown)} (tested: {', '.join(sorted(targets)) or 'none'})")

    selected = []
    output_root = testing.test_output_dir()
    for dep in deployments:
        configured = dep.config.get("targets")
        if configured:
            for name in configured:
                other = (targets.get(name) or {}).get("distribution")
                if other and other != dep.distribution:
                    errors.append(f"'{dep.where}.targets' has {name}, which is a {other} target")
            names = list(configured)
        else:
            names = sorted(n for n, e in targets.items() if e.get("distribution") == dep.distribution)
        if target_filter:
            names = [n for n in names if n in target_filter]
            if not names:
                continue  # -t picked other distributions' targets

        for name in names:
            entry = targets.get(name)
            if entry is None:
                dep.skipped_targets.append(name)
                dep.skipped.append((name, f"it hasn't been tested yet: run `pkggen test -t {name}`"))
                continue
            path = os.path.join(output_root, entry.get("output") or testing.NAME_RE.sub("-", name))
            if not os.path.isdir(path):
                dep.skipped_targets.append(name)
                reason = not_passed_reason(entry) if not entry.get("ok") else "its test output is missing"
                dep.skipped.append((name, f"{reason}: run `pkggen test -t {name}`" if entry.get("ok") else reason))
                continue
            dep.targets.append({
                "name": name, "path": path, "ok": bool(entry.get("ok")),
                "reason": None if entry.get("ok") else not_passed_reason(entry),
                "packages": entry.get("packages") or {},
                "executables": entry.get("executables"),
                # How its repository is uploaded, from its testing generator
                "publish": entry.get("publish") or {},
            })
        if not dep.targets and not names:
            dep.skipped.append(("*", f"{dep.distribution} has no tested target: run `pkggen test -d {dep.distribution}`"))
        selected.append(dep)

    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))
    if target_filter and not selected:
        raise utils.GenericError(f"No deployment uses the target(s) {', '.join(target_filter)}")
    return selected


# --- Running the generators --------------------------------------------------------

def job(dep, config, package_filter, skip_tests, dry_run, check):
    return {
        "check": check,
        "id": dep.id,
        "where": dep.where,
        "distribution": dep.distribution,
        "deployment": dep.config,
        "repository": (config.get("repositories") or {}).get(dep.distribution) or {},
        "run_path": utils.get_run_path(),
        "work_path": os.path.join(utils.get_run_path(), utils.DEFAULT_BUILD_DIR, DEPLOY_DIR, dep.id),
        "targets": dep.targets,
        "skipped_targets": dep.skipped_targets,
        "packages": list(package_filter) if package_filter else None,
        "skip_tests": skip_tests,
        "interactive": sys.stdin.isatty() and sys.stderr.isatty(),
        "dry_run": dry_run,
    }


def parse_output(dep, stdout, keys):
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as e:
        preview = stdout.decode("utf-8", "replace").strip()[:200] if isinstance(stdout, bytes) else stdout[:200]
        raise RuntimeError(f"{dep.generator}.py printed invalid JSON ({e}): {preview!r}")
    if not isinstance(result, dict) or not all(isinstance(result.get(k, []), list) for k in keys):
        raise RuntimeError(f"{dep.generator}.py printed a result without {', '.join(keys)} lists")
    return result


def check_all(deployments, config, package_filter, skip_tests, dry_run):
    """Every generator checks its configuration. Raises one error listing every problem."""
    errors = []
    for dep in deployments:
        process = subprocess.run([sys.executable, dep.script], input=json.dumps(
            job(dep, config, package_filter, skip_tests, dry_run, check=True)), text=True, capture_output=True,
            encoding="utf-8", errors="replace", env={**os.environ, **SCRIPT_ENV})
        if process.returncode != 0:
            message = generate.ANSI_RE.sub("", process.stderr).strip() or f"exit code {process.returncode}"
            errors.append(f"'{dep.where}': the {dep.generator} deployment generator failed: {message}")
            continue
        try:
            result = parse_output(dep, process.stdout, ("errors", "warnings"))
        except RuntimeError as e:
            errors.append(f"'{dep.where}': {e}")
            continue
        errors += [str(e) for e in result.get("errors") or []]
        for warning in result.get("warnings") or []:
            say(f"{YELLOW}Warning [{dep.id}]: {warning}{RESET}")
    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))


def run_deployment(dep, payload):
    """Run a deployment generator. Its stderr (progress, prompts) is shown live and kept for the summary."""
    process = subprocess.Popen([sys.executable, dep.script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={**os.environ, **SCRIPT_ENV})
    captured = []

    def tee():
        for chunk in iter(lambda: process.stderr.read1(4096), b""):
            captured.append(chunk)
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    reader = threading.Thread(target=tee, daemon=True)
    reader.start()
    try:
        process.stdin.write(json.dumps(payload).encode("utf-8"))
        process.stdin.close()
    except BrokenPipeError:
        pass
    stdout = process.stdout.read()
    process.wait()
    reader.join()
    if process.returncode == 130:
        raise KeyboardInterrupt
    if process.returncode != 0:
        stderr = b"".join(captured).decode("utf-8", "replace")
        raise RuntimeError(generate.error_lines(stderr) or f"the deployment generator exited with code {process.returncode}")
    return parse_output(dep, stdout, ("pushed", "unchanged", "failed", "skipped"))


# --- Summary -----------------------------------------------------------------------

def print_summary(deployments, dry_run):
    pushed = sum(len(d.result["pushed"]) for d in deployments if d.result)
    unchanged = sum(len(d.result["unchanged"]) for d in deployments if d.result)
    problems = []
    for d in deployments:
        problems += [(YELLOW, "-", d.id, name, f"skipped: {reason}") for name, reason in d.skipped]
        if d.error:
            problems.append((RED, "✗", d.id, "*", d.error))
        if d.result:
            problems += [(YELLOW, "-", d.id, s.get("name", "*"), f"skipped: {s.get('reason')}") for s in d.result["skipped"]]
            problems += [(RED, "✗", d.id, f.get("name", "*"), f.get("message") or "failed") for f in d.result["failed"]]

    line = f"{pushed} {'would be pushed (dry run)' if dry_run else 'pushed'}, {unchanged} already up to date"
    say("")
    if not problems:
        say(f"{GREEN}Done:{RESET} {line}.")
        return 0
    say(f"{RED}Finished with {plural(len(problems), 'problem')}:{RESET} {line}.")
    for color, mark, ident, name, message in problems:
        lines = str(message).splitlines() or ["unknown error"]
        who = f"[{ident}]" if name == "*" else f"[{ident}] {name}"
        say(f"  {color}{mark}{RESET} {who}: {lines[0]}")
        for extra in lines[1:]:
            say(f"      {extra}")
    return 1


# --- Entry point -------------------------------------------------------------------

def deploy(package_filter=None, distribution_filter=None, target_filter=None, skip_tests=False, dry_run=False):
    """Run stage 6. Returns the process exit code."""
    config = generate.load_config()
    deployments = load_deployments(config, distribution_filter)
    if package_filter:
        generate.load_packages(config, package_filter)  # reports unknown names

    results = testing.load_results()
    if results is None:
        raise utils.GenericError(f"There's nothing tested to deploy yet ({utils.display_path(testing.results_path())} "
                                 "doesn't exist): run `pkggen test` first")
    deployments = select_targets(deployments, results, target_filter, skip_tests)

    runnable = [d for d in deployments if d.targets]
    check_all(runnable, config, package_filter, skip_tests, dry_run)

    for dep in runnable:
        say(f"Deploying {dep.id} ({', '.join(t['name'] for t in dep.targets)})"
            + (" as a dry run" if dry_run else ""))
        try:
            dep.result = run_deployment(dep, job(dep, config, package_filter, skip_tests, dry_run, check=False))
        except RuntimeError as e:
            dep.error = generate.ERROR_PREFIX_RE.sub("", generate.ANSI_RE.sub("", str(e))).strip()
            say(f"{RED}✗{RESET} [{dep.id}] failed")
    return print_summary(deployments, dry_run)
