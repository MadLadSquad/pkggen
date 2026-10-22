#!/usr/bin/env python3
# Renders packages/debian/<name>/ into <repo>/packages/<name>/, the source
# area of the reprepro repository. The templates hold a debian/ folder.
import os
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


def pick(job, dep, package, kind):
    if kind == "dev":
        return lib.dev_package(package, dep["name"], "-dev")
    return lib.main_package(package, dep["name"])


def render(job, dep, names):
    """libglfw3-dev (>= 3.4); releases that name it differently become alternatives (a | b)."""
    suffix = f" ({OPERATORS[dep['op']]} {dep['version']})" if dep.get("op") else ""
    return " | ".join(f"{name}{suffix}" for name in lib.releases_in_order(job, names))


def main(job):
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ["any"])
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, render)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, "packages", job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), executable={"debian/rules"})


if __name__ == "__main__":
    lib.run(main)
