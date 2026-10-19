#!/usr/bin/env python3
# Renders packages/rpm-based/<name>/ into the rpmbuild tree:
#   spec.tmpl        -> SPECS/<name>.spec
#   everything else  -> SOURCES/ (patches, desktop files, ...)
import os
import lib

REQUIRED = ["spec.tmpl"]
# OpenMandriva builds x86_64 packages as znver1, which its %{x86_64} macro includes.
# openSUSE Leap doesn't define the macro, so fall back to plain x86_64 there
ARCHITECTURES = {"amd64": "%{?x86_64}%{!?x86_64:x86_64}", "arm64": "aarch64"}


def main(job):
    # "any" means no ExclusiveArch line
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, [])
    lib.ensure_templates(job, REQUIRED)

    def place(rel):
        if rel == "spec":
            return os.path.join(job.repository_path, "SPECS", f"{job.name}.spec")
        return os.path.join(job.repository_path, "SOURCES", rel)

    job.output = job.repository_path
    lib.render_tree(job, place)


if __name__ == "__main__":
    lib.run(main)
