#!/usr/bin/env python3
# Creates a Gentoo overlay skeleton from `repositories.gentoo` in pkggen.yaml.
from xml.sax.saxutils import escape, quoteattr
import lib

QUALITIES = ("core", "stable", "testing", "experimental", "graveyard")
STATUSES = ("official", "unofficial")
OWNER_TYPES = ("person", "project")


def build_copyright(gentoo):
    copyright_obj = gentoo.get("copyright")
    if not copyright_obj:
        return
    if not isinstance(copyright_obj, dict):
        lib.fail("'repositories.gentoo.copyright' must be a mapping with 'years', 'owner' and 'license'")

    missing = [key for key in ("years", "owner", "license") if not copyright_obj.get(key)]
    if missing:
        lib.fail(f"'repositories.gentoo.copyright' is missing: {', '.join(missing)}")

    lib.write_file("COPYRIGHT.txt", f"""\
Copyright {copyright_obj['years']} {copyright_obj['owner']}. \
Distributed under the terms of the {copyright_obj['license']}.
""")


def build_profiles(gentoo, name):
    categories = lib.words(gentoo.get("categories"))
    if categories:
        lib.write_file("profiles/categories", lib.lines(*categories))

    # Every overlay needs profiles/repo_name with the repository name
    lib.write_file("profiles/repo_name", f"""\
{name}
""")


def build_metadata(gentoo, name):
    def bool_str(key, default):
        return str(lib.to_bool(gentoo.get(key), default)).lower()

    def joined(key, default=None):
        values = lib.words(gentoo.get(key))
        return " ".join(values) if values else default

    entries = [
        f"repo-name = {name}",
        f"masters = {joined('masters', 'gentoo')}",
        f"thin-manifests = {bool_str('thin-manifests', True)}",
        f"sign-manifests = {bool_str('sign-manifests', False)}",
        f"profile-formats = {joined('profile-formats', 'portage-2')}",
        f"cache-formats = {joined('cache-formats', 'md5-dict')}",
    ]

    # Optional keys, only written when set
    for key in ("manifest-hashes", "manifest-required-hashes", "eapis-banned", "eapis-deprecated"):
        value = joined(key)
        if value:
            entries.append(f"{key} = {value}")

    lib.write_file("metadata/layout.conf", lib.lines(*entries))


def build_repositories_xml(gentoo, name):
    quality = gentoo.get("quality")
    status = gentoo.get("status")
    if quality is not None and quality not in QUALITIES:
        lib.warn(f"unknown gentoo quality '{quality}', expected one of: {', '.join(QUALITIES)}")
    if status is not None and status not in STATUSES:
        lib.warn(f"unknown gentoo status '{status}', expected one of: {', '.join(STATUSES)}")

    attrs = ""
    for attr, value in (("priority", gentoo.get("priority")), ("quality", quality), ("status", status)):
        if value is not None:
            attrs += f" {attr}={quoteattr(str(value))}"

    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE repositories SYSTEM "https://www.gentoo.org/dtd/repositories.dtd">',
        '<repositories xmlns="" version="1.0">',
        f"  <repo{attrs}>",
        f"    <name>{escape(name)}</name>",
    ]

    for key in ("description", "homepage"):
        value = gentoo.get(key)
        if value:
            xml.append(f"    <{key}>{escape(str(value))}</{key}>")

    for i, owner in enumerate(lib.as_list(gentoo.get("owners"))):
        if not isinstance(owner, dict):
            lib.fail(f"'repositories.gentoo.owners[{i}]' must be a mapping with 'email', and optionally 'name' "
                     f"and 'type', got {owner!r}")
        o_name = owner.get("name")
        o_email = owner.get("email")
        o_type = owner.get("type")
        if not o_name and not o_email:
            continue
        if not o_email:
            lib.warn(f"gentoo owner '{o_name}' has no email, which repositories.dtd requires")
        if o_type is not None and o_type not in OWNER_TYPES:
            lib.warn(f"unknown gentoo owner type '{o_type}', expected one of: {', '.join(OWNER_TYPES)}")

        xml.append(f"    <owner type={quoteattr(o_type)}>" if o_type else "    <owner>")
        if o_email:
            xml.append(f"      <email>{escape(str(o_email))}</email>")
        if o_name:
            xml.append(f"      <name>{escape(str(o_name))}</name>")
        xml.append("    </owner>")

    for i, source in enumerate(lib.as_list(gentoo.get("source"))):
        if not isinstance(source, dict):
            lib.fail(f"'repositories.gentoo.source[{i}]' must be a mapping with 'type' and 'url', got {source!r}")
        s_type = source.get("type")
        s_url = source.get("url")
        if s_type and s_url:
            xml.append(f"    <source type={quoteattr(str(s_type))}>{escape(str(s_url))}</source>")

    for feed in lib.as_list(gentoo.get("feed")):
        xml.append(f"    <feed>{escape(str(feed))}</feed>")

    xml += ["  </repo>", "</repositories>"]
    lib.write_file("repositories.xml", lib.lines(*xml))


def main():
    gentoo = lib.load_repo_config("gentoo", required=("name",))
    name = gentoo["name"]

    build_copyright(gentoo)
    build_profiles(gentoo, name)
    build_metadata(gentoo, name)
    build_repositories_xml(gentoo, name)


if __name__ == "__main__":
    main()
