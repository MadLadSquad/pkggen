#!/usr/bin/env python3
# Renders packages/arch/<name>/ into <repo>/<name>/ (a PKGBUILD folder, AUR style).
import os
import lib

REQUIRED = ["PKGBUILD.tmpl"]
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}


def main(job):
    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ["any"])
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel))


if __name__ == "__main__":
    lib.run(main)
