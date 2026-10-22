import pytest

from tests.packaging.conftest import artifact

NAME = "untitled-cli-parser"


def package_dir(tmp_path, category="ude-base", name=NAME):
    return tmp_path / "pkggen-build" / "gentoo" / category / name


def test_default_templates(package_with, tmp_path):
    result = package_with("gentoo")
    assert result.code == 0, result.err
    out = package_dir(tmp_path)
    assert result.json["output"].replace("\\", "/") == f"pkggen-build/gentoo/ude-base/{NAME}"
    assert sorted(p.name for p in out.iterdir()) == ["Manifest", "metadata.xml", f"{NAME}-1.2.0.ebuild"]
    warnings = result.json["warnings"]
    assert f"{NAME} has no 'category', so it went into 'ude-base'" in warnings[1]

    ebuild = (out / f"{NAME}-1.2.0.ebuild").read_text()
    a = artifact(f"{NAME}-1.2.0.tar.xz")
    assert ebuild.startswith("# Copyright 2022 - 2027 MadLadSquad\n# Distributed under the terms of the MIT License\n")
    for line in ['DESCRIPTION="A command line argument parser"', 'HOMEPAGE="https://github.com/MadLadSquad/UntitledCLIParser"',
                 f"\t{a['url']} -> {NAME}-1.2.0.tar.xz", 'S="${WORKDIR}/${P}"', 'LICENSE="MIT"', 'SLOT="0"',
                 'KEYWORDS="~amd64 ~arm64"', 'DEPEND=""', 'RDEPEND="${DEPEND}"', 'BDEPEND=""', "EAPI=8"]:
        assert line in ebuild.splitlines(), line

    assert (out / "Manifest").read_text() == \
        f"DIST {NAME}-1.2.0.tar.xz {a['size']} BLAKE2B {a['checksums']['blake2b']} SHA512 {a['checksums']['sha2_512']}\n"

    assert (out / "metadata.xml").read_text() == """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE pkgmetadata SYSTEM "https://www.gentoo.org/dtd/metadata.dtd">
<pkgmetadata>
	<maintainer type="person">
		<email>contact@madladsquad.com</email>
		<name>MadLadSquad</name>
	</maintainer>
	<upstream>
		<maintainer>
			<name>MadLadSquad</name>
			<email>contact@madladsquad.com</email>
		</maintainer>
		<remote-id type="github">MadLadSquad/UntitledCLIParser</remote-id>
	</upstream>
</pkgmetadata>
"""


@pytest.mark.parametrize("version", ["1.2.0-rc1", "v1.2", "1.2.0beta", "abc", "1.2_alpha-x"])
def test_invalid_versions(package_with, version):
    result = package_with("gentoo", version=version)
    assert result.code == 1
    assert f"version '{version}' of {NAME} isn't a valid Gentoo version" in result.err_plain


@pytest.mark.parametrize("version", ["1", "1.2.3", "1.2.3a", "1.2_rc1", "1.2_p20260101", "1.2_beta1_p2-r3", "2026.10"])
def test_valid_versions(package_with, version):
    assert package_with("gentoo", version=version).code == 0


def test_category_from_package_and_gentoo_block(package_with, tmp_path):
    assert package_with("gentoo", category="ude-apps").json["warnings"][1:] == []
    assert package_dir(tmp_path, "ude-apps").is_dir()
    assert package_with("gentoo", category="ude-apps", gentoo={"category": "ude-misc"}).json["warnings"][1:] == []
    assert package_dir(tmp_path, "ude-misc").is_dir()


def test_unlisted_category_warns_without_masters(package_with, make_job, example_config, tmp_path):
    repository = {k: v for k, v in example_config["repositories"]["gentoo"].items() if k != "masters"}
    result = package_with("gentoo", make_job("gentoo", repository=repository, category="dev-libs"))
    assert result.code == 0
    assert "category 'dev-libs' isn't listed in 'repositories.gentoo.categories'" in result.json["warnings"][-1]
    assert package_dir(tmp_path, "dev-libs").is_dir()


def test_categories_of_the_masters_dont_warn(package_with, tmp_path):
    # The example overlay builds on ::gentoo, which has dev-libs
    result = package_with("gentoo", category="dev-libs")
    assert result.code == 0
    assert not any("isn't listed" in w for w in result.json["warnings"])


@pytest.mark.parametrize("overrides, repository, message", [
    ({}, {"name": "o"}, "has no 'category' and 'repositories.gentoo.categories' is empty"),
    ({"category": "bad/category"}, {"name": "o"}, "'bad/category' isn't a valid Gentoo category name"),
    ({"category": "x"}, {"manifest-hashes": "SHA256 WHIRLPOOL"},
     "unsupported manifest hash WHIRLPOOL in 'repositories.gentoo.manifest-hashes'"),
])
def test_errors(package_with, make_job, overrides, repository, message):
    result = package_with("gentoo", make_job("gentoo", repository=repository, **overrides))
    assert result.code == 1
    assert message in result.err_plain


def test_keywords(package_with, tmp_path):
    result = package_with("gentoo", architectures=["amd64", "ppc64", "~riscv", "arm64"])
    assert result.code == 0
    assert "'~riscv' in 'architectures' of untitled-cli-parser isn't a valid Gentoo keyword" in \
        " ".join(result.json["warnings"])
    ebuild = (package_dir(tmp_path) / f"{NAME}-1.2.0.ebuild").read_text()
    assert 'KEYWORDS="~amd64 ~ppc64 ~arm64"' in ebuild


def test_any_keywords_and_dist_overrides(package_with, tmp_path):
    assert package_with("gentoo", architectures="any").code == 0
    ebuild = (package_dir(tmp_path) / f"{NAME}-1.2.0.ebuild").read_text()
    assert 'KEYWORDS="~alpha ~amd64 ~arm ~arm64 ~hppa ~loong ~m68k ~mips ~ppc ~ppc64 ~riscv ~s390 ~sparc ~x86"' in ebuild

    assert package_with("gentoo", license="BSD-3-Clause", gentoo={
        "keywords": "amd64 ~arm64", "depend": ["dev-libs/a", "dev-libs/b"], "rdepend": ["x11-libs/c"],
        "bdepend": ["dev-build/cmake"]}).code == 0
    lines = (package_dir(tmp_path) / f"{NAME}-1.2.0.ebuild").read_text().splitlines()
    for line in ['KEYWORDS="amd64 ~arm64"', 'DEPEND="dev-libs/a dev-libs/b"', 'RDEPEND="${DEPEND} x11-libs/c"',
                 'BDEPEND="dev-build/cmake"', 'LICENSE="BSD"']:
        assert line in lines, line


def test_manifest_keeps_other_distfiles_sorted(package_with, make_job, tmp_path):
    out = package_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "Manifest").write_text("DIST zzz-old.tar.gz 1 BLAKE2B a SHA512 b\n"
                                  f"DIST {NAME}-1.2.0.tar.xz 1 BLAKE2B stale SHA512 stale\n"
                                  "DIST aaa-old.tar.gz 2 BLAKE2B c SHA512 d\n"
                                  "EBUILD ignored 1 SHA512 x\n")
    assert package_with("gentoo").code == 0
    lines = (out / "Manifest").read_text().splitlines()
    assert [line.split()[1] for line in lines] == ["aaa-old.tar.gz", f"{NAME}-1.2.0.tar.xz", "zzz-old.tar.gz"]
    assert "stale" not in lines[1]


def test_manifest_hashes_option(package_with, make_job, tmp_path):
    repository = {"categories": ["ude-base"], "manifest-hashes": ["sha256", "MD5"]}
    a = artifact("x-1.tar.gz")
    result = package_with("gentoo", make_job("gentoo", repository=repository, artifacts=[a]))
    assert result.code == 0, result.err
    assert (package_dir(tmp_path) / "Manifest").read_text() == \
        f"DIST x-1.tar.gz {a['size']} SHA256 {a['checksums']['sha2_256']} MD5 {a['checksums']['md5']}\n"


def test_thick_manifests_warn(package_with, make_job):
    result = package_with("gentoo", make_job("gentoo", repository={"categories": ["c"], "thin-manifests": False}))
    assert "'thin-manifests' is off" in " ".join(result.json["warnings"])


def test_no_artifacts_no_manifest(package_with, tmp_path):
    assert package_with("gentoo", artifacts=[], source={"directory": "x"}).code == 0
    assert not (package_dir(tmp_path) / "Manifest").exists()


def test_second_run_is_unchanged(package_with, make_job):
    job = make_job("gentoo")
    package_with("gentoo", job)
    result = package_with("gentoo", job).json
    assert result["files_written"] == []
    assert len(result["files_unchanged"]) == 3
