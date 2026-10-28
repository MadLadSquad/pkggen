#!/usr/bin/env python3
# Tests Alpine Linux packages: every <repo>/<name>/APKBUILD is built with
# abuild (as an unprivileged user in the abuild group), installed with apk and
# added to an apk repository in <repo>/<arch>/ (APKINDEX.tar.gz). The packages
# are signed with a throwaway key made in the container: `pkggen deploy` signs
# them again with the repository's own key (`rsa-key`). The real key never
# enters the container: abuild reads it as the user that runs the upstream
# build scripts, so a compromised release could steal it.
import lib

TARGETS = {
    "alpine_edge": {"name": "alpine-edge", "image": "docker.io/library/alpine:edge"},
    "alpine_3_24": {"name": "alpine-3.24", "image": "docker.io/library/alpine:3.24"},
    "alpine_3_23": {"name": "alpine-3.23", "image": "docker.io/library/alpine:3.23"},
}

BUILDER = "builder"
# abuild's aports tree: <APORTS>/<repository name>/<package>/APKBUILD
APORTS = f"{lib.WORK}/aports"
# Where abuild puts the packages: <REPODEST>/<repository name>/<arch>/
REPODEST = f"{lib.STATE}/apk"

SETUP = [
    lib.step("install the build tools", f"""\
# abuild -r installs the build dependencies from the indexes apk update keeps
apk update
apk add alpine-sdk sudo curl
id {BUILDER} >/dev/null 2>&1 || adduser -D {BUILDER}
addgroup {BUILDER} abuild
echo '{BUILDER} ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/pkggen
chmod 440 /etc/sudoers.d/pkggen
"""),
    # The key abuild signs the packages and the index with in the container. It's
    # named after PACKAGER's address ($USER isn't set in `exec -u`)
    lib.step("make a signing key", f"PACKAGER='pkggen <noreply@madladsquad.com>' abuild-keygen -a -n\n",
             user=BUILDER),
    # Newer abuild keeps its keys in ~/.config/abuild, older in ~/.abuild
    lib.step("trust the signing key", f"""\
found=
for key in /home/{BUILDER}/.config/abuild/*.rsa.pub /home/{BUILDER}/.abuild/*.rsa.pub; do
  if [ -e "$key" ]; then cp "$key" /etc/apk/keys/; found=1; fi
done
test -n "$found"
"""),
]


def repository_name(job):
    return job.repository.get("name") or "pkggen"


def prepare(job):
    folder = f"{APORTS}/{repository_name(job)}"
    code = f"mkdir -p {lib.shquote(folder)} {REPODEST}\n"
    for package in job.packages:
        code += f"cp -r {lib.shquote(lib.REPO + '/' + package.output)} {lib.shquote(folder + '/' + package.name)}\n"
    code += f"chown -R {BUILDER}: {APORTS} {REPODEST}\n"
    return [lib.step("copy the APKBUILDs into an aports tree", code)]


def build(job, package):
    name = package.name
    repository = f"{REPODEST}/{repository_name(job)}"
    # JOBS sets abuild's MAKEFLAGS (/etc/abuild.conf), to the job count pkggen picked
    return [lib.step(f"build {name}", f"""\
export REPODEST={REPODEST}
export JOBS="${{PKGGEN_JOBS:-$(nproc)}}"
abuild -r
arch=$(abuild -A)
abuild listpkg | while read -r pkg; do
  case "$pkg" in *-dbg-*) continue ;; esac
  if [ -f {lib.shquote(repository)}/"$arch/$pkg" ]; then echo {lib.shquote(repository)}/"$arch/$pkg"; fi
done > {lib.STATE}/{name}.pkgs
test -s {lib.STATE}/{name}.pkgs
""", user=BUILDER, workdir=f"{APORTS}/{repository_name(job)}/{name}")]


def install(job, package):
    repository = f"{REPODEST}/{repository_name(job)}"
    # The repository is listed, so the next packages' builds find this one
    return [lib.step(f"install {package.name}", f"""\
grep -qx {lib.shquote(repository)} /etc/apk/repositories || echo {lib.shquote(repository)} >> /etc/apk/repositories
apk add $(cat {lib.STATE}/{package.name}.pkgs)
""")]


# apk reads <arch>/APKINDEX.tar.gz, which lists (and checksums) the packages
PUBLISH = {
    "metadata": [r"(^|/)APKINDEX\.tar\.gz$"],
    # Not Android packages (mimetypes' guess for .apk)
    "content_types": {".apk": "application/octet-stream"},
}


def main(job):
    repository = f"{REPODEST}/{repository_name(job)}"
    finalize = [
        lib.step("collect the apk repository", f"""\
arch=$(apk --print-arch)
mkdir -p {lib.REPO}/"$arch"
if [ -d {lib.shquote(repository)}/"$arch" ]; then
  cp {lib.shquote(repository)}/"$arch"/* {lib.REPO}/"$arch"/
fi
"""),
        lib.collect_step(),
    ]
    return [
        lib.target(job, rp, spec,
                   setup=list(SETUP),
                   prepare=prepare(job),
                   packages=[{"name": p.name, "build": build(job, p), "install": install(job, p)}
                             for p in job.packages],
                   finalize=finalize,
                   publish=PUBLISH)
        for rp, spec in lib.select_targets(job, TARGETS)
    ]


if __name__ == "__main__":
    lib.run(main)
