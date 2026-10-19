from tests.repositories.conftest import files


def test_arch_skeleton(gen_repo, example_config):
    result = gen_repo("arch", example_config["repositories"]["arch"])
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore"]
    assert (gen_repo.out / ".gitignore").read_bytes() == b"src/\npkg/\n*.pkg.tar.*\n*.tar.*\n*.log\n"
    assert result.out == ""


def test_arch_block_is_optional(gen_repo):
    result = gen_repo("arch", None, config={})
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore"]


def test_arch_rerun_skips_and_force_overwrites(gen_repo):
    gen_repo("arch", {})
    (gen_repo.out / ".gitignore").write_text("mine\n")
    assert "skipped .gitignore" in gen_repo("arch", {}).err
    assert (gen_repo.out / ".gitignore").read_text() == "mine\n"
    assert "overwrote .gitignore" in gen_repo("arch", {}, force=True).err
    assert (gen_repo.out / ".gitignore").read_text() != "mine\n"
