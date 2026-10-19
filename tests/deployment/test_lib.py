import io
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from tests.deployment.conftest import needs_ssh_keygen
from tests.support.helpers import run_generator, strip_ansi

NOW = "2026-10-19T12:00:00+00:00"


def job(deploy_lib, **fields):
    data = {"distribution": "dist", "id": "dist-git", "deployment": {}, "interactive": False}
    data.update(fields)
    return deploy_lib.Job(data)


# --- Secrets ---------------------------------------------------------------------------


def test_env_name(deploy_lib):
    assert deploy_lib.env_name("PKGGEN_PASSWORD_", "my-github.com") == "PKGGEN_PASSWORD_MY_GITHUB_COM"


def test_secrets_from_the_file_and_the_environment(deploy_lib, secrets, tmp_path, monkeypatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    secrets({"git_passwords": {"github": "from-file", "other": 1234}, "ssh_keys": {"mine": str(key)}})
    assert deploy_lib.secret_password("github") == "from-file"
    assert deploy_lib.secret_password("other") == "1234"
    assert deploy_lib.secret_ssh_key("mine") == str(key)
    monkeypatch.setenv("PKGGEN_PASSWORD_GITHUB", "from-env")
    assert deploy_lib.secret_password("github") == "from-env"
    other = tmp_path / "ci_key"
    other.write_text("private")
    monkeypatch.setenv("PKGGEN_SSH_KEY_CI", str(other))
    assert deploy_lib.secret_ssh_key("ci") == str(other)


def test_secret_problems(deploy_lib, secrets, tmp_path):
    assert "there's no password called 'gh'" in deploy_lib.secret_problem("password", "gh")
    assert "set PKGGEN_PASSWORD_GH" in deploy_lib.secret_problem("password", "gh")
    secrets({"ssh_keys": {"gone": str(tmp_path / "missing")}, "git_passwords": ["not", "a", "mapping"]})
    assert "which doesn't exist" in deploy_lib.secret_problem("ssh-key", "gone")
    assert "'git_passwords'" in deploy_lib.secret_problem("password", "x")
    with pytest.raises(deploy_lib.DeployError, match="there's no SSH key called 'nope'"):
        deploy_lib.secret_ssh_key("nope")


def test_ssh_key_paths_expand_home(deploy_lib, secrets, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "key").write_text("private")
    secrets({"ssh_keys": {"home": "~/.ssh/key"}})
    assert deploy_lib.secret_ssh_key("home") == str(tmp_path / ".ssh" / "key")


# --- Managed keys ---------------------------------------------------------------------


@needs_ssh_keygen
def test_new_key_without_a_terminal_stops_with_instructions(deploy_lib, isolated_env, monkeypatch, capsys):
    monkeypatch.setenv("PKGGEN_TEST_NOW", NOW)
    with pytest.raises(deploy_lib.DeployError, match="add the new SSH key 'aur'") as info:
        deploy_lib.managed_key(job(deploy_lib), "aur", "to your AUR account")
    keys = isolated_env / "pkggen" / "keys"
    assert str(keys / "aur.pub") in str(info.value)
    public = (keys / "aur.pub").read_text().strip()
    assert public.startswith("ssh-ed25519 ") and public.endswith(" pkggen-aur")
    assert json.loads((keys / "aur.json").read_text()) == {"created": NOW, "expires": "2027-01-17T12:00:00+00:00"}
    if os.name != "nt":
        assert (keys / "aur").stat().st_mode & 0o777 == 0o600
    err = strip_ansi(capsys.readouterr().err)
    assert "pkggen made the SSH key 'aur' for dist-git. Add it to your AUR account:" in err
    assert public in err and "It's valid until 2027-01-17" in err

    # Once it exists, it's used as it is
    assert deploy_lib.managed_key(job(deploy_lib), "aur", "to your AUR account") == str(keys / "aur")


@needs_ssh_keygen
def test_new_key_in_a_terminal_waits_for_enter(deploy_lib, isolated_env, monkeypatch, capsys):
    monkeypatch.setattr(deploy_lib, "open_console", lambda: io.StringIO("\n"))
    path = deploy_lib.managed_key(job(deploy_lib, interactive=True), "git-example.org", "to example.org")
    assert path == str(isolated_env / "pkggen" / "keys" / "git-example.org")
    assert "Press Enter once you've added it" in capsys.readouterr().err

    # End of input (Ctrl-D) stops instead
    monkeypatch.setattr(deploy_lib, "open_console", lambda: io.StringIO(""))
    with pytest.raises(deploy_lib.DeployError, match="stopped"):
        deploy_lib.managed_key(job(deploy_lib, interactive=True), "other", "somewhere")


@needs_ssh_keygen
def test_key_expiry(deploy_lib, isolated_env, monkeypatch, capsys):
    monkeypatch.setenv("PKGGEN_TEST_NOW", NOW)
    monkeypatch.setattr(deploy_lib, "open_console", lambda: io.StringIO("\n"))
    deploy_lib.managed_key(job(deploy_lib, interactive=True), "k", "there")
    keys = isolated_env / "pkggen" / "keys"
    first = (keys / "k.pub").read_text()

    # Less than a week left: a warning, the same key
    monkeypatch.setenv("PKGGEN_TEST_NOW", "2027-01-12T12:00:00+00:00")
    warned = job(deploy_lib)
    deploy_lib.managed_key(warned, "k", "there")
    assert warned.warnings == ["the pkggen SSH key 'k' expires on 2027-01-17. pkggen then creates a new one, "
                               "which you'll have to add there"]
    assert (keys / "k.pub").read_text() == first

    # Expired: a new key, and the old one's fingerprint to remove
    monkeypatch.setenv("PKGGEN_TEST_NOW", "2027-01-18T00:00:00+00:00")
    capsys.readouterr()
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.managed_key(job(deploy_lib), "k", "there")
    assert (keys / "k.pub").read_text() != first
    assert json.loads((keys / "k.json").read_text())["expires"] == "2027-04-18T00:00:00+00:00"
    err = strip_ansi(capsys.readouterr().err)
    assert "The pkggen SSH key 'k' expired, so pkggen made a new one. Remove the old key (SHA256:" in err


@needs_ssh_keygen
def test_key_without_its_dates_counts_from_the_file(deploy_lib, isolated_env, monkeypatch):
    monkeypatch.setattr(deploy_lib, "open_console", lambda: io.StringIO("\n"))
    deploy_lib.managed_key(job(deploy_lib, interactive=True), "k", "there")
    info = isolated_env / "pkggen" / "keys" / "k.json"
    info.unlink()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    os.utime(isolated_env / "pkggen" / "keys" / "k", (week_ago, week_ago))
    deploy_lib.managed_key(job(deploy_lib), "k", "there")
    expires = datetime.fromisoformat(json.loads(info.read_text())["expires"])
    assert timedelta(days=82) < expires - datetime.now(timezone.utc) < timedelta(days=84)


def test_invalid_key_names(deploy_lib):
    with pytest.raises(deploy_lib.DeployError, match="can't be the name of an SSH key"):
        deploy_lib.managed_key(job(deploy_lib), "../escape", "there")


def test_missing_ssh_keygen(deploy_lib, monkeypatch):
    monkeypatch.setattr(deploy_lib.shutil, "which", lambda name: None)
    with pytest.raises(deploy_lib.DeployError, match="pkggen needs ssh-keygen"):
        deploy_lib.managed_key(job(deploy_lib), "k", "there")


# --- Protocol ---------------------------------------------------------------------------


def test_usable_targets(deploy_lib):
    j = job(deploy_lib, targets=[{"name": "a", "path": "/a", "ok": True},
                                 {"name": "b", "path": "/b", "ok": False, "reason": "its last test failed (boom)"}])
    assert [t.name for t in j.usable_targets()] == ["a"]
    assert j.skipped == [{"name": "b", "reason": "its last test failed (boom) (--skip-tests deploys it anyway)"}]
    j = job(deploy_lib, skip_tests=True, targets=[{"name": "b", "path": "/b", "ok": False}])
    assert [t.name for t in j.usable_targets()] == ["b"]


def test_run_reports_errors_on_one_line():
    result = run_generator("deployment", "git", "not json")
    assert result.code == 1
    assert "Error: invalid deployment job: JSONDecodeError" in result.err_plain
    assert "Traceback" not in result.err


def test_check_mode_prints_the_errors():
    result = run_generator("deployment", "git", {"check": True, "distribution": "rpm-based",
                                                 "where": "deployment.rpm-based", "deployment": {"generator": "git"}})
    assert result.code == 0, result.err
    assert result.json["errors"] == ["'deployment.rpm-based.username' is required",
                                     "'deployment.rpm-based.email' is required",
                                     "'deployment.rpm-based.url' is required"]
