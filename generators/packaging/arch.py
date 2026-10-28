#!/usr/bin/env python3
# Renders packages/arch/<name>/ into <repo>/<name>/ (a PKGBUILD folder, AUR style).
import os
import re

import lib

REQUIRED = ["PKGBUILD.tmpl"]
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
# Arch packages aren't split into development files: a library is one package
DEPENDENCY_FIELDS = {
    "build": [("makedepends", "main")],
    "libraries": [("depends", "main")],
    "runtime": [("depends", "main")],
    "development": [("depends", "main")],
}
# pkgver can't contain '-', ':', '/' or whitespace
pkgver = lib.substitute(r"[-:/\s]", "_")
FILTERS = {"pkgver": pkgver}
# Split packages a dependency never means (see lib.drop_unwanted())
NOISE = ("-doc", "-docs", "-src")
# Packages for other targets: 32-bit multilib and the MinGW cross toolchain
OTHER_TARGETS = re.compile(r"^(lib32-|mingw-w64-)")


def pick(job, dep, package, kind):
    return package.get("binname") or lib.srcname(package) or None


def render(job, dep, names):
    """glfw>=3.4"""
    name = lib.releases_in_order(job, names)[0]
    return f"{name}{dep['op']}{dep['version']}" if dep.get("op") else name


def main(job):
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ["any"])
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, render, noise=NOISE, other_targets=OTHER_TARGETS)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), filters=FILTERS)


if __name__ == "__main__":
    lib.run(main)
