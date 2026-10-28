import os
import shutil
import subprocess

import pytest

from tests.deployment.conftest import git, target, write_tree
from tests.deployment.test_s3 import gpg_key  # noqa: F401 (fixture)

IDENTITY = {"username": "Packager", "email": "packager@example.org"}


def config(url, **extra):
    return {"generator": "git", "url": url, **IDENTITY, **extra}


def built(tmp_path, name, files):
    """A target's test output."""
    return write_tree(tmp_path / "test" / name, files)


# --- URLs and authentication ------------------------------------------------------------


@pytest.mark.parametrize("url, kind, host", [
    ("git@github.com:me/repo.git", "ssh", "github.com"),
    ("github.com:me/repo.git", "ssh", "github.com"),
    ("ssh://aur@aur.archlinux.org/foo.git", "ssh", "aur.archlinux.org"),
    ("https://gitlab.com/me/repo.git", "https", "gitlab.com"),
    ("http://git.local/repo.git", "https", "git.local"),
    ("file:///srv/git/repo.git", "file", ""),
    ("/srv/git/repo.git", "file", ""),
    ("../relative/repo.git", "file", ""),
    ("C:\\repos\\repo.git", "file", ""),
    ("C:/repos/repo.git", "file", ""),
    ("git://example.org/repo.git", "other", "example.org"),
])
def test_url_kinds(git_generator, url, kind, host):
    assert git_generator.url_kind(url) == kind
    assert git_generator.url_host(url) == host


def test_password_auth_only_goes_through_the_environment(git_generator, make_job, secrets):
    secrets({"git_passwords": {"gh": "s3cr3t 'quoted' $HOME"}})
    job = make_job(git_generator, config("https://github.com/me/repo.git", password="gh"))
    env = git_generator.auth_env(job, job.config["url"])
    assert env["PKGGEN_GIT_PASSWORD"] == "s3cr3t 'quoted' $HOME"
    assert env["PKGGEN_GIT_USERNAME"] == "Packager"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "s3cr3t" not in "".join(v for k, v in env.items() if k != "PKGGEN_GIT_PASSWORD")

    # The helper answers git with exactly these credentials, and only those (the user's helpers are cleared)
    process = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, text=True, env={**os.environ, **env})
    assert process.returncode == 0, process.stderr
    assert "username=Packager\n" in process.stdout
    assert "password=s3cr3t 'quoted' $HOME\n" in process.stdout


def test_the_url_user_wins_for_https(git_generator, make_job, secrets):
    secrets({"git_passwords": {"gl": "token"}})
    job = make_job(git_generator, config("https://oauth2@gitlab.com/me/repo.git", password="gl"))
    assert git_generator.auth_env(job, job.config["url"])["PKGGEN_GIT_USERNAME"] == "oauth2"


def test_ssh_key_auth(git_generator, make_job, secrets, tmp_path):
    key = tmp_path / "my key"
    key.write_text("private")
    secrets({"ssh_keys": {"mine": str(key)}})
    job = make_job(git_generator, config("git@github.com:me/repo.git", **{"ssh-key": "mine"}))
    command = git_generator.auth_env(job, job.config["url"])["GIT_SSH_COMMAND"]
    assert command == (f"ssh -i '{str(key).replace(os.sep, '/')}' -o IdentitiesOnly=yes "
                       "-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
    interactive = make_job(git_generator, job.config, interactive=True)
    assert "BatchMode" not in git_generator.auth_env(interactive, job.config["url"])["GIT_SSH_COMMAND"]


def test_local_remotes_need_no_auth(git_generator, make_job):
    job = make_job(git_generator, config("/srv/repo.git"))
    assert git_generator.auth_env(job, "/srv/repo.git") == {"GIT_TERMINAL_PROMPT": "0"}


# --- Configuration checks --------------------------------------------------------------------


def check(git_generator, make_job, deployment, targets=("t1",), skipped=()):
    job = make_job(git_generator, deployment, targets=[target("/x", name=n) for n in targets],
                   skipped_targets=list(skipped))
    return git_generator.check(job)


def test_check_needs_git(git_generator, make_job, monkeypatch):
    """The git and AUR generators check for git themselves: pkggen's core doesn't know them."""
    monkeypatch.setattr(git_generator.shutil, "which", lambda name: None)
    errors = check(git_generator, make_job, config("/srv/repo.git"))
    assert "'deployment.dist' needs git, which isn't installed" in errors[0]


def test_check_accepts_a_complete_configuration(git_generator, make_job, secrets):
    secrets({"git_passwords": {"gh": "token"}})
    assert check(git_generator, make_job, config("https://github.com/me/r.git", password="gh", branch="pages",
                                                 path="repos/{target}", prune=True, preserve=["^README"],
                                                 message="Update", sign=False, targets=["t1"]),
                 targets=("t1", "t2")) == []


def test_check_errors(git_generator, make_job, secrets, tmp_path, isolated_env):
    key = tmp_path / "key"
    key.write_text("x")
    secrets({"ssh_keys": {"mine": str(key)}, "git_passwords": {"gh": "token"}})
    errors = check(git_generator, make_job, {
        "generator": "git", "url": "/srv/repo.git", "email": "", "ssh-key": "mine", "password": "nope",
        "prune": "yes", "preserve": ["(unclosed"], "typo": 1, "path": "../out",
    })
    assert errors == [
        "'deployment.dist' has unknown keys: typo (allowed: branch, email, exclude, generator, gpg-key, "
        "gpg-passphrase, key-name, managed-key, message, password, path, preserve, prune, public-key, rsa-key, "
            "rsa-passphrase, sign, signed-by, ssh-key, targets, "
        "url, username)",
        "'deployment.dist.username' is required",
        "'deployment.dist.email' must be a non-empty string",
        "set either 'deployment.dist.ssh-key' or 'deployment.dist.password', not both",
        "'deployment.dist.password': there's no password called 'nope': add it under 'git_passwords' in "
        f"{isolated_env / 'pkggen' / 'secrets.yaml'}, or set PKGGEN_PASSWORD_NOPE",
        "'deployment.dist.prune' must be true or false",
        "'deployment.dist.preserve' has an invalid regular expression '(unclosed': missing ), unterminated "
        "subpattern at position 0",
        "'deployment.dist.password' only works with https URLs, but 'deployment.dist.url' is /srv/repo.git",
        "'deployment.dist.ssh-key' only works with SSH URLs (git@host:path or ssh://), but "
        "'deployment.dist.url' is /srv/repo.git",
        "'deployment.dist.path' must be a folder inside the repository, got '../out'",
    ]


@pytest.mark.parametrize("path, targets, message", [
    # Targets can share a folder: their files are merged
    ("", ("a", "b"), None),
    ("repo", ("a",), None),
    ("", ("a",), None),
    ("{target}", ("a", "a/b"), "puts one target's folder (a/b) inside another one's (a)"),
    ("{target}", ("a",), None),
])
def test_check_target_folders(git_generator, make_job, path, targets, message):
    errors = check(git_generator, make_job, config("/srv/r.git", path=path), targets=targets[:1], skipped=targets[1:])
    assert (errors == []) if message is None else any(message in e for e in errors)


def test_check_managed_key_needs_ssh(git_generator, make_job):
    errors = check(git_generator, make_job, config("https://h/r.git", **{"managed-key": "bad name!"}))
    assert "'deployment.dist.managed-key' must only contain letters, digits, '.', '_' and '-'" in errors
    assert any("'deployment.dist.managed-key' only works with SSH URLs" in e for e in errors)


def test_check_warns_about_preserve_without_prune(git_generator, make_job):
    job = make_job(git_generator, config("/srv/r.git", preserve=["x"]), targets=[target("/x")])
    assert git_generator.check(job) == []
    assert job.warnings == ["'deployment.dist.preserve' only matters with 'prune: true': without it nothing is deleted"]


# --- Deploying -----------------------------------------------------------------------------------


def deploy(git_generator, make_job, deployment, targets, **fields):
    job = make_job(git_generator, deployment, targets=targets, **fields)
    assert git_generator.check(job) == []
    git_generator.main(job)
    return job


def test_first_push_and_unchanged_rerun(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    out = built(tmp_path, "fedora-44", {"RPMS/x86_64/foo.rpm": "rpm", "repodata/repomd.xml": "xml"})
    job = deploy(git_generator, make_job, config(up.url), [target(out, "fedora-44")])
    assert job.pushed == ["dist-gen"]
    assert up.files() == {"fedora-44/RPMS/x86_64/foo.rpm": "rpm", "fedora-44/repodata/repomd.xml": "xml"}
    assert up.log() == ["Update packages (fedora-44)|Packager|packager@example.org"]

    job = deploy(git_generator, make_job, config(up.url), [target(out, "fedora-44")])
    assert job.unchanged == ["dist-gen"] and job.pushed == []
    assert len(up.log()) == 1


def test_default_branch_comes_from_the_remote(git_generator, make_job, upstream, tmp_path):
    up = upstream(branch="trunk")
    up.commit(tmp_path, {"README.md": "hi"})
    out = built(tmp_path, "t1", {"a": "1"})
    deploy(git_generator, make_job, config(up.url, path=""), [target(out)])
    assert up.files() == {"README.md": "hi", "a": "1"}

    deploy(git_generator, make_job, config(up.url, path="", branch="pages"), [target(out)])
    assert up.files("pages") == {"a": "1"}


def test_changes_go_on_top_of_the_upstream(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    up.commit(tmp_path, {"README.md": "readme", "t1/old.rpm": "old", "t1/keep.txt": "v1", ".github/ci.yaml": "ci"})
    out = built(tmp_path, "t1", {"keep.txt": "v2", "new.rpm": "new"})

    # Without prune nothing upstream is deleted, changed files are overwritten
    deploy(git_generator, make_job, config(up.url, message="Release"), [target(out)])
    assert up.files() == {"README.md": "readme", "t1/old.rpm": "old", "t1/keep.txt": "v2", "t1/new.rpm": "new",
                          ".github/ci.yaml": "ci"}
    assert up.log()[0] == "Release|Packager|packager@example.org"


def test_prune_and_preserve(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    up.commit(tmp_path, {"README.md": "readme", "docs/guide/a.md": "a", "notes.txt": "n", "t1/old.rpm": "old",
                         "t2/kept.rpm": "kept", ".github/ci.yaml": "ci"})
    out = built(tmp_path, "t1", {"new.rpm": "new"})
    # t2 failed its tests: its folder upstream stays as it is
    failed = built(tmp_path, "t2", {"broken.rpm": "broken"})
    job = deploy(git_generator, make_job,
                 config(up.url, prune=True, preserve=[r"^README\.md$", r"^\.github$", "^docs$"]),
                 [target(out), target(failed, "t2", ok=False, reason="its last test failed (boom)")])
    assert up.files() == {"README.md": "readme", "docs/guide/a.md": "a", "t1/new.rpm": "new",
                          "t2/kept.rpm": "kept", ".github/ci.yaml": "ci"}
    assert job.skipped == [{"name": "t2", "reason": "its last test failed (boom) (--skip-tests deploys it anyway)"}]

    # --skip-tests deploys it anyway
    deploy(git_generator, make_job, config(up.url, prune=True), [target(out), target(failed, "t2", ok=False)],
           skip_tests=True)
    assert up.files() == {"t1/new.rpm": "new", "t2/broken.rpm": "broken"}


def test_selected_targets_without_output_are_never_pruned(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    up.commit(tmp_path, {"t2/kept.rpm": "kept", "stale.txt": "x"})
    out = built(tmp_path, "t1", {"new.rpm": "new"})
    deploy(git_generator, make_job, config(up.url, prune=True), [target(out)], skipped_targets=["t2"])
    assert up.files() == {"t1/new.rpm": "new", "t2/kept.rpm": "kept"}


def test_nothing_usable(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    out = built(tmp_path, "t1", {"a": "1"})
    job = deploy(git_generator, make_job, config(up.url), [target(out, ok=False)])
    assert job.pushed == [] and len(job.skipped) == 1
    assert up.files() is None


def test_dry_run_commits_but_doesnt_push(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    out = built(tmp_path, "t1", {"a": "1"})
    job = deploy(git_generator, make_job, config(up.url), [target(out)], dry_run=True)
    assert job.pushed == ["dist-gen"]
    assert up.files() is None
    assert git("log", "--format=%s", cwd=tmp_path / "work").strip() == "Update packages (t1)"


def test_commits_arent_signed_unless_asked(git_generator, make_job, upstream, tmp_path, isolated_env):
    # A signing setup that can't work here, like a key that needs a touch
    with open(isolated_env / "gitconfig", "a", encoding="utf-8") as f:
        f.write("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    up = upstream()
    out = built(tmp_path, "t1", {"a": "1"})
    deploy(git_generator, make_job, config(up.url), [target(out)])
    assert up.files() == {"t1/a": "1"}

    write_tree(out, {"a": "2"})
    job = make_job(git_generator, config(up.url, sign=True), targets=[target(out)])
    with pytest.raises(git_generator.GitError, match="git commit failed"):
        git_generator.main(job)


def test_files_are_pushed_byte_for_byte(git_generator, make_job, upstream, tmp_path, isolated_env):
    # Git for Windows sets core.autocrlf=true system-wide
    with open(isolated_env / "gitconfig", "a", encoding="utf-8") as f:
        f.write("[core]\n\tautocrlf = true\n")
    up = upstream()
    out = tmp_path / "test" / "t1"
    out.mkdir(parents=True)
    (out / "run.bat").write_bytes(b"echo hi\r\n")
    (out / "PKGBUILD").write_bytes(b"pkgname=foo\npkgver=1\n")
    deploy(git_generator, make_job, config(up.url), [target(out)])

    def blob(name):
        return subprocess.run(["git", "--git-dir", up.url, "show", f"main:{name}"], capture_output=True,
                              check=True).stdout
    assert blob("t1/run.bat") == b"echo hi\r\n"
    assert blob("t1/PKGBUILD") == b"pkgname=foo\npkgver=1\n"
    # The work clone's checkout isn't converted either, so a rerun has nothing to commit
    job = deploy(git_generator, make_job, config(up.url), [target(out)])
    assert job.unchanged == ["dist-gen"]


@pytest.mark.skipif(os.name != "nt", reason="the 260 character path limit is Windows' own")
def test_paths_over_260_characters(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    name = "/".join(["RPMS"] + ["d" * 40] * 4 + ["0" * 64 + "-filelists.xml.zst"])
    try:
        out = built(tmp_path, "t1", {name: "x"})
    except OSError:
        pytest.skip("long paths aren't enabled in Windows (LongPathsEnabled)")
    assert len(str(tmp_path / "work" / "t1" / name)) > 260
    deploy(git_generator, make_job, config(up.url), [target(out)])
    assert up.files() == {f"t1/{name}": "x"}


def test_upstream_moving_during_the_push_is_retried(git_generator, make_job, upstream, tmp_path, monkeypatch):
    up = upstream()
    up.commit(tmp_path, {"README.md": "v1"})
    out = built(tmp_path, "t1", {"a": "1"})
    real = git_generator.commit
    moved = []

    def commit_then_someone_pushes(*args, **kwargs):
        changed = real(*args, **kwargs)
        if not moved:
            moved.append(True)
            up.commit(tmp_path, {"README.md": "v2"}, message="concurrent change")
        return changed

    monkeypatch.setattr(git_generator, "commit", commit_then_someone_pushes)
    job = deploy(git_generator, make_job, config(up.url), [target(out)])
    assert job.pushed == ["dist-gen"]
    assert up.files() == {"README.md": "v2", "t1/a": "1"}
    assert [line.split("|")[0] for line in up.log()] == ["Update packages (t1)", "concurrent change", "upstream change"]


def test_push_errors_name_the_problem(git_generator, make_job, tmp_path):
    out = built(tmp_path, "t1", {"a": "1"})
    job = make_job(git_generator, config(str(tmp_path / "missing.git")), targets=[target(out)])
    with pytest.raises(git_generator.GitError) as info:
        git_generator.main(job)
    assert str(info.value).startswith("git fetch failed: ")


def test_the_work_clone_is_reset_between_runs(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    out = built(tmp_path, "t1", {"a": "1"})
    deploy(git_generator, make_job, config(up.url), [target(out)], dry_run=True)
    # The dry run's commit was never pushed; the real run starts from the upstream again
    (tmp_path / "work" / "junk.txt").write_text("left over")
    job = deploy(git_generator, make_job, config(up.url), [target(out)])
    assert job.pushed == ["dist-gen"]
    assert up.files() == {"t1/a": "1"}


def no_filemode(monkeypatch):
    """Make git ignore the file system's executable bits, like Git for Windows does."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.filemode")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


def modes(up, folder="t1"):
    lines = git("--git-dir", up.url, "ls-tree", "-r", "main", folder).splitlines()
    return {line.split("\t")[1]: line.split()[0] for line in lines}


def test_executable_bits_come_from_the_test_containers(git_generator, make_job, upstream, tmp_path, monkeypatch):
    no_filemode(monkeypatch)
    up = upstream()
    # Copied out onto a file system without executable bits: only the list says which ones are
    out = built(tmp_path, "t1", {"debian/rules": "#!/usr/bin/make -f\n", "run.sh": "#!/bin/sh\n", "README": "r"})
    deploy(git_generator, make_job, config(up.url), [target(out, executables=["debian/rules", "run.sh"])])
    assert modes(up) == {"t1/README": "100644", "t1/debian/rules": "100755", "t1/run.sh": "100755"}

    # A file that isn't executable in the container anymore loses the bit upstream too, like on Linux
    job = deploy(git_generator, make_job, config(up.url), [target(out, executables=["debian/rules"])])
    assert job.pushed == ["dist-gen"]
    assert modes(up) == {"t1/README": "100644", "t1/debian/rules": "100755", "t1/run.sh": "100644"}

    # Without a list (results of an older pkggen), the upstream modes stay
    job = deploy(git_generator, make_job, config(up.url), [target(out)])
    assert job.unchanged == ["dist-gen"]


def test_executables_are_relative_to_each_targets_output(deploy_lib):
    t = deploy_lib.Target({"name": "arch", "path": "/x", "executables": ["foo/a", "foo/b/c", "foobar/d", "e"]})
    assert t.executables_in("foo") == {"a", "b/c"}
    assert t.executables_in("") == {"foo/a", "foo/b/c", "foobar/d", "e"}
    assert deploy_lib.Target({"name": "arch", "path": "/x"}).executables_in("foo") is None


@pytest.mark.skipif(os.name == "nt", reason="no executable bit on Windows")
def test_executable_bits_are_kept(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    out = built(tmp_path, "t1", {"run.sh": "#!/bin/sh\n"})
    os.chmod(out / "run.sh", 0o755)
    deploy(git_generator, make_job, config(up.url), [target(out)])
    mode = git("--git-dir", up.url, "ls-tree", "main", "t1/run.sh").split()[0]
    assert mode == "100755"


# --- Shared folders, exclude and signing ---------------------------------------------------


def test_targets_can_share_a_folder(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    a = built(tmp_path, "debian-13", {"pool/a_1~deb13.deb": "a", "dists/trixie/Release": "t", "index.html": "same"})
    b = built(tmp_path, "ubuntu-24.04", {"pool/a_1~ubuntu24.04.deb": "b", "dists/noble/Release": "n",
                                         "index.html": "same"})
    deploy(git_generator, make_job, config(up.url, path=""), [target(a, "debian-13"), target(b, "ubuntu-24.04")])
    assert up.files() == {"pool/a_1~deb13.deb": "a", "pool/a_1~ubuntu24.04.deb": "b", "dists/trixie/Release": "t",
                          "dists/noble/Release": "n", "index.html": "same"}


def test_shared_folder_conflicts(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    a = built(tmp_path, "a", {"index.html": "one"})
    b = built(tmp_path, "b", {"index.html": "two"})
    job = make_job(git_generator, config(up.url, path=""), targets=[target(a, "a"), target(b, "b")])
    with pytest.raises(git_generator.lib.DeployError, match="a and b both have index.html, with different content"):
        git_generator.main(job)
    assert up.files() is None


def test_exclude(git_generator, make_job, upstream, tmp_path):
    up = upstream()
    up.commit(tmp_path, {"binpkgs/old.gpkg.tar": "kept: excluded files aren't pruned", "stale.ebuild": "x"})
    out = built(tmp_path, "gentoo", {"app/foo/foo-1.ebuild": "e", "binpkgs/app/foo-1.gpkg.tar": "binary"})
    deploy(git_generator, make_job, config(up.url, path="", exclude=["^binpkgs/"], prune=True),
           [target(out, "gentoo")])
    assert up.files() == {"app/foo/foo-1.ebuild": "e", "binpkgs/old.gpkg.tar": "kept: excluded files aren't pruned"}


def test_a_skipped_target_holds_back_pruning_in_a_shared_folder(git_generator, make_job, upstream, tmp_path, capfd):
    up = upstream()
    up.commit(tmp_path, {"dists/noble/Release": "the last noble release"})
    a = built(tmp_path, "debian-13", {"dists/trixie/Release": "t"})
    deploy(git_generator, make_job, config(up.url, path="", prune=True), [target(a, "debian-13")],
           skipped_targets=["ubuntu-24.04"])
    assert up.files() == {"dists/noble/Release": "the last noble release", "dists/trixie/Release": "t"}
    assert "nothing is pruned in the root this time" in capfd.readouterr().err


@pytest.mark.skipif(shutil.which("gpg") is None, reason="needs GnuPG")
def test_signed_repository_in_git(git_generator, make_job, upstream, tmp_path, gpg_key):
    up = upstream()
    out = built(tmp_path, "debian-13", {"dists/trixie/Release": "Codename: trixie\n"})
    deployment = config(up.url, path="", **{"gpg-key": "test", "public-key": "keys/public.asc"})
    deploy(git_generator, make_job, deployment, [target(out, "debian-13")])
    files = up.files()
    assert set(files) == {"dists/trixie/Release", "dists/trixie/InRelease", "dists/trixie/Release.gpg",
                          "keys/public.asc"}
    assert files["dists/trixie/InRelease"].startswith("-----BEGIN PGP SIGNED MESSAGE-----")
    # The signatures of an unchanged Release are kept: nothing to commit
    job = deploy(git_generator, make_job, deployment, [target(out, "debian-13")])
    assert job.unchanged == ["dist-gen"] and len(up.log()) == 1
