#!/usr/bin/env python3
# Creates a self-hosted APT repository skeleton (reprepro layout)
# from `repositories.debian` in pkggen.yaml.
#
# Layout:
#   conf/distributions  - one reprepro stanza per suite
#   conf/options        - reprepro defaults
#   packages/           - source trees with debian/ folders (filled by packaging generators)
#   <name>.sources      - optional deb822 file users drop into /etc/apt/sources.list.d
import lib

DEFAULT_ARCHITECTURES = ["amd64", "source"]
DEFAULT_COMPONENTS = ["main"]


def get_suites(debian):
    suites = lib.as_list(debian.get("suites"))
    if not suites:
        lib.fail("'repositories.debian.suites' needs at least one entry")
    for i, suite in enumerate(suites):
        if not isinstance(suite, dict) or not suite.get("codename"):
            lib.fail(f"'repositories.debian.suites[{i}]' needs a 'codename'")
    return suites


def build_distributions(debian, suites):
    origin = debian["origin"]
    label = debian.get("label") or origin
    description = debian.get("description")
    sign_with = debian.get("sign-with")
    architectures = lib.as_list(debian.get("architectures")) or DEFAULT_ARCHITECTURES
    components = lib.as_list(debian.get("components")) or DEFAULT_COMPONENTS

    stanzas = []
    for suite in suites:
        stanza = [f"Origin: {origin}", f"Label: {label}"]
        if suite.get("suite"):
            stanza.append(f"Suite: {suite['suite']}")
        stanza += [
            f"Codename: {suite['codename']}",
            f"Architectures: {' '.join(lib.as_list(suite.get('architectures')) or architectures)}",
            f"Components: {' '.join(lib.as_list(suite.get('components')) or components)}",
        ]
        if suite.get("description") or description:
            stanza.append(f"Description: {suite.get('description') or description}")
        if sign_with:
            stanza.append(f"SignWith: {sign_with}")
        stanzas.append("\n".join(stanza))

    lib.write_file("conf/distributions", "\n\n".join(stanzas) + "\n")
    lib.write_file("conf/options", """\
verbose
basedir .
""")


def build_sources_file(debian, suites):
    url = debian.get("url")
    if not url:
        return

    name = debian.get("name")
    if not name:
        lib.fail("'repositories.debian.name' is required when 'url' is set")

    components = lib.as_list(debian.get("components")) or DEFAULT_COMPONENTS
    entries = [
        "Types: deb",
        f"URIs: {url}",
        f"Suites: {' '.join(suite['codename'] for suite in suites)}",
        f"Components: {' '.join(components)}",
    ]
    if debian.get("keyring"):
        entries.append(f"Signed-By: {debian['keyring']}")

    lib.write_file(f"{name}.sources", lib.lines(*entries))


def main():
    debian = lib.load_repo_config("debian", required=("origin",))
    suites = get_suites(debian)

    build_distributions(debian, suites)
    build_sources_file(debian, suites)
    lib.make_dirs(["packages"])
    lib.write_file(".gitignore", """\
# reprepro state and published output
db/
dists/
pool/
lists/
logs/
tmp/
# build artifacts
*.deb
*.udeb
*.ddeb
*.dsc
*.changes
*.buildinfo
*.tar.*
""")


if __name__ == "__main__":
    main()
