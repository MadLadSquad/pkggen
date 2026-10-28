#!/usr/bin/env python3
# Creates a void-packages style template tree (srcpkgs/<name>/template), plus
# an optional xbps.d file for the binary repository, from `repositories.void`.
# The templates are built inside a void-packages checkout by `pkggen test`.
import re

import lib

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_conf_file(void):
    name = void.get("name")
    url = void.get("url")
    if not name or not url:
        if name or url:
            lib.warn("'repositories.void' needs both 'name' and 'url' to write an xbps.d file")
        return
    if not NAME_RE.match(str(name)):
        lib.fail(f"'repositories.void.name' must be letters, digits, '.', '_' and '-', got {name!r}")

    # Copied to /etc/xbps.d/ by the users of the repository
    lib.write_file(f"{name}.conf", f"""\
# {void.get('description') or name}
repository={url}
""")


def main():
    void = lib.load_repo_config("void")

    lib.make_dirs(["srcpkgs"])
    lib.write_file(".gitignore", """\
hostdir/
masterdir*/
*.xbps
*.tar.*
""")
    build_conf_file(void)


if __name__ == "__main__":
    main()
