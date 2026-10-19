import pytest

from tests.repositories.conftest import files


def test_rpm_skeleton(gen_repo, example_config):
    result = gen_repo("rpm-based", example_config["repositories"]["rpm-based"])
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore", "BUILD/.gitkeep", "RPMS/.gitkeep", "SOURCES/.gitkeep",
                                   "SPECS/.gitkeep", "SRPMS/.gitkeep", "untitled-desktop-rpm.repo"]
    assert (gen_repo.out / "untitled-desktop-rpm.repo").read_bytes() == (
        b"[untitled-desktop-rpm]\n"
        b"name=RPM packages for the UntitledDesktop project\n"
        b"baseurl=https://rpms.madladsquad.com/\n"
        b"type=rpm-md\n"
        b"enabled=1\n"
        b"gpgcheck=1\n"
        b"gpgkey=https://rpms.madladsquad.com/rpmkey.pub\n"
    )
    assert (gen_repo.out / ".gitignore").read_text() == "*.tar.*\nBUILD/*/\nBUILDROOT/\n"


@pytest.mark.parametrize("block, expected", [
    ({"name": "r", "baseurl": "https://x/"}, ["name=r", "gpgcheck=0"]),
    ({"name": "r", "baseurl": "https://x/", "gpgcheck": True}, ["gpgcheck=1"]),
    ({"name": "r", "baseurl": "https://x/", "gpgkey": "https://k", "gpgcheck": "no"}, ["gpgcheck=0", "gpgkey=https://k"]),
    ({"name": "r", "baseurl": "https://x/", "repo_gpgcheck": "yes"}, ["repo_gpgcheck=1"]),
    ({"name": "r", "baseurl": "https://x/", "repo_gpgcheck": False}, ["repo_gpgcheck=0"]),
])
def test_repo_file_options(gen_repo, block, expected):
    assert gen_repo("rpm-based", block).code == 0
    lines = (gen_repo.out / "r.repo").read_text().splitlines()
    for line in expected:
        assert line in lines
    if "repo_gpgcheck" not in block:
        assert not any(line.startswith("repo_gpgcheck") for line in lines)


def test_no_repo_file_without_name_and_baseurl(gen_repo):
    result = gen_repo("rpm-based", {})
    assert result.code == 0
    assert not list(gen_repo.out.glob("*.repo"))
    assert "Warning" not in result.err


@pytest.mark.parametrize("block", [{"name": "r"}, {"baseurl": "https://x/"}])
def test_half_configured_repo_file_warns(gen_repo, block):
    result = gen_repo("rpm-based", block)
    assert result.code == 0
    assert "needs both 'name' and 'baseurl'" in result.err
    assert not list(gen_repo.out.glob("*.repo"))


def test_rpm_rerun_keeps_files(gen_repo, example_config):
    block = example_config["repositories"]["rpm-based"]
    gen_repo("rpm-based", block)
    (gen_repo.out / "untitled-desktop-rpm.repo").write_text("edited\n")
    result = gen_repo("rpm-based", block)
    assert result.code == 0
    assert (gen_repo.out / "untitled-desktop-rpm.repo").read_text() == "edited\n"
    gen_repo("rpm-based", block, force=True)
    assert (gen_repo.out / "untitled-desktop-rpm.repo").read_text().startswith("[untitled-desktop-rpm]")
