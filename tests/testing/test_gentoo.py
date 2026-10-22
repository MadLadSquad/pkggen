import pytest

from tests.testing.conftest import entry, scripts


def test_target(describe):
    targets, _ = describe("gentoo")
    target = targets[0]
    assert (target["name"], target["image"]) == ("gentoo", "docker.io/gentoo/stage3:latest")
    assert target["data_images"] == [{"image": "docker.io/gentoo/portage:latest", "path": "/var/db/repos/gentoo"}]
    assert "-ipc-sandbox -network-sandbox -pid-sandbox" in scripts(target["setup"])


def test_category_comes_from_the_packaging_output(describe):
    targets, _ = describe("gentoo", packages=[entry(name="foo", output="dev-libs/foo"),
                                              entry(name="bar", output="ude-base/bar")])
    target = targets[0]
    prepare = scripts(target["prepare"])
    assert "[$repo]\nlocation = /pkggen/repo\nauto-sync = no\n" in prepare
    assert 'echo "*/*::$repo ~amd64" > /etc/portage/package.accept_keywords/pkggen' in prepare
    # Categories that profiles/categories doesn't list are added in the container's copy
    assert ("grep -qx 'dev-libs' /var/db/repos/gentoo/profiles/categories /pkggen/repo/profiles/categories || "
            "echo 'dev-libs' >>") in prepare
    assert "grep -qx 'ude-base'" in prepare

    foo = target["packages"][0]
    assert "emerge --oneshot --onlydeps --noreplace '=dev-libs/foo-1.2.0'::\"$repo\"" in scripts(foo["build"])
    assert "ebuild '/pkggen/repo/dev-libs/foo/foo-1.2.0.ebuild' clean install" in scripts(foo["build"])
    install = scripts(foo["install"])
    assert "ebuild '/pkggen/repo/dev-libs/foo/foo-1.2.0.ebuild' qmerge" in install
    assert "echo '=dev-libs/foo-1.2.0' >> /pkggen/state/installed.atoms" in install
    assert "PKGDIR=/pkggen/out/binpkgs quickpkg" in scripts(target["finalize"])


@pytest.mark.parametrize("values, category", [
    ({"gentoo": {"category": "app-misc"}}, "app-misc"),
    ({"category": "dev-util"}, "dev-util"),
    ({}, "ude-base"),  # the first of repositories.gentoo.categories in the example
])
def test_category_fallbacks(describe, values, category):
    targets, _ = describe("gentoo", packages=[entry(name="foo", output="", **values)])
    assert f"'={category}/foo-1.2.0'" in scripts(targets[0]["packages"][0]["build"])


def test_arm64_keyword(describe):
    targets, _ = describe("gentoo", host_arch="arm64", packages=[entry(output="dev-libs/foo")])
    assert '::$repo ~arm64"' in scripts(targets[0]["prepare"])


def test_no_category_at_all(describe):
    with pytest.raises(Exception, match="can't tell the category of foo") as info:
        describe("gentoo", repository={"name": "x"}, packages=[entry(name="foo", output="")])
    assert type(info.value).__name__ == "TestingError"
