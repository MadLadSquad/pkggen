import pytest

from tests.testing.conftest import entry, scripts


def test_glibc_and_musl_share_one_rp_name(describe):
    targets, warnings = describe("void")
    assert [(t["name"], t["rp_name"], t["image"]) for t in targets] == [
        ("void-glibc", "void_x86_64", "ghcr.io/void-linux/void-glibc-full:latest"),
        ("void-musl", "void_x86_64", "ghcr.io/void-linux/void-musl-full:latest"),
    ]
    setup = scripts(targets[0]["setup"])
    assert "git clone --depth 1 https://github.com/void-linux/void-packages.git /opt/void-packages" in setup
    assert "XBPS_CHROOT_CMD=ethereal" in setup and "ln -sfn / masterdir" in setup
    assert "./xbps-src binary-bootstrap" in setup
    assert warnings == []


@pytest.mark.parametrize("versions, names", [
    (["void-musl"], ["void-musl"]),
    (["void_x86_64"], ["void-glibc", "void-musl"]),   # the rp-name selects both
    (["void-glibc", "void_x86_64"], ["void-glibc", "void-musl"]),
])
def test_versions(describe, versions, names):
    targets, _ = describe("void", versions=versions)
    assert [t["name"] for t in targets] == names


def test_unknown_version_lists_the_targets(describe):
    with pytest.raises(Exception, match=r"unknown void version\(s\).*: void-x\. Available: void_x86_64, "
                                        r"void-glibc, void-musl"):
        describe("void", versions=["void-x"])


def test_build_registers_shared_libraries(describe):
    targets, _ = describe("void", packages=[entry(name="foo", output="srcpkgs/foo")])
    target = targets[0]
    assert "cp -a /pkggen/repo/srcpkgs/. /opt/void-packages/srcpkgs/" in scripts(target["prepare"])
    build = target["packages"][0]["build"][0]
    assert build["workdir"] == "/opt/void-packages" and build["user"] is None
    assert './xbps-src -j "${PKGGEN_JOBS:-$(nproc)}" pkg \'foo\'' in build["run"]
    assert "--property=shlib-provides 'foo'" in build["run"]
    assert 'echo "$soname $pkgver" | tee -a common/shlibs >> /pkggen/state/shlibs' in build["run"]
    assert scripts(target["packages"][0]["install"]) == \
        "xbps-install -y --repository=/opt/void-packages/hostdir/binpkgs 'foo'\n"
    finalize = scripts(target["finalize"])
    assert 'xbps-rindex --compression xz -a "$PWD"/*.xbps' in finalize
    assert "/pkggen/repo/common/shlibs" in finalize
