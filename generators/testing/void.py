#!/usr/bin/env python3
# Tests Void Linux packages: the templates in <repo>/srcpkgs/ are copied into
# a void-packages checkout and built with xbps-src, which runs straight in the
# container (the "ethereal" chroot style, meant for throwaway containers),
# installed with xbps-install and indexed into <repo>/binpkgs/. The glibc and
# musl builds share repology's void_x86_64; their packages and indexes have
# different names (x86_64, x86_64-musl), so both can go into one repository.
# The packages aren't signed here: `pkggen deploy` signs them (`rsa-key`).
import lib

TARGETS = {
    "void_x86_64": [
        {"name": "void-glibc", "image": "ghcr.io/void-linux/void-glibc-full:latest"},
        {"name": "void-musl", "image": "ghcr.io/void-linux/void-musl-full:latest"},
    ],
}

# Not /void-packages: ethereal links that path to the checkout
CHECKOUT = "/opt/void-packages"
BINPKGS = f"{CHECKOUT}/hostdir/binpkgs"
VOID_PACKAGES = "https://github.com/void-linux/void-packages.git"

SETUP = [
    lib.step("update the system", """\
xbps-install -Syu xbps
xbps-install -yu
xbps-install -y bash git curl xz bzip2 unzip
"""),
    # ethereal: xbps-src uses the container itself as its masterdir. Its
    # base-chroot package conflicts with the image's own bash, grep and
    # util-linux, so its other dependencies are installed instead. Left out of
    # them: the chroot-* tools (they're what conflicts) and the Ada toolchain
    # (gcc-ada, libada-devel), which only building gcc itself needs. The names
    # are matched whole, so packages that merely contain "ada" stay in
    lib.step("set up xbps-src", f"""\
if [ ! -d {CHECKOUT} ]; then git clone --depth 1 {VOID_PACKAGES} {CHECKOUT}; fi
cd {CHECKOUT}
printf 'XBPS_CHROOT_CMD=ethereal\\nXBPS_ALLOW_CHROOT_BREAKOUT=yes\\n' >> etc/conf
ln -sfn / masterdir
arch=$(xbps-uhelper arch)
echo "$arch" > /.xbps_chroot_init
case "$arch" in
  x86_64) conf=repos-remote.conf ;;
  x86_64-musl) conf=repos-remote-musl.conf ;;
  *) conf="repos-remote-$arch.conf" ;;
esac
cp "etc/xbps.d/$conf" /etc/xbps.d/00-pkggen-bootstrap.conf
xbps-install -S
deps=$(xbps-query -R -x base-chroot | sed 's/[<>=].*//' | grep -v -E -e '^chroot-' -e '^(gcc-ada|libada-devel)$')
test -n "$deps"
xbps-install -y gawk $deps
./xbps-src binary-bootstrap
"""),
]


def prepare(job):
    return [lib.step("copy the templates into void-packages", f"""\
cp -a {lib.REPO}/srcpkgs/. {CHECKOUT}/srcpkgs/
if [ -f {lib.REPO}/common/shlibs ]; then cat {lib.REPO}/common/shlibs >> {CHECKOUT}/common/shlibs; fi
: > {lib.STATE}/shlibs
""")]


def build(package):
    name = lib.shquote(package.name)
    # A package that links against an earlier one needs its libraries in
    # common/shlibs, or xbps-src can't tell which package provides them
    return [lib.step(f"build {package.name}", f"""\
./xbps-src -j "${{PKGGEN_JOBS:-$(nproc)}}" pkg {name}
pkgver=$(xbps-query --repository={BINPKGS} -R --property=pkgver {name})
xbps-query --repository={BINPKGS} -R --property=shlib-provides {name} | while read -r soname; do
  [ -n "$soname" ] || continue
  awk -v so="$soname" '$1 != so' common/shlibs > common/shlibs.new
  mv common/shlibs.new common/shlibs
  echo "$soname $pkgver" | tee -a common/shlibs >> {lib.STATE}/shlibs
done
""", workdir=CHECKOUT)]


def install(package):
    return [lib.step(f"install {package.name}",
                     f"xbps-install -y --repository={BINPKGS} {lib.shquote(package.name)}\n")]


FINALIZE = lib.step("build the xbps repository", f"""\
mkdir -p {lib.REPO}/binpkgs
set -- {BINPKGS}/*.xbps
if [ -e "$1" ]; then
  cp "$@" {lib.REPO}/binpkgs/
  cd {lib.REPO}/binpkgs
  rm -f ./*-repodata
  # xz, which Python reads, so `pkggen deploy` can sign the index
  xbps-rindex --compression xz -a "$PWD"/*.xbps
fi
if [ -s {lib.STATE}/shlibs ]; then
  mkdir -p {lib.REPO}/common
  touch {lib.REPO}/common/shlibs
  sort -u {lib.REPO}/common/shlibs {lib.STATE}/shlibs > {lib.STATE}/shlibs.all
  cp {lib.STATE}/shlibs.all {lib.REPO}/common/shlibs
fi
""")


# xbps reads <arch>-repodata, which lists the packages (and holds the signing key)
PUBLISH = {
    "metadata": [r"-repodata$"],
    "content_types": {".xbps": "application/octet-stream", ".sig2": "application/octet-stream"},
}


def main(job):
    return [
        lib.target(job, rp, spec,
                   setup=list(SETUP),
                   prepare=prepare(job),
                   packages=[{"name": p.name, "build": build(p), "install": install(p)} for p in job.packages],
                   finalize=[FINALIZE, lib.collect_step()],
                   publish=PUBLISH)
        for rp, spec in lib.select_targets(job, TARGETS)
    ]


if __name__ == "__main__":
    lib.run(main)
