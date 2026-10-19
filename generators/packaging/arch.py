#!/usr/bin/env python3
# Renders packages/arch/<name>/ into <repo>/<name>/ (a PKGBUILD folder, AUR style).
import os
import lib

REQUIRED = ["PKGBUILD.tmpl"]


def main(job):
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel))


if __name__ == "__main__":
    lib.run(main)
