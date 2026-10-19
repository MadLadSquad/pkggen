"""`pkggen test` with a real container engine. Run with `pytest --containers`.

It needs podman or docker and the network: it fetches the first example
package from GitHub (set GITHUB_TOKEN to avoid the rate limit), pulls the
images and builds the package with templates that install its README.
Environment variables:
  PKGGEN_TEST_CONTAINERS_TARGETS  the releases to test (default: one per family, `all`: every release)
  PKGGEN_TEST_CONTAINERS_ENGINE   podman or docker (default: whichever is installed, podman first)
  PKGGEN_TEST_CONTAINERS_LOGS     a folder to copy the logs to, e.g. to upload them from CI
"""
import os
import shutil

import pytest

from tests.support import paths
from tests.support.helpers import run_cli

pytestmark = pytest.mark.containers

PACKAGE = "untitled-cli-parser"
DEFAULT_TARGETS = "arch debian-13 fedora-44 gentoo"
DISTRIBUTIONS = ["arch", "debian", "gentoo", "rpm-based"]


def edit(path, old, new):
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


def one_file_templates(project):
    """The default templates, changed so the package installs only its README."""
    for dist in DISTRIBUTIONS:
        shutil.copytree(paths.TEMPLATES / dist, project / "packages" / dist / PACKAGE)
    root = project / "packages"
    edit(root / "arch" / PACKAGE / "PKGBUILD.tmpl", "\t# Install the package's files into \"$pkgdir\"\n\t:\n",
         '\tinstall -Dm644 "$srcdir/{{ source.directory }}/README.md" "$pkgdir/usr/share/doc/$pkgname/README.md"\n')
    rules = root / "debian" / PACKAGE / "debian" / "rules.tmpl"
    rules.write_text(rules.read_text() + "\noverride_dh_auto_configure override_dh_auto_build override_dh_auto_test:\n\n"
                     "override_dh_auto_install:\n"
                     "\tinstall -Dm644 README.md debian/{{ name }}/usr/share/doc/{{ name }}/README.md\n")
    spec = root / "rpm-based" / PACKAGE / "spec.tmpl"
    edit(spec, "Name:  ", "%global debug_package %{nil}\nName:  ")
    edit(spec, "# Install the package's files into %%{buildroot} here\n",
         "install -Dm644 README.md %{buildroot}%{_docdir}/%{name}/README.md\n")
    edit(spec, "%files\n", "%files\n%{_docdir}/%{name}/README.md\n")
    # The default ebuild already installs README.md with einstalldocs


@pytest.fixture
def github_secrets(secrets):
    token = os.getenv("GITHUB_TOKEN")
    if token:
        secrets({"github_key": token})


def test_build_and_install_in_containers(example_project, github_secrets):
    one_file_templates(example_project)
    for dist in DISTRIBUTIONS:
        assert run_cli("gen-repo", dist).code == 0

    targets = (os.getenv("PKGGEN_TEST_CONTAINERS_TARGETS") or DEFAULT_TARGETS).split()
    engine = os.getenv("PKGGEN_TEST_CONTAINERS_ENGINE")
    args = ["test", "-p", PACKAGE]
    if targets != ["all"]:
        args += ["-t", *targets]
    if engine:
        args += ["--engine", engine]
    out = example_project / "pkggen-build" / "test"
    try:
        result = run_cli(*args, timeout=3 * 60 * 60)
    finally:
        logs = os.getenv("PKGGEN_TEST_CONTAINERS_LOGS")
        if logs and out.is_dir():
            os.makedirs(logs, exist_ok=True)
            for log in out.glob("*.log"):
                shutil.copy(log, logs)
    assert result.code == 0, result.err
    assert "targets passed" in result.err_plain and "Done:" in result.err_plain
    if engine:
        assert f"with {engine}" in result.err_plain

    expected = {
        "arch": lambda d: list(d.glob(f"*/{PACKAGE}-*.pkg.tar.zst")) and list(d.glob("*/pkggen.db")),
        "debian": lambda d: list(d.glob(f"pool/main/u/{PACKAGE}/*.deb")) and (d / "dists").is_dir(),
        "fedora": lambda d: list(d.glob(f"RPMS/*/{PACKAGE}-*.rpm")) and (d / "RPMS" / "repodata").is_dir(),
        "opensuse": lambda d: list(d.glob(f"RPMS/*/{PACKAGE}-*.rpm")),
        "openmandriva": lambda d: list(d.glob(f"RPMS/*/{PACKAGE}-*.rpm")),
        "ubuntu": lambda d: list(d.glob(f"pool/main/u/{PACKAGE}/*.deb")),
        "gentoo": lambda d: list(d.glob(f"binpkgs/*/{PACKAGE}/*")) and (d / "profiles" / "repo_name").is_file(),
    }
    for target in (p for p in out.iterdir() if p.is_dir()):
        family = target.name.split("-")[0]
        assert expected[family](target), f"{target.name} has no built package: {sorted(target.rglob('*'))}"
        assert (out / f"{target.name}.log").is_file()
