import os
import shutil
import sys

import pytest
import requests
import responses
import yaml

from tests.support import paths
from tests.support.http_shim.pkggen_test_http import Routes

# The core modules import each other by bare name (`import utils`)
sys.path.insert(0, str(paths.SRC))

# A fixed date for reproducible output: 2026-10-19 12:00:00 UTC
SOURCE_DATE_EPOCH = "1792411200"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true",
                     help="also run tests that talk to the real GitHub and repology APIs")
    parser.addoption("--update-golden", action="store_true",
                     help="rewrite the snapshots in tests/golden instead of comparing against them")


def live_enabled(config):
    return config.getoption("--live") or os.getenv("PKGGEN_TEST_LIVE") == "1"


def pytest_collection_modifyitems(config, items):
    if live_enabled(config):
        return
    skip = pytest.mark.skip(reason="live test: run with --live or PKGGEN_TEST_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory, monkeypatch):
    """Keep tests away from the user's config, environment and pkggen.yaml.

    Returns the config directory that stands in for ~/.config (and %APPDATA%).
    """
    config = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("APPDATA", str(config))
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(paths.GENERATORS))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    for var in ("PKGGEN_RUN_PATH", "PKGGEN_FORCE", "PKGGEN_DEBUG", "PKGGEN_TEST_HTTP",
                "PKGGEN_TEST_HTTP_LOG", "SOURCE_DATE_EPOCH"):
        monkeypatch.delenv(var, raising=False)
    return config


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """In-process requests calls fail unless a test mocks them (or is a live test)."""
    if "live" in request.keywords:
        return

    def blocked(self, request, *args, **kwargs):
        raise requests.ConnectionError(f"tests must not use the network: {request.method} {request.url}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked)


@pytest.fixture
def secrets(isolated_env):
    """Write secrets.yaml: secrets({"github_key": "..."})."""
    def write(data):
        path = isolated_env / "pkggen" / "secrets.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data) if isinstance(data, dict) else data, encoding="utf-8")
        return path
    return write


@pytest.fixture
def http():
    """Mock requests in-process: http.add(...), or Routes(...).install(http)."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def http_subprocess(tmp_path_factory, monkeypatch):
    """Serve routes to every subprocess started afterwards.

    use(routes) sets it up and returns a function that lists the requests the
    subprocesses made (once they've exited).
    """
    directory = tmp_path_factory.mktemp("http")

    def use(routes):
        log_dir = directory / "log"
        log_dir.mkdir(exist_ok=True)
        monkeypatch.setenv("PKGGEN_TEST_HTTP", str(routes.write(directory / "routes.json")))
        monkeypatch.setenv("PKGGEN_TEST_HTTP_LOG", str(log_dir))

        def calls():
            import json
            result = []
            for entry in sorted(log_dir.iterdir()):
                result += json.loads(entry.read_text(encoding="utf-8"))
            return result
        return calls
    return use


@pytest.fixture
def example_project(tmp_path, monkeypatch):
    """A copy of example/ to run pkggen in. The real example/ is never written to."""
    project = tmp_path / "example"
    shutil.copytree(paths.EXAMPLE, project)
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(project))
    return project


@pytest.fixture
def example_config():
    with open(paths.EXAMPLE / "pkggen.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def routes():
    return Routes()
