"""`pip install` gives a working `pkggen` command that finds its own generators.

pkggen is installed offline (no index, no dependencies, no build isolation) into
a new venv, which sees the test environment's packages (Jinja2, requests,
setuptools, ...) through a .pth file.
"""
import os
import shutil
import subprocess
import sys
import sysconfig

import pytest

from tests.support import paths

DISTRIBUTIONS = ["arch", "debian", "gentoo", "rpm-based"]


def sh(*args, cwd=None, env=None):
    process = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=300)
    assert process.returncode == 0, f"{args} exited with {process.returncode}\n{process.stdout}\n{process.stderr}"
    return process.stdout


@pytest.fixture
def checkout(tmp_path):
    """A copy of src/ and generators/, so building doesn't leave build/ or *.egg-info in the repository."""
    root = tmp_path / "checkout"
    ignore = shutil.ignore_patterns("venv", "build", "*.egg-info", "__pycache__")
    shutil.copytree(paths.REPO / "src", root / "src", ignore=ignore)
    shutil.copytree(paths.GENERATORS, root / "generators", ignore=ignore)
    return root


@pytest.fixture
def install(tmp_path):
    def install_into_venv(*pip_args):
        venv = tmp_path / "venv"
        sh(sys.executable, "-m", "venv", venv)
        bin_dir, exe = (venv / "Scripts", ".exe") if os.name == "nt" else (venv / "bin", "")
        python = bin_dir / f"python{exe}"
        site = sh(python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])").strip()
        with open(os.path.join(site, "test-environment.pth"), "w", encoding="utf-8") as f:
            f.write(sysconfig.get_paths()["purelib"] + "\n")
        sh(python, "-m", "pip", "install", "-q", "--no-index", "--no-deps", "--no-build-isolation", *pip_args)
        return python, bin_dir / f"pkggen{exe}"
    return install_into_venv


def environment():
    """No PKGGEN_GENERATORS_PATH: the installed pkggen has to find its generators itself."""
    env = dict(os.environ)
    for key in ("PKGGEN_GENERATORS_PATH", "PKGGEN_RUN_PATH", "PYTHONPATH"):
        env.pop(key, None)
    return env


def generators_path(python, cwd):
    return sh(python, "-c", "import sys; sys.argv = ['pkggen']; import pkggen.cli, utils; "
                            "print(utils.get_generators_path())", cwd=cwd, env=environment()).strip()


def source_files(root):
    """Every file under root, without compiled files (running the generators makes them)."""
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                  for d, _, names in os.walk(root) if "__pycache__" not in d
                  for f in names if f != ".gitkeep")


def check_commands(pkggen, project):
    version = open(paths.SRC / "version.py", encoding="utf-8").read().split('"')[1]
    assert sh(pkggen, "version", cwd=project, env=environment()).strip() == f"pkggen version {version}"
    shutil.copy(paths.EXAMPLE / "pkggen.yaml", project / "pkggen.yaml")
    shutil.copytree(paths.EXAMPLE / "repository-files", project / "repository-files")
    for dist in DISTRIBUTIONS:
        sh(pkggen, "gen-repo", dist, cwd=project, env=environment())
    assert (project / "pkggen-build" / "gentoo" / "profiles" / "repo_name").is_file()
    assert (project / "pkggen-build" / "debian" / "conf" / "distributions").is_file()
    assert (project / "pkggen-build" / "gentoo" / "eclass" / "uimgui.eclass").is_file()


def test_install(checkout, install, tmp_path):
    python, pkggen = install(str(checkout / "src"))
    project = tmp_path / "project"
    project.mkdir()
    check_commands(pkggen, project)

    # The generators were bundled into the package
    bundled = generators_path(python, project)
    site = sh(python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])").strip()
    assert os.path.normcase(bundled) == os.path.normcase(os.path.join(site, "pkggen", "generators"))
    assert source_files(bundled) == source_files(paths.GENERATORS)


def test_editable_install(checkout, install, tmp_path):
    python, pkggen = install("-e", str(checkout / "src"))
    project = tmp_path / "project"
    project.mkdir()
    check_commands(pkggen, project)
    # Editable installs use the checkout's generators/, so edits to them apply right away
    assert os.path.normcase(generators_path(python, project)) == os.path.normcase(str(checkout / "generators"))
