import pytest

from tests.testing.conftest import SHA256, artifact, entry, scripts


def by_name(targets):
    return {t["name"]: t for t in targets}


def test_every_release_by_default(describe):
    targets, _ = describe("debian")
    assert [t["name"] for t in targets] == [
        "debian-unstable", "debian-14", "debian-13", "debian-12",
        "ubuntu-26.10", "ubuntu-26.04", "ubuntu-24.04", "ubuntu-22.04"]
    assert by_name(targets)["ubuntu-24.04"]["image"] == "docker.io/library/ubuntu:24.04"
    assert all(t["env"] == {"DEBIAN_FRONTEND": "noninteractive", "DEB_BUILD_OPTIONS": "parallel=${PKGGEN_JOBS}"}
               for t in targets)


def test_suite_from_the_repository_config(describe, example_config):
    # The example has trixie (suite: stable, signed with the default key) and noble
    targets, warnings = describe("debian", versions=["debian_13", "ubuntu_24_04"])
    trixie = scripts(by_name(targets)["debian-13"]["prepare"])
    assert ("Origin: MadLadSquad\nLabel: UntitledDesktopDEB\nSuite: stable\nCodename: trixie\n"
            "Architectures: amd64 source\nComponents: main\n"
            "Description: Debian packages for the UntitledDesktop project\n") in trixie
    assert "cat > '/pkggen/repo/conf/distributions'" in trixie
    # The test repository is never signed: there's no key in the container
    assert "SignWith" not in trixie
    assert "Codename: noble" in scripts(by_name(targets)["ubuntu-24.04"]["prepare"])
    assert warnings == []


def test_releases_outside_the_configured_suites(describe):
    # The example configures trixie and sid, but not bookworm
    targets, warnings = describe("debian", versions=["debian_12", "debian_unstable", "debian_13", "ubuntu_22_04"],
                                 host_arch="arm64")
    bookworm = scripts(by_name(targets)["debian-12"]["prepare"])
    assert "Codename: bookworm\nArchitectures: arm64 source\n" in bookworm
    assert "Suite:" not in bookworm
    assert "includedeb 'sid'" in scripts(by_name(targets)["debian-unstable"]["finalize"])
    assert "includedeb 'jammy'" in scripts(by_name(targets)["ubuntu-22.04"]["finalize"])
    assert warnings == ["debian-12, ubuntu-22.04 aren't in 'repositories.debian.suites', so each test "
                        "repository gets a suite named after the release's codename"]


def test_suite_matched_by_suite_name(describe):
    repository = {"origin": "Me", "suites": [{"codename": "unstable", "components": ["main", "extra"]}]}
    targets, warnings = describe("debian", versions=["debian_unstable"], repository=repository)
    prepare = scripts(targets[0]["prepare"])
    assert "Label: Me\nCodename: unstable\n" in prepare and "Components: main extra\n" in prepare
    assert "includedeb 'unstable'" in scripts(targets[0]["finalize"])
    assert warnings == []


def test_build_and_install_steps(describe):
    targets, _ = describe("debian", versions=["debian_13"], packages=[entry(name="foo", output="packages/foo")])
    package = targets[0]["packages"][0]
    build = scripts(package["build"])
    assert package["name"] == "foo"
    for fragment in [
        "curl -fsSL --retry 3 -o '/pkggen/work/foo/foo-1.2.0.tar.xz' 'https://downloads.example.com/foo-1.2.0.tar.xz'",
        f"echo '{SHA256}  /pkggen/work/foo/foo-1.2.0.tar.xz' | sha256sum -c -",
        "cp 'foo-1.2.0.tar.xz' 'foo_1.2.0.orig.tar.xz'",
        "dir='foo-1.2.0'",
        "cp -r /pkggen/repo/packages/foo/debian build/",
        "apt-get build-dep -y '/pkggen/work/foo'/build",
        "dpkg-buildpackage -us -uc -b",
        "mv '/pkggen/work/foo'/*.deb /pkggen/state/debs/foo/",
    ]:
        assert fragment in build
    assert scripts(package["install"]) == "apt-get install -y /pkggen/state/debs/foo/*.deb\n"
    # debian/rules may come in without its executable bit (from Windows)
    assert targets[0]["prepare"][-1]["run"] == "chmod +x '/pkggen/repo/packages/foo/debian/rules'\n"
    finalize = targets[0]["finalize"]
    assert "reprepro -b /pkggen/repo includedeb 'trixie' \"$@\"" in finalize[0]["run"]
    assert finalize[-1]["run"] == "rm -rf /pkggen/out\ncp -rL /pkggen/repo /pkggen/out\n"


@pytest.mark.parametrize("alias, fragment", [
    ("foo-1.2.0.zip", "unzip -q 'foo-1.2.0.zip' -d src"),
    ("foo.bin", "cp 'foo.bin' src/"),
])
def test_sources_that_are_not_tarballs(describe, alias, fragment):
    targets, _ = describe("debian", versions=["debian_13"], packages=[entry(artifacts=[artifact(alias)])])
    build = scripts(targets[0]["packages"][0]["build"])
    assert fragment in build and ".orig." not in build


def test_package_without_source_fails_its_build(describe):
    targets, _ = describe("debian", versions=["debian_13"], packages=[entry(artifacts=[])])
    build = scripts(targets[0]["packages"][0]["build"])
    assert "has no source artifact" in build and "exit 1" in build


def test_user_steps(describe):
    test = {"setup": [{"name": "deps", "run": "apt-get install -y libfoo-dev"}],
            "post-setup": [{"name": "check", "run": "foo --version"}]}
    targets, _ = describe("debian", versions=["debian_13"], test=test)
    assert [s["name"] for s in targets[0]["setup"]] == ["install the build tools", "deps"]
    assert [s["name"] for s in targets[0]["post_setup"]] == ["check"]


def test_each_release_gets_its_own_version_suffix(describe):
    targets, _ = describe("debian", versions=["debian_13", "ubuntu_24_04", "debian_unstable"],
                          packages=[entry(name="foo", output="packages/foo")])
    builds = {t["name"]: scripts(t["packages"][0]["build"]) for t in targets}
    assert ("sed -i -E '1s/^([^ ]+) \\(([^)]*)\\) [^;]*;/\\1 (\\2~deb13) trixie;/' debian/changelog"
            in builds["debian-13"])
    assert "(\\2~ubuntu24.04) noble;" in builds["ubuntu-24.04"]
    assert "(\\2~debsid) sid;" in builds["debian-unstable"]
    # The changelog is rewritten in the build copy, before the build dependencies are read
    build = builds["debian-13"]
    assert build.index("cp -r /pkggen/repo/packages/foo/debian build/") < build.index("sed -i -E") \
        < build.index("apt-get build-dep")


def test_version_suffix_sorts_like_the_releases(describe):
    """dpkg's ordering: later releases must get higher versions, so upgrades pick them."""
    import functools
    import shutil
    import subprocess
    if not shutil.which("dpkg"):
        pytest.skip("needs dpkg")
    from tests.support.helpers import load_generator
    table = load_generator("testing", "debian").TARGETS
    order = ["debian_12", "debian_13", "debian_14", "debian_unstable"]
    versions = ["1.0-1" + table[rp]["version-suffix"] for rp in order]
    ubuntu = ["1.0-1" + table[rp]["version-suffix"] for rp in ("ubuntu_22_04", "ubuntu_24_04", "ubuntu_26_04",
                                                                 "ubuntu_26_10")]

    def compare(a, b):
        return -1 if subprocess.run(["dpkg", "--compare-versions", a, "lt", b]).returncode == 0 else 1
    assert sorted(versions, key=functools.cmp_to_key(compare)) == versions
    assert sorted(ubuntu, key=functools.cmp_to_key(compare)) == ubuntu
    # The plain version (an official package) still wins
    assert compare(versions[-1], "1.0-1") == -1


@pytest.mark.parametrize("suite, fragment", [
    ({"codename": "trixie", "version-suffix": "+mine1"}, "(\\2+mine1) trixie;"),
    ({"codename": "trixie", "version-suffix": ""}, "(\\2) trixie;"),
    ({"codename": "trixie", "version-suffix": None}, "(\\2) trixie;"),
])
def test_version_suffix_from_the_suite(describe, suite, fragment):
    targets, _ = describe("debian", versions=["debian_13"], repository={"suites": [suite]},
                          packages=[entry(name="foo", output="packages/foo")])
    assert fragment in scripts(targets[0]["packages"][0]["build"])


def test_bad_version_suffix(describe):
    with pytest.raises(Exception, match="'version-suffix' of the trixie suite must only contain letters"):
        describe("debian", versions=["debian_13"], repository={"suites": [{"codename": "trixie",
                                                                           "version-suffix": "a/b"}]})
