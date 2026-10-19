import shutil

import pytest

import gen_repo
import utils
from tests.support import paths


def test_get_distributions_lists_dists_with_a_repository_generator():
    assert sorted(gen_repo.get_distributions()) == ["arch", "debian", "gentoo", "rpm-based"]


def test_get_distributions_skips_dists_without_generator(monkeypatch, tmp_path):
    shutil.copytree(paths.GENERATORS / "distributions", tmp_path / "distributions")
    (tmp_path / "repositories").mkdir()
    (tmp_path / "repositories" / "arch.py").write_text("")
    (tmp_path / "repositories" / "not-listed.py").write_text("")
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    assert gen_repo.get_distributions() == ["arch"]


def test_unknown_distribution(example_project):
    with pytest.raises(utils.GenericError, match="Couldn't find repository generator for nope"):
        gen_repo.gen_repo("nope")


def test_empty_pkggen_yaml(monkeypatch, tmp_path):
    (tmp_path / "pkggen.yaml").write_text("")
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    with pytest.raises(utils.GenericError, match="Couldn't load pkggen.yaml"):
        gen_repo.gen_repo("arch")


def test_default_output_is_pkggen_build(example_project, capfd):
    gen_repo.gen_repo("arch")
    assert (example_project / "pkggen-build" / "arch" / ".gitignore").is_file()
    assert "Generating the arch repository in pkggen-build" in capfd.readouterr().err


def test_custom_repository_path(example_project, monkeypatch, tmp_path):
    (example_project / "pkggen.yaml").write_text("repositories:\n  arch:\n    path: ../my-aur\n")
    gen_repo.gen_repo("arch")
    assert (tmp_path / "my-aur" / ".gitignore").is_file()


def test_output_override(example_project, tmp_path):
    gen_repo.gen_repo("arch", output_dir=str(tmp_path / "out"))
    assert (tmp_path / "out" / ".gitignore").is_file()
    assert not (example_project / "pkggen-build").exists()


def test_force_overwrites(example_project, capfd):
    target = example_project / "pkggen-build" / "arch" / ".gitignore"
    gen_repo.gen_repo("arch")
    target.write_text("custom\n")

    gen_repo.gen_repo("arch")
    assert target.read_text() == "custom\n"
    assert "skipped" in capfd.readouterr().err

    gen_repo.gen_repo("arch", force=True)
    assert target.read_text() != "custom\n"
    assert "overwrote" in capfd.readouterr().err


def test_failing_generator(example_project, capfd):
    # debian needs 'origin'
    (example_project / "pkggen.yaml").write_text("repositories:\n  debian:\n    name: x\n")
    with pytest.raises(utils.GenericError, match="generator for debian"):
        gen_repo.gen_repo("debian")
    err = capfd.readouterr().err
    assert "'repositories.debian' is missing: origin" in err
    assert "Exit code: 1" in err
