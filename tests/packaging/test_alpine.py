import pytest

from tests.packaging.conftest import artifact
from tests.support.helpers import load_generator


@pytest.fixture(scope="module")
def alpine():
    return load_generator("packaging", "alpine")


@pytest.mark.parametrize("value, expected", [
    ("1.2.0", "1.2.0"), ("v1.0-rc1", "1.0_rc1"), ("V2.0", "2.0"), ("2.0.0-beta.2", "2.0.0_beta2"),
    ("1.0_alpha", "1.0_alpha"), ("1.2p1", "1.2_p1"),
    ("1.0-foo", "1.0-foo"),  # left as it is, VERSION_RE rejects it
])
def test_apkver(alpine, value, expected):
    assert alpine.apkver(value) == expected
    assert alpine.FILTERS["apkver"] is alpine.apkver


@pytest.mark.parametrize("version, valid", [
    ("1.2.3", True), ("1.2.3a", True), ("1.2.3_rc1", True), ("1.2_p2_rc1", True), ("1", True),
    ("1.2-3", False), ("v1.2", False), ("1.2_foo", False), ("", False),
])
def test_version_syntax(alpine, version, valid):
    assert bool(alpine.VERSION_RE.match(version)) == valid


def output(tmp_path, name="untitled-cli-parser"):
    return tmp_path / "pkggen-build" / "alpine" / name


def test_default_templates(package_with, tmp_path):
    result = package_with("alpine")
    assert result.code == 0, result.err
    data = result.json
    assert data["output"].replace("\\", "/") == "pkggen-build/alpine/untitled-cli-parser"
    assert [f.replace("\\", "/") for f in data["files_written"]] == ["pkggen-build/alpine/untitled-cli-parser/APKBUILD"]
    assert "default templates were created in" in data["warnings"][0]

    lines = (output(tmp_path) / "APKBUILD").read_text().splitlines()
    a = artifact("untitled-cli-parser-1.2.0.tar.xz")
    for line in [
        "# Maintainer: MadLadSquad <contact@madladsquad.com>",
        'maintainer="MadLadSquad <contact@madladsquad.com>"',
        "pkgname=untitled-cli-parser",
        "pkgver=1.2.0",
        "pkgrel=0",
        'pkgdesc="A command line argument parser"',
        'url="https://github.com/MadLadSquad/UntitledCLIParser"',
        'arch="x86_64 aarch64"',
        'license="MIT"',
        'depends=""',
        'options="!check"',
        f"\tuntitled-cli-parser-1.2.0.tar.xz::{a['url']}",
        'builddir="$srcdir"/"untitled-cli-parser-1.2.0"',
        f"{a['checksums']['sha2_512']}  untitled-cli-parser-1.2.0.tar.xz",
    ]:
        assert line in lines, line


def test_second_run_is_unchanged(package_with, make_job):
    job = make_job("alpine")
    package_with("alpine", job)
    data = package_with("alpine", job).json
    assert data["files_written"] == [] and data["warnings"] == []


def test_dist_block_any_arch_and_versions(package_with, tmp_path):
    result = package_with("alpine", architectures="any", version="v2.0.0-beta.2", license="GPL-3.0",
                          alpine={"depends": ["musl"], "makedepends": ["meson"], "pkgrel": 3, "options": "!strip"})
    assert result.code == 0, result.err
    lines = (output(tmp_path) / "APKBUILD").read_text().splitlines()
    for line in ['arch="all"', "pkgver=2.0.0_beta2", "pkgrel=3", 'license="GPL-3.0-only"', 'depends="musl"',
                 'makedepends="meson"', 'options="!strip"']:
        assert line in lines, line


def test_invalid_version_fails_with_a_hint(package_with):
    result = package_with("alpine", version="2024-05-01-nightly")
    assert result.code == 1
    assert "isn't a valid alpine version" in result.err and "alpine: { pkgver: ... }" in result.err


def test_pkgver_override(package_with, tmp_path):
    result = package_with("alpine", version="nightly", alpine={"pkgver": "0.0.1_git20260101"})
    assert result.code == 0, result.err
    assert "pkgver=0.0.1_git20260101" in (output(tmp_path) / "APKBUILD").read_text().splitlines()


def test_unknown_architectures_fail(package_with):
    result = package_with("alpine", architectures=["riscv64"])
    assert result.code == 1 and "none of the architectures of untitled-cli-parser (riscv64)" in result.err
