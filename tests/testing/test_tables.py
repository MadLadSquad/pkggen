"""The testing generators cover exactly the releases listed in distributions.yaml."""
import re

import pytest
import yaml

from tests.support import paths
from tests.support.helpers import load_generator

DISTRIBUTIONS = yaml.safe_load((paths.GENERATORS / "distributions" / "distributions.yaml").read_text())["distributions"]
TESTING = sorted(p.stem for p in (paths.GENERATORS / "testing").glob("*.py") if p.stem != "lib")
PACKAGING = sorted(p.stem for p in (paths.GENERATORS / "packaging").glob("*.py") if p.stem != "lib")
# registry/path:tag, always with the registry so podman never asks which one to use
IMAGE_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]+/[a-z0-9._/-]+:[A-Za-z0-9._-]+$")


def test_every_packaging_generator_has_a_testing_generator():
    assert TESTING == PACKAGING


@pytest.mark.parametrize("dist", [d for d in DISTRIBUTIONS if d["name"] in TESTING], ids=lambda d: d["name"])
def test_every_rp_name_has_a_target(dist):
    generator = load_generator("testing", dist["name"])
    assert sorted(generator.TARGETS) == sorted(dist["rp-names"])


def specs(dist):
    generator = load_generator("testing", dist)
    return generator.lib.table_specs(generator.TARGETS)


def test_target_names_are_unique_and_images_fully_qualified():
    names = []
    for dist in TESTING:
        for rp, spec in specs(dist):
            names.append(spec["name"])
            assert IMAGE_RE.match(spec["image"]), spec["image"]
            assert re.match(r"^[a-z0-9][a-z0-9.-]*$", spec["name"]), spec["name"]
    assert len(names) == len(set(names))


def test_pclinuxos_is_not_supported():
    assert "pclinuxos" not in (paths.GENERATORS / "distributions" / "distributions.yaml").read_text()


def test_target_names_match_their_rp_names():
    """A package's `distributions.<dist>.versions` can use either name: pkggen compares them normalised.

    An rp-name with several targets names them in distributions.yaml's `targets` instead.
    """
    import generate
    for dist in TESTING:
        for rp, value in load_generator("testing", dist).TARGETS.items():
            if isinstance(value, list):
                entry = next(d for d in DISTRIBUTIONS if d["name"] == dist)
                assert [spec["name"] for spec in value] == entry["targets"][rp], (dist, rp)
            else:
                assert generate.release_key(value["name"]) == generate.release_key(rp), (dist, rp, value["name"])


def test_only_rp_names_with_several_targets_are_in_targets():
    for dist in DISTRIBUTIONS:
        if dist["name"] not in TESTING:
            continue
        table = load_generator("testing", dist["name"]).TARGETS
        assert sorted(dist.get("targets") or {}) == sorted(rp for rp, value in table.items() if isinstance(value, list))


@pytest.mark.parametrize("dist", TESTING)
def test_every_testing_generator_describes_its_repository(dist):
    """`publish` tells deployment generators which files are indexes and metadata; pkggen checks it."""
    import testing  # src/pkggen/testing.py
    publish = load_generator("testing", dist).PUBLISH
    assert testing.publish_problem(publish) is None
    assert publish.get("indexes") or publish.get("metadata")


@pytest.mark.parametrize("dist, path, kind", [
    ("debian", "dists/trixie/InRelease", "metadata"), ("debian", "dists/trixie/main/binary-amd64/Packages", "indexes"),
    ("debian", "pool/main/f/foo/foo_1.0_amd64.deb", None),
    ("rpm-based", "repodata/repomd.xml.asc", "metadata"), ("rpm-based", "repodata/primary.xml.zst", "indexes"),
    ("rpm-based", "RPMS/x86_64/foo.rpm", None),
    ("arch", "x86_64/pkggen.db.tar.zst", "indexes"), ("arch", "x86_64/foo-1.0-1-x86_64.pkg.tar.zst", None),
    ("alpine", "x86_64/APKINDEX.tar.gz", "metadata"), ("alpine", "x86_64/foo-1.0-r0.apk", None),
    ("void", "binpkgs/x86_64-repodata", "metadata"), ("void", "binpkgs/foo-1.0_1.x86_64.xbps", None),
    ("gentoo", "binpkgs/Packages", "indexes"), ("gentoo", "binpkgs/app-misc/foo/foo-1.0-1.gpkg.tar", None),
])
def test_publish_patterns(dist, path, kind):
    publish = load_generator("testing", dist).PUBLISH
    kinds = [k for k in ("metadata", "indexes") if any(re.search(p, path) for p in publish.get(k) or ())]
    assert (kinds[0] if kinds else None) == kind


@pytest.mark.parametrize("publish, problem", [
    (None, None), ({}, None),
    ({"indexes": ["a"], "metadata": [], "content_types": {".x": "y"}}, None),
    ([], "isn't a mapping"), ({"other": []}, "unknown keys: other"),
    ({"indexes": "a"}, "'indexes' isn't a list"), ({"metadata": ["("]}, "invalid regular expression"),
    ({"content_types": {".x": 1}}, "'content_types' isn't a mapping"),
])
def test_publish_problems(publish, problem):
    import testing
    found = testing.publish_problem(publish)
    assert (found is None) if problem is None else (problem in found)


@pytest.mark.parametrize("dist, expected", [
    ("debian", {"DEB_BUILD_OPTIONS": "parallel=${PKGGEN_JOBS}"}),
    ("rpm-based", {"RPM_BUILD_NCPUS": "${PKGGEN_JOBS}"}),
    ("gentoo", {"MAKEOPTS": "-j${PKGGEN_JOBS}"}),
    ("arch", {}), ("alpine", {}), ("void", {}),   # makepkg reads MAKEFLAGS; abuild and xbps-src PKGGEN_JOBS
])
def test_build_tools_get_the_job_count(describe, dist, expected):
    """Each testing generator sets its own build tool's parallelism (pkggen fills in ${PKGGEN_JOBS}),
    and describes its repository for the deployment generators."""
    targets, _ = describe(dist)
    assert targets
    for target in targets:
        env = {k: v for k, v in target["env"].items() if "PKGGEN_JOBS" in v}
        assert env == expected
        assert target["publish"] == load_generator("testing", dist).PUBLISH
