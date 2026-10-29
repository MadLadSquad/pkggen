import pytest

from tests.support import paths
from tests.support.helpers import run_cli

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


def test_deploy_needs_a_deployment_block(example_project):
    (example_project / "pkggen.yaml").write_text("repositories:\n  arch: {}\n")
    result = run_cli("deploy")
    assert result.code == 1
    assert result.err_plain.strip() == "Error: Nothing to deploy: pkggen.yaml has no 'deployment' block"


def test_deploy_options():
    result = run_cli("deploy", "--help")
    assert result.code == 0
    for option in ("--packages", "--distributions", "--targets", "--skip-tests", "--dry-run"):
        assert option in result.out


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


@pytest.mark.parametrize("args", [["version"], ["-v"], ["--version"], ["--help"]])
def test_version_and_help_do_not_require_distributions_yaml(tmp_path, args):
    result = run_cli(*args, env={"PKGGEN_GENERATORS_PATH": tmp_path})
    assert result.code == 0, result.err
    if args != ["--help"]:
        assert result.out.strip() == f"pkggen version {VERSION}"


@pytest.mark.parametrize("args", [["arch"], ["--help"], ["-o", "out", "arch"], []])
def test_gen_repo_reports_missing_distributions_yaml_cleanly(tmp_path, args):
    result = run_cli("gen-repo", *args, env={"PKGGEN_GENERATORS_PATH": tmp_path})
    assert result.code == 1
    assert "Traceback" not in result.err
    assert "Error: Couldn't find " in result.err_plain

