#!/usr/bin/env python3
# Alpine Linux repositories are folders of APKBUILD directories (like an aports
# repository), so this only ignores build leftovers. Packaging generators add
# the APKBUILDs later; `pkggen test` builds the apk repository (<arch>/APKINDEX.tar.gz).
import re

import lib

# abuild names the packages' folder in REPODEST after the repository
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def main():
    # `repositories.alpine` is optional; `name` is abuild's repository name
    alpine = lib.load_repo_config("alpine")
    name = alpine.get("name")
    if name is not None and not (isinstance(name, str) and NAME_RE.match(name)):
        lib.fail(f"'repositories.alpine.name' must be lower-case letters, digits, '.', '_' and '-', got {name!r}")

    lib.write_file(".gitignore", """\
src/
pkg/
*.apk
*.tar.*
*.log
""")


if __name__ == "__main__":
    main()
