#!/usr/bin/env python3
# `pkggen generate`: fetch every package (stage 3), then render its packaging
# templates into every distribution's repository (stage 4).
#
# Both stages run generators as subprocesses that talk JSON over stdin/stdout.
# Packaging for a package starts as soon as its fetch finishes, while other
# packages are still downloading.
import json
import os
import queue
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from tqdm import tqdm

from . import dependencies, utils

# Top-level pkggen.yaml keys that aren't package groups
RESERVED_KEYS = {"repositories", "test", "deployment"}
FETCH_LAYER, PACKAGING_LAYER = "generation", "packaging"
# Keys of a package group that aren't defaults for its packages
GROUP_KEYS = {"generator", "packages"}
# Package keys that can't be set as group defaults: they differ per package
PACKAGE_ONLY_KEYS = ("maintainer", "author")
# Package keys exported by fetch generators when the package doesn't set them
EXPORTED_KEYS = ("description", "license", "homepage")

MAX_FETCH_WORKERS = 10
# Fetch generators keep API answers and artifact checksums here (see generation/lib.py)
CACHE_ENV = "PKGGEN_CACHE_DIR"
CACHE_DIR = "cache"

# A `templates` value names one folder in packages/<dist>/
TEMPLATE_FOLDER_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")

GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"

_print_lock = threading.Lock()


def say(message):
    with _print_lock:
        tqdm.write(message, file=sys.stderr)


class Failure:
    def __init__(self, stage, package, message, distribution=None):
        self.stage = stage
        self.package = package
        # Generators print "Error [pkg]: ..." in color; the summary adds its own label
        self.message = utils.ERROR_PREFIX_RE.sub("", utils.ANSI_RE.sub("", message)).strip()
        self.distribution = distribution

    def label(self):
        return f"[{self.distribution}] {self.package}" if self.distribution else f"{self.stage} {self.package}"


class Distribution:
    """A distribution to package for."""
    def __init__(self, name, script, repo_path, repository, rp_names=(), aliases=None):
        self.name = name
        self.script = script
        self.repo_path = repo_path
        self.repository = repository
        self.rp_names = list(rp_names)  # its releases in distributions.yaml
        self.aliases = dict(aliases or {})  # {target name: rp-name} of its `targets`


def deep_merge(base, override):
    """Merge two mappings; nested mappings are merged too, everything else is replaced."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def package_distributions(package):
    """The distributions a package is limited to: None (every one), or {distribution: versions}.

    `versions` is None (every release) or the rp-names/target names the
    package is tested on.
    """
    value = package.get("distributions")
    if value is None:
        return None
    if isinstance(value, list):
        return {dist: None for dist in value}
    return {dist: (spec or {}).get("versions") for dist, spec in value.items()}


def builds_for(package, distribution):
    allowed = package_distributions(package)
    return allowed is None or distribution in allowed


def release_key(name):
    """Target names and rp-names compare equal: 'ubuntu-24.04' and 'ubuntu_24_04' -> 'ubuntu_24_04'."""
    return re.sub(r"[-.]", "_", str(name)).lower()


def tested_on(package, distribution, target_name, rp_name):
    """Whether a package's `distributions` lets it be tested on this release."""
    allowed = package_distributions(package)
    if allowed is None:
        return True
    if distribution not in allowed:
        return False
    versions = allowed[distribution]
    if versions is None:
        return True
    keys = {release_key(v) for v in versions}
    return release_key(target_name) in keys or release_key(rp_name) in keys


def check_distributions(package, known):
    # known: {distribution name: its rp-names}
    """Problems with a package's `distributions`: a list of names, or {name: {versions: [...]}}."""
    name = package["name"]
    value = package["distributions"]
    where = f"'distributions' of package '{name}'"
    shape = (f"{where} must be a list of distributions like [ gentoo, arch ], or a mapping like "
             "{ arch: {}, debian: { versions: [ debian-13 ] } }")
    if isinstance(value, list):
        if not value or not all(isinstance(d, str) and d for d in value):
            return [shape]
        names = value
    elif isinstance(value, dict):
        if not value:
            return [shape]
        errors = []
        for dist, spec in value.items():
            if spec is None:
                continue
            if not isinstance(spec, dict) or set(spec) - {"versions"}:
                errors.append(f"{where}: '{dist}' must be {{}} or {{ versions: [...] }}")
                continue
            versions = spec.get("versions")
            if "versions" in spec and not (isinstance(versions, list) and versions
                                           and all(isinstance(v, str) and v for v in versions)):
                errors.append(f"{where}: '{dist}.versions' must be a non-empty list of releases "
                              "(rp-names or test target names, like debian_13 or debian-13)")
            elif versions and dist in known:
                releases = {release_key(rp): rp for rp in known[dist]}
                unknown = [v for v in versions if release_key(v) not in releases]
                if unknown:
                    errors.append(f"{where}: '{dist}.versions' names unknown release(s): {', '.join(unknown)} "
                                  f"(known: {', '.join(known[dist])})")
        if errors:
            return errors
        names = list(value)
    else:
        return [shape]
    unknown = [d for d in names if d not in known]
    if unknown:
        return [f"{where} names unknown distribution(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"]
    return []


def release_list_problem(distribution, versions, known=None):
    """Why `test.distributions.<distribution>.versions` is wrong, or None.

    It's a non-empty list of releases: rp-names or test target names (debian_13,
    debian-13), which are checked against `known` (the distribution's release
    names in distributions.yaml) when given.
    """
    where = f"'test.distributions.{distribution}.versions'"
    if versions is None:
        return None
    if not (isinstance(versions, list) and versions and all(isinstance(v, str) and v for v in versions)):
        return f"{where} must be a list of release names like debian_13 (the rp-names in distributions.yaml)"
    if known is not None:
        releases = {release_key(name) for name in known}
        unknown = [v for v in versions if release_key(v) not in releases]
        if unknown:
            return f"{where} names unknown release(s): {', '.join(unknown)} (known: {', '.join(known)})"
    return None


def check_test_versions(config, known):
    """Problems with every `test.distributions.<dist>.versions`. known: {distribution: its release names}.

    `pkggen generate` builds dependencies for these releases too, so a typo
    there is reported by every command, not only by `pkggen test`.
    """
    test = config.get("test")
    per_dist = test.get("distributions") if isinstance(test, dict) else None
    if not isinstance(per_dist, dict):
        return []  # `pkggen test` reports a malformed `test` block
    errors = []
    unknown = [d for d in per_dist if d not in known]
    if unknown:
        errors.append(f"'test.distributions' names unknown distribution(s): {', '.join(unknown)} "
                      f"(known: {', '.join(sorted(known))})")
    for dist, entry in per_dist.items():
        if dist in known and isinstance(entry, dict):
            problem = release_list_problem(dist, entry.get("versions"), known[dist])
            if problem:
                errors.append(problem)
    return errors


def check_package(package, known_distributions=None):
    """Problems with the optional fields every packaging generator reads."""
    name = package["name"]
    errors = []
    for key in PACKAGE_ONLY_KEYS:
        if key in package and not (isinstance(package[key], str) and package[key].strip()):
            errors.append(f"'{key}' of package '{name}' must be a string like \"Jane Doe <jane@example.com>\"")
    if "architectures" in package:
        value = package["architectures"]
        if value != "any" and not (isinstance(value, list) and value
                                   and all(isinstance(arch, str) and arch.strip() for arch in value)):
            errors.append(f"'architectures' of package '{name}' must be \"any\" or a list of names "
                          "like [ amd64, arm64 ]")
    if "templates" in package:
        value = package["templates"]
        if not (isinstance(value, str) and TEMPLATE_FOLDER_RE.match(value) and value not in (".", "..")):
            errors.append(f"'templates' of package '{name}' must be the name of a folder in "
                          f"packages/<distribution>/, like \"cmake-library\", got {value!r}")
    if "distributions" in package:
        errors += check_distributions(package, known_distributions or {})
    if "dependencies" in package:
        errors += dependencies.check_dependencies(package, known_distributions or {})
    return errors


def load_packages(config, package_filter):
    """Collect every package from every group, with group defaults applied.

    Returns a list of (package, fetch generator path). Raises one error that
    lists every problem found in pkggen.yaml.
    """
    available = utils.list_generators(FETCH_LAYER)

    known_distributions = {}  # filled from distributions.yaml when a package uses `distributions`
    errors = []
    packages = []
    seen = {}
    for key, group in config.items():
        if key in RESERVED_KEYS:
            continue
        if not isinstance(group, dict):
            errors.append(f"'{key}' must be a package group with 'generator' and 'packages' keys")
            continue

        generator = group.get("generator")
        entries = group.get("packages")
        group_errors = []
        if not generator:
            group_errors.append(f"group '{key}' has no 'generator'")
        elif generator not in available:
            group_errors.append(f"group '{key}' uses the fetch generator '{generator}', which isn't in "
                                f"{FETCH_LAYER}/ of {utils.shown_dirs()} (available: {', '.join(available) or 'none'})")
        if not isinstance(entries, list) or not entries:
            group_errors.append(f"group '{key}' needs a non-empty 'packages' list")
        for field in PACKAGE_ONLY_KEYS:
            if field in group:
                group_errors.append(f"'{field}' is set on group '{key}', but it can only be set per package: "
                                    f"move it into each entry of '{key}.packages'")
        if group_errors:
            errors += group_errors
            continue

        defaults = {k: v for k, v in group.items() if k not in GROUP_KEYS}
        script = utils.find_generator(FETCH_LAYER, generator)
        for i, package in enumerate(entries):
            if not isinstance(package, dict) or not package.get("name"):
                errors.append(f"'{key}.packages[{i}]' needs a 'name'")
                continue
            name = package["name"]
            if name in seen:
                errors.append(f"package '{name}' is defined in both '{seen[name]}' and '{key}'")
                continue
            seen[name] = key
            merged = deep_merge(defaults, package)
            if "dependencies" in defaults and "dependencies" in package:
                # A package's dependencies add to its group's
                merged["dependencies"] = dependencies.merge(defaults["dependencies"], package["dependencies"])
            package = merged
            if ("distributions" in package or "dependencies" in package) and not known_distributions:
                known_distributions.update({dist["name"]: utils.release_names(dist)
                                            for dist in utils.load_distributions()})
            errors += check_package(package, known_distributions)
            packages.append((package, script))

    if package_filter:
        unknown = sorted(set(package_filter) - set(seen))
        if unknown:
            errors.append(f"unknown package(s) passed to --packages: {', '.join(unknown)}")
        packages = [entry for entry in packages if entry[0]["name"] in package_filter]

    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))
    return packages


def load_distributions(config, distribution_filter, failures):
    """Distributions with a packaging generator and a `repositories.<dist>` block."""
    repositories = config.get("repositories") or {}

    known = utils.load_distributions()
    rp_names = {dist["name"]: list(dist.get("rp-names") or []) for dist in known}
    aliases = {dist["name"]: utils.target_aliases(dist) for dist in known}
    available = utils.list_generators(PACKAGING_LAYER)
    candidates = [name for name in rp_names if name in available]

    if distribution_filter:
        unknown = sorted(set(distribution_filter) - set(candidates))
        if unknown:
            raise utils.GenericError(f"No packaging generator for: {', '.join(unknown)} "
                                     f"(available: {', '.join(candidates) or 'none'})")
        candidates = [dist for dist in candidates if dist in distribution_filter]

    distributions = []
    for dist in candidates:
        if dist not in repositories:
            if distribution_filter:
                failures.append(Failure("package", "*", f"pkggen.yaml has no 'repositories.{dist}' block", dist))
            continue
        repo_path = utils.get_repo_path(dist, config)
        if not os.path.isdir(repo_path):
            failures.append(Failure("package", "*", f"the repository {utils.display_path(repo_path)} doesn't exist "
                                                    f"yet. Run `pkggen gen-repo {dist}` first", dist))
            continue
        distributions.append(Distribution(dist, utils.find_generator(PACKAGING_LAYER, dist), repo_path,
                                          repositories[dist] or {}, rp_names[dist], aliases[dist]))
    return distributions


def error_lines(stderr_text):
    """The error lines a generator printed, for the summary."""
    lines = [line for line in stderr_text.splitlines() if "Error" in line]
    return "\n".join(lines[-5:]) if lines else ""


def fetch_environment():
    """The fetch generators' environment: PKGGEN_CACHE_DIR defaults to pkggen-build/cache. Empty turns it off."""
    env = dict(os.environ)
    # Their output is read as UTF-8, also where the locale's code page isn't (Windows)
    env["PYTHONIOENCODING"] = "utf-8"
    if CACHE_ENV not in env:
        env[CACHE_ENV] = os.path.join(utils.get_run_path(), utils.DEFAULT_BUILD_DIR, CACHE_DIR)
    return env


def run_fetch(package, script, positions):
    """Run a fetch generator. Its stderr is shown live (progress bars) and kept for the summary."""
    position = positions.get()
    try:
        payload = json.dumps({**package, "position": position})
        process = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=utils.generator_env(FETCH_LAYER, fetch_environment()))

        captured = []

        def tee():
            for chunk in iter(lambda: process.stderr.read1(4096), b""):
                captured.append(chunk)
                with _print_lock:
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.buffer.flush()

        reader = threading.Thread(target=tee, daemon=True)
        reader.start()
        # Not communicate(): it would read stderr too, racing the tee thread
        try:
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass  # the generator exited early; its stderr says why
        stdout = process.stdout.read()
        process.wait()
        reader.join()
    finally:
        positions.put(position)

    stderr_text = b"".join(captured).decode("utf-8", "replace")
    if process.returncode != 0:
        raise RuntimeError(error_lines(stderr_text) or f"the fetch generator exited with code {process.returncode}")

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as e:
        preview = stdout.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"{os.path.basename(script)} printed invalid JSON ({e}): {preview!r}")

    if not isinstance(result, dict) or not result.get("version") or not isinstance(result.get("artifacts"), list):
        raise RuntimeError(f"{os.path.basename(script)} returned a result without a 'version' or 'artifacts' list")
    return result


def build_context(package, result):
    """The template context: the package's own keys plus everything the fetch generator found."""
    context = dict(package)
    exports = result.get("exports") or {}
    for key in EXPORTED_KEYS:
        if not context.get(key) and exports.get(key):
            context[key] = exports[key]
    for key in ("version", "date", "tag", "commit", "artifacts"):
        if key in result:
            context[key] = result[key]
    context["source"] = context["artifacts"][0] if context["artifacts"] else {}
    return context


def default_templates(distribution):
    """generators/templates/<distribution>/ from the highest-priority generators folder that has it.

    The base folder's when none has it, so the packaging generator's error names it.
    """
    return (utils.find_shared("templates", distribution, kind=f"the default {distribution} templates")
            or os.path.join(utils.base_generators_dir(), "templates", distribution))


def run_packaging(distribution, context, releases=None, package_dependencies=None):
    run_path = utils.get_run_path()
    templates_root = os.path.join(run_path, "packages", distribution.name)
    job = {
        "distribution": distribution.name,
        "package": context,
        "repository": distribution.repository,
        "repository_path": distribution.repo_path,
        # Packages with the same `templates` share one folder
        "templates_path": os.path.join(templates_root, context.get("templates") or context["name"]),
        "templates_root": templates_root,
        "default_templates_path": default_templates(distribution.name),
        "run_path": run_path,
        # The releases the package is built for, and its dependencies with repology's packages in them
        "releases": releases if releases is not None else list(distribution.rp_names),
        "dependencies": package_dependencies or {role: [] for role in dependencies.ROLES},
    }
    # The child writes UTF-8 even where the locale's code page isn't (Windows)
    process = subprocess.run([sys.executable, distribution.script], input=json.dumps(job), text=True,
                             capture_output=True, encoding="utf-8", errors="replace",
                             env=utils.generator_env(PACKAGING_LAYER))
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"the packaging generator exited with code {process.returncode}")
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{os.path.basename(distribution.script)} printed invalid JSON ({e}): {process.stdout.strip()[:200]!r}")


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def report_packaging(distribution, context, result):
    name = context["name"]
    for warning in result.get("warnings") or []:
        say(f"{YELLOW}Warning [{distribution.name}/{name}]: {warning}{RESET}")
    written = result.get("files_written") or []
    unchanged = result.get("files_unchanged") or []
    where = f" → {result['output']}" if result.get("output") else ""
    say(f"{GREEN}✓{RESET} [{distribution.name}] {name} {context['version']}{where} "
        f"({len(written)} written, {len(unchanged)} unchanged)")


def print_summary(stats, total_packages, distributions, failures):
    names = f" ({', '.join(d.name for d in distributions)})" if distributions else ""
    line = (f"{stats['fetched']}/{total_packages} packages fetched, "
            f"{stats['packaged']}/{stats['jobs']} distribution packages generated{names}")
    say("")
    if not failures:
        say(f"{GREEN}Done:{RESET} {line}.")
        return

    say(f"{RED}Finished with {plural(len(failures), 'problem')}:{RESET} {line}.")
    for failure in failures:
        lines = failure.message.splitlines() or ["unknown error"]
        say(f"  {RED}✗{RESET} {failure.label()}: {lines[0]}")
        for extra in lines[1:]:
            say(f"      {extra}")


@dataclass
class PipelineResult:
    """What run_pipeline() did, for callers that continue with the packages (`pkggen test`)."""
    packages: list       # (package, fetch generator path), in pkggen.yaml order
    distributions: list  # [Distribution]
    contexts: dict       # package name -> template context, for every fetched package
    packaged: dict       # (distribution, package name) -> packaging generator result
    failures: list       # [Failure]
    stats: dict


def load_config():
    """pkggen.yaml as a mapping. Creates the secrets file so generators can rely on it."""
    utils.create_secrets_file()
    config = utils.get_pkggen_config()
    if not isinstance(config, dict):
        raise utils.GenericError("pkggen.yaml is empty or isn't a mapping!")
    return config


def generate(package_filter=None, distribution_filter=None):
    """Run stages 3 and 4. Returns the process exit code."""
    result = run_pipeline(load_config(), package_filter, distribution_filter)
    print_summary(result.stats, len(result.packages), result.distributions, result.failures)
    return 1 if result.failures else 0


def run_pipeline(config, package_filter=None, distribution_filter=None):
    """Fetch every package and package it for every selected distribution."""
    packages = load_packages(config, package_filter)
    if not packages:
        raise utils.GenericError("pkggen.yaml defines no packages!")
    # Dependencies are rendered for the releases in `test.distributions.<dist>.versions`
    errors = check_test_versions(config, {dist["name"]: utils.release_names(dist)
                                          for dist in utils.load_distributions()})
    if errors:
        raise utils.GenericError("Problems found in pkggen.yaml:\n  - " + "\n  - ".join(errors))

    failures = []
    distributions = load_distributions(config, distribution_filter, failures)
    if not distributions:
        say(f"{YELLOW}Warning: no distribution to package for, so packages are only fetched. Add a "
            f"'repositories.<dist>' block and run `pkggen gen-repo <dist>` to package them.{RESET}")

    if distributions:
        names = [d.name for d in distributions]
        for package, _ in packages:
            if not any(builds_for(package, name) for name in names):
                say(f"{YELLOW}Warning: {package['name']} is only packaged for "
                    f"{', '.join(package_distributions(package))} (see its 'distributions'), so it's only fetched "
                    f"this time{RESET}")

    say(f"Fetching {plural(len(packages), 'package')}"
        + (f", packaging for {', '.join(d.name for d in distributions)}" if distributions else ""))

    # Dependencies are looked up once for every package, while the packages are fetched
    distribution_names = [d.name for d in distributions]
    everything = load_packages(config, None) if package_filter else packages
    local = dependencies.local_packages([p for p, _ in everything], distribution_names, builds_for)
    projects = dependencies.projects([p for p, _ in packages], distribution_names, local, builds_for)
    test_config = config.get("test") if isinstance(config.get("test"), dict) else {}

    fetch_workers = min(len(packages), MAX_FETCH_WORKERS)
    positions = queue.Queue()
    for position in range(fetch_workers):
        positions.put(position)

    stats = {"fetched": 0, "packaged": 0, "jobs": 0}
    stats_lock = threading.Lock()
    contexts = {}
    packaged = {}

    def on_packaged(future, distribution, context):
        try:
            result = future.result()
        except Exception as e:
            with stats_lock:
                failures.append(Failure("package", context["name"], str(e), distribution.name))
            say(f"{RED}✗{RESET} [{distribution.name}] {context['name']} failed")
            return
        with stats_lock:
            stats["packaged"] += 1
            packaged[(distribution.name, context["name"])] = result
        report_packaging(distribution, context, result)

    def look_up():
        rp_names = list(dict.fromkeys(rp for d in distributions for rp in d.rp_names))
        found, warnings = dependencies.run_generator(projects, rp_names, env=fetch_environment())
        for line in dict.fromkeys(warnings):
            say(f"{YELLOW}{utils.ANSI_RE.sub('', line).strip()}{RESET}")
        count = len(projects)
        say(f"{GREEN}✓{RESET} looked up {count} {'dependency' if count == 1 else 'dependencies'} on repology")
        return found

    def package_one(distribution, context):
        found = {}
        # Only packages that need something from repology wait for it
        if projects and dependencies.projects([context], [distribution.name], local, builds_for):
            try:
                found = looked_up.result()
            except Exception as e:
                raise RuntimeError(f"couldn't look its dependencies up: {e}")
        allowed = package_distributions(context)
        test_spec = (test_config.get("distributions") or {}).get(distribution.name) \
            if isinstance(test_config.get("distributions"), dict) else None
        releases = dependencies.releases(allowed.get(distribution.name) if allowed else None, distribution.rp_names,
                                         test_spec.get("versions") if isinstance(test_spec, dict) else None,
                                         release_key, distribution.aliases)
        return run_packaging(distribution, context, releases,
                             dependencies.for_job(context, distribution.name, releases, found, local))

    with ThreadPoolExecutor(max_workers=fetch_workers) as fetch_pool, \
            ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as package_pool, \
            ThreadPoolExecutor(max_workers=1) as dependency_pool:
        looked_up = dependency_pool.submit(look_up) if projects and distributions else None
        fetches = {fetch_pool.submit(run_fetch, package, script, positions): package
                   for package, script in packages}

        for future in as_completed(fetches):
            package = fetches[future]
            try:
                result = future.result()
            except Exception as e:
                with stats_lock:
                    failures.append(Failure("fetch", package["name"], str(e)))
                say(f"{RED}✗{RESET} couldn't fetch {package['name']}")
                continue

            stats["fetched"] += 1
            context = build_context(package, result)
            contexts[package["name"]] = context
            say(f"{GREEN}✓{RESET} fetched {package['name']} {context['version']} "
                f"({plural(len(context['artifacts']), 'artifact')})")
            # Start packaging right away, while other packages are still downloading
            for distribution in distributions:
                if not builds_for(package, distribution.name):
                    continue
                stats["jobs"] += 1
                job = package_pool.submit(package_one, distribution, context)
                job.add_done_callback(lambda f, d=distribution, c=context: on_packaged(f, d, c))
        # Leaving the `with` block waits for the remaining packaging jobs

    return PipelineResult(packages, distributions, contexts, packaged, failures, stats)
