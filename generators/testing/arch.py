#!/usr/bin/env python3
# Tests Arch Linux packages: every <repo>/<name>/PKGBUILD is built with
# makepkg (as an unprivileged user, makepkg refuses root), installed with
# pacman -U and added to a pacman repository in <repo>/<arch>/.
import lib

# Arch Linux only publishes x86_64 images
TARGETS = {
    "arch": {"name": "arch", "image": "docker.io/library/archlinux:base-devel", "arches": ["amd64"]},
}

BUILDER = "builder"

SETUP = lib.step("install the build tools", f"""\
pacman -Syu --noconfirm --needed base-devel sudo
id {BUILDER} >/dev/null 2>&1 || useradd -m {BUILDER}
echo '{BUILDER} ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/pkggen
chmod 440 /etc/sudoers.d/pkggen
""")


def build(package):
    name = package.name
    folder = f"{lib.REPO}/{name}"
    return [
        lib.step(f"hand {name} to the build user", f"chown -R {BUILDER}: {lib.shquote(folder)}\n"),
        # makepkg downloads the sources and checks them against sha256sums
        lib.step(f"build {name}", f"""\
makepkg -s --noconfirm
makepkg --packagelist | while read -r pkg; do
  case "$pkg" in *-debug-*) continue ;; esac
  if [ -f "$pkg" ]; then echo "$pkg"; fi
done > {lib.STATE}/{name}.pkgs
test -s {lib.STATE}/{name}.pkgs
""", user=BUILDER, workdir=folder),
    ]


def install(package):
    return [lib.step(f"install {package.name}",
                     f"pacman -U --noconfirm $(cat {lib.STATE}/{package.name}.pkgs)\n")]


def cleanup(job):
    """Shell code that removes makepkg's build folders and downloaded sources from the PKGBUILD folders."""
    code = "rm -rf ./*/src ./*/pkg\n"
    for package in job.packages:
        for artifact in package.artifacts:
            code += f"rm -f {lib.shquote(package.name + '/' + artifact['alias'])}\n"
    return code


# pacman's databases: <name>.db, <name>.files, their .tar.zst and their signatures
PUBLISH = {"indexes": [r"\.(db|files)(\.tar\.[a-z0-9]+)?(\.sig)?$"]}


def main(job):
    repo_name = job.repository.get("name") or "pkggen"
    finalize = [
        lib.step("build the pacman repository", f"""\
cd {lib.REPO}
{cleanup(job)}arch=$(uname -m)
mkdir -p "$arch"
set -- {lib.STATE}/*.pkgs
if [ -e "$1" ]; then
  cat "$@" | while read -r pkg; do mv "$pkg" "$arch/"; done
  cd "$arch"
  repo-add {lib.shquote(repo_name + '.db.tar.zst')} ./*.pkg.tar.*
fi
"""),
        lib.collect_step(),
    ]
    return [
        lib.target(job, rp, spec,
                   setup=[SETUP],
                   prepare=[],
                   packages=[{"name": p.name, "build": build(p), "install": install(p)} for p in job.packages],
                   finalize=finalize,
                   publish=PUBLISH)
        for rp, spec in lib.select_targets(job, TARGETS)
    ]


if __name__ == "__main__":
    lib.run(main)
