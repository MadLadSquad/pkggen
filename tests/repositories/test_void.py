import pytest

from tests.repositories.conftest import files


def test_void_skeleton(gen_repo):
    result = gen_repo("void", {"name": "untitled-desktop", "url": "https://xbps.example.com/binpkgs",
                               "description": "Void packages"})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore", "srcpkgs/.gitkeep", "untitled-desktop.conf"]
    assert (gen_repo.out / "untitled-desktop.conf").read_bytes() == \
        b"# Void packages\nrepository=https://xbps.example.com/binpkgs\n"
    assert (gen_repo.out / ".gitignore").read_text() == "hostdir/\nmasterdir*/\n*.xbps\n*.tar.*\n"


def test_void_without_a_url(gen_repo):
    result = gen_repo("void", {})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore", "srcpkgs/.gitkeep"]
    assert "Warning" not in result.err


@pytest.mark.parametrize("block", [{"name": "r"}, {"url": "https://x/"}])
def test_void_half_configured_conf_file_warns(gen_repo, block):
    result = gen_repo("void", block)
    assert result.code == 0
    assert "needs both 'name' and 'url'" in result.err
    assert not list(gen_repo.out.glob("*.conf"))


def test_void_invalid_name(gen_repo):
    result = gen_repo("void", {"name": "a/b", "url": "https://x/"})
    assert result.code == 1 and "'repositories.void.name' must be letters" in result.err
