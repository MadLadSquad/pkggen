import pytest

from tests.repositories.conftest import files


def test_gentoo_skeleton(gen_repo, example_config):
    result = gen_repo("gentoo", example_config["repositories"]["gentoo"])
    assert result.code == 0, result.err
    assert "Warning" not in result.err
    out = gen_repo.out
    assert files(out) == ["COPYRIGHT.txt", "metadata/layout.conf", "profiles/categories", "profiles/repo_name",
                          "repositories.xml"]
    assert (out / "COPYRIGHT.txt").read_text() == \
        "Copyright 2022 - 2027 MadLadSquad. Distributed under the terms of the MIT License.\n"
    assert (out / "profiles" / "categories").read_text() == "ude-base\nude-apps\nude-misc\n"
    assert (out / "profiles" / "repo_name").read_text() == "untitled-desktop-overlay\n"
    assert (out / "metadata" / "layout.conf").read_bytes() == (
        b"repo-name = untitled-desktop-overlay\n"
        b"masters = gentoo\n"
        b"thin-manifests = true\n"
        b"sign-manifests = false\n"
        b"profile-formats = portage-2\n"
        b"cache-formats = md5-dict\n"
    )
    assert (out / "repositories.xml").read_text() == """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE repositories SYSTEM "https://www.gentoo.org/dtd/repositories.dtd">
<repositories xmlns="" version="1.0">
  <repo priority="10" quality="experimental" status="unofficial">
    <name>untitled-desktop-overlay</name>
    <description>An overlay containing Gentoo ebuilds and autogens for the applications and libraries part of the UntitledDesktop project</description>
    <homepage>https://github.com/MadLadSquad/UntitledDesktopOverlay.git</homepage>
    <owner type="project">
      <email>contact@madladsquad.com</email>
      <name>MadLadSquad</name>
    </owner>
    <source type="git">https://github.com/MadLadSquad/UntitledDesktopOverlay.git</source>
    <feed>https://github.com/MadLadSquad/UntitledDesktopOverlay.git</feed>
  </repo>
</repositories>
"""


def test_minimal_overlay(gen_repo):
    result = gen_repo("gentoo", {"name": "mini"})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == ["metadata/layout.conf", "profiles/repo_name", "repositories.xml"]
    assert "  <repo>" in (gen_repo.out / "repositories.xml").read_text()


def test_layout_options(gen_repo):
    block = {"name": "o", "masters": ["gentoo", "guru"], "thin-manifests": "no", "sign-manifests": "yes",
             "profile-formats": ["portage-2", "profile-set"], "manifest-hashes": "BLAKE2B SHA512",
             "eapis-banned": [0, 1, 2]}
    assert gen_repo("gentoo", block).code == 0
    lines = (gen_repo.out / "metadata" / "layout.conf").read_text().splitlines()
    for line in ["masters = gentoo guru", "thin-manifests = false", "sign-manifests = true",
                 "profile-formats = portage-2 profile-set", "manifest-hashes = BLAKE2B SHA512",
                 "eapis-banned = 0 1 2"]:
        assert line in lines
    assert not any(line.startswith("eapis-deprecated") for line in lines)


def test_xml_is_escaped(gen_repo):
    block = {"name": "a&b", "description": "<x> & y", "owners": [{"name": "Tom & Jerry", "email": "t@x"}],
             "source": [{"type": "git", "url": "https://x/?a=1&b=2"}, {"type": "git"}], "feed": ["f1", "f2"]}
    assert gen_repo("gentoo", block).code == 0
    xml = (gen_repo.out / "repositories.xml").read_text()
    assert "<name>a&amp;b</name>" in xml
    assert "<description>&lt;x&gt; &amp; y</description>" in xml
    assert "<name>Tom &amp; Jerry</name>" in xml
    assert "<owner>" in xml
    assert xml.count("<source ") == 1 and "a=1&amp;b=2" in xml
    assert xml.count("<feed>") == 2


def test_warnings(gen_repo):
    block = {"name": "o", "quality": "great", "status": "maybe",
             "owners": [{"name": "NoMail"}, {"email": "x@y", "type": "robot"}, {}]}
    result = gen_repo("gentoo", block)
    assert result.code == 0
    for message in ["unknown gentoo quality 'great'", "unknown gentoo status 'maybe'",
                    "gentoo owner 'NoMail' has no email", "unknown gentoo owner type 'robot'"]:
        assert message in result.err
    assert (gen_repo.out / "repositories.xml").read_text().count("<owner") == 2


@pytest.mark.parametrize("block, message", [
    ({}, "'repositories.gentoo' is missing: name"),
    ({"name": "o", "copyright": {"years": "2026"}}, "'repositories.gentoo.copyright' is missing: owner, license"),
])
def test_errors(gen_repo, block, message):
    result = gen_repo("gentoo", block)
    assert result.code == 1
    assert message in result.err_plain


def test_rerun_skips_existing(gen_repo, example_config):
    block = example_config["repositories"]["gentoo"]
    gen_repo("gentoo", block)
    (gen_repo.out / "profiles" / "categories").write_text("mine\n")
    result = gen_repo("gentoo", block)
    assert result.err.count("skipped") == 5
    assert (gen_repo.out / "profiles" / "categories").read_text() == "mine\n"
    result = gen_repo("gentoo", block, force=True)
    assert result.err.count("overwrote") == 5
    assert (gen_repo.out / "profiles" / "categories").read_text().startswith("ude-base")
