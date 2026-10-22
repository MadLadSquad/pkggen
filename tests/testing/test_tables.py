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


def test_target_names_are_unique_and_images_fully_qualified():
    names = []
    for dist in TESTING:
        for rp, spec in load_generator("testing", dist).TARGETS.items():
            names.append(spec["name"])
            assert IMAGE_RE.match(spec["image"]), spec["image"]
            assert re.match(r"^[a-z0-9][a-z0-9.-]*$", spec["name"]), spec["name"]
    assert len(names) == len(set(names))


def test_pclinuxos_is_not_supported():
    assert "pclinuxos" not in (paths.GENERATORS / "distributions" / "distributions.yaml").read_text()


def test_target_names_match_their_rp_names():
    """A package's `distributions.<dist>.versions` can use either name: pkggen compares them normalised."""
    import generate
    for dist in TESTING:
        for rp, spec in load_generator("testing", dist).TARGETS.items():
            assert generate.release_key(spec["name"]) == generate.release_key(rp), (dist, rp, spec["name"])
