#!/usr/bin/env python3
# Creates an rpmbuild tree, plus an optional dnf/zypper .repo file,
# from `repositories.rpm-based` in pkggen.yaml.
import lib


def build_repo_file(rpm):
    name = rpm.get("name")
    baseurl = rpm.get("baseurl")
    if not name or not baseurl:
        if name or baseurl:
            lib.warn("'repositories.rpm-based' needs both 'name' and 'baseurl' to write a .repo file")
        return

    gpgkey = rpm.get("gpgkey")
    entries = [
        f"[{name}]",
        f"name={rpm.get('description') or name}",
        f"baseurl={baseurl}",
        "type=rpm-md",
        "enabled=1",
        f"gpgcheck={int(lib.to_bool(rpm.get('gpgcheck'), bool(gpgkey)))}",
    ]
    if rpm.get("repo_gpgcheck") is not None:
        entries.append(f"repo_gpgcheck={int(lib.to_bool(rpm['repo_gpgcheck'], False))}")
    if gpgkey:
        entries.append(f"gpgkey={gpgkey}")

    lib.write_file(f"{name}.repo", lib.lines(*entries))


def main():
    rpm = lib.load_repo_config("rpm-based")

    lib.make_dirs(["BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"])
    lib.write_file(".gitignore", """\
*.tar.*
BUILD/*/
BUILDROOT/
""")
    build_repo_file(rpm)


if __name__ == "__main__":
    main()
