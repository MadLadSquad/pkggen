import pytest

from tests.support import paths
from tests.support.example import archive_bytes, checksums
from tests.support.helpers import load_generator, run_generator


def artifact(alias, url=None, kind="asset", directory=None):
    data = archive_bytes(alias)
    return {"url": url or f"https://downloads.example.com/{alias}", "alias": alias, "type": kind,
            "directory": directory or alias.rsplit(".tar", 1)[0], "size": len(data), "checksums": checksums(data)}


def context(**extra):
    """A template context like the one `pkggen generate` builds for the first example package."""
    artifacts = extra.pop("artifacts", [artifact("untitled-cli-parser-1.2.0.tar.xz")])
    result = {
        "name": "untitled-cli-parser",
        "maintainer": "MadLadSquad <contact@madladsquad.com>",
        "author": "MadLadSquad <contact@madladsquad.com>",
        "architectures": ["amd64", "arm64"],
        "github": {"user": "MadLadSquad", "repo": "UntitledCLIParser", "query": "releases"},
        "version": "1.2.0",
        "date": "2026-09-01T10:00:00+00:00",
        "tag": "v1.2.0",
        "artifacts": artifacts,
        "source": artifacts[0] if artifacts else {},
        "description": "A command line argument parser",
        "license": "MIT",
        "homepage": "https://github.com/MadLadSquad/UntitledCLIParser",
    }
    result.update(extra)
    return result


@pytest.fixture
def make_job(tmp_path, example_config):
    """make_job(dist, **context overrides) -> the job `pkggen generate` would send."""
    def make(dist, repository=None, package=None, **overrides):
        package = package if package is not None else context(**overrides)
        return {
            "distribution": dist,
            "package": package,
            "repository": example_config["repositories"].get(dist) if repository is None else repository,
            "repository_path": str(tmp_path / "pkggen-build" / dist),
            "templates_path": str(tmp_path / "packages" / dist / package["name"]),
            "default_templates_path": str(paths.TEMPLATES / dist),
            "run_path": str(tmp_path),
        }
    return make


@pytest.fixture
def package_with(make_job):
    """package_with(dist, job=None, **overrides) runs packaging/<dist>.py and returns the Result."""
    def run(dist, job=None, **overrides):
        return run_generator("packaging", dist, job or make_job(dist, **overrides))
    return run


@pytest.fixture
def plib():
    return load_generator("packaging", "lib")
