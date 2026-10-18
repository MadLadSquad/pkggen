#!/usr/bin/env python3
# Arch Linux repositories are plain folders of PKGBUILD directories, so this
# only ignores build leftovers. Packaging generators add the PKGBUILDs later.
import lib


def main():
    # `repositories.arch` is optional; nothing in it is needed yet
    lib.load_repo_config("arch")

    lib.write_file(".gitignore", """\
src/
pkg/
*.pkg.tar.*
*.tar.*
*.log
""")


if __name__ == "__main__":
    main()
