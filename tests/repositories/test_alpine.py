import pytest

from tests.repositories.conftest import files


def test_alpine_skeleton(gen_repo):
    result = gen_repo("alpine", {"name": "untitled-desktop"})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore"]
    assert (gen_repo.out / ".gitignore").read_bytes() == b"src/\npkg/\n*.apk\n*.tar.*\n*.log\n"
    assert result.out == ""


def test_alpine_block_is_optional(gen_repo):
    result = gen_repo("alpine", None, config={})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore"]


@pytest.mark.parametrize("name", ["Upper", "with space", "-dash", 3])
def test_alpine_invalid_name(gen_repo, name):
    result = gen_repo("alpine", {"name": name})
    assert result.code == 1
    assert "'repositories.alpine.name' must be lower-case letters" in result.err
