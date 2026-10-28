import shutil
import subprocess

import pytest

from tests.support.helpers import load_generator

needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen (OpenSSH)")
IDENTITY = ["-c", "user.name=Upstream", "-c", "user.email=up@example.org", "-c", "commit.gpgsign=false"]


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write_tree(root, files):
    """Write {relative path: content} below root and return root."""
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


class Upstream:
    """A bare repository standing in for the remote."""

    def __init__(self, path, branch):
        self.path = path
        self.url = str(path)
        self.branch = branch

    def files(self, branch=None):
        """{path: content} of the branch, or None when it doesn't exist."""
        branch = branch or self.branch
        refs = git("--git-dir", self.url, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        if branch not in refs.split():
            return None
        names = git("--git-dir", self.url, "ls-tree", "-r", "--name-only", branch).split("\n")
        return {n: git("--git-dir", self.url, "show", f"{branch}:{n}") for n in names if n}

    def log(self, branch=None):
        return git("--git-dir", self.url, "log", "--format=%s|%an|%ae", branch or self.branch).splitlines()

    def commit(self, tmp_path, files, message="upstream change", remove=()):
        """Commit files to the branch from a separate clone, like someone else would."""
        work = tmp_path / f"other-{len(list(tmp_path.glob('other-*')))}"
        git("clone", "-q", self.url, str(work))
        git("checkout", "-q", "-B", self.branch, cwd=work)
        write_tree(work, files)
        for path in remove:
            (work / path).unlink()
        git("add", "-A", cwd=work)
        git(*IDENTITY, "commit", "-q", "-m", message, cwd=work)
        git("push", "-q", "origin", f"HEAD:{self.branch}", cwd=work)


@pytest.fixture
def upstream(tmp_path):
    def make(name="upstream.git", branch="main"):
        path = tmp_path / "remotes" / name
        path.parent.mkdir(exist_ok=True)
        git("init", "-q", "--bare", f"--initial-branch={branch}", str(path))
        return Upstream(path, branch)
    return make


@pytest.fixture
def deploy_lib():
    return load_generator("deployment", "lib")


@pytest.fixture
def git_generator():
    return load_generator("deployment", "git")


@pytest.fixture
def aur_generator():
    return load_generator("deployment", "aur")


@pytest.fixture
def make_job(tmp_path):
    """make_job(generator, deployment, targets=[...], **fields) -> a Job of that generator's lib."""
    def make(generator, deployment, targets=(), **fields):
        data = {
            "id": "dist-gen", "where": "deployment.dist", "distribution": "debian", "deployment": deployment,
            "repository": {}, "run_path": str(tmp_path), "work_path": str(tmp_path / "work"),
            "targets": list(targets), "skipped_targets": [], "packages": None, "skip_tests": False,
            "interactive": False, "dry_run": False,
        }
        data.update(fields)
        return generator.lib.Job(data)
    return make


# Target name prefix -> the testing generator whose `publish` its test results record
TESTING_GENERATORS = {"debian-": "debian", "ubuntu-": "debian", "fedora-": "rpm-based", "opensuse-": "rpm-based",
                      "openmandriva-": "rpm-based", "alpine-": "alpine", "void-": "void", "arch": "arch",
                      "gentoo": "gentoo"}


def publish_of(name):
    """The `publish` pkggen test records for a target of this name ({} for other names)."""
    for prefix, generator in TESTING_GENERATORS.items():
        if name.startswith(prefix):
            return load_generator("testing", generator).PUBLISH
    return {}


def target(path, name="t1", ok=True, packages=None, reason=None, publish=None, executables=None):
    return {"name": name, "path": str(path), "ok": ok, "reason": reason, "packages": packages or {},
            "publish": publish_of(name) if publish is None else publish, "executables": executables}
