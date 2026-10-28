#!/usr/bin/env python3
# Renders packages/debian/<name>/ into <repo>/packages/<name>/, the source
# area of the reprepro repository. The templates hold a debian/ folder.
import os
import re
from email.utils import format_datetime

import lib

REQUIRED = ["debian/control.tmpl", "debian/rules.tmpl", "debian/changelog.tmpl"]
ARCHITECTURES = {"amd64": "amd64", "arm64": "arm64"}
# Libraries need their -dev package to build; the runtime library is found by
# dh_shlibdeps (${shlibs:Depends})
DEPENDENCY_FIELDS = {
    "build": [("build_depends", "main")],
    "libraries": [("build_depends", "dev")],
    "runtime": [("depends", "main")],
    "development": [("build_depends", "dev"), ("depends", "dev")],
}
OPERATORS = {">=": ">=", "<=": "<=", "=": "=", ">": ">>", "<": "<<"}
DEV_SUFFIX = "-dev"
# Binary packages a dependency never means (see lib.drop_unwanted())
NOISE = ("-doc", "-dbg", "-dbgsym", "-src", "-tests")
# Packages for other targets: MinGW and the cross toolchains (gcc-14-cross, libc6-dev-arm64-cross, ...)
OTHER_TARGETS = re.compile(r"^mingw-|-cross(-|$)")


def pick(job, dep, package, kind):
    if kind == "dev":
        return lib.dev_package(package, dep["name"], DEV_SUFFIX)
    return lib.main_package(package, dep["name"], DEV_SUFFIX)


def render(job, dep, names):
    """libglfw3-dev (>= 3.4); releases that name it differently become alternatives (a | b)."""
    suffix = f" ({OPERATORS[dep['op']]} {dep['version']})" if dep.get("op") else ""
    return " | ".join(f"{name}{suffix}" for name in lib.releases_in_order(job, names))


# SPDX id (including the deprecated ids GitHub still returns) -> DEP-5 short name (debian/copyright)
LICENSES = {
    "MIT": "Expat",
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
    "LGPL-2.0": "LGPL-2", "LGPL-2.0-only": "LGPL-2", "LGPL-2.0-or-later": "LGPL-2+",
    "LGPL-2.1": "LGPL-2.1", "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1+",
    "LGPL-3.0": "LGPL-3", "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3+",
    "AGPL-3.0": "AGPL-3", "AGPL-3.0-only": "AGPL-3", "AGPL-3.0-or-later": "AGPL-3+",
    "BSD-2-Clause": "BSD-2-clause", "BSD-3-Clause": "BSD-3-clause",
}


def debian_license(value):
    return LICENSES.get(str(value), str(value))


def rfc2822(value):
    """The changelog's date: 'Mon, 19 Oct 2026 12:00:00 +0000'."""
    return format_datetime(lib.date_of(value))


FILTERS = {"debian_license": debian_license, "rfc2822": rfc2822}


def main(job):
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ["any"])
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, render, noise=NOISE, other_targets=OTHER_TARGETS)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, "packages", job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), executable={"debian/rules"}, filters=FILTERS)


if __name__ == "__main__":
    lib.run(main)
