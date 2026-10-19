from tests.testing.conftest import SHA256, artifact, entry, scripts


def by_name(targets):
    return {t["name"]: t for t in targets}


def test_every_release_by_default(describe):
    targets, warnings = describe("rpm-based")
    assert [t["name"] for t in targets] == [
        "fedora-rawhide", "fedora-44", "fedora-43", "opensuse-tumbleweed", "opensuse-leap-15.6",
        "openmandriva-rolling", "openmandriva-6.0"]
    assert by_name(targets)["opensuse-leap-15.6"]["image"] == "registry.opensuse.org/opensuse/leap:15.6"
    assert warnings == []


def test_rolling_releases_upgrade_first(describe):
    targets = by_name(describe("rpm-based")[0])
    assert [s["name"] for s in targets["opensuse-tumbleweed"]["setup"]] == ["upgrade the system", "install the build tools"]
    assert targets["opensuse-tumbleweed"]["setup"][0]["run"] == "zypper -n dup\n"
    assert targets["openmandriva-rolling"]["setup"][0]["run"] == "dnf -y distro-sync\n"
    assert [s["name"] for s in targets["fedora-44"]["setup"]] == ["install the build tools"]
    assert [s["name"] for s in targets["opensuse-leap-15.6"]["setup"]] == ["install the build tools"]


def test_fedora_build_and_install(describe):
    package = entry(name="foo", output="", artifacts=[artifact("foo-1.2.0.tar.xz"), artifact("extra.tar.gz")])
    targets, _ = describe("rpm-based", versions=["fedora_44"], packages=[package])
    steps = targets[0]["packages"][0]
    build = scripts(steps["build"])
    for fragment in [
        "curl -fsSL --retry 3 -o '/pkggen/repo/SOURCES/foo-1.2.0.tar.xz'",
        f"echo '{SHA256}  /pkggen/repo/SOURCES/extra.tar.gz' | sha256sum -c -",
        "dnf -y builddep '/pkggen/repo/SPECS/foo.spec'",
        "rpmbuild --define \"_topdir /pkggen/repo\" -ba '/pkggen/repo/SPECS/foo.spec' > /pkggen/state/foo.rpmbuild 2>&1 "
        "|| { cat /pkggen/state/foo.rpmbuild; exit 1; }",
        "sed -n 's|^Wrote: \\(.*/RPMS/.*\\.rpm\\)$|\\1|p' /pkggen/state/foo.rpmbuild | grep -v -e '-debuginfo-' "
        "-e '-debugsource-' > /pkggen/state/foo.rpms",
    ]:
        assert fragment in build
    assert scripts(steps["install"]) == "dnf -y install $(cat /pkggen/state/foo.rpms)\n"
    assert "createrepo_c /pkggen/repo/RPMS" in scripts(targets[0]["finalize"])


def test_opensuse_uses_zypper(describe):
    targets, _ = describe("rpm-based", versions=["opensuse-leap-15.6"], packages=[entry(name="foo")])
    steps = targets[0]["packages"][0]
    assert ("rpmspec --define \"_topdir /pkggen/repo\" -q --buildrequires '/pkggen/repo/SPECS/foo.spec' "
            "| xargs -r -d '\\n' zypper -n in") in scripts(steps["build"])
    assert scripts(steps["install"]) == ("zypper -n --no-gpg-checks in --allow-unsigned-rpm "
                                         "$(cat /pkggen/state/foo.rpms)\n")
