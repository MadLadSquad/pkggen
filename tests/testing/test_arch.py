from tests.testing.conftest import entry, scripts


def test_target(describe):
    targets, warnings = describe("arch")
    assert [(t["name"], t["image"]) for t in targets] == [("arch", "docker.io/library/archlinux:base-devel")]
    assert "useradd -m builder" in scripts(targets[0]["setup"])
    assert warnings == []


def test_no_arm64_image(describe):
    targets, warnings = describe("arch", host_arch="arm64", packages=[entry()])
    assert targets == []
    assert warnings == ["arch has no arm64 container image, so it's skipped"]


def test_build_runs_makepkg_as_the_build_user(describe):
    targets, _ = describe("arch", packages=[entry(name="foo")])
    hand_over, build = targets[0]["packages"][0]["build"]
    assert hand_over == {"name": "hand foo to the build user", "run": "chown -R builder: '/pkggen/repo/foo'\n",
                         "user": None, "workdir": None}
    assert (build["user"], build["workdir"]) == ("builder", "/pkggen/repo/foo")
    assert build["run"].startswith("makepkg -s --noconfirm\n")
    assert "> /pkggen/state/foo.pkgs" in build["run"]
    assert scripts(targets[0]["packages"][0]["install"]) == "pacman -U --noconfirm $(cat /pkggen/state/foo.pkgs)\n"


def test_repository_name(describe):
    finalize = scripts(describe("arch")[0][0]["finalize"])
    assert "repo-add 'pkggen.db.tar.zst' ./*.pkg.tar.*" in finalize
    finalize = scripts(describe("arch", repository={"name": "mine"})[0][0]["finalize"])
    assert "repo-add 'mine.db.tar.zst'" in finalize
    assert "rm -rf ./*/src ./*/pkg" in finalize


def test_finalize_removes_build_leftovers(describe):
    finalize = scripts(describe("arch", packages=[entry(name="foo")])[0][0]["finalize"])
    assert "rm -rf ./*/src ./*/pkg\nrm -f 'foo/foo-1.2.0.tar.xz'\n" in finalize
