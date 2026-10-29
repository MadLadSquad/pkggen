import os
import re
import shutil

import pytest

from pkggen import gen_repo, utils
from tests.support import paths


def test_get_distributions_lists_dists_with_a_repository_generator():
    assert sorted(gen_repo.get_distributions()) == ["alpine", "arch", "debian", "gentoo", "rpm-based", "void"]


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


# --- seed-files ----------------------------------------------------------------------


def seed_project(example_project, value="seeds/arch"):
    (example_project / "pkggen.yaml").write_text(f"repositories:\n  arch:\n    seed-files: {value}\n")
    seeds = example_project / "seeds" / "arch"
    (seeds / "eclass").mkdir(parents=True)
    (seeds / "eclass" / "x.eclass").write_bytes(b"# eclass\r\n")
    (seeds / "README.md").write_text("seed readme\n")
    return example_project / "pkggen-build" / "arch"


def test_seed_files_are_copied(example_project, capfd):
    repo = seed_project(example_project)
    gen_repo.gen_repo("arch")
    assert (repo / "eclass" / "x.eclass").read_bytes() == b"# eclass\r\n"  # byte for byte
    assert (repo / "README.md").read_text() == "seed readme\n"
    assert (repo / ".gitignore").is_file()
    assert "Copied 2 seed files from " + os.path.join("seeds", "arch") in capfd.readouterr().err


def test_seed_files_keep_existing_files_unless_forced(example_project, capfd):
    repo = seed_project(example_project)
    gen_repo.gen_repo("arch")
    (repo / "README.md").write_text("mine\n")
    capfd.readouterr()

    gen_repo.gen_repo("arch")
    assert (repo / "README.md").read_text() == "mine\n"
    assert "Copied 0 seed files" in (err := capfd.readouterr().err) and "2 already there, kept" in err

    gen_repo.gen_repo("arch", force=True)
    assert (repo / "README.md").read_text() == "seed readme\n"


@pytest.mark.parametrize("value, message", [
    ("missing", "'repositories.arch.seed-files' is missing, but missing isn't a folder"),
    ("''", "'repositories.arch.seed-files' must be the path of a folder"),
])
def test_seed_files_errors(example_project, value, message):
    seed_project(example_project, value)
    with pytest.raises(utils.GenericError, match=re.escape(message)):
        gen_repo.gen_repo("arch")
    # Nothing is generated when the seed folder is wrong
    assert not (example_project / "pkggen-build" / "arch").exists()
