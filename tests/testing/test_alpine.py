from tests.testing.conftest import entry, scripts


def test_targets(describe):
    targets, warnings = describe("alpine")
    assert [(t["name"], t["rp_name"], t["image"]) for t in targets] == [
        ("alpine-edge", "alpine_edge", "docker.io/library/alpine:edge"),
        ("alpine-3.24", "alpine_3_24", "docker.io/library/alpine:3.24"),
        ("alpine-3.23", "alpine_3_23", "docker.io/library/alpine:3.23"),
    ]
    setup = scripts(targets[0]["setup"])
    assert "apk update\napk add alpine-sdk sudo curl" in setup and "addgroup builder abuild" in setup
    keygen = targets[0]["setup"][1]
    assert keygen["user"] == "builder"
    assert keygen["run"] == "PACKAGER='pkggen <noreply@madladsquad.com>' abuild-keygen -a -n\n"
    assert warnings == []


def test_arm64(describe):
    targets, warnings = describe("alpine", host_arch="arm64")
    assert len(targets) == 3 and warnings == []


def test_packages_are_built_in_an_aports_tree(describe):
    targets, _ = describe("alpine", packages=[entry(name="foo"), entry(name="bar")],
                          repository={"name": "untitled-desktop"}, versions=["alpine-3.24"])
    target = targets[0]
    prepare = scripts(target["prepare"])
    assert "cp -r '/pkggen/repo/foo' '/pkggen/work/aports/untitled-desktop/foo'" in prepare
    assert "chown -R builder: /pkggen/work/aports /pkggen/state/apk" in prepare

    build = target["packages"][0]["build"][0]
    assert (build["user"], build["workdir"]) == ("builder", "/pkggen/work/aports/untitled-desktop/foo")
    assert "export REPODEST=/pkggen/state/apk\n" in build["run"] and "abuild -r\n" in build["run"]
    assert '> /pkggen/state/foo.pkgs' in build["run"]
    install = scripts(target["packages"][0]["install"])
    assert "echo '/pkggen/state/apk/untitled-desktop' >> /etc/apk/repositories" in install
    assert "apk add $(cat /pkggen/state/foo.pkgs)" in install

    finalize = scripts(target["finalize"])
    assert "cp '/pkggen/state/apk/untitled-desktop'/\"$arch\"/* /pkggen/repo/\"$arch\"/" in finalize
    assert target["finalize"][-1]["name"] == "collect the repository"


def test_default_repository_name(describe):
    targets, _ = describe("alpine", packages=[entry(name="foo")], repository={})
    assert "/pkggen/work/aports/pkggen/foo" in scripts(targets[0]["prepare"])
