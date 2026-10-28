#!/usr/bin/env python3
# Renders packages/alpine/<name>/ into <repo>/<name>/ (an APKBUILD folder, aports style).
# One APKBUILD serves every Alpine release, like the Arch PKGBUILDs.
import os
import re
from collections import Counter

import lib

REQUIRED = ["APKBUILD.tmpl"]
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
# Libraries need their -dev package to build; abuild finds the runtime
# libraries itself (so: dependencies)
DEPENDENCY_FIELDS = {
    "build": [("makedepends", "main")],
    "libraries": [("makedepends", "dev")],
    "runtime": [("depends", "main")],
    "development": [("makedepends", "dev"), ("depends", "dev")],
}
# "=" means any revision of the version, like Gentoo's ~
OPERATORS = {">=": ">=", "<=": "<=", "=": "~", ">": ">", "<": "<"}
# Subpackages a dependency never means (see lib.drop_unwanted())
NOISE = ("-doc", "-dbg", "-static", "-src", "-openrc", "-lang", "-pyc",
         "-bash-completion", "-zsh-completion", "-fish-completion")
# Packages for other targets: the MinGW cross toolchain
OTHER_TARGETS = re.compile(r"^mingw-w64-")

# Alpine's version syntax (apk's version.c): 1.2.3, 1.2.3a, 1.2.3_rc1, 1.2.3_p2
VERSION_RE = re.compile(r"^\d+(\.\d+)*[a-z]?(_(alpha|beta|pre|rc|cvs|svn|git|hg|p)\d*)*$")
# A leading v before the number, and the suffixes apk writes with _ (-rc1, .beta.2, p1)
UPSTREAM_VERSION_RE = re.compile(r"^v(?=\d)|[-_.~]?(alpha|beta|pre|rc|p)[-_.]?(\d*)(?=$|[-_.~])", re.IGNORECASE)
# An upstream version in Alpine's syntax: v1.0-rc1 -> 1.0_rc1, 2.0.0-beta.2 -> 2.0.0_beta2. Versions it
# can't translate stay as they are (and fail VERSION_RE)
apkver = lib.substitute(UPSTREAM_VERSION_RE, lambda m: f"_{m.group(1).lower()}{m.group(2)}" if m.group(1) else "")
FILTERS = {"apkver": apkver}


DEV_SUFFIX = "-dev"


def pick(job, dep, package, kind):
    if kind == "dev":
        return lib.dev_package(package, dep["name"], DEV_SUFFIX)
    return lib.main_package(package, dep["name"], DEV_SUFFIX)


def common_name(job, dep, names):
    """The name most releases use (the first release's on a tie): one APKBUILD can't tell them apart."""
    ordered = [names[rp] for rp in job.releases if rp in names] or list(names.values())
    counts = Counter(ordered)
    chosen = max(ordered, key=lambda n: counts[n])
    if len(counts) > 1:
        job.warn(f"the {job.distribution} releases call {dep['name']} {', '.join(counts)}: "
                 f"using {chosen} for all of them")
    return chosen


def render(job, dep, names):
    """glfw-dev>=3.4"""
    name = common_name(job, dep, names)
    return f"{name}{OPERATORS[dep['op']]}{dep['version']}" if dep.get("op") else name


def main(job):
    job.context["apk_version"] = lib.package_version(job, "pkgver", apkver, VERSION_RE, "1.2.3, 1.2.3_rc1")
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ["all"])
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, render, noise=NOISE,
                     other_targets=OTHER_TARGETS, prepare=lib.merge_split_packages)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), filters=FILTERS)


if __name__ == "__main__":
    lib.run(main)
