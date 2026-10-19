import pytest

from tests.deployment.conftest import needs_ssh_keygen, target, write_tree
from tests.support.helpers import run_generator

IDENTITY = {"generator": "aur", "username": "Packager", "email": "packager@example.org"}


def srcinfo(name, version="1.2", rel="1", epoch=None):
    lines = [f"pkgbase = {name}", f"\tpkgver = {version}", f"\tpkgrel = {rel}"]
    if epoch:
        lines.append(f"\tepoch = {epoch}")
    return "\n".join(lines + ["", f"pkgname = {name}", ""])


def package(root, name, **version):
    return write_tree(root, {f"{name}/PKGBUILD": f"pkgname={name}\n", f"{name}/.SRCINFO": srcinfo(name, **version)})


@pytest.fixture
def arch_output(tmp_path):
    """pkggen-build/test/arch/ with two packages and the pacman repository."""
    root = tmp_path / "test" / "arch"
    package(root, "foo")
    package(root, "bar", version="2.0", rel="3", epoch="1")
    write_tree(root, {"x86_64/foo-1.2-1-x86_64.pkg.tar.zst": "binary", "x86_64/pkggen.db.tar.zst": "db"})
    return root


@pytest.fixture
def aur(upstream, monkeypatch):
    """Local bare repositories stand in for the AUR: aur(name) is the one of a package."""
    remotes = {}

    def get(name):
        if name not in remotes:
            remotes[name] = upstream(f"{name}.git", branch="master")
        return remotes[name]
    monkeypatch.setenv("PKGGEN_TEST_AUR_URL", str(get("foo").path.parent / "{pkgbase}.git"))
    return get


def statuses(*names, ok=True):
    return {n: {"version": "1", "ok": ok} for n in names}


def run(aur_generator, make_job, arch_output, tested=None, deployment=None, **fields):
    """Deploy arch_output; tested are its packages' test results, fields the job's (packages = -p)."""
    job = make_job(aur_generator, {**IDENTITY, **(deployment or {})},
                   targets=[target(arch_output, "arch", packages=tested or statuses("foo", "bar"))], **fields)
    assert aur_generator.check(job) == []
    aur_generator.main(job)
    return job


def test_every_package_goes_to_its_own_repository(aur_generator, make_job, arch_output, aur):
    aur("bar")
    job = run(aur_generator, make_job, arch_output)
    assert job.pushed == ["bar", "foo"] and job.failed == [] and job.skipped == []
    assert aur("foo").files() == {"PKGBUILD": "pkgname=foo\n", ".SRCINFO": srcinfo("foo")}
    assert aur("foo").log() == ["Update to 1.2-1|Packager|packager@example.org"]
    assert aur("bar").log() == ["Update to 1:2.0-3|Packager|packager@example.org"]

    # Nothing changed: nothing is pushed
    job = run(aur_generator, make_job, arch_output)
    assert job.unchanged == ["bar", "foo"] and job.pushed == []


def test_custom_message(aur_generator, make_job, arch_output, aur):
    run(aur_generator, make_job, arch_output, tested=statuses("foo"), deployment={"message": "{name}: {version}"},
        packages=["foo"])
    assert aur("foo").log()[0].startswith("foo: 1.2-1|")


def test_packages_that_failed_their_tests_are_skipped(aur_generator, make_job, arch_output, aur):
    aur("bar")
    job = run(aur_generator, make_job, arch_output, tested={**statuses("foo"), **statuses("bar", ok=False)})
    assert job.pushed == ["foo"]
    assert job.skipped == [{"name": "bar", "reason": "it failed its tests on arch (--skip-tests deploys it anyway)"}]
    assert aur("bar").files() is None

    job = run(aur_generator, make_job, arch_output, tested=statuses("foo"), skip_tests=True)
    assert job.pushed == ["bar"] and job.unchanged == ["foo"]


def test_untested_packages_are_skipped(aur_generator, make_job, arch_output, aur):
    job = run(aur_generator, make_job, arch_output, tested=statuses("foo"))
    assert job.skipped == [{"name": "bar", "reason": "it wasn't tested on arch (--skip-tests deploys it anyway)"}]


def test_package_filter(aur_generator, make_job, arch_output, aur):
    job = run(aur_generator, make_job, arch_output, packages=["foo", "ghost"])
    assert job.pushed == ["foo"]
    assert job.skipped == [{"name": "ghost", "reason": "arch has no PKGBUILD folder for it"}]


def test_folders_the_aur_rejects_only_fail_themselves(aur_generator, make_job, arch_output, aur):
    (arch_output / "bar" / ".SRCINFO").unlink()
    write_tree(arch_output, {"baz/PKGBUILD": "x", "baz/.SRCINFO": srcinfo("baz"), "baz/patches/fix.patch": "p"})
    job = run(aur_generator, make_job, arch_output, tested=statuses("foo", "bar", "baz"))
    assert job.pushed == ["foo"]
    assert [f["name"] for f in job.failed] == ["bar", "baz"]
    assert job.failed[0]["message"].startswith(".SRCINFO is missing, and the AUR rejects pushes without it")
    assert job.failed[1]["message"] == "the AUR doesn't accept folders, but it has patches"


def test_a_push_failure_only_fails_its_package(aur_generator, make_job, arch_output, aur, monkeypatch):
    aur("bar")
    monkeypatch.setenv("PKGGEN_TEST_AUR_URL", str(aur("foo").path.parent / "{pkgbase}-missing.git"))
    job = run(aur_generator, make_job, arch_output)
    assert job.pushed == [] and [f["name"] for f in job.failed] == ["bar", "foo"]
    assert job.failed[0]["message"].startswith("git fetch failed: ")


def test_urls(aur_generator, monkeypatch):
    monkeypatch.delenv("PKGGEN_TEST_AUR_URL", raising=False)
    assert aur_generator.urls("foo") == ("https://aur.archlinux.org/foo.git", "ssh://aur@aur.archlinux.org/foo.git")


@needs_ssh_keygen
def test_first_use_makes_an_aur_key(aur_generator, make_job, arch_output, isolated_env, monkeypatch, capsys):
    monkeypatch.delenv("PKGGEN_TEST_AUR_URL", raising=False)
    job = make_job(aur_generator, IDENTITY, targets=[target(arch_output, "arch", packages=statuses("foo", "bar"))])
    # Nobody at the terminal: it stops before anything touches the network
    with pytest.raises(aur_generator.lib.DeployError, match="add the new SSH key 'aur'"):
        aur_generator.main(job)
    assert (isolated_env / "pkggen" / "keys" / "aur.pub").exists()
    assert "Add it to your AUR account (https://aur.archlinux.org/account/Packager/edit" in capsys.readouterr().err


def test_check(aur_generator, make_job, secrets):
    job = make_job(aur_generator, {"generator": "aur", "username": "me", "url": "x", "password": "p", "branch": "b",
                                   "path": "p", "prefix": 1})
    assert aur_generator.check(job) == [
        "'deployment.dist' has unknown keys: prefix (allowed: branch, email, generator, managed-key, message, password, "
        "path, preserve, prune, sign, ssh-key, targets, url, username)",
        "'deployment.dist.email' is required",
        "'deployment.dist.url' can't be set for AUR deployments",
        "'deployment.dist.branch' can't be set for AUR deployments",
        "'deployment.dist.password' can't be set for AUR deployments: the AUR only accepts SSH keys",
        "'deployment.dist.path' can't be set for AUR deployments",
    ]


def test_runs_as_a_script(arch_output, aur, tmp_path):
    result = run_generator("deployment", "aur", {
        "distribution": "arch", "id": "arch-aur", "deployment": IDENTITY, "work_path": str(tmp_path / "work"),
        "targets": [target(arch_output, "arch", packages=statuses("foo"))], "packages": ["foo"],
    })
    assert result.code == 0, result.err
    assert result.json == {"pushed": ["foo"], "unchanged": [], "failed": [], "skipped": [], "warnings": []}
    assert "[arch-aur] foo 1.2-1: pushing 2 files changed" in result.err_plain
