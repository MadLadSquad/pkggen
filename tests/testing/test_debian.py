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
    assert all(t["env"] == {"DEBIAN_FRONTEND": "noninteractive"} for t in targets)


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
    targets, warnings = describe("debian", versions=["debian_12", "debian_unstable", "debian_13"], host_arch="arm64")
    bookworm = scripts(by_name(targets)["debian-12"]["prepare"])
    assert "Codename: bookworm\nArchitectures: arm64 source\n" in bookworm
    assert "Suite:" not in bookworm
    assert "includedeb 'sid'" in scripts(by_name(targets)["debian-unstable"]["finalize"])
    assert warnings == ["debian-12, debian-unstable aren't in 'repositories.debian.suites', so each test "
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
