#!/usr/bin/env python3
# Renders packages/void/<name>/ into <repo>/srcpkgs/<name>/ (a void-packages
# template folder: template, plus files/, patches/, ...).
import os
import re

import lib

REQUIRED = ["template.tmpl"]
# The trailing * also matches the musl architectures (x86_64-musl)
ARCHITECTURES = {"amd64": "x86_64*", "arm64": "aarch64*"}
# Build tools run on the build host (hostmakedepends), libraries are built
# against (makedepends, their -devel package). xbps-src finds the runtime
# libraries itself (shlibs)
DEPENDENCY_FIELDS = {
    "build": [("hostmakedepends", "main")],
    "libraries": [("makedepends", "dev")],
    "runtime": [("depends", "main")],
    "development": [("makedepends", "dev"), ("depends", "dev")],
}
# xbps can't pin a version without its revision: "=" becomes ">="
OPERATORS = {">=": ">=", "<=": "<=", "=": ">=", ">": ">", "<": "<"}
# Subpackages a dependency never means (see lib.drop_unwanted())
NOISE = ("-doc", "-dbg", "-static", "-src")
# Packages for other targets: the cross toolchains (cross-aarch64-linux-gnu, ...)
OTHER_TARGETS = re.compile(r"^cross-")

# xbps versions can't contain '-' (the name separator) or '_' (the revision separator)
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~]*$")
# An upstream version in xbps' syntax: '-', '_', ':', '/' and whitespace become '.'
xbpsver = lib.substitute(r"[-_:/\s]", ".")
FILTERS = {"xbpsver": xbpsver}


DEV_SUFFIX = "-devel"


def pick(job, dep, package, kind):
    if kind == "dev":
        return lib.dev_package(package, dep["name"], DEV_SUFFIX)
    return lib.main_package(package, dep["name"], DEV_SUFFIX)


def name_of(job, names):
    return lib.releases_in_order(job, names)[0]


def render_bare(job, dep, names):
    """hostmakedepends and makedepends name packages without versions: glfw-devel"""
    return name_of(job, names)


def render_depends(job, dep, names):
    """glfw>=3.4"""
    name = name_of(job, names)
    if not dep.get("op"):
        return name
    if dep["op"] == "=":
        job.warn(f"xbps can't depend on exactly {lib.describe(dep)}, so {job.name} depends on "
                 f"{name}>={dep['version']}")
    return f"{name}{OPERATORS[dep['op']]}{dep['version']}"


RENDER = {"hostmakedepends": render_bare, "makedepends": render_bare, "depends": render_depends}


def main(job):
    job.context["xbps_version"] = lib.package_version(job, "version", xbpsver, VERSION_RE, "1.2.3, 1.2.3rc1")
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, [])
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, RENDER, noise=NOISE,
                     other_targets=OTHER_TARGETS, prepare=lib.merge_split_packages)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, "srcpkgs", job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), filters=FILTERS)


if __name__ == "__main__":
    lib.run(main)
