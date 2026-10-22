"""The whole pipeline on a copy of example/: gen-repo for every distribution, then generate.

GitHub (and B-Trust's download) is answered from canned data (tests/support/example.py),
so this runs offline.
"""
import re

import pytest

from tests.conftest import SOURCE_DATE_EPOCH
from tests.support import paths
from tests.support.example import EVERYWHERE, EXAMPLE_DEPENDENCIES, PACKAGES, example_routes, lock_btrustbiss
from tests.support.golden import assert_golden, snapshot
from tests.support.helpers import run_cli

DISTRIBUTIONS = ["arch", "debian", "gentoo", "rpm-based"]
GENTOO_ONLY = ["abcccid", "btrustbiss"]
JOBS = len(EVERYWHERE) * len(DISTRIBUTIONS) + len(GENTOO_ONLY)
DONE_LINE = re.compile(r"✓ \[(?P<dist>[\w-]+)\] (?P<name>[\w-]+) (?P<version>\S+) → (?P<output>\S+) "
                       r"\((?P<written>\d+) written, (?P<unchanged>\d+) unchanged\)")
# Taken when the tests are collected, before any of them runs. Runs in the real
# example/ (pkggen-build/) don't count
EXAMPLE_BEFORE = snapshot(paths.EXAMPLE, exclude=("pkggen-build",))


@pytest.fixture
def project(example_project, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", SOURCE_DATE_EPOCH)
    lock_btrustbiss(example_project)
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
    # The overlay's seed files: the eclass the applications inherit, and btrustbiss's license
    assert (project / "pkggen-build" / "gentoo" / "eclass" / "uimgui.eclass").read_bytes() == \
        (paths.EXAMPLE / "repository-files" / "gentoo" / "eclass" / "uimgui.eclass").read_bytes()
    assert (project / "pkggen-build" / "gentoo" / "licenses" / "B-Trust-EULA").is_file()

    result = run_cli("generate")
    assert result.code == 0, result.err
    err = result.err_plain
    assert "Fetching 12 packages, packaging for rpm-based, debian, arch, gentoo" in err
    for name, (_, _, tag) in PACKAGES.items():
        assert f"✓ fetched {name} {tag.lstrip('v')} (1 artifact)" in err
    assert "✓ fetched btrustbiss 3.46 (1 artifact)" in err
    # The example has templates for everything, and puts every package in a category
    assert "default templates were created" not in err
    assert "Warning" not in err
    assert f"Done: 12/12 packages fetched, {JOBS}/{JOBS} distribution packages generated " \
           "(rpm-based, debian, arch, gentoo)." in err
    assert sorted((d["dist"], d["name"]) for d in done_lines(result)) == sorted(
        [(dist, name) for dist in DISTRIBUTIONS for name in EVERYWHERE] + [("gentoo", name) for name in GENTOO_ONLY])

    # Everything gen-repo and generate wrote, byte for byte (without the HTTP cache)
    assert_golden(request, "example.snapshot", snapshot(project / "pkggen-build", exclude=("cache",)))
    # The templates are only read
    assert snapshot(project / "packages") == snapshot(paths.EXAMPLE / "packages")

    # One release list and one download per package, and nothing else: every
    # package sets its description, license and homepage. No token without secrets
    github = [call for call in calls() if "repology.org" not in call["url"]]
    assert len(github) == 2 * len(PACKAGES) + 1
    assert not any("Authorization" in call["headers"] for call in calls())
    # Every dependency is looked up once, in pkggen.yaml order, as pkggen
    repology = [call for call in calls() if "repology.org" in call["url"]]
    assert [call["url"].rsplit("/", 1)[1] for call in repology] == EXAMPLE_DEPENDENCIES
    assert all(call["headers"]["User-Agent"].startswith("pkggen/") for call in repology)
    assert "looked up 19 dependencies on repology" in err


def test_second_run_changes_nothing(project, http_subprocess):
    http_subprocess(example_routes())
    gen_repos()
    assert run_cli("generate").code == 0
    before = snapshot(project)

    result = run_cli("generate")
    assert result.code == 0, result.err
    lines = done_lines(result)
    assert len(lines) == JOBS
    assert all(line["written"] == "0" for line in lines)
    assert snapshot(project) == before


def test_package_and_distribution_filters(project, http_subprocess):
    calls = http_subprocess(example_routes())
    gen_repos()
    result = run_cli("generate", "-p", "untitled-cli-parser", "abcccid", "-d", "gentoo", "arch")
    assert result.code == 0, result.err
    # abcccid is only packaged for Gentoo
    assert "Done: 2/2 packages fetched, 3/3 distribution packages generated (arch, gentoo)." in result.err_plain
    assert (project / "pkggen-build" / "gentoo" / "ude-base" / "untitled-cli-parser").is_dir()
    assert (project / "pkggen-build" / "gentoo" / "app-crypt" / "abcccid").is_dir()
    assert (project / "pkggen-build" / "arch" / "untitled-cli-parser").is_dir()
    assert not (project / "pkggen-build" / "arch" / "abcccid").exists()
    assert not any((project / "pkggen-build" / "debian" / "packages").glob("untitled-*"))
    github = [call["url"] for call in calls() if "repology.org" not in call["url"]]
    assert all("UntitledCLIParser" in url or "abcccid" in url for url in github)
    # Only the selected packages' dependencies are looked up
    assert [call["url"].rsplit("/", 1)[1] for call in calls() if "repology.org" in call["url"]] == ["cmake", "pkgconf"]


def test_fetch_failure_does_not_stop_other_packages(project, http_subprocess):
    http_subprocess(example_routes(failing=["untitled-imgui-framework"]))
    gen_repos()
    result = run_cli("generate")
    assert result.code == 1
    err = result.err_plain
    assert "✗ couldn't fetch untitled-imgui-framework" in err
    jobs = JOBS - len(DISTRIBUTIONS)
    assert f"Finished with 1 problem: 11/12 packages fetched, {jobs}/{jobs} distribution packages generated" in err
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
    assert "12/12 packages fetched, 0/0 distribution packages generated" in err


def test_invalid_config_is_reported_up_front(project, http_subprocess):
    calls = http_subprocess(example_routes())
    text = (project / "pkggen.yaml").read_text()
    (project / "pkggen.yaml").write_text(text.replace('generator: "github"', 'generator: "github"\n  maintainer: "x"', 1))
    result = run_cli("generate")
    assert result.code == 1
    assert "'maintainer' is set on group 'ude-libraries', but it can only be set per package" in result.err_plain
    assert calls() == []


def test_example_folder_is_untouched():
    # Runs after the tests above: they only ever work on copies
    assert snapshot(paths.EXAMPLE, exclude=("pkggen-build",)) == EXAMPLE_BEFORE
