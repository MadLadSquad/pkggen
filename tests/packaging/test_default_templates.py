"""Every default template must render for any package pkggen can produce.

Templates use StrictUndefined, so an unguarded optional value makes rendering
fail. Each distribution is rendered with the smallest context a fetch generator
can produce and with every optional key set.
"""
import os

import pytest

from tests.packaging.conftest import artifact, context
from tests.support import paths

DISTRIBUTIONS = ["alpine", "arch", "debian", "gentoo", "rpm-based", "void"]

FULL_DIST_BLOCKS = {
    "alpine": {"depends": ["a"], "makedepends": ["b"], "pkgrel": 2, "arch": "noarch", "options": "!check !strip",
               "pkgver": "1.2.0_p1"},
    "arch": {"depends": ["a"], "makedepends": ["b"], "pkgrel": 2, "arch": ["x86_64"]},
    "debian": {"section": "libs", "build-depends": ["b"], "depends": ["a"], "revision": 2, "architecture": "all",
               "long-description": "Long", "copyright": "2020 Me"},
    "gentoo": {"category": "ude-apps", "depend": ["a/b"], "rdepend": ["c/d"], "bdepend": ["e/f"], "keywords": "amd64"},
    "rpm-based": {"build-requires": ["b"], "requires": ["a"], "release": 2, "long-description": "Long"},
    "void": {"depends": ["a"], "makedepends": ["b-devel"], "hostmakedepends": ["c"], "revision": 2,
             "archs": "noarch", "version": "1.2.0.1"},
}


def minimal_package():
    a = artifact("pkg-1.0.tar.gz", kind="source")
    # What url.py returns for a package with nothing but a name and a URL
    return {"name": "pkg", "url-generator": {"url": a["url"], "version": "1.0"}, "version": "1.0",
            "artifacts": [a], "source": a}


def full_package(dist):
    return context(**{dist: FULL_DIST_BLOCKS[dist], "source_dir": "custom-dir", "category": "ude-apps",
                      "artifacts": [artifact("one-1.2.0.tar.gz"), artifact("two.zip")]})


def rendered_files(tmp_path):
    root = tmp_path / "pkggen-build"
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


def test_every_distribution_is_covered():
    assert sorted(os.listdir(paths.TEMPLATES)) == DISTRIBUTIONS


@pytest.mark.parametrize("dist", DISTRIBUTIONS)
@pytest.mark.parametrize("kind", ["minimal", "full"])
def test_default_templates_render(dist, kind, package_with, make_job, tmp_path):
    package = minimal_package() if kind == "minimal" else full_package(dist)
    repository = {"categories": ["ude-base", "ude-apps"]} if dist == "gentoo" and kind == "minimal" else None
    result = package_with(dist, make_job(dist, package=package, repository=repository))
    assert result.code == 0, result.err
    files = rendered_files(tmp_path)
    assert files
    for name, text in files.items():
        assert "None" not in text, f"{name} renders a missing value as None"
        assert "{{" not in text and "{%" not in text, f"{name} has unrendered Jinja"


# The debian templates don't reference the source folder
@pytest.mark.parametrize("dist", ["alpine", "arch", "gentoo", "rpm-based", "void"])
def test_source_dir_overrides_the_guessed_folder(dist, package_with, make_job, tmp_path):
    assert package_with(dist, make_job(dist, package=full_package(dist))).code == 0
    assert "custom-dir" in "\n".join(rendered_files(tmp_path).values())


# --- metadata.xml branches -------------------------------------------------------------

def metadata(package_with, make_job, tmp_path, repository=None, **overrides):
    package = context(category="ude-base", **overrides)
    for key in [k for k, v in overrides.items() if v is None]:
        del package[key]
    result = package_with("gentoo", make_job("gentoo", package=package, repository=repository))
    assert result.code == 0, result.err
    return (tmp_path / "pkggen-build" / "gentoo" / "ude-base" / package["name"] / "metadata.xml").read_text()


def test_metadata_uses_overlay_owners_without_maintainer_email(package_with, make_job, tmp_path):
    xml = metadata(package_with, make_job, tmp_path, maintainer="Just A Name")
    assert '<maintainer type="project">' in xml
    assert "<email>contact@madladsquad.com</email>" in xml


def test_metadata_without_maintainer_or_owners(package_with, make_job, tmp_path):
    xml = metadata(package_with, make_job, tmp_path, repository={"categories": ["ude-base"]}, maintainer=None)
    assert "<maintainer type" not in xml


def test_metadata_author_without_email(package_with, make_job, tmp_path):
    xml = metadata(package_with, make_job, tmp_path, author="Upstream & Co")
    assert "<name>Upstream &amp; Co</name>" in xml
    assert xml.count("<email>") == 1  # only the package maintainer's


def test_metadata_without_upstream(package_with, make_job, tmp_path):
    xml = metadata(package_with, make_job, tmp_path, author=None, github=None)
    assert "<upstream>" not in xml


@pytest.mark.parametrize("gitlab, remote", [
    ({"user": "grp/sub", "repo": "r"}, '<remote-id type="gitlab">grp/sub/r</remote-id>'),
    ({"user": "u", "repo": "r", "domain": "gitlab.com"}, '<remote-id type="gitlab">u/r</remote-id>'),
    ({"user": "u", "repo": "r", "domain": "git.example.org"}, None),
])
def test_metadata_gitlab_remote_id(package_with, make_job, tmp_path, gitlab, remote):
    xml = metadata(package_with, make_job, tmp_path, author=None, github=None, gitlab=gitlab)
    if remote:
        assert remote in xml
    else:
        assert "remote-id" not in xml and "<upstream>" not in xml


def test_debian_copyright_without_author(package_with, make_job, tmp_path):
    package = context(author=None)
    del package["author"]
    assert package_with("debian", make_job("debian", package=package)).code == 0
    copyright_file = tmp_path / "pkggen-build" / "debian" / "packages" / "untitled-cli-parser" / "debian" / "copyright"
    assert "Copyright: 2026 the upstream authors" in copyright_file.read_text()
