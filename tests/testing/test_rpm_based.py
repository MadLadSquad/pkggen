import os
import shutil
import subprocess

import pytest

from tests.support.helpers import load_generator
from tests.testing.conftest import SHA256, artifact, entry, scripts


def by_name(targets):
    return {t["name"]: t for t in targets}


def test_every_release_by_default(describe):
    targets, warnings = describe("rpm-based")
    assert [t["name"] for t in targets] == [
        "fedora-rawhide", "fedora-44", "fedora-43", "opensuse-tumbleweed", "opensuse-leap-15.6",
        "openmandriva-rolling", "openmandriva-6.0"]
    assert by_name(targets)["opensuse-leap-15.6"]["image"] == "registry.opensuse.org/opensuse/leap:15.6"
    assert warnings == []


def test_rolling_releases_upgrade_first(describe):
    targets = by_name(describe("rpm-based")[0])
    assert [s["name"] for s in targets["opensuse-tumbleweed"]["setup"]] == ["upgrade the system", "install the build tools"]
    assert targets["opensuse-tumbleweed"]["setup"][0]["run"] == "zypper -n dup || [ $? -eq 106 ]\n"
    assert targets["openmandriva-rolling"]["setup"][0]["run"] == "dnf -y distro-sync\n"
    assert [s["name"] for s in targets["fedora-44"]["setup"]] == ["install the build tools"]
    assert [s["name"] for s in targets["opensuse-leap-15.6"]["setup"]] == ["install the build tools"]


def test_fedora_build_and_install(describe):
    package = entry(name="foo", output="", artifacts=[artifact("foo-1.2.0.tar.xz"), artifact("extra.tar.gz")])
    targets, _ = describe("rpm-based", versions=["fedora_44"], packages=[package])
    steps = targets[0]["packages"][0]
    build = scripts(steps["build"])
    for fragment in [
        "curl -fsSL --retry 3 -o '/pkggen/repo/SOURCES/foo-1.2.0.tar.xz'",
        f"echo '{SHA256}  /pkggen/repo/SOURCES/extra.tar.gz' | sha256sum -c -",
        "dnf -y builddep '/pkggen/repo/SPECS/foo.spec'",
        "rpmbuild --define \"_topdir /pkggen/repo\" -ba '/pkggen/repo/SPECS/foo.spec' > /pkggen/state/foo.rpmbuild 2>&1 "
        "|| { cat /pkggen/state/foo.rpmbuild; exit 1; }",
        "sed -n 's|^Wrote: \\(.*/RPMS/.*\\.rpm\\)$|\\1|p' /pkggen/state/foo.rpmbuild | grep -v -e '-debuginfo-' "
        "-e '-debugsource-' > /pkggen/state/foo.rpms",
    ]:
        assert fragment in build
    assert scripts(steps["install"]) == "dnf -y install $(cat /pkggen/state/foo.rpms)\n"
    assert "createrepo_c /pkggen/repo/RPMS" in scripts(targets[0]["finalize"])


def test_opensuse_uses_zypper(describe):
    targets, _ = describe("rpm-based", versions=["opensuse-leap-15.6"], packages=[entry(name="foo")])
    steps = targets[0]["packages"][0]
    assert ("rpmspec --define \"_topdir /pkggen/repo\" -q --buildrequires '/pkggen/repo/SPECS/foo.spec' "
            "> /pkggen/state/buildrequires\n") in scripts(steps["build"])
    assert '[ $# -eq 0 ] || zypper -n in "$@" || [ $? -eq 106 ]\n' in scripts(steps["build"])
    assert scripts(steps["install"]) == ("zypper -n --no-gpg-checks in --allow-unsigned-rpm "
                                         "$(cat /pkggen/state/foo.rpms) || [ $? -eq 106 ]\n")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
@pytest.mark.parametrize("code, ok", [(0, True), (106, True), (104, False), (1, False)])
def test_zypper_builddep_passes_each_requirement_and_tolerates_skipped_repositories(tmp_path, code, ok):
    """Each build requirement is one argument (they can contain spaces), and exit code 106
    (some repositories couldn't be refreshed) doesn't fail the step."""
    gen = load_generator("testing", "rpm-based")
    script = gen.FAMILIES["opensuse"]["builddep"].format(spec="foo.spec").replace(
        "/pkggen/state", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rpmspec").write_text("#!/bin/sh\nprintf '%s\\n' cmake 'go >= 1.25'\n")
    (bin_dir / "zypper").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/args\nexit {code}\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    result = subprocess.run(["sh", "-ec", script], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert (result.returncode == 0) == ok
    assert (tmp_path / "args").read_text().splitlines() == ["-n", "in", "cmake", "go >= 1.25"]
