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
#                    "reason": null, "packages": { "foo": { "version": "1.2", "ok": true } },
#                    "executables": [ "SOURCES/run.sh" ],  // executable in the container (null: unknown)
#                    "publish": { "indexes": [regex], "metadata": [regex], "content_types": {...} } } ],
#     "skipped_targets": [ "fedora-43" ],  // selected, but with no test output to deploy
#     "packages": null,                // or the names given with -p
#     "skip_tests": false,             // --skip-tests: deploy targets and packages that failed their tests
#     "interactive": true,             // a person is at the terminal, so prompts can wait for them
#     "dry_run": false                 // --dry-run: commit locally, don't push
#   }
# Generators that publish whole repositories stage them first (stage_targets():
# several targets may share a folder, `exclude` leaves files out), then sign
# them with the distribution's hook (hooks/<distribution>.py, see load_hook()):
# GPG (`gpg-key`) for apt and dnf, RSA (`rsa-key`) for Alpine and Void.
# Progress, prompts and errors go to stderr. It prints on stdout:
#   check mode: { "errors": [...], "warnings": [...] }
#   otherwise:  { "pushed": [names], "unchanged": [names], "failed": [{name, message}],
#                 "skipped": [{name, reason}], "warnings": [...] }
import filecmp
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
        # Files of path (POSIX, relative to it) that are executable in the test container: the
        # file system may not have kept the bit (NTFS). None when the test run didn't list them.
        executables = data.get("executables")
        self.executables = None if executables is None else set(executables)
        # From the testing generator: {indexes, metadata, content_types} of its repository format
        self.publish = data.get("publish") or {}

    def executables_in(self, folder):
        """The executables below folder of the output, relative to it (None when unknown)."""
        if self.executables is None:
            return None
        prefix = folder.strip("/") + "/" if folder.strip("/") else ""
        return {p[len(prefix):] for p in self.executables if p.startswith(prefix)}


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
        self._hook = None

    @property
    def hook(self):
        """The distribution's deployment hook (hooks/<distribution>.py)."""
        if self._hook is None:
            self._hook = load_hook(self.distribution)
        return self._hook

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
    # A key fingerprint (in the user's keyring), an armored private key's path, or the armored key itself
    "gpg-key": ("gpg_keys", "PKGGEN_GPG_KEY_", "GPG key"),
    "gpg-passphrase": ("gpg_passphrases", "PKGGEN_GPG_PASSPHRASE_", "GPG key passphrase"),
    # A PEM private key's path, or the PEM key itself (Alpine's abuild-keygen, xbps' openssl genrsa)
    "rsa-key": ("rsa_keys", "PKGGEN_RSA_KEY_", "RSA key"),
    "rsa-passphrase": ("rsa_passphrases", "PKGGEN_RSA_PASSPHRASE_", "RSA key passphrase"),
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
    if kind == "gpg-key" and not (FINGERPRINT_RE.match(value.strip()) or ARMORED_KEY in value
                                  or os.path.isfile(os.path.expanduser(value))):
        return (f"the GPG key '{nickname}' must be a key fingerprint, the path of an armored private key "
                f"(gpg --armor --export-secret-keys), or the armored key itself")
    if kind == "rsa-key" and not (PEM_MARKER in value or os.path.isfile(os.path.expanduser(value))):
        return (f"the RSA key '{nickname}' must be the path of a PEM private key (openssl genrsa -out key.pem 4096), "
                f"or the PEM key itself")
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


# --- Staging: the files a deployment publishes -------------------------------------

def normalise(path):
    """A relative POSIX path without ./ and trailing slashes: '' is the repository root."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts)


def under(path, folder):
    return folder == "" or path == folder or path.startswith(folder + "/")


def preserved(path, patterns):
    """Whether path, or a folder it's in, matches one of the regexes."""
    parts = path.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return any(p.search(c) for p in patterns for c in candidates)


def same_file(a, b):
    return filecmp.cmp(a, b, shallow=False) and (os.stat(a).st_mode & 0o111) == (os.stat(b).st_mode & 0o111)


class Staged:
    """The files to publish: {relative path: source file}, and where they came from."""

    def __init__(self, folders, exclude):
        self.files = {}
        self.owners = {}
        self.folders = folders    # the folder of every deployed target
        self.exclude = exclude    # compiled `exclude` regexes
        self.hold = []            # folders shared with a target that isn't deployed: nothing is pruned there

    def excluded(self, path):
        """Whether a path of the published repository is left out by `exclude` (matched inside its target's folder)."""
        for folder in self.folders:
            if under(path, folder):
                inner = path[len(folder) + 1:] if folder else path
                if any(p.search(inner) for p in self.exclude):
                    return True
        return False

    def prunable(self, path, preserve=(), protected=()):
        """Whether a published file that no target has any more can be deleted."""
        return not (path in self.files or preserved(path, preserve) or self.excluded(path)
                    or any(under(path, normalise(folder)) for folder in list(protected) + self.hold))


def stage_targets(sources, exclude=(), skipped_folders=()):
    """Collect what a deployment publishes. sources: [(target name, output folder, destination folder)].

    `exclude` regexes are matched against paths inside each target's output.
    Targets may share a destination: their identical files merge, and a file
    they have with different content is an error.
    """
    patterns = [re.compile(p) for p in exclude or ()]
    folders = sorted({normalise(destination) for _, _, destination in sources})
    staged = Staged(folders, patterns)
    for name, source, destination in sources:
        destination = normalise(destination)
        for root, dirs, files in os.walk(source):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for file in sorted(files):
                path = os.path.join(root, file)
                inner = normalise(os.path.relpath(path, source))
                if any(p.search(inner) for p in patterns):
                    continue
                relative = f"{destination}/{inner}" if destination else inner
                if relative in staged.files:
                    if same_file(staged.files[relative], path):
                        continue
                    raise DeployError(f"{staged.owners[relative]} and {name} both have {relative}, with different "
                                      "content: give every target its own folder ({target} in 'path'), or leave "
                                      "the file out with 'exclude'")
                staged.files[relative] = path
                staged.owners[relative] = name
    staged.hold = sorted({normalise(f) for f in skipped_folders} & set(folders))
    return staged


def write_staged(staged, folder):
    """Copy the staged files into an empty folder (for signing), returning it."""
    if os.path.exists(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    for relative, source in staged.files.items():
        target = os.path.join(folder, *relative.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(source, target)
        shutil.copymode(source, target)
    return folder


def check_patterns(job, key, errors):
    """A list of regexes, like `preserve` and `exclude`."""
    value = job.config.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        errors.append(f"{job.key(key)} must be a list of regular expressions")
        return
    for pattern in value:
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"{job.key(key)} has an invalid regular expression {pattern!r}: {e}")


# --- Distribution hooks ------------------------------------------------------------
#
# What depends on the repository format lives in hooks/<distribution>.py, which
# run() loads for the job's distribution (load_hook()) as `job.hook`. Every hook
# defines:
#   SIGNING                       "gpg", "rsa" or None: the key (`gpg-key`, `rsa-key`) its repositories are signed with
#   OPTIONS                       the other signing keys of the deployment block it reads (e.g. ("key-name",))
#   check(job, errors)            its own checks of the deployment block, appended to errors
#   sign(job, folder, published)  signs the repositories in folder; see sign_repository()
# Hooks `import lib` to use the helpers below (this module, whatever its name).

HOOKS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks")
HOOK_API = ("SIGNING", "OPTIONS", "check", "sign")
HOOK_OPTIONS = ("key-name", "signed-by")  # what a hook's OPTIONS may name
HOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SIGNING_KEYS = {"gpg": "gpg-key", "rsa": "rsa-key"}
_hooks = {}


def load_hook(distribution):
    """The deployment hook of a distribution: hooks/<distribution>.py, imported once."""
    if distribution in _hooks:
        return _hooks[distribution]
    path = os.path.join(HOOKS_PATH, f"{distribution}.py")
    if not HOOK_NAME_RE.match(str(distribution)) or not os.path.isfile(path):
        raise DeployError(f"there's no deployment hook for the distribution '{distribution}' (expected {path})")
    spec = importlib.util.spec_from_file_location(f"pkggen_deployment_hook_{distribution.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    # The hook's `import lib` is this module, also when it was loaded under another name (as the tests do)
    saved = sys.modules.get("lib")
    sys.modules["lib"] = sys.modules[__name__]
    try:
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("lib", None)
        else:
            sys.modules["lib"] = saved
    missing = [name for name in HOOK_API if not hasattr(module, name)]
    if missing:
        raise DeployError(f"the deployment hook {path} doesn't define {', '.join(missing)}")
    if module.SIGNING not in (None, *SIGNING_KEYS):
        raise DeployError(f"the deployment hook {path} has SIGNING = {module.SIGNING!r}: use 'gpg', 'rsa' or None")
    unknown = sorted(set(module.OPTIONS) - set(HOOK_OPTIONS))
    if unknown:
        raise DeployError(f"the deployment hook {path} has unknown OPTIONS: {', '.join(unknown)} (add them to "
                          "lib.HOOK_OPTIONS)")
    _hooks[distribution] = module
    return module


def read_relative(folder, relative):
    with open(os.path.join(folder, *relative.split("/")), "rb") as f:
        return f.read()


def write_relative(folder, relative, data):
    with open(os.path.join(folder, *relative.split("/")), "wb") as f:
        f.write(data)


# --- Signing repository metadata with GPG -----------------------------------------
#
# The hooks say which metadata files to sign (apt's dists/<suite>/Release, dnf's
# repodata/repomd.xml); sign_gpg() signs them on the host with the gpg command,
# so the key never enters the test containers. A signature is only made again
# when the file it signs changed, so unchanged repositories stay unchanged.

FINGERPRINT_RE = re.compile(r"^(0x)?([0-9A-Fa-f]{16}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})$")
ARMORED_KEY = "-----BEGIN PGP PRIVATE KEY BLOCK-----"


def gpg_binary():
    path = shutil.which("gpg") or shutil.which("gpg2")
    if not path:
        raise DeployError("signing repositories needs GnuPG (the gpg command). Install it, or remove 'gpg-key'")
    return path


class Signer:
    """Signs with the deployment's `gpg-key`, in a GnuPG home pkggen owns unless it's a fingerprint."""

    def __init__(self, job, nickname):
        self.job = job
        self.gpg = gpg_binary()
        value = lookup_secret("gpg-key", nickname)
        problem = secret_problem("gpg-key", nickname)
        if problem:
            raise DeployError(f"{job.key('gpg-key')}: {problem}")
        passphrase_nick = job.config.get("gpg-passphrase") or nickname
        self.passphrase = lookup_secret("gpg-passphrase", passphrase_nick)
        self.home = None
        value = value.strip()
        if FINGERPRINT_RE.match(value):
            self.key = value[2:] if value.lower().startswith("0x") else value
        else:
            armored = value if ARMORED_KEY in value else open(os.path.expanduser(value), encoding="utf-8").read()
            os.makedirs(job.work_path, exist_ok=True)
            self.home = tempfile.mkdtemp(prefix="gnupg-", dir=job.work_path)
            os.chmod(self.home, 0o700)
            self.run(["--import"], data=armored, passphrase=False)
            self.key = self.fingerprint()

    def env(self):
        env = dict(os.environ)
        if self.home:
            env["GNUPGHOME"] = self.home
        return env

    def run(self, args, data=None, passphrase=True):
        options = ["--batch", "--yes", "--no-tty"]
        if passphrase and self.passphrase is not None:
            # The passphrase comes first on stdin, the data (if any) is in files
            options += ["--pinentry-mode", "loopback", "--passphrase-fd", "0"]
            data = self.passphrase + "\n" + (data or "")
        process = subprocess.run([self.gpg, *options, *args], input=data, capture_output=True, text=True,
                                 env=self.env())
        if process.returncode != 0:
            lines = [line for line in process.stderr.strip().splitlines() if line.strip()]
            raise DeployError(f"gpg {' '.join(args[:2])} failed: {lines[-1] if lines else process.returncode}")
        return process.stdout

    def fingerprint(self):
        output = self.run(["--with-colons", "--list-secret-keys"], passphrase=False)
        for line in output.splitlines():
            fields = line.split(":")
            if fields[0] == "fpr" and len(fields) > 9:
                return fields[9]
        raise DeployError(f"{self.job.key('gpg-key')} has no private key")

    def public_key(self):
        return self.run(["--armor", "--export", self.key], passphrase=False)

    def sign(self, path, output, kind):
        if kind == "public-key":
            with open(output, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.public_key())
            return
        # Armored, like reprepro's Release.gpg: text files are easier to keep in git
        mode = {"clearsign": ["--clearsign"], "detach": ["--detach-sign", "--armor"]}[kind]
        self.run(["--local-user", self.key, "--digest-algo", "SHA512", *mode, "--output", output, path])

    def close(self):
        if self.home:
            # Stop the gpg-agent that gpg started for this home before deleting it
            if shutil.which("gpgconf"):
                subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=self.env(), capture_output=True,
                               stdin=subprocess.DEVNULL)
            shutil.rmtree(self.home, ignore_errors=True)


def sign_gpg(job, folder, published, metadata, what):
    """Sign the metadata files in folder with the deployment's `gpg-key`, for a hook's sign().

    metadata: [(file, [(signature file, kind)])] as relative POSIX paths, kind
    being "clearsign", "detach" or "public-key" (the armored public key). what
    names the files in the warning when there are none.
    """
    nickname = job.config["gpg-key"]
    added = []
    signer = None

    def get_signer():
        nonlocal signer
        signer = signer or Signer(job, nickname)
        return signer

    try:
        for base, signatures in metadata:
            previous = [published(signature) for signature, _ in signatures]
            if published(base) == read_relative(folder, base) and all(p is not None for p in previous):
                for (signature, _), data in zip(signatures, previous):
                    write_relative(folder, signature, data)
            else:
                for signature, kind in signatures:
                    get_signer().sign(os.path.join(folder, *base.split("/")),
                                      os.path.join(folder, *signature.split("/")), kind)
            added += [signature for signature, _ in signatures]
        return finish_signing(job, folder, added, what, lambda: get_signer().public_key())
    finally:
        if signer:
            signer.close()


def finish_signing(job, folder, added, what, public_key):
    """End a hook's sign(): warn when nothing was signed, write `public-key` (public_key() is its text)."""
    if not added:
        key = SIGNING_KEYS[job.hook.SIGNING]
        job.warn(f"{job.key(key)} is set, but there's no repository to sign ({what})")
    if job.config.get("public-key"):
        added.append(write_public_key(job, folder, public_key()))
    return added


def write_public_key(job, folder, text):
    """Write the public key to `public-key` (relative to the repository's root). Returns its path."""
    public = normalise(job.config["public-key"])
    target = os.path.join(folder, *public.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return public


def signs(job):
    """Whether the deployment signs its repositories (`gpg-key` or `rsa-key`)."""
    return bool(job.config.get("gpg-key") or job.config.get("rsa-key"))


def sign_repository(job, folder, published):
    """Sign the repository metadata in folder, and write the public key to `public-key`.

    The distribution's hook does the signing: its SIGNING says which key it
    uses (`gpg-key` for apt and dnf metadata, `rsa-key` for Alpine's and Void's
    packages and indexes). published(path) returns the bytes now published at
    a relative path, or None: a file that didn't change keeps its published
    signatures, so re-deploying an unchanged repository changes nothing.
    Returns the relative paths of the files it added or re-signed.
    """
    if not signs(job):
        return []
    return job.hook.sign(job, folder, published)


def check_signing(job, errors):
    """The checks of `gpg-key`, `gpg-passphrase`, `rsa-key`, `rsa-passphrase`, `key-name`, `signed-by`, `public-key`.

    The generic ones are here; the distribution's hook adds its own.
    """
    nickname = check_string(job, "gpg-key", errors)
    check_string(job, "gpg-passphrase", errors)
    rsa = check_string(job, "rsa-key", errors)
    check_string(job, "rsa-passphrase", errors)
    for key in HOOK_OPTIONS:
        check_string(job, key, errors)
    public = check_string(job, "public-key", errors)
    try:
        hook = job.hook
    except DeployError as e:
        errors.append(str(e))
        return
    signing_key = SIGNING_KEYS.get(hook.SIGNING)
    for kind, key in SIGNING_KEYS.items():
        if job.config.get(key) and key != signing_key:
            how = f"they're signed with {job.key(signing_key)}" if signing_key else "pkggen doesn't sign them"
            errors.append(f"{job.key(key)} doesn't apply to {job.distribution} repositories: {how}")
        passphrase = f"{kind}-passphrase"
        if job.config.get(passphrase) and not job.config.get(key):
            errors.append(f"{job.key(passphrase)} needs {job.key(key)}")
    for key in HOOK_OPTIONS:
        if not job.config.get(key):
            continue
        if key not in hook.OPTIONS:
            errors.append(f"{job.key(key)} doesn't apply to {job.distribution} repositories")
        elif signing_key and not job.config.get(signing_key):
            errors.append(f"{job.key(key)} needs {job.key(signing_key)}")
    if public and (".." in normalise(public).split("/") or os.path.isabs(public)):
        errors.append(f"{job.key('public-key')} must be a path inside the repository, got {public!r}")
    if public and not signs(job):
        if signing_key:
            errors.append(f"{job.key('public-key')} needs {job.key(signing_key)}: it's the public half of that key")
        else:
            errors.append(f"{job.key('public-key')} doesn't apply to {job.distribution} repositories: pkggen "
                          "doesn't sign them")
    if nickname:
        problem = secret_problem("gpg-key", nickname)
        if problem:
            errors.append(f"{job.key('gpg-key')}: {problem}")
        if not (shutil.which("gpg") or shutil.which("gpg2")):
            errors.append(f"{job.key('gpg-key')} needs GnuPG (the gpg command), which isn't installed")
    if rsa:
        problem = secret_problem("rsa-key", rsa)
        if problem:
            errors.append(f"{job.key('rsa-key')}: {problem}")
        if not have_cryptography():
            errors.append(f"{job.key('rsa-key')} needs the Python package 'cryptography' (pip install cryptography)")
    hook.check(job, errors)


# --- Signing with RSA ------------------------------------------------------------------
#
# Alpine and Void sign with plain RSA keys (PKCS#1 v1.5). Their hooks sign on
# the host with RsaSigner, which uses the Python package cryptography, so the
# key never enters the test containers.

PEM_MARKER = "-----BEGIN"


def have_cryptography():
    try:
        import cryptography.hazmat.primitives.asymmetric.rsa  # noqa: F401
    except ImportError:
        return False
    return True


class RsaSigner:
    """Signs with the deployment's `rsa-key`: a PEM private key (path or text), with `rsa-passphrase` if it has one."""

    def __init__(self, job, nickname):
        if not have_cryptography():
            raise DeployError(f"{job.key('rsa-key')} needs the Python package 'cryptography' (pip install cryptography)")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        problem = secret_problem("rsa-key", nickname)
        if problem:
            raise DeployError(f"{job.key('rsa-key')}: {problem}")
        value = lookup_secret("rsa-key", nickname).strip()
        if PEM_MARKER in value:
            data = value.encode("utf-8")
        else:
            with open(os.path.expanduser(value), "rb") as f:
                data = f.read()
        passphrase = lookup_secret("rsa-passphrase", job.config.get("rsa-passphrase") or nickname)
        try:
            key = serialization.load_pem_private_key(data, passphrase.encode("utf-8") if passphrase else None)
        except (ValueError, TypeError) as e:
            hint = "" if passphrase else f" (if it has a passphrase, set {job.key('rsa-passphrase')})"
            raise DeployError(f"{job.key('rsa-key')}: couldn't load the RSA key '{nickname}': {e}{hint}")
        if not isinstance(key, rsa.RSAPrivateKey):
            raise DeployError(f"{job.key('rsa-key')}: '{nickname}' isn't an RSA key")
        self.key = key
        self.nickname = nickname

    def sign(self, data, digest="sha256"):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        algorithm = {"sha1": hashes.SHA1, "sha256": hashes.SHA256}[digest]()
        return self.key.sign(data, padding.PKCS1v15(), algorithm)

    def public_key(self):
        """The public key in PEM (SubjectPublicKeyInfo): Alpine's .rsa.pub files and xbps' index-meta.plist."""
        from cryptography.hazmat.primitives import serialization
        return self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")

    @property
    def bits(self):
        return self.key.key_size


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
        job.hook  # load the distribution's hook up front: a missing one is an error in every mode
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
