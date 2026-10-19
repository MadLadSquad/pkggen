"""The whole pipeline on a copy of example/: gen-repo for every distribution, then generate.

GitHub is answered from canned data (tests/support/example.py), so this runs offline.
"""
import re

import pytest

from tests.conftest import SOURCE_DATE_EPOCH
from tests.support import paths
from tests.support.example import example_routes
from tests.support.golden import assert_golden, snapshot
from tests.support.helpers import run_cli

DISTRIBUTIONS = ["arch", "debian", "gentoo", "rpm-based"]
PACKAGES = ["untitled-cli-parser", "untitled-imgui-framework"]
DONE_LINE = re.compile(r"✓ \[(?P<dist>[\w-]+)\] (?P<name>[\w-]+) (?P<version>\S+) → (?P<output>\S+) "
                       r"\((?P<written>\d+) written, (?P<unchanged>\d+) unchanged\)")


@pytest.fixture
def project(example_project, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", SOURCE_DATE_EPOCH)
    return example_project


def gen_repos(dists=DISTRIBUTIONS):
    for dist in dists:
        result = run_cli("gen-repo", dist)
        assert result.code == 0, result.err


def done_lines(result):
    return [m.groupdict() for m in map(DONE_LINE.search, result.err_plain.splitlines()) if m]


def test_full_pipeline(request, project, http_subprocess):
    calls = http_subprocess(example_routes())
    gen_repos()

    result = run_cli("generate")
    assert result.code == 0, result.err
    err = result.err_plain
    assert "Fetching 2 packages, packaging for rpm-based, debian, arch, gentoo" in err
    assert "✓ fetched untitled-cli-parser 1.2.0 (1 artifact)" in err
    assert "✓ fetched untitled-imgui-framework 1.1.2 (1 artifact)" in err
    assert err.count("default templates were created") == 8
    assert err.count("has no 'category', so it went into 'ude-base'") == 2
    assert "Done: 2/2 packages fetched, 8/8 distribution packages generated (rpm-based, debian, arch, gentoo)." in err
    assert sorted((d["dist"], d["name"]) for d in done_lines(result)) == \
        sorted((dist, name) for dist in DISTRIBUTIONS for name in PACKAGES)

    # Everything gen-repo and generate wrote, byte for byte
    assert_golden(request, "example.snapshot", snapshot(project / "pkggen-build"))

    # Packages without templates got a copy of the defaults
    for dist in DISTRIBUTIONS:
        for name in PACKAGES:
            assert snapshot(project / "packages" / dist / name) == snapshot(paths.TEMPLATES / dist)

    # Releases, one download and the repository metadata per package. No token without secrets
    assert len(calls()) == 6
    assert not any("Authorization" in call["headers"] for call in calls())


def test_second_run_changes_nothing(project, http_subprocess):
    http_subprocess(example_routes())
    gen_repos()
    assert run_cli("generate").code == 0
    before = snapshot(project)

    result = run_cli("generate")
    assert result.code == 0, result.err
    lines = done_lines(result)
    assert len(lines) == 8
    assert all(line["written"] == "0" for line in lines)
    assert "default templates were created" not in result.err_plain
    assert snapshot(project) == before


def test_package_and_distribution_filters(project, http_subprocess):
    calls = http_subprocess(example_routes())
    gen_repos()
    result = run_cli("generate", "-p", "untitled-cli-parser", "-d", "gentoo", "arch")
    assert result.code == 0, result.err
    assert "Done: 1/1 packages fetched, 2/2 distribution packages generated (arch, gentoo)." in result.err_plain
    assert (project / "pkggen-build" / "gentoo" / "ude-base" / "untitled-cli-parser").is_dir()
    assert (project / "pkggen-build" / "arch" / "untitled-cli-parser").is_dir()
    assert not (project / "pkggen-build" / "debian" / "packages").exists() or \
        not any((project / "pkggen-build" / "debian" / "packages").glob("untitled-*"))
    assert sorted((project / "packages").iterdir()) == [project / "packages" / "arch", project / "packages" / "gentoo"]
    assert all("UntitledCLIParser" in call["url"] for call in calls())


def test_fetch_failure_does_not_stop_other_packages(project, http_subprocess):
    http_subprocess(example_routes(failing=["untitled-imgui-framework"]))
    gen_repos()
    result = run_cli("generate")
    assert result.code == 1
    err = result.err_plain
    assert "✗ couldn't fetch untitled-imgui-framework" in err
    assert "Finished with 1 problem: 1/2 packages fetched, 4/4 distribution packages generated" in err
    assert "✗ fetch untitled-imgui-framework: couldn't list releases of MadLadSquad/UntitledImGuiFramework: " \
           "HTTP 404" in err
    assert (project / "pkggen-build" / "arch" / "untitled-cli-parser" / "PKGBUILD").is_file()
    assert not (project / "pkggen-build" / "arch" / "untitled-imgui-framework").exists()


def test_generate_before_gen_repo(project, http_subprocess):
    http_subprocess(example_routes())
    result = run_cli("generate")
    assert result.code == 1
    err = result.err_plain
    assert "Warning: no distribution to package for, so packages are only fetched" in err
    for dist in DISTRIBUTIONS:
        assert f"Run `pkggen gen-repo {dist}` first" in err
    assert "2/2 packages fetched, 0/0 distribution packages generated" in err


def test_invalid_config_is_reported_up_front(project, http_subprocess):
    calls = http_subprocess(example_routes())
    text = (project / "pkggen.yaml").read_text()
    (project / "pkggen.yaml").write_text(text.replace('generator: "github"', 'generator: "github"\n  maintainer: "x"'))
    result = run_cli("generate")
    assert result.code == 1
    assert "'maintainer' is set on group 'pkggen', but it can only be set per package" in result.err_plain
    assert calls() == []


def test_example_folder_is_untouched():
    # Runs after the tests above: they only ever work on copies.
    # example/packages is empty, so it only exists in some checkouts
    assert not (paths.EXAMPLE / "pkggen-build").exists()
    packages = paths.EXAMPLE / "packages"
    assert not packages.exists() or not any(packages.iterdir())
