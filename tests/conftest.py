import json
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

# The user's settings that isolated_env hides, for the tests that use a real container engine
REAL_ENV = {var: os.environ.get(var) for var in ("XDG_CONFIG_HOME", "APPDATA", "CONTAINER_HOST", "DOCKER_HOST")}


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true",
                     help="also run tests that talk to the real GitHub and repology APIs")
    parser.addoption("--containers", action="store_true",
                     help="also run tests that build packages in real containers (needs podman or docker and the network)")
    parser.addoption("--update-golden", action="store_true",
                     help="rewrite the snapshots in tests/golden instead of comparing against them")


def live_enabled(config):
    return config.getoption("--live") or os.getenv("PKGGEN_TEST_LIVE") == "1"


def containers_enabled(config):
    return config.getoption("--containers") or os.getenv("PKGGEN_TEST_CONTAINERS") == "1"


def pytest_collection_modifyitems(config, items):
    skip_live = pytest.mark.skip(reason="live test: run with --live or PKGGEN_TEST_LIVE=1")
    skip_containers = pytest.mark.skip(reason="container test: run with --containers or PKGGEN_TEST_CONTAINERS=1")
    for item in items:
        if "live" in item.keywords and not live_enabled(config):
            item.add_marker(skip_live)
        if "containers" in item.keywords and not containers_enabled(config):
            item.add_marker(skip_containers)


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
    # git (used by `pkggen deploy`) ignores the user's and the system's configuration
    git_config = config / "gitconfig"
    git_config.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("PKGGEN_RUN_PATH", "PKGGEN_FORCE", "PKGGEN_DEBUG", "PKGGEN_TEST_HTTP",
                "PKGGEN_TEST_HTTP_LOG", "SOURCE_DATE_EPOCH", "PKGGEN_TEST_ENGINE", "PKGGEN_FAKE_ENGINE",
                "CONTAINER_HOST", "DOCKER_HOST", "PKGGEN_TEST_NOW", "PKGGEN_TEST_AUR_URL",
                "GIT_SSH_COMMAND", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "PKGGEN_CACHE_DIR",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GNUPGHOME"):
        monkeypatch.delenv(var, raising=False)
    for var in [v for v in os.environ if v.startswith(("PKGGEN_PASSWORD_", "PKGGEN_SSH_KEY_", "PKGGEN_GPG_",
                                                         "PKGGEN_S3_"))]:
        monkeypatch.delenv(var)
    return config


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """In-process requests calls fail unless a test mocks them (or is a live test)."""
    if "live" in request.keywords or "containers" in request.keywords:
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
        # Also for subprocesses the code under test starts itself (not through tests.support.helpers)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(paths.HTTP_SHIM),
                                                                         os.environ.get("PYTHONPATH")])))

        def calls():
            import json
            result = []
            for entry in sorted(log_dir.iterdir()):
                result += json.loads(entry.read_text(encoding="utf-8"))
            return result
        return calls
    return use


class FakeEngine:
    """Controls tests/support/fake_engine.py, which stands in for podman/docker."""

    def __init__(self, state):
        self.state = state

    def configure(self, **values):
        path = self.state / "config.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.update(values)
        path.write_text(json.dumps(data), encoding="utf-8")

    def calls(self, command=None):
        """Every command line the fake got, without the connection option, optionally only one command's."""
        path = self.state / "calls.jsonl"
        result = []
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            args = json.loads(line)
            while args and args[0] in ("--connection", "--context"):
                args = args[2:]
            if command is None or args[:1] == [command]:
                result.append(args)
        return result

    def raw_calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def containers(self):
        directory = self.state / "containers"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def scripts(self):
        """The steps that were run: (container, script) pairs."""
        return [(args[-4], args[-1]) for args in self.calls("exec") if args[-3:-1] == ["sh", "-exc"]]


@pytest.fixture
def fake_engine(tmp_path_factory, monkeypatch):
    state = tmp_path_factory.mktemp("engine")
    monkeypatch.setenv("PKGGEN_TEST_ENGINE", json.dumps([sys.executable, str(paths.REPO / "tests" / "support" /
                                                                             "fake_engine.py")]))
    monkeypatch.setenv("PKGGEN_FAKE_ENGINE", str(state))
    return FakeEngine(state)


@pytest.fixture
def real_engine(isolated_env, monkeypatch):
    """Let a real podman/docker see the user's setup, while pkggen still gets the isolated config.

    Podman keeps its machines, connections and registries in <config dir>/containers. Without
    the user's, pkggen finds no machine on macOS/Windows, and `podman machine init` overwrites
    the disk image of the user's existing 'pkggen' machine, which lives outside that folder.
    """
    for var in ("CONTAINER_HOST", "DOCKER_HOST"):
        if REAL_ENV[var]:
            monkeypatch.setenv(var, REAL_ENV[var])
    if os.name == "nt":  # podman's config dir on Windows is %APPDATA%
        config_dir = REAL_ENV["APPDATA"] or os.path.expanduser(os.path.join("~", "AppData", "Roaming"))
    else:
        config_dir = REAL_ENV["XDG_CONFIG_HOME"] or os.path.expanduser(os.path.join("~", ".config"))
    real = os.path.join(config_dir, "containers")
    os.makedirs(real, exist_ok=True)  # podman would create it; it can't through a dangling link
    try:
        os.symlink(real, isolated_env / "containers", target_is_directory=True)
    except OSError as e:
        if os.name != "nt":
            pytest.skip(f"can't link the podman configuration into the test's config folder: {e}")
        # Symlinks need Developer Mode or admin rights on Windows, junctions don't
        import _winapi
        _winapi.CreateJunction(real, str(isolated_env / "containers"))


@pytest.fixture
def example_project(tmp_path, monkeypatch):
    """A copy of example/ to run pkggen in. The real example/ is never written to."""
    project = tmp_path / "example"
    # Without the output of runs in the real example/
    shutil.copytree(paths.EXAMPLE, project, ignore=shutil.ignore_patterns("pkggen-build"))
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(project))
    return project


@pytest.fixture
def example_config():
    with open(paths.EXAMPLE / "pkggen.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def routes():
    return Routes()
