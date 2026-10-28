#!/usr/bin/env python3
# Tests Gentoo ebuilds: the overlay is registered in repos.conf, every
# ebuild's dependencies come from the official binary host, the ebuild is
# built with `ebuild ... install` (which checks the Manifest), merged with
# `ebuild ... qmerge`, and packed with quickpkg into binpkgs/ of the result.
import re
import lib

PORTAGE_TREE = "/var/db/repos/gentoo"
TARGETS = {
    "gentoo": {"name": "gentoo", "image": "docker.io/gentoo/stage3:latest"},
}
# The Gentoo repository comes from the daily gentoo/portage image, mounted with --volumes-from
DATA_IMAGES = [{"image": "docker.io/gentoo/portage:latest", "path": PORTAGE_TREE}]

INSTALLED = f"{lib.STATE}/installed.atoms"
CATEGORY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9+_.-]*$")

# Unprivileged containers can't create the namespaces these sandboxes use
SETUP = lib.step("configure Portage", """\
cat >> /etc/portage/make.conf <<'PKGGEN_EOF'
FEATURES="-ipc-sandbox -network-sandbox -pid-sandbox -mount-sandbox getbinpkg"
PKGGEN_EOF
if command -v getuto >/dev/null; then getuto; fi
""")

REPO_NAME = f"repo=$(cat {lib.REPO}/profiles/repo_name)\n"


def category(job, package):
    """The category the packaging generator used: the first part of its output path."""
    parts = package.output.split("/")
    if len(parts) >= 2 and CATEGORY_RE.match(parts[-2]):
        return parts[-2]
    block = package.block("gentoo")
    value = block.get("category") or package.context.get("category")
    if not value:
        categories = job.repository.get("categories") or []
        value = (categories.split() if isinstance(categories, str) else categories or [None])[0]
    if not value:
        raise lib.TestingError(f"can't tell the category of {package.name}: set 'category' in pkggen.yaml")
    return str(value)


def ebuild(job, package):
    return f"{lib.REPO}/{category(job, package)}/{package.name}/{package.name}-{package.version}.ebuild"


def atom(job, package):
    return f"={category(job, package)}/{package.name}-{package.version}"


def build(job, package):
    path = lib.shquote(ebuild(job, package))
    return [lib.step(f"build {package.name}", f"""\
{REPO_NAME}emerge --oneshot --onlydeps --noreplace {lib.shquote(atom(job, package))}::"$repo"
ebuild {path} clean install
""")]


def install(job, package):
    path = lib.shquote(ebuild(job, package))
    return [lib.step(f"install {package.name}", f"""\
ebuild {path} qmerge
ebuild {path} clean
echo {lib.shquote(atom(job, package))} >> {INSTALLED}
""")]


def prepare(job):
    keyword = f"~{job.host_arch}"
    code = f"""\
{REPO_NAME}mkdir -p /etc/portage/repos.conf /etc/portage/package.accept_keywords
cat > /etc/portage/repos.conf/pkggen.conf <<PKGGEN_EOF
[$repo]
location = {lib.REPO}
auto-sync = no
PKGGEN_EOF
echo "*/*::$repo {keyword}" > /etc/portage/package.accept_keywords/pkggen
touch {lib.REPO}/profiles/categories
"""
    # Portage ignores categories that aren't listed in profiles/categories, of
    # the overlay or of the Gentoo repository it builds on
    for name in sorted({category(job, p) for p in job.packages}):
        quoted = lib.shquote(name)
        code += (f"grep -qx {quoted} {PORTAGE_TREE}/profiles/categories {lib.REPO}/profiles/categories || "
                 f"echo {quoted} >> {lib.REPO}/profiles/categories\n")
    return [lib.step("register the overlay", code)]


FINALIZE = [
    lib.collect_step(),
    lib.step("pack the installed packages", f"""\
if [ -f {INSTALLED} ]; then
  while read -r atom; do PKGDIR={lib.OUT}/binpkgs quickpkg --include-config=y "$atom"; done < {INSTALLED}
fi
"""),
]


# The binary package host's index: binpkgs/Packages
PUBLISH = {"indexes": [r"(^|/)binpkgs/Packages$"]}


def main(job):
    return [
        lib.target(job, rp, spec,
                   setup=[SETUP],
                   prepare=prepare(job),
                   packages=[{"name": p.name, "build": build(job, p), "install": install(job, p)}
                             for p in job.packages],
                   finalize=FINALIZE,
                   data_images=DATA_IMAGES,
                   env={"MAKEOPTS": f"-j{lib.JOBS}"},
                   publish=PUBLISH)
        for rp, spec in lib.select_targets(job, TARGETS)
    ]


if __name__ == "__main__":
    lib.run(main)
