import pytest

from tests.support import paths
from tests.support.helpers import run, run_cli

VERSION = open(paths.SRC / "version.py").read().split('"')[1]


@pytest.mark.parametrize("args", [["version"], ["-v"], ["--version"]])
def test_version(args):
    result = run_cli(*args)
    assert result.code == 0
    assert result.out.strip() == f"pkggen version {VERSION}"


def test_help_lists_every_command():
    result = run_cli("--help")
    assert result.code == 0
    for command in ("generate", "gen-repo", "test", "deploy", "repology", "version"):
        assert command in result.out


def test_no_command_is_an_error():
    result = run_cli()
    assert result.code == 2
    assert "required" in result.err


def test_gen_repo_offers_every_repository_generator():
    result = run_cli("gen-repo", "--help")
    assert result.code == 0
    for dist in ("rpm-based", "debian", "arch", "gentoo"):
        assert dist in result.out


def test_gen_repo_rejects_unknown_distribution():
    result = run_cli("gen-repo", "slackware")
    assert result.code == 2
    assert "invalid choice" in result.err


@pytest.mark.parametrize("command", ["test", "deploy"])
def test_stub_commands(command):
    result = run_cli(command)
    assert result.code == 0
    assert result.out.strip() == "This command is not currently implemented."


def test_expected_errors_are_one_line_without_traceback(tmp_path):
    result = run_cli("generate", env={"PKGGEN_RUN_PATH": tmp_path})
    assert result.code == 1
    assert "Traceback" not in result.err
    lines = result.err_plain.strip().splitlines()
    assert lines == [f"Error: Couldn't find {tmp_path / 'pkggen.yaml'}! "
                     "Run pkggen from its directory or set PKGGEN_RUN_PATH."]
    assert result.err.startswith("\x1b[31m")


def test_gen_repo_end_to_end(example_project):
    result = run_cli("gen-repo", "gentoo")
    assert result.code == 0, result.err
    assert (example_project / "pkggen-build" / "gentoo" / "profiles" / "repo_name").is_file()


@pytest.mark.xfail(reason="setup.py's entry point is pkggen.pkggen:main, but src/pkggen has no __init__.py "
                          "and the CLI script has no .py suffix, so `pip install -e src` installs a "
                          "`pkggen` command that fails with ModuleNotFoundError")
def test_installed_entry_point_is_importable():
    # The console script that `pip install` generates runs exactly this import,
    # with src/ (the editable install's root) on sys.path
    result = run(["-c", "import sys; sys.path.insert(0, sys.argv[1]); from pkggen.pkggen import main",
                  paths.REPO / "src"])
    assert result.code == 0, result.err
