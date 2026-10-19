#!/usr/bin/env python3
# Shared helpers for deployment generators.
#
# A deployment generator pushes what `pkggen test` built to a production
# repository. It reads one job (JSON) on stdin:
#   {
#     "check": false,                  // true: only validate the configuration
#     "id": "rpm-based-git",           // names the deployment in messages
#     "where": "deployment.rpm-based", // its place in pkggen.yaml, for errors
#     "distribution": "rpm-based",
#     "deployment": { ...the deployment's block in pkggen.yaml... },
#     "repository": { ...repositories.<dist>... },
#     "run_path": "/abs/dir/of/pkggen.yaml",
#     "work_path": "/abs/pkggen-build/deploy/rpm-based-git",  // the generator's own scratch space
#     "targets": [ { "name": "fedora-44", "path": "/abs/pkggen-build/test/fedora-44", "ok": true,
#                    "reason": null, "packages": { "foo": { "version": "1.2", "ok": true } } } ],
#     "skipped_targets": [ "fedora-43" ],  // selected, but with no test output to deploy
#     "packages": null,                // or the names given with -p
#     "skip_tests": false,             // --skip-tests: deploy targets and packages that failed their tests
#     "interactive": true,             // a person is at the terminal, so prompts can wait for them
#     "dry_run": false                 // --dry-run: commit locally, don't push
#   }
# Progress, prompts and errors go to stderr. It prints on stdout:
#   check mode: { "errors": [...], "warnings": [...] }
#   otherwise:  { "pushed": [names], "unchanged": [names], "failed": [{name, message}],
#                 "skipped": [{name, reason}], "warnings": [...] }
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone

import yaml

DEBUG = os.getenv("PKGGEN_DEBUG") == "1"

# pkggen-managed SSH keys are replaced after this long
KEY_LIFETIME = timedelta(days=90)
# and pkggen warns this long before
KEY_WARNING = timedelta(days=7)
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

YELLOW = "\x1b[33m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


class DeployError(Exception):
    """An expected error. Reported as a single message, without a traceback."""


class Target:
    def __init__(self, data):
        self.name = data["name"]
        self.path = data["path"]
        self.ok = bool(data.get("ok"))
        self.reason = data.get("reason")
        self.packages = data.get("packages") or {}


class Job:
    def __init__(self, data):
        self.check = bool(data.get("check"))
        self.distribution = data["distribution"]
        self.id = data.get("id") or self.distribution
        self.where = data.get("where") or f"deployment.{self.distribution}"
        self.config = data.get("deployment") or {}
        self.repository = data.get("repository") or {}
        self.run_path = data.get("run_path") or os.getcwd()
        self.work_path = data.get("work_path") or os.path.join(self.run_path, "pkggen-build", "deploy", self.id)
        self.targets = [Target(t) for t in data.get("targets") or []]
        self.skipped_targets = list(data.get("skipped_targets") or [])
        self.packages = data.get("packages")
        self.skip_tests = bool(data.get("skip_tests"))
        self.interactive = bool(data.get("interactive"))
        self.dry_run = bool(data.get("dry_run"))
        self.warnings = []
        self.pushed = []
        self.unchanged = []
        self.failed = []
        self.skipped = []

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
            log(f"{YELLOW}Warning [{self.id}]: {message}{RESET}")

    def say(self, message):
        log(f"[{self.id}] {message}")

    def key(self, name):
        """Where a key of this deployment's block is, for error messages."""
        return f"'{self.where}.{name}'"

    def usable_targets(self):
        """The targets whose tests passed (all of them with --skip-tests). The others are recorded as skipped."""
        usable = []
        for target in self.targets:
            if target.ok or self.skip_tests:
                usable.append(target)
            else:
                reason = target.reason or "its tests failed"
                self.skipped.append({"name": target.name, "reason": f"{reason} (--skip-tests deploys it anyway)"})
        return usable

    def result(self):
        return {"pushed": self.pushed, "unchanged": self.unchanged, "failed": self.failed,
                "skipped": self.skipped, "warnings": self.warnings}


def log(message):
    print(message, file=sys.stderr, flush=True)


# --- Secrets -----------------------------------------------------------------

def config_dir():
    """pkggen's configuration folder: $XDG_CONFIG_HOME/pkggen, or %APPDATA%\\pkggen on Windows."""
    if os.name == "nt":
        base_dir = os.getenv("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
    else:
        base_dir = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base_dir, "pkggen")


def secrets_path():
    return os.path.join(config_dir(), "secrets.yaml")


def load_secrets():
    path = secrets_path()
    try:
        with open(path, "r", encoding="utf-8") as stream:
            result = yaml.safe_load(stream)
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise DeployError(f"couldn't parse {path}: {e}")
    if result is not None and not isinstance(result, dict):
        raise DeployError(f"{path} must contain a mapping of secret names to values")
    return result or {}


def env_name(prefix, nickname):
    """PKGGEN_PASSWORD_ + 'my-github' -> PKGGEN_PASSWORD_MY_GITHUB."""
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", nickname).upper()


SECRET_KINDS = {
    # kind: (secrets.yaml key, environment variable prefix, what it is)
    "password": ("git_passwords", "PKGGEN_PASSWORD_", "password"),
    "ssh-key": ("ssh_keys", "PKGGEN_SSH_KEY_", "SSH key"),
}


def lookup_secret(kind, nickname):
    """The value of a password or SSH key nickname: from the environment, else secrets.yaml. None if unset."""
    section, prefix, _ = SECRET_KINDS[kind]
    value = os.getenv(env_name(prefix, nickname))
    if value:
        return value
    entries = load_secrets().get(section)
    if entries is None:
        return None
    if not isinstance(entries, dict):
        raise DeployError(f"'{section}' in {secrets_path()} must be a mapping of nicknames to values")
    value = entries.get(nickname)
    return None if value is None else str(value)


def secret_problem(kind, nickname):
    """Why a nickname can't be used, or None."""
    section, prefix, what = SECRET_KINDS[kind]
    try:
        value = lookup_secret(kind, nickname)
    except DeployError as e:
        return str(e)
    if not value:
        return (f"there's no {what} called '{nickname}': add it under '{section}' in {secrets_path()}, "
                f"or set {env_name(prefix, nickname)}")
    if kind == "ssh-key":
        path = os.path.expanduser(value)
        if not os.path.isfile(path):
            return f"the SSH key '{nickname}' is {path}, which doesn't exist"
    return None


def secret_password(nickname):
    problem = secret_problem("password", nickname)
    if problem:
        raise DeployError(problem)
    return lookup_secret("password", nickname)


def secret_ssh_key(nickname):
    problem = secret_problem("ssh-key", nickname)
    if problem:
        raise DeployError(problem)
    return os.path.abspath(os.path.expanduser(lookup_secret("ssh-key", nickname)))


# --- pkggen-managed SSH keys ---------------------------------------------------

def now():
    """The current time. PKGGEN_TEST_NOW (an ISO 8601 date) replaces it in tests."""
    fake = os.getenv("PKGGEN_TEST_NOW")
    if fake:
        value = datetime.fromisoformat(fake.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def keys_dir():
    return os.path.join(config_dir(), "keys")


class ManagedKey:
    def __init__(self, name):
        self.name = name
        self.path = os.path.join(keys_dir(), name)
        self.public_path = self.path + ".pub"
        self.info_path = self.path + ".json"
        self.created = None
        self.expires = None

    def exists(self):
        return os.path.isfile(self.path) and os.path.isfile(self.public_path)

    def load(self):
        """Read the dates. A key without them (deleted by hand) counts from its file's age."""
        try:
            with open(self.info_path, encoding="utf-8") as f:
                info = json.load(f)
            self.created = datetime.fromisoformat(info["created"])
            self.expires = datetime.fromisoformat(info["expires"])
        except (OSError, ValueError, KeyError, TypeError):
            self.created = datetime.fromtimestamp(os.path.getmtime(self.path), timezone.utc)
            self.expires = self.created + KEY_LIFETIME
            self.save()

    def save(self):
        with open(self.info_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"created": self.created.isoformat(timespec="seconds"),
                       "expires": self.expires.isoformat(timespec="seconds")}, f, indent=2)
            f.write("\n")

    def public_key(self):
        with open(self.public_path, encoding="utf-8") as f:
            return f.read().strip()

    def fingerprint(self):
        process = subprocess.run([ssh_keygen(), "-l", "-f", self.public_path], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL)
        fields = process.stdout.split()
        return fields[1] if process.returncode == 0 and len(fields) > 1 else self.public_key()

    def generate(self):
        os.makedirs(keys_dir(), exist_ok=True)
        for path in (self.path, self.public_path):
            if os.path.exists(path):
                os.remove(path)  # ssh-keygen would ask whether to overwrite it
        process = subprocess.run([ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", f"pkggen-{self.name}",
                                  "-f", self.path], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if process.returncode != 0 or not self.exists():
            raise DeployError(f"ssh-keygen couldn't create {self.path}: {process.stderr.strip() or process.returncode}")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.created = now()
        self.expires = self.created + KEY_LIFETIME
        self.save()


def ssh_keygen():
    path = shutil.which("ssh-keygen")
    if not path:
        raise DeployError("pkggen needs ssh-keygen (part of OpenSSH) to create its SSH keys. Install OpenSSH, or "
                          "set 'ssh-key' to a key of your own")
    return path


def managed_key(job, name, where_to_add):
    """The private key path of the pkggen-managed key `name`, created or replaced when needed.

    where_to_add tells the user where the public key goes (e.g. their AUR
    account page). After creating a key pkggen waits until the user has added
    it, or stops when nobody is at the terminal.
    """
    if not KEY_NAME_RE.match(name):
        raise DeployError(f"'{name}' can't be the name of an SSH key: use letters, digits, '.', '_' and '-'")
    key = ManagedKey(name)
    if not key.exists():
        key.generate()
        announce_key(job, key, where_to_add)
        return key.path

    key.load()
    current = now()
    if current >= key.expires:
        old = key.fingerprint()
        key.generate()
        announce_key(job, key, where_to_add, replaced=old)
    elif key.expires - current < KEY_WARNING:
        job.warn(f"the pkggen SSH key '{name}' expires on {key.expires:%Y-%m-%d}. pkggen then creates a new "
                 f"one, which you'll have to add {where_to_add}")
    return key.path


def announce_key(job, key, where_to_add, replaced=None):
    if replaced:
        intro = (f"The pkggen SSH key '{key.name}' expired, so pkggen made a new one. Remove the old key "
                 f"({replaced}) and add the new one {where_to_add}:")
    else:
        intro = f"pkggen made the SSH key '{key.name}' for {job.id}. Add it {where_to_add}:"
    log("")
    log(f"{BOLD}{YELLOW}{intro}{RESET}")
    log("")
    log(f"    {key.public_key()}")
    log("")
    log(f"It's valid until {key.expires:%Y-%m-%d} and stored in {key.path}.")
    if job.interactive:
        wait_for_user("Press Enter once you've added it, or Ctrl-C to stop: ")
    else:
        raise DeployError(f"add the new SSH key '{key.name}' (printed above, and in {key.public_path}), "
                          "then run `pkggen deploy` again")


def open_console():
    """The terminal, for reading answers: stdin carries the job."""
    return open("CONIN$" if os.name == "nt" else "/dev/tty", "r", encoding="utf-8", errors="replace")


def wait_for_user(prompt):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        with open_console() as console:
            line = console.readline()
    except OSError as e:
        raise DeployError(f"couldn't read from the terminal ({e}). Run `pkggen deploy` again once the key is added")
    if not line:  # end of input, e.g. Ctrl-D
        raise DeployError("stopped. Run `pkggen deploy` again once the key is added")
    return line


# --- Configuration checks ----------------------------------------------------------

def check_keys(job, allowed, errors):
    unknown = sorted(set(job.config) - set(allowed))
    if unknown:
        errors.append(f"'{job.where}' has unknown keys: {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def check_string(job, key, errors, required=False, empty=False):
    value = job.config.get(key)
    if value is None:
        if required:
            errors.append(f"{job.key(key)} is required")
        return None
    if not isinstance(value, str) or (not empty and not value.strip()):
        errors.append(f"{job.key(key)} must be a{'' if empty else ' non-empty'} string")
        return None
    return value


# --- Entry point -------------------------------------------------------------

def run(main, check):
    """Read the job from stdin and run check(job) -> [errors] or main(job), printing the result as JSON."""
    try:
        data = json.loads(sys.stdin.buffer.read())
        job = Job(data)
        if job.check:
            output = {"errors": check(job), "warnings": job.warnings}
        else:
            main(job)
            output = job.result()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        if isinstance(e, (DeployError, OSError)):
            message = str(e)
        elif isinstance(e, (json.JSONDecodeError, KeyError)):
            message = f"invalid deployment job: {type(e).__name__}: {e}"
        else:
            message = f"unexpected {type(e).__name__}: {e} (set PKGGEN_DEBUG=1 for a traceback)"
        print(f"\x1b[31mError: {message}\x1b[0m", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(json.dumps(output) + "\n")
