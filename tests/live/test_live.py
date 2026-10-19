"""Tests against the real GitHub and repology APIs. Run with `pytest --live`.

They check structure, not exact content, since upstream releases change. Set
GITHUB_TOKEN to avoid GitHub's anonymous rate limit.
"""
import hashlib
import json
import os

import pytest
import requests

from tests.support.helpers import run_cli

pytestmark = pytest.mark.live

DISTRIBUTIONS = ["arch", "debian", "gentoo", "rpm-based"]
PACKAGES = ["untitled-cli-parser", "untitled-imgui-framework"]


@pytest.fixture
def github_secrets(secrets):
    token = os.getenv("GITHUB_TOKEN")
    if token:
        secrets({"github_key": token})


def test_example_against_github(example_project, github_secrets):
    for dist in DISTRIBUTIONS:
        assert run_cli("gen-repo", dist).code == 0
    result = run_cli("generate")
    assert result.code == 0, result.err
    assert "Done: 2/2 packages fetched, 8/8 distribution packages generated" in result.err_plain

    build = example_project / "pkggen-build"
    for name in PACKAGES:
        assert (build / "arch" / name / "PKGBUILD").is_file()
        assert (build / "debian" / "packages" / name / "debian" / "control").is_file()
        assert (build / "rpm-based" / "SPECS" / f"{name}.spec").is_file()
        ebuilds = list((build / "gentoo").glob(f"*/{name}/{name}-*.ebuild"))
        assert len(ebuilds) == 1

        # The Manifest must match what Portage will download
        manifest = (ebuilds[0].parent / "Manifest").read_text().split()
        alias, size, sha512 = manifest[1], int(manifest[2]), manifest[manifest.index("SHA512") + 1]
        url = next(line.split()[0] for line in ebuilds[0].read_text().splitlines() if line.endswith(f"-> {alias}"))
        data = requests.get(url, timeout=60).content
        assert len(data) == size
        assert hashlib.sha512(data).hexdigest() == sha512


def test_repology():
    result = run_cli("repology", "zlib", "--json")
    assert result.code == 0, result.err
    entries = json.loads(result.out)
    assert entries
    assert all(entry["distribution_type"] == "supported" for entry in entries)
    assert {"fedora_rawhide", "arch"} & {entry["distribution"] for entry in entries}
