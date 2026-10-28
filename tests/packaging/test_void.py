import pytest

from tests.packaging.conftest import artifact
from tests.support.helpers import load_generator


@pytest.fixture(scope="module")
def void():
    return load_generator("packaging", "void")


@pytest.mark.parametrize("value, expected", [("1.0-rc1_2:x/y z", "1.0.rc1.2.x.y.z"), (3, "3")])
def test_xbpsver(void, value, expected):
    assert void.xbpsver(value) == expected
    assert void.FILTERS["xbpsver"] is void.xbpsver


@pytest.mark.parametrize("version, valid", [
    ("1.2.3", True), ("1.2.3rc1", True), ("2.0+git", True), ("1.2-3", False), ("1.2_3", False), (".1", False),
])
def test_version_syntax(void, version, valid):
    assert bool(void.VERSION_RE.match(version)) == valid


def output(tmp_path, name="untitled-cli-parser"):
    return tmp_path / "pkggen-build" / "void" / "srcpkgs" / name


def test_default_templates(package_with, tmp_path):
    result = package_with("void")
    assert result.code == 0, result.err
    data = result.json
    assert data["output"].replace("\\", "/") == "pkggen-build/void/srcpkgs/untitled-cli-parser"
    assert [f.replace("\\", "/") for f in data["files_written"]] == [
        "pkggen-build/void/srcpkgs/untitled-cli-parser/template"]

    lines = (output(tmp_path) / "template").read_text().splitlines()
    a = artifact("untitled-cli-parser-1.2.0.tar.xz")
    for line in [
        "# Template file for 'untitled-cli-parser'",
        "pkgname=untitled-cli-parser",
        "version=1.2.0",
        "revision=1",
        'archs="x86_64* aarch64*"',
        'hostmakedepends=""',
        'short_desc="A command line argument parser"',
        'maintainer="MadLadSquad <contact@madladsquad.com>"',
        'license="MIT"',
        'homepage="https://github.com/MadLadSquad/UntitledCLIParser"',
        f'distfiles="{a["url"]}>untitled-cli-parser-1.2.0.tar.xz"',
        f'checksum="{a["checksums"]["sha2_256"]}"',
        "do_install() {",
    ]:
        assert line in lines, line


def test_any_architecture_has_no_archs(package_with, tmp_path):
    result = package_with("void", architectures="any", version="1.0-rc1", void={"revision": 4, "depends": ["glibc"]})
    assert result.code == 0, result.err
    lines = (output(tmp_path) / "template").read_text().splitlines()
    assert not any(line.startswith("archs=") for line in lines)
    for line in ["version=1.0.rc1", "revision=4", 'depends="glibc"']:
        assert line in lines, line


def test_noarch_and_several_distfiles(package_with, tmp_path):
    artifacts = [artifact("one-1.2.0.tar.gz"), artifact("two.zip")]
    result = package_with("void", artifacts=artifacts, source=artifacts[0], void={"archs": "noarch"})
    assert result.code == 0, result.err
    lines = (output(tmp_path) / "template").read_text().splitlines()
    assert 'archs="noarch"' in lines
    assert f'distfiles="{artifacts[0]["url"]}>one-1.2.0.tar.gz {artifacts[1]["url"]}>two.zip"' in lines


def test_invalid_version_fails_with_a_hint(package_with):
    result = package_with("void", version="v1!")
    assert result.code == 1
    assert "isn't a valid void version" in result.err and "void: { version: ... }" in result.err


def test_template_folder_files_are_copied(package_with, make_job, tmp_path):
    job = make_job("void")
    folder = tmp_path / "packages" / "void" / "untitled-cli-parser"
    (folder / "patches").mkdir(parents=True)
    (folder / "template.tmpl").write_text("pkgname={{ name }}\nversion={{ xbps_version }}\n")
    (folder / "patches" / "fix.patch").write_text("--- a\n+++ b\n")
    result = package_with("void", job)
    assert result.code == 0, result.err
    assert (output(tmp_path) / "patches" / "fix.patch").read_text() == "--- a\n+++ b\n"
    assert (output(tmp_path) / "template").read_text() == "pkgname=untitled-cli-parser\nversion=1.2.0\n"
