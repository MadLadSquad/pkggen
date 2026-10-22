#!/usr/bin/env python3
# Tests Debian and Ubuntu packages: every package is built from its upstream
# source with the debian/ folder from <repo>/packages/<name>/, installed, and
# added to the reprepro repository, which is collected with its dists/ and pool/.
import re

import lib

# version-suffix keeps the .debs of different releases apart in one repository's
# pool/ (name_1.0-1~deb13_amd64.deb). The suffixes sort like the releases, so
# upgrading the system also upgrades the packages.
TARGETS = {
    "debian_unstable": {"name": "debian-unstable", "image": "docker.io/library/debian:sid",
                        "codename": "sid", "suite": "unstable", "version-suffix": "~debsid"},
    "debian_14": {"name": "debian-14", "image": "docker.io/library/debian:forky",
                  "codename": "forky", "suite": "testing", "version-suffix": "~deb14"},
    "debian_13": {"name": "debian-13", "image": "docker.io/library/debian:trixie",
                  "codename": "trixie", "suite": "stable", "version-suffix": "~deb13"},
    "debian_12": {"name": "debian-12", "image": "docker.io/library/debian:bookworm",
                  "codename": "bookworm", "suite": "oldstable", "version-suffix": "~deb12"},
    "ubuntu_26_10": {"name": "ubuntu-26.10", "image": "docker.io/library/ubuntu:26.10", "codename": "stonking",
                     "version-suffix": "~ubuntu26.10"},
    "ubuntu_26_04": {"name": "ubuntu-26.04", "image": "docker.io/library/ubuntu:26.04", "codename": "resolute",
                     "version-suffix": "~ubuntu26.04"},
    "ubuntu_24_04": {"name": "ubuntu-24.04", "image": "docker.io/library/ubuntu:24.04", "codename": "noble",
                     "version-suffix": "~ubuntu24.04"},
    "ubuntu_22_04": {"name": "ubuntu-22.04", "image": "docker.io/library/ubuntu:22.04", "codename": "jammy",
                     "version-suffix": "~ubuntu22.04"},
}

DEBS = f"{lib.STATE}/debs"
# Characters a Debian version may contain (and that are safe in the sed below)
SUFFIX_RE = re.compile(r"^[A-Za-z0-9.+~]*$")
CODENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")

SETUP = lib.step("install the build tools", """\
apt-get update
apt-get install -y --no-install-recommends build-essential reprepro curl ca-certificates xz-utils bzip2 zstd
""")


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(v) for v in value]


def find_suite(job, spec):
    """The repositories.debian.suites entry for this target, if there is one."""
    names = {spec["codename"], spec.get("suite")}
    for suite in as_list_of_dicts(job.repository.get("suites")):
        if suite.get("codename") in names:
            return suite
    return None


def as_list_of_dicts(value):
    return [entry for entry in (value if isinstance(value, list) else []) if isinstance(entry, dict)]


def distributions_stanza(job, spec, suite):
    """conf/distributions with only this target's suite. It isn't signed: the result is a test artifact."""
    repo = job.repository
    origin = repo.get("origin") or "pkggen"
    lines = [f"Origin: {origin}", f"Label: {repo.get('label') or origin}"]
    if suite and suite.get("suite"):
        lines.append(f"Suite: {suite['suite']}")
    lines += [
        f"Codename: {suite['codename'] if suite else spec['codename']}",
        f"Architectures: {job.host_arch} source",
        f"Components: {' '.join(as_list((suite or {}).get('components')) or as_list(repo.get('components')) or ['main'])}",
    ]
    description = (suite or {}).get("description") or repo.get("description")
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines) + "\n"


def version_suffix(job, spec, suite):
    """The suffix for this release: repositories.debian.suites[].version-suffix, else the table's."""
    value = spec.get("version-suffix", "")
    if suite and "version-suffix" in suite:
        value = suite["version-suffix"]
        value = "" if value is None else str(value)
        if not SUFFIX_RE.match(value):
            raise lib.TestingError(f"'version-suffix' of the {suite.get('codename')} suite must only contain letters, "
                                   f"digits, '.', '+' and '~', got {value!r}")
    return value


def changelog_step(suffix, codename):
    """Shell code that gives the build's newest changelog entry this release's version and codename.

    Only the copy in the build folder changes, so every release builds from the
    same debian/ folder.
    """
    return f"sed -i -E '1s/^([^ ]+) \\(([^)]*)\\) [^;]*;/\\1 (\\2{suffix}) {codename};/' debian/changelog\nhead -n 1 debian/changelog\n"


def build(package, suffix, codename):
    name = package.name
    work = f"{lib.WORK}/{name}"
    source = package.source
    if not source.get("url"):
        return [lib.step(f"build {name}", f"echo 'pkggen: {name} has no source artifact to build from' >&2\nexit 1\n")]

    alias = source["alias"]
    ext = lib.tarball_extension(alias)
    code = f"rm -rf {lib.shquote(work)}\nmkdir -p {lib.shquote(work)}/src\ncd {lib.shquote(work)}\n"
    code += lib.download(source, work)
    if ext:
        orig = f"{name}_{package.version}.orig.{ext}"
        code += f"cp {lib.shquote(alias)} {lib.shquote(orig)}\ntar -xf {lib.shquote(alias)} -C src\n"
    elif alias.endswith(".zip"):
        code += f"apt-get install -y --no-install-recommends unzip\nunzip -q {lib.shquote(alias)} -d src\n"
    else:
        code += f"cp {lib.shquote(alias)} src/\n"
    # Build in the folder the archive extracts to: the guessed one, else its only folder
    code += f"""\
cd src
dir={lib.shquote(package.source_dir())}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  set -- */
  if [ $# -eq 1 ] && [ -d "$1" ]; then dir=$1; else dir=.; fi
fi
cd {lib.shquote(work)}
if [ "$dir" = . ]; then mv src build; else mv "src/$dir" build; fi
rm -rf build/debian
cp -r {lib.REPO}/packages/{name}/debian build/
cd build
{changelog_step(suffix, codename)}apt-get build-dep -y {lib.shquote(work)}/build
dpkg-buildpackage -us -uc -b
mkdir -p {DEBS}/{name}
mv {lib.shquote(work)}/*.deb {DEBS}/{name}/
"""
    return [lib.step(f"build {name}", code)]


def install(package):
    return [lib.step(f"install {package.name}", f"apt-get install -y {DEBS}/{package.name}/*.deb\n")]


def main(job):
    targets = []
    unconfigured = []
    for rp, spec in lib.select_targets(job, TARGETS):
        suite = find_suite(job, spec)
        if not suite:
            unconfigured.append(spec["name"])
        codename = suite["codename"] if suite else spec["codename"]
        if not CODENAME_RE.match(str(codename)):
            raise lib.TestingError(f"the codename {codename!r} in 'repositories.debian.suites' isn't a valid suite name")
        suffix = version_suffix(job, spec, suite)

        prepare = [lib.step("write conf/distributions",
                            lib.heredoc(f"{lib.REPO}/conf/distributions", distributions_stanza(job, spec, suite)))]
        if job.packages:
            # The repository loses the bit on its way in from a host without one (Windows)
            rules = " ".join(lib.shquote(f"{lib.REPO}/packages/{p.name}/debian/rules") for p in job.packages)
            prepare.append(lib.step("make debian/rules executable", f"chmod +x {rules}\n"))
        finalize = [
            lib.step("add the packages to the repository", f"""\
set -- {DEBS}/*/*.deb
if [ -e "$1" ]; then reprepro -b {lib.REPO} includedeb {lib.shquote(codename)} "$@"; fi
"""),
            lib.collect_step(),
        ]
        targets.append(lib.target(
            job, rp, spec,
            setup=[SETUP],
            prepare=prepare,
            packages=[{"name": p.name, "build": build(p, suffix, codename), "install": install(p)}
                      for p in job.packages],
            finalize=finalize,
            env={"DEBIAN_FRONTEND": "noninteractive"},
        ))

    if unconfigured and job.repository.get("suites"):
        job.warn(f"{', '.join(unconfigured)} aren't in 'repositories.debian.suites', so each test repository "
                 "gets a suite named after the release's codename")
    return targets


if __name__ == "__main__":
    lib.run(main)
