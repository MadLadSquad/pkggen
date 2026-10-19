#!/usr/bin/env python3
# Renders packages/rpm-based/<name>/ into the rpmbuild tree:
#   spec.tmpl        -> SPECS/<name>.spec
#   everything else  -> SOURCES/ (patches, desktop files, ...)
import os
import lib

REQUIRED = ["spec.tmpl"]


def main(job):
    lib.ensure_templates(job, REQUIRED)

    def place(rel):
        if rel == "spec":
            return os.path.join(job.repository_path, "SPECS", f"{job.name}.spec")
        return os.path.join(job.repository_path, "SOURCES", rel)

    job.output = job.repository_path
    lib.render_tree(job, place)


if __name__ == "__main__":
    lib.run(main)
