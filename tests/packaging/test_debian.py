import os
import sys

import pytest

from tests.packaging.conftest import artifact


def debian_dir(tmp_path, name="untitled-cli-parser"):
    return tmp_path / "pkggen-build" / "debian" / "packages" / name / "debian"


def test_default_templates(package_with, tmp_path):
    result = package_with("debian")
    assert result.code == 0, result.err
    assert result.json["output"].replace("\\", "/") == "pkggen-build/debian/packages/untitled-cli-parser"
    out = debian_dir(tmp_path)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "changelog", "control", "copyright", "rules", "source/format"]

    assert (out / "changelog").read_text() == (
        "untitled-cli-parser (1.2.0-1) trixie; urgency=medium\n"
        "\n"
        "  * New upstream release 1.2.0.\n"
        "\n"
        " -- MadLadSquad <contact@madladsquad.com>  Tue, 01 Sep 2026 10:00:00 +0000\n"
    )
    control = (out / "control").read_text().splitlines()
    for line in ["Source: untitled-cli-parser", "Section: misc", "Maintainer: MadLadSquad <contact@madladsquad.com>",
                 "Build-Depends: debhelper-compat (= 13)", "Homepage: https://github.com/MadLadSquad/UntitledCLIParser",
                 "Package: untitled-cli-parser", "Architecture: amd64 arm64",
                 "Depends: ${shlibs:Depends}, ${misc:Depends}", "Description: A command line argument parser",
                 " A command line argument parser"]:
        assert line in control, line
    copyright_lines = (out / "copyright").read_text().splitlines()
    assert "Copyright: 2026 MadLadSquad <contact@madladsquad.com>" in copyright_lines
    assert "License: Expat" in copyright_lines
    assert (out / "source" / "format").read_bytes() == b"3.0 (quilt)\n"
    assert (out / "rules").read_text().startswith("#!/usr/bin/make -f\n")


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_rules_is_executable(package_with, tmp_path):
    assert package_with("debian").code == 0
    assert os.access(debian_dir(tmp_path) / "rules", os.X_OK)
    assert not os.access(debian_dir(tmp_path) / "control", os.X_OK)


def test_dist_block(package_with, tmp_path):
    result = package_with("debian", architectures="any", license="GPL-2.0-or-later", debian={
        "section": "libs", "build-depends": ["cmake"], "depends": ["libc6"], "revision": 2,
        "long-description": "Longer text", "copyright": "2020 Someone"})
    assert result.code == 0, result.err
    out = debian_dir(tmp_path)
    control = (out / "control").read_text().splitlines()
    for line in ["Section: libs", "Build-Depends: debhelper-compat (= 13), cmake", "Architecture: any",
                 "Depends: ${shlibs:Depends}, ${misc:Depends}, libc6", " Longer text"]:
        assert line in control, line
    assert (out / "changelog").read_text().startswith("untitled-cli-parser (1.2.0-2) trixie;")
    copyright_text = (out / "copyright").read_text()
    assert "Copyright: 2020 Someone" in copyright_text and "License: GPL-2+" in copyright_text


def test_architecture_all(package_with, tmp_path):
    assert package_with("debian", debian={"architecture": "all"}).code == 0
    assert "Architecture: all" in (debian_dir(tmp_path) / "control").read_text().splitlines()


def test_without_suites_or_optional_fields(package_with, make_job, tmp_path):
    a = artifact("bare-3.zip")
    package = {"name": "bare", "version": "3", "artifacts": [a], "source": a}
    job = make_job("debian", repository={}, package=package)
    assert package_with("debian", job).code == 0, package_with("debian", job).err
    out = debian_dir(tmp_path, "bare")
    assert (out / "changelog").read_text().startswith("bare (3-1) unstable; urgency=medium")
    control = (out / "control").read_text()
    assert "Homepage" not in control
    assert "Maintainer: Your Name <you@example.com>" in control
    assert "License: unknown" in (out / "copyright").read_text()
