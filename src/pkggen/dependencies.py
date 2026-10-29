"""A package's `dependencies`: written once, by repology project name, and resolved per distribution.

    dependencies:
      build: [ cmake ]                    # tools needed to build
      libraries: [ "glfw >= 3.4" ]        # linked against: development files to build
      runtime: [ xdg-desktop-portal ]     # needed to run
      development: [ ]                    # development files to build and to run (SDKs)

An entry is "<project> [<op> <version>]", or a mapping
{name, version: "<op> <version>", <distribution>: "<literal>" | false}. A
name that is one of pkggen.yaml's own packages is that package. Everything
else is looked up on repology by the dependency generator (once per run, for
every package), and the packaging generators pick each release's package names.
"""
import json
import os
import re
import subprocess
import sys

from . import utils
from .version import PKGGEN_VERSION

ROLES = ("build", "libraries", "runtime", "development")
GENERATOR = "repology"
USER_AGENT = f"pkggen/{PKGGEN_VERSION} (+https://github.com/MadLadSquad/pkggen)"
# Repology project names: lowercase words, also with ':' (e.g. "python:requests")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]*$")
CONSTRAINT_RE = re.compile(r"^\s*(>=|<=|=|>|<)\s*([A-Za-z0-9][A-Za-z0-9_.+:~-]*)\s*$")
ENTRY_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.+:-]*)\s*(?:(>=|<=|=|>|<)\s*([A-Za-z0-9][A-Za-z0-9_.+:~-]*))?\s*$")
ENTRY_KEYS = {"name", "version"}


def parse_entry(entry):
    """{name, op, version, overrides} of one dependency, or raises ValueError with what's wrong."""
    if isinstance(entry, str):
        match = ENTRY_RE.match(entry)
        if not match:
            raise ValueError(f"{entry!r} isn't \"<project>\" or \"<project> <op> <version>\" (op: >=, <=, =, >, <)")
        return {"name": match.group(1), "op": match.group(2), "version": match.group(3), "overrides": {}}
    if not isinstance(entry, dict):
        raise ValueError(f"{entry!r} must be a string like \"glfw >= 3.4\" or a mapping with a 'name'")
    name = entry.get("name")
    if not (isinstance(name, str) and NAME_RE.match(name)):
        raise ValueError(f"{entry!r} needs a 'name': the dependency's repology project")
    op = version = None
    if entry.get("version") is not None:
        match = CONSTRAINT_RE.match(str(entry["version"]))
        if not match:
            raise ValueError(f"the 'version' of '{name}' must be \"<op> <version>\" like \">= 3.4\" "
                             f"(op: >=, <=, =, >, <), got {entry['version']!r}")
        op, version = match.groups()
    overrides = {}
    for key, value in entry.items():
        if key in ENTRY_KEYS:
            continue
        if value is not False and not (isinstance(value, str) and value.strip()):
            raise ValueError(f"'{name}.{key}' must be the dependency's name there (used as it is) or false "
                             f"(no dependency there), got {value!r}")
        overrides[key] = value.strip() if isinstance(value, str) else value
    return {"name": name, "op": op, "version": version, "overrides": overrides}


def entry_name(entry):
    """The project name of an entry, or None when it isn't valid (check_dependencies() reports that)."""
    try:
        return parse_entry(entry)["name"]
    except ValueError:
        return None


def merge(group, package):
    """A group's `dependencies` with a package's: the lists add up, and a package entry replaces the
    group's entry of the same name."""
    if not isinstance(group, dict) or not isinstance(package, dict):
        return package
    result = dict(group)
    for role, entries in package.items():
        inherited = group.get(role)
        if not isinstance(entries, list) or not isinstance(inherited, list):
            result[role] = entries
            continue
        own = {entry_name(e) for e in entries} - {None}
        result[role] = [e for e in inherited if entry_name(e) not in own] + entries
    return result


def check_dependencies(package, known):
    """Problems with a package's `dependencies`. `known` holds the distribution names."""
    name = package["name"]
    value = package.get("dependencies")
    where = f"'dependencies' of package '{name}'"
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{where} must be a mapping with the lists {', '.join(ROLES)}"]
    errors = []
    for role, entries in value.items():
        if role not in ROLES:
            errors.append(f"{where} has an unknown list '{role}' (known: {', '.join(ROLES)})")
            continue
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"'dependencies.{role}' of package '{name}' must be a list")
            continue
        for entry in entries:
            try:
                parsed = parse_entry(entry)
            except ValueError as e:
                errors.append(f"'dependencies.{role}' of package '{name}': {e}")
                continue
            unknown = sorted(set(parsed["overrides"]) - set(known))
            if unknown:
                errors.append(f"'dependencies.{role}' of package '{name}': '{parsed['name']}' has unknown "
                              f"key(s) {', '.join(unknown)} (name, version or a distribution: "
                              f"{', '.join(sorted(known))})")
    return errors


def parse(package):
    """{role: [entry]} of a valid package (every role is there)."""
    value = package.get("dependencies") or {}
    return {role: [parse_entry(entry) for entry in value.get(role) or []] for role in ROLES}


def local_packages(packages, distributions, builds_for):
    """pkggen.yaml's own packages, as dependencies: {name: {name, package, builds_for: [distributions]}}.

    `package` is the package's pkggen.yaml settings: the packaging generators
    read what they need from it (e.g. Gentoo's category).
    """
    return {package["name"]: {"name": package["name"], "package": package,
                              "builds_for": [d for d in distributions if builds_for(package, d)]}
            for package in packages}


def projects(packages, distributions, local, builds_for):
    """The repology projects the packages need on the given distributions, in order."""
    names = []
    for package in packages:
        dists = [d for d in distributions if builds_for(package, d)]
        if not dists:
            continue
        for entries in parse(package).values():
            for entry in entries:
                if entry["name"] in local or all(d in entry["overrides"] for d in dists):
                    continue  # one of pkggen's own packages, or every distribution has its own name
                names.append(entry["name"])
    return list(dict.fromkeys(names))


def releases(versions, rp_names, test_versions, release_key, aliases=None):
    """The releases a package is built for: its own `versions`, else the test's, else all of them.

    `aliases` maps target names to their rp-name (distributions.yaml's `targets`).
    """
    versions = versions or test_versions
    if not versions:
        return list(rp_names)
    by_key = {release_key(name): rp for name, rp in (aliases or {}).items()}
    keys = {release_key(by_key.get(release_key(v), v)) for v in versions}
    return [rp for rp in rp_names if release_key(rp) in keys]


def for_job(package, distribution, chosen, resolved, local):
    """The `dependencies` of a packaging job: every entry with this distribution's override, the local
    package it is (if any) and repology's packages for each of the `chosen` releases."""
    result = {}
    for role, entries in parse(package).items():
        result[role] = []
        for entry in entries:
            item = {"name": entry["name"], "op": entry["op"], "version": entry["version"], "local": None,
                    "releases": {}}
            if distribution in entry["overrides"]:
                item["override"] = entry["overrides"][distribution]
            elif entry["name"] in local:
                own = local[entry["name"]]
                item["local"] = {"name": own["name"], "packaged": distribution in own["builds_for"],
                                 "package": own["package"]}
            else:
                found = (resolved or {}).get(entry["name"]) or {}
                item["releases"] = {rp: found[rp] for rp in chosen if found.get(rp)}
            result[role].append(item)
    return result


def run_generator(names, rp_names, wildcards=(), env=None):
    """Run the dependency generator for `names`. Returns (projects, warning lines); raises RuntimeError."""
    script = os.path.join(utils.get_generators_path(), "dependencies", f"{GENERATOR}.py")
    job = {"projects": list(names), "repositories": list(rp_names), "wildcards": list(wildcards),
           "user_agent": USER_AGENT}
    process = subprocess.run([sys.executable, script], input=json.dumps(job), text=True, encoding="utf-8",
                             capture_output=True, env=env)
    lines = [utils.ANSI_RE.sub("", line).strip() for line in process.stderr.splitlines()]
    warnings = [line for line in lines if "Warning" in line]
    if process.returncode != 0:
        errors = [utils.ERROR_PREFIX_RE.sub("", line) for line in lines if "Error" in line]
        raise RuntimeError("\n".join(errors[-5:]) or f"the dependency generator exited with code {process.returncode}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{GENERATOR}.py printed invalid JSON ({e}): {process.stdout.strip()[:200]!r}")
    return result.get("projects") or {}, warnings + list(result.get("warnings") or [])
