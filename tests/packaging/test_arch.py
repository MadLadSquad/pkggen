from tests.packaging.conftest import artifact


def output(tmp_path, name="untitled-cli-parser"):
    return tmp_path / "pkggen-build" / "arch" / name


def test_default_templates(package_with, tmp_path):
    result = package_with("arch")
    assert result.code == 0, result.err
    data = result.json
    assert data["output"].replace("\\", "/") == "pkggen-build/arch/untitled-cli-parser"
    assert sorted(f.replace("\\", "/") for f in data["files_written"]) == [
        "pkggen-build/arch/untitled-cli-parser/.SRCINFO", "pkggen-build/arch/untitled-cli-parser/PKGBUILD"]
    assert "default templates were created in" in data["warnings"][0]
    assert (tmp_path / "packages" / "arch" / "untitled-cli-parser" / "PKGBUILD.tmpl").is_file()

    pkgbuild = (output(tmp_path) / "PKGBUILD").read_text()
    a = artifact("untitled-cli-parser-1.2.0.tar.xz")
    for line in [
        "# Maintainer: MadLadSquad <contact@madladsquad.com>",
        "pkgname=untitled-cli-parser",
        "pkgver=1.2.0",
        "pkgrel=1",
        'pkgdesc="A command line argument parser"',
        "arch=('x86_64' 'aarch64')",
        'url="https://github.com/MadLadSquad/UntitledCLIParser"',
        "license=('MIT')",
        "depends=()",
        f'\t"untitled-cli-parser-1.2.0.tar.xz::{a["url"]}"',
        f"\t'{a['checksums']['sha2_256']}'",
        '# The sources are extracted to "$srcdir" (most likely "$srcdir/untitled-cli-parser-1.2.0").',
    ]:
        assert line in pkgbuild.splitlines(), line

    srcinfo = (output(tmp_path) / ".SRCINFO").read_text().splitlines()
    assert srcinfo[0] == "pkgbase = untitled-cli-parser"
    assert "\tarch = x86_64" in srcinfo and "\tarch = aarch64" in srcinfo
    assert f"\tsha256sums = {a['checksums']['sha2_256']}" in srcinfo
    assert srcinfo[-1] == "pkgname = untitled-cli-parser"


def test_second_run_is_unchanged(package_with, make_job):
    job = make_job("arch")
    package_with("arch", job)
    data = package_with("arch", job).json
    assert data["files_written"] == [] and data["warnings"] == []
    assert len(data["files_unchanged"]) == 2


def test_dist_block_and_any_arch(package_with, tmp_path):
    result = package_with("arch", architectures="any", version="1.0-rc1", license="GPL-3.0",
                          arch={"depends": ["glibc", "it's"], "makedepends": ["cmake"], "pkgrel": 3})
    assert result.code == 0, result.err
    lines = (output(tmp_path) / "PKGBUILD").read_text().splitlines()
    for line in ["arch=('any')", "pkgver=1.0_rc1", "pkgrel=3", "license=('GPL-3.0-only')",
                 "depends=('glibc' 'it'\\''s')", "makedepends=('cmake')"]:
        assert line in lines
    srcinfo = (output(tmp_path) / ".SRCINFO").read_text().splitlines()
    assert "\tdepends = glibc" in srcinfo and "\tmakedepends = cmake" in srcinfo and "\tarch = any" in srcinfo


def test_unknown_architectures_fail(package_with):
    result = package_with("arch", architectures=["ppc64"])
    assert result.code == 1
    assert "none of the architectures of untitled-cli-parser (ppc64) can be packaged for arch" in result.err_plain


def test_missing_optional_fields_use_defaults(package_with, tmp_path):
    a = artifact("bare-2.zip")
    package = {"name": "bare", "version": "2", "artifacts": [a], "source": a}
    result = package_with("arch", package=package)
    assert result.code == 0, result.err
    lines = (output(tmp_path, "bare") / "PKGBUILD").read_text().splitlines()
    assert "# Maintainer: Your Name <you@example.com>" in lines
    assert 'pkgdesc="bare"' in lines
    assert "license=('LicenseRef-unknown')" in lines


def test_homepage_falls_back_to_source_url(package_with, tmp_path, make_job):
    job = make_job("arch")
    del job["package"]["homepage"]
    assert package_with("arch", job).code == 0
    assert 'url="https://downloads.example.com/untitled-cli-parser-1.2.0.tar.xz"' in \
        (output(tmp_path) / "PKGBUILD").read_text()
