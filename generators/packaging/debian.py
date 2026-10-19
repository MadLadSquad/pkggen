#!/usr/bin/env python3
# Renders packages/debian/<name>/ into <repo>/packages/<name>/, the source
# area of the reprepro repository. The templates hold a debian/ folder.
import os
import lib

REQUIRED = ["debian/control.tmpl", "debian/rules.tmpl", "debian/changelog.tmpl"]


def main(job):
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, "packages", job.name)
    job.output = out
    lib.render_tree(job, lambda rel: os.path.join(out, rel), executable={"debian/rules"})


if __name__ == "__main__":
    lib.run(main)
