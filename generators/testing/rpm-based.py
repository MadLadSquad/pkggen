#!/usr/bin/env python3
# Tests RPM packages on Fedora, openSUSE and OpenMandriva. The repository is
# an rpmbuild tree, so it's used as %_topdir: sources are downloaded into
# SOURCES/, every spec is built with rpmbuild -ba, the RPMs are installed and
# RPMS/ is indexed with createrepo_c.
import lib

TARGETS = {
    "fedora_rawhide": {"name": "fedora-rawhide", "image": "registry.fedoraproject.org/fedora:rawhide", "family": "fedora"},
    "fedora_44": {"name": "fedora-44", "image": "registry.fedoraproject.org/fedora:44", "family": "fedora"},
    "fedora_43": {"name": "fedora-43", "image": "registry.fedoraproject.org/fedora:43", "family": "fedora"},
    "opensuse_tumbleweed": {"name": "opensuse-tumbleweed", "image": "registry.opensuse.org/opensuse/tumbleweed:latest",
                            "family": "opensuse", "rolling": True},
    "opensuse_leap_15_6": {"name": "opensuse-leap-15.6", "image": "registry.opensuse.org/opensuse/leap:15.6",
                           "family": "opensuse"},
    "openmandriva_rolling": {"name": "openmandriva-rolling", "image": "docker.io/openmandriva/minimal:rolling",
                             "family": "openmandriva", "rolling": True},
    "openmandriva_6_0": {"name": "openmandriva-6.0", "image": "docker.io/openmandriva/minimal:rock",
                         "family": "openmandriva"},
}

TOPDIR = f'--define "_topdir {lib.REPO}"'


def zypper(args):
    """A zypper command that also succeeds with exit code 106, which only means that
    some repositories couldn't be refreshed (a mirror timing out): a package that
    really is missing still fails the step, or rpmbuild after it."""
    return f"zypper -n {args} || [ $? -eq 106 ]\n"


# Per family: install command, set up the tools, install build dependencies, install local RPMs
FAMILIES = {
    "fedora": {
        "setup": "dnf -y install rpm-build createrepo_c 'dnf-command(builddep)'\n"
                 "command -v curl >/dev/null || dnf -y install curl\n",
        "builddep": "dnf -y builddep {spec}\n",
        "install": "dnf -y install {files}\n",
    },
    "openmandriva": {
        "upgrade": "dnf -y distro-sync\n",
        "setup": "dnf -y install rpm-build createrepo_c 'dnf-command(builddep)' curl\n",
        "builddep": "dnf -y builddep {spec}\n",
        "install": "dnf -y install {files}\n",
    },
    "opensuse": {
        "upgrade": zypper("dup"),
        "setup": zypper("in rpm-build createrepo_c curl tar gzip xz bzip2"),
        # zypper has no builddep: install what rpmspec lists, one argument per line
        "builddep": f"rpmspec {TOPDIR} -q --buildrequires {{spec}} > {lib.STATE}/buildrequires\n"
                    "set --\n"
                    f"while IFS= read -r dep; do set -- \"$@\" \"$dep\"; done < {lib.STATE}/buildrequires\n"
                    "[ $# -eq 0 ] || " + zypper('in "$@"'),
        "install": zypper("--no-gpg-checks in --allow-unsigned-rpm {files}"),
    },
}


def build(package, family):
    name = package.name
    spec = f"{lib.REPO}/SPECS/{name}.spec"
    code = f"mkdir -p {lib.REPO}/SOURCES\n"
    for artifact in package.artifacts:
        code += lib.download(artifact, f"{lib.REPO}/SOURCES")
    code += FAMILIES[family]["builddep"].format(spec=lib.shquote(spec))
    # Remember the binary RPMs rpmbuild wrote, without debug info packages. Their
    # names differ between distributions (OpenMandriva adds its release, -omv6000)
    output = f"{lib.STATE}/{name}.rpmbuild"
    code += f"""\
rpmbuild {TOPDIR} -ba {lib.shquote(spec)} > {output} 2>&1 || {{ cat {output}; exit 1; }}
cat {output}
sed -n 's|^Wrote: \\(.*/RPMS/.*\\.rpm\\)$|\\1|p' {output} | grep -v -e '-debuginfo-' -e '-debugsource-' > {lib.STATE}/{name}.rpms
test -s {lib.STATE}/{name}.rpms
"""
    return [lib.step(f"build {name}", code)]


def install(package, family):
    files = f'$(cat {lib.STATE}/{package.name}.rpms)'
    return [lib.step(f"install {package.name}", FAMILIES[family]["install"].format(files=files))]


# dnf and zypper read repodata/repomd.xml (and its signature) first, then the files it lists
PUBLISH = {
    "indexes": [r"(^|/)repodata/"],
    "metadata": [r"(^|/)repomd\.xml(\.asc|\.key)?$"],
    "content_types": {".rpm": "application/x-rpm"},
}


def main(job):
    targets = []
    for rp, spec in lib.select_targets(job, TARGETS):
        family = FAMILIES[spec["family"]]
        setup = []
        if spec.get("rolling") and family.get("upgrade"):
            setup.append(lib.step("upgrade the system", family["upgrade"]))
        setup.append(lib.step("install the build tools", family["setup"]))

        finalize = [
            lib.step("index the repository", f"""\
rm -rf {lib.REPO}/BUILD {lib.REPO}/BUILDROOT
if [ -d {lib.REPO}/RPMS ]; then createrepo_c {lib.REPO}/RPMS; fi
"""),
            lib.collect_step(),
        ]
        targets.append(lib.target(
            job, rp, spec,
            setup=setup,
            prepare=[],
            packages=[{"name": p.name, "build": build(p, spec["family"]), "install": install(p, spec["family"])}
                      for p in job.packages],
            finalize=finalize,
            env={"RPM_BUILD_NCPUS": lib.JOBS},  # %{_smp_build_ncpus}
            publish=PUBLISH,
        ))
    return targets


if __name__ == "__main__":
    lib.run(main)
