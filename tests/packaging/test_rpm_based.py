from tests.packaging.conftest import artifact


def spec(tmp_path, name="untitled-cli-parser"):
    return (tmp_path / "pkggen-build" / "rpm-based" / "SPECS" / f"{name}.spec").read_text()


def test_default_template(package_with, tmp_path):
    result = package_with("rpm-based")
    assert result.code == 0, result.err
    assert result.json["output"].replace("\\", "/") == "pkggen-build/rpm-based"
    a = artifact("untitled-cli-parser-1.2.0.tar.xz")
    lines = spec(tmp_path).splitlines()
    for line in [
        "Name:           untitled-cli-parser",
        "Version:        1.2.0",
        "Release:        1%{?dist}",
        "Summary:        A command line argument parser",
        "License:        MIT",
        "URL:            https://github.com/MadLadSquad/UntitledCLIParser",
        f"Source0:        {a['url']}#/untitled-cli-parser-1.2.0.tar.xz",
        "ExclusiveArch:  x86_64 aarch64",
        "%autosetup -n untitled-cli-parser-1.2.0",
        "* Tue Sep 01 2026 MadLadSquad <contact@madladsquad.com> - 1.2.0-1",
        "- Update to 1.2.0",
    ]:
        assert line in lines, line
    assert "BuildRequires" not in spec(tmp_path)


def test_other_files_go_to_sources(package_with, make_job, tmp_path):
    job = make_job("rpm-based")
    templates = tmp_path / "packages" / "rpm-based" / "untitled-cli-parser"
    templates.mkdir(parents=True)
    (templates / "spec.tmpl").write_text("Name: {{ name }}\n")
    (templates / "fix.patch").write_text("--- a\n+++ b\n")
    (templates / "app.desktop.tmpl").write_text("Name={{ name }}\n")
    result = package_with("rpm-based", job)
    assert result.code == 0, result.err
    assert result.json["warnings"] == []
    repo = tmp_path / "pkggen-build" / "rpm-based"
    assert (repo / "SOURCES" / "fix.patch").read_text() == "--- a\n+++ b\n"
    assert (repo / "SOURCES" / "app.desktop").read_text() == "Name=untitled-cli-parser\n"
    assert spec(tmp_path) == "Name: untitled-cli-parser\n"


def test_any_arch_and_dist_block(package_with, tmp_path):
    result = package_with("rpm-based", architectures="any", version="2.0-beta", license="LGPL-3.0",
                          artifacts=[artifact("a-2.tar.gz"), artifact("b.zip")],
                          **{"rpm-based": {"build-requires": ["cmake", "pkgconfig(x11)"], "requires": ["glibc"],
                                           "release": 4, "long-description": "Long"}})
    assert result.code == 0, result.err
    text = spec(tmp_path)
    assert "ExclusiveArch" not in text
    lines = text.splitlines()
    for line in ["Version:        2.0_beta", "Release:        4%{?dist}", "License:        LGPL-3.0-only",
                 "Source1:        https://downloads.example.com/b.zip#/b.zip", "BuildRequires:  cmake",
                 "BuildRequires:  pkgconfig(x11)", "Requires:       glibc", "Long",
                 "- Update to 2.0-beta"]:
        assert line in lines, line
    assert text.splitlines()[-2].endswith("- 2.0_beta-4")
