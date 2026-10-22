"""`pkggen repology <project>`, which shows what the dependency generator finds."""
import json

import pytest

from tests.support.helpers import run_cli, strip_ansi
from tests.support.http_shim.pkggen_test_http import Routes
from utilities import repology

URL = "https://repology.org/api/v1/project/zlib"

ITEMS = [
    {"repo": "debian_13", "srcname": "zlib", "binnames": ["zlib1g", "zlib1g-dev"], "subrepo": "trixie/main",
     "version": "1.3.dfsg", "status": "outdated"},
    {"repo": "fedora_44", "srcname": "zlib-ng", "binnames": ["zlib-ng-compat", "zlib-ng-compat-devel"],
     "version": "2.2", "status": "newest", "summary": "Compression library"},
    # Not one of the supported releases, but matches a wildcard: only with --include-outdated
    {"repo": "fedora_30", "srcname": "zlib", "version": "1.2.11", "status": "legacy"},
    # A repository pkggen doesn't know about
    {"repo": "freebsd", "srcname": "zlib", "version": "1.3.1", "status": "newest"},
]


def query(http_subprocess, capsys, *args, items=ITEMS, status=200):
    http_subprocess(Routes().json(URL, items, status=status))
    code = repology.query_repology("zlib", *args)
    return code, capsys.readouterr().out


def test_json_output_supported_only(http_subprocess, capsys):
    code, out = query(http_subprocess, capsys, True, False)
    output = json.loads(out)
    assert code == 0
    # In distributions.yaml order: rpm-based comes before debian
    assert [(o["distribution"], o["distribution_type"], o["version"]) for o in output] == [
        ("fedora_44", "supported", "2.2"),
        ("debian_13", "supported", "1.3.dfsg"),
    ]
    assert output[1] == {"distribution": "debian_13", "distribution_type": "supported", "srcname": "zlib",
                         "binname": "", "binnames": ["zlib1g", "zlib1g-dev"], "repository": "trixie/main",
                         "version": "1.3.dfsg", "status": "outdated", "summary": ""}


def test_json_output_with_outdated(http_subprocess, capsys):
    _, out = query(http_subprocess, capsys, True, True)
    output = json.loads(out)
    assert [(o["distribution"], o["distribution_type"]) for o in output] == [
        ("fedora_44", "supported"), ("debian_13", "supported"), ("fedora_30", "discontinued")]


def test_table_output(http_subprocess, capsys):
    _, out = query(http_subprocess, capsys, False, False)
    lines = out.splitlines()
    width = len(lines[0])
    assert lines[0].startswith("┏") and lines[0].endswith("┓")
    assert lines[-1].startswith("┗") and lines[-1].endswith("┛")
    assert sum(line.startswith("┣") for line in lines) == 1
    assert all(len(line) == width for line in lines)
    assert "┃ Distribution: fedora_44" in lines[1]
    assert any("Binary package names: zlib1g, zlib1g-dev" in line for line in lines)


@pytest.mark.parametrize("status, items", [(404, {}), (200, [{"repo": "freebsd", "srcname": "zlib"}])])
def test_not_found(http_subprocess, capsys, status, items):
    code, out = query(http_subprocess, capsys, False, False, items=items, status=status)
    assert code == 1
    assert "repology has no package of 'zlib' in the releases pkggen supports" in out
    assert "https://repology.org/projects/?search=zlib" in out
    code, out = query(http_subprocess, capsys, True, False, items=items, status=status)
    assert json.loads(out) == {"error": "No such dependency", "project": "zlib"}


def test_errors_are_one_line(http_subprocess, capsys):
    http_subprocess(Routes().json(URL, {"error": "down"}, status=500))
    import utils
    with pytest.raises(utils.GenericError, match="look 'zlib' up on repology: HTTP 500"):
        repology.query_repology("zlib", False, False)


def test_answers_are_cached_in_a_pkggen_project(http_subprocess, tmp_path, monkeypatch):
    calls = http_subprocess(Routes().json(URL, ITEMS, headers={"Cache-Control": "max-age=3600"}))
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    (tmp_path / "pkggen.yaml").write_text("{}\n")
    assert repology.query_repology("zlib", True, False) == 0
    assert repology.query_repology("zlib", True, False) == 0
    assert len(calls()) == 1 and (tmp_path / "pkggen-build" / "cache" / "http").is_dir()


def test_nothing_is_cached_outside_a_project(http_subprocess, tmp_path, monkeypatch):
    calls = http_subprocess(Routes().json(URL, ITEMS, headers={"Cache-Control": "max-age=3600"}))
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    repology.query_repology("zlib", True, False)
    repology.query_repology("zlib", True, False)
    assert len(calls()) == 2 and not (tmp_path / "pkggen-build").exists()


def test_cli(http_subprocess, tmp_path):
    calls = http_subprocess(Routes().json(URL, ITEMS))
    result = run_cli("repology", "zlib", "--json", cwd=tmp_path)
    assert result.code == 0, result.err
    assert len(json.loads(result.out)) == 2
    assert calls()[0]["headers"]["User-Agent"].startswith("pkggen/")
    missing = run_cli("repology", "nothing", cwd=tmp_path)
    assert missing.code == 1 and "Error" in strip_ansi(missing.err)  # no route: a network error
