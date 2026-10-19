import json

import pytest

from tests.support.helpers import run_cli
from tests.support.http_shim.pkggen_test_http import Routes
from utilities import repology

URL = "https://repology.org/api/v1/project/zlib"

ITEMS = [
    {"repo": "fedora_44", "srcname": "zlib", "binname": "zlib", "version": "1.3.1", "status": "newest",
     "summary": "Compression library"},
    {"repo": "debian_13", "srcname": "zlib", "binname": "zlib", "subrepo": "main", "version": "1.3.dfsg",
     "status": "outdated"},
    # Not one of our rp-names, but matches a wildcard: only with --include-outdated
    {"repo": "fedora_30", "srcname": "zlib", "version": "1.2.11", "status": "legacy"},
    # Different package name
    {"repo": "fedora_44", "srcname": "zlib-ng", "binname": "zlib-ng", "version": "2.2", "status": "newest"},
    # A repository pkggen doesn't know about
    {"repo": "freebsd", "srcname": "zlib", "version": "1.3.1", "status": "newest"},
]


@pytest.mark.parametrize("version, expected", [
    ("1.2.13.dfsg", "1.2.13"), ("1.3_p1", "1.3"), ("abc", "0"), ("2.0", "2.0"),
])
def test_version_sort_key(version, expected):
    from packaging.version import parse
    assert repology.version_sort_key(version) == parse(expected)


def query(http, capsys, *args, items=ITEMS, status=200):
    http.add("GET", URL, json=items, status=status)
    repology.query_repology("zlib", *args)
    return capsys.readouterr().out


def test_json_output_supported_only(http, capsys):
    output = json.loads(query(http, capsys, True, False))
    assert [(o["distribution"], o["distribution_type"], o["version"]) for o in output] == [
        ("fedora_44", "supported", "1.3.1"),
        ("debian_13", "supported", "1.3.dfsg"),
    ]
    assert output[0] == {"distribution": "fedora_44", "distribution_type": "supported", "srcname": "zlib",
                         "binname": "zlib", "repository": "", "version": "1.3.1", "status": "newest",
                         "summary": "Compression library"}
    assert output[1]["repository"] == "main"


def test_json_output_with_outdated(http, capsys):
    output = json.loads(query(http, capsys, True, True))
    discontinued = [o for o in output if o["distribution_type"] == "discontinued"]
    assert [(o["distribution"], o["binname"], o["repository"]) for o in discontinued] == [
        ("fedora_30", "None", "Default")]
    # Supported entries always come first
    assert output[-1]["distribution_type"] == "discontinued"


def test_table_output(http, capsys):
    lines = query(http, capsys, False, False).splitlines()
    width = len(lines[0])
    assert lines[0].startswith("┏") and lines[0].endswith("┓")
    assert lines[-1].startswith("┗") and lines[-1].endswith("┛")
    assert sum(line.startswith("┣") for line in lines) == 1
    assert all(len(line) == width for line in lines)
    assert "┃ Distribution: fedora_44" in lines[1]


def test_not_found(http, capsys):
    assert query(http, capsys, False, False, items={}, status=404).strip() == \
        "No such dependency! Response code: 404"
    http.add("GET", URL, json={}, status=404)
    repology.query_repology("zlib", True, False)
    assert json.loads(capsys.readouterr().out) == {"error": "No such dependency", "response_code": 404}


def test_cli(http_subprocess):
    http_subprocess(Routes().json(URL, ITEMS))
    result = run_cli("repology", "zlib", "--json")
    assert result.code == 0, result.err
    assert len(json.loads(result.out)) == 2
