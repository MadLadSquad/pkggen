#!/usr/bin/env python3
# Renders packages/rpm-based/<name>/ into the rpmbuild tree:
#   spec.tmpl        -> SPECS/<name>.spec
#   everything else  -> SOURCES/ (patches, desktop files, ...)
import os
import re
from collections import Counter

import lib

REQUIRED = ["spec.tmpl"]
# OpenMandriva builds x86_64 packages as znver1, which its %{x86_64} macro includes.
# openSUSE Leap doesn't define the macro, so fall back to plain x86_64 there
ARCHITECTURES = {"amd64": "%{?x86_64}%{!?x86_64:x86_64}", "arm64": "aarch64"}
# Libraries need their -devel package to build; the runtime library is found
# by rpmbuild's automatic dependencies
DEPENDENCY_FIELDS = {
    "build": [("build_requires", "main")],
    "libraries": [("build_requires", "dev")],
    "runtime": [("requires", "main")],
    "development": [("build_requires", "dev"), ("requires", "dev")],
}
# The families share one spec: a dependency they name differently is written in
# an %if block per family. rp-name prefix -> (family, the spec condition)
FAMILIES = {
    "fedora": "0%{?fedora}",
    "opensuse": "0%{?suse_version}",
    "openmandriva": "0%{?mdvver}",
}
DEV_SUFFIX = "-devel"
# OpenMandriva names its libraries lib64<name> on 64-bit architectures
LIBRARY_PREFIXES = ("lib", "lib64")
# Subpackages a dependency never means (see lib.drop_unwanted()); -devel-doc and
# -devel-static end in -doc and -static
NOISE = ("-doc", "-docs", "-debuginfo", "-debugsource", "-static", "-src", "-test", "-tests", "-lang",
         "-bash-completion", "-zsh-completion", "-fish-completion")
# Packages for other targets: the MinGW toolchains (mingw-, mingw32-, mingw64-, ucrt64-) and openSUSE's and
# Fedora's cross compilers (cross-aarch64-gcc14, cross-gcc)
OTHER_TARGETS = re.compile(r"^(mingw(32|64)?-|ucrt64-|cross-)|-cross-")


def family(rp_name):
    return rp_name.split("_", 1)[0]


def pick(job, dep, package, kind):
    if kind != "dev":
        return lib.main_package(package, dep["name"], DEV_SUFFIX, LIBRARY_PREFIXES)
    if lib.lists_binaries(package):
        return lib.dev_package(package, dep["name"], DEV_SUFFIX, LIBRARY_PREFIXES)
    # openSUSE Leap: repology only knows the binary package's own name, so go by the convention
    guess = f"{lib.srcname(package)}{DEV_SUFFIX}"
    job.warn(f"repology doesn't list the packages of {dep['name']} in {package.get('repo')}: guessed {guess}")
    return guess


def render(job, dep, names):
    """glfw-devel >= 3.4, or {"families": {family: name}} when the families name it differently."""
    suffix = f" {dep['op']} {dep['version']}" if dep.get("op") else ""
    by_family = {}
    for rp in job.releases or list(names):
        if rp in names:
            by_family.setdefault(family(rp), []).append(names[rp])
    if not by_family:  # a local package: the same name everywhere
        return f"{next(iter(names.values()))}{suffix}"
    chosen = {}
    for name, found in by_family.items():
        counts = Counter(found)
        chosen[name] = max(found, key=lambda n: counts[n])  # the most common, the first release's on a tie
        if len(counts) > 1:
            job.warn(f"the {name} releases call {dep['name']} {', '.join(counts)}: using {chosen[name]} for all of them")
    families = {family(rp) for rp in job.releases} or set(chosen)
    if set(chosen) == families and len(set(chosen.values())) == 1:
        return f"{next(iter(chosen.values()))}{suffix}"
    return {"families": {name: f"{value}{suffix}" for name, value in chosen.items()}}


def split_families(job, deps):
    """Move the per-family dependencies to deps["by_family"]: [{family, condition, build_requires, requires}]."""
    blocks = {}
    for field in ("build_requires", "requires"):
        common = []
        for value in deps[field]:
            if isinstance(value, dict):
                for name, rendered in value["families"].items():
                    if name not in FAMILIES:
                        job.warn(f"pkggen can't tell {name} releases apart in a spec, so {rendered} is left out")
                        continue
                    block = blocks.setdefault(name, {"family": name, "condition": FAMILIES[name],
                                                     "build_requires": [], "requires": []})
                    if rendered not in block[field]:
                        block[field].append(rendered)
            else:
                common.append(value)
        deps[field] = common
    deps["by_family"] = [blocks[name] for name in FAMILIES if name in blocks]


# Version can't contain '-'; pkggen also replaces ':', '/' and whitespace, like Arch's pkgver
pkgver = lib.substitute(r"[-:/\s]", "_")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def rpmdate(value):
    """The %changelog date: 'Mon Oct 19 2026' (always English, unlike strftime)."""
    date = lib.date_of(value)
    return f"{DAYS[date.weekday()]} {MONTHS[date.month - 1]} {date.day:02d} {date.year}"


FILTERS = {"pkgver": pkgver, "rpmdate": rpmdate}


def main(job):
    # "any" means no ExclusiveArch line
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, [])
    split_families(job, lib.dependencies(job, DEPENDENCY_FIELDS, pick, render, noise=NOISE,
                                         other_targets=OTHER_TARGETS))
    lib.ensure_templates(job, REQUIRED)

    def place(rel):
        if rel == "spec":
            return os.path.join(job.repository_path, "SPECS", f"{job.name}.spec")
        return os.path.join(job.repository_path, "SOURCES", rel)

    job.output = job.repository_path
    lib.render_tree(job, place, filters=FILTERS)


if __name__ == "__main__":
    lib.run(main)
