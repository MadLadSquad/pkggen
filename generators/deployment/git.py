#!/usr/bin/env python3
# Pushes the repositories `pkggen test` built (pkggen-build/test/<target>/) to a
# git remote, each target in its own folder (`path`, default `{target}`).
#
# The upstream is fetched into pkggen-build/deploy/<id>/, the new files are put
# on top of it (overwriting changed files), and the result is committed and
# pushed. Files that only exist upstream are kept unless `prune` is set;
# `preserve` regexes exempt paths from pruning. Executable bits come from the
# test containers' list when there is one, so they survive file systems
# without them (NTFS, where Git for Windows has core.filemode=false).
#
# The AUR generator imports this module, so everything here is reusable.
import filecmp
import os
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

import lib

KEYS = {"generator", "url", "branch", "username", "email", "ssh-key", "password", "managed-key", "targets",
        "path", "prune", "preserve", "message", "sign"}
DEFAULT_BRANCH = "main"
DEFAULT_PATH = "{target}"

SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
# user@host:path or host:path, but not C:\path (a Windows drive)
SCP_RE = re.compile(r"^(?:[^@/\\]+@)?([^@/\\:]{2,}):(?!//)")


class GitError(lib.DeployError):
    pass


# --- URLs and authentication ---------------------------------------------------

def url_kind(url):
    """'ssh', 'https', 'file' (a local path or file://) or 'other' (git://, ...)."""
    match = SCHEME_RE.match(url)
    if match:
        scheme = match.group(1).lower()
        if scheme in ("ssh", "git+ssh", "ssh+git"):
            return "ssh"
        if scheme in ("https", "http"):
            return "https"
        if scheme == "file":
            return "file"
        return "other"
    return "ssh" if SCP_RE.match(url) else "file"


def url_host(url):
    if SCHEME_RE.match(url):
        return urlsplit(url).hostname or ""
    match = SCP_RE.match(url)
    return match.group(1) if match else ""


def url_user(url):
    if SCHEME_RE.match(url):
        user = urlsplit(url).username
        return unquote(user) if user else None
    return None


def shquote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def ssh_command(key, interactive):
    """GIT_SSH_COMMAND that uses only `key`. git runs it with its own sh, also on Windows."""
    command = (f"ssh -i {shquote(key.replace(os.sep, '/'))} -o IdentitiesOnly=yes "
               "-o StrictHostKeyChecking=accept-new")
    if not interactive:
        command += " -o BatchMode=yes"
    return command


# Prints the credentials from the environment, so the password is never on a command line or on disk
CREDENTIAL_HELPER = ('!f() { test "$1" = get || return 0; printf "username=%s\\npassword=%s\\n" '
                     '"$PKGGEN_GIT_USERNAME" "$PKGGEN_GIT_PASSWORD"; }; f')


def credential_env(username, password):
    return {
        # An empty helper clears the user's own helpers, so they neither answer nor store these credentials
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": CREDENTIAL_HELPER,
        "PKGGEN_GIT_USERNAME": username,
        "PKGGEN_GIT_PASSWORD": password,
    }


def managed_key_hint(url):
    host = url_host(url) or "the git server"
    return (f"to the account (or as a deploy key with write access, to the repository) that {url} belongs to "
            f"on {host}")


def auth_env(job, url, default_key=None, hint=None):
    """The environment git needs to fetch from and push to url."""
    config = job.config
    kind = url_kind(url)
    env = {} if job.interactive else {"GIT_TERMINAL_PROMPT": "0"}
    if kind == "https" and config.get("password"):
        password = lib.secret_password(config["password"])
        env.update(credential_env(url_user(url) or config.get("username") or "git", password))
    elif kind == "ssh":
        if config.get("ssh-key"):
            key = lib.secret_ssh_key(config["ssh-key"])
        else:
            name = config.get("managed-key") or default_key or f"git-{url_host(url) or 'remote'}"
            key = lib.managed_key(job, name, hint or managed_key_hint(url))
        env["GIT_SSH_COMMAND"] = ssh_command(key, job.interactive)
    return env


# --- Running git -------------------------------------------------------------------

# Push the tested files as they are: Git for Windows sets core.autocrlf=true system-wide,
# which would rewrite line endings, and without core.longpaths it can't add files whose
# path is over 260 characters, like createrepo_c's repodata/<sha256>-filelists.xml.zst
GIT_OPTIONS = ["-c", "core.autocrlf=false", "-c", "core.longpaths=true"]


def git(args, cwd=None, env=None, allow_failure=False):
    """Run git and return its stdout. Failures raise GitError with git's last lines."""
    process = subprocess.run(["git", *GIT_OPTIONS, *args], cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if process.returncode != 0 and not allow_failure:
        lines = [line.strip() for line in process.stderr.strip().splitlines() if line.strip()]
        command = next((a for a in args if not a.startswith("-") and "=" not in a), "")
        # The last line says what went wrong; the ones before it why
        message = f"git {command} failed: {lines[-1] if lines else f'exit code {process.returncode}'}"
        raise GitError("\n".join([message] + lines[-6:-1]))
    return process.stdout if process.returncode == 0 else None


def default_branch(work, env):
    """The remote's default branch, else main."""
    output = git(["ls-remote", "--symref", "origin", "HEAD"], cwd=work, env=env, allow_failure=True) or ""
    for line in output.splitlines():
        match = re.match(r"^ref:\s+refs/heads/(\S+)\s+HEAD$", line)
        if match:
            return match.group(1)
    return DEFAULT_BRANCH


def remote_commit(work, branch):
    output = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}"], cwd=work,
                 allow_failure=True)
    return output.strip() if output else None


def prepare(work, fetch_url, push_url, branch, env):
    """Make work a clean checkout of the remote branch (an empty one for a new repository).

    Returns (branch, the remote commit or None). work is pkggen's own clone, so
    whatever is left in it from earlier runs is thrown away.
    """
    if not os.path.isdir(os.path.join(work, ".git")):
        if os.path.exists(work):
            shutil.rmtree(work)
        os.makedirs(work)
        git(["init", "-q"], cwd=work)
        git(["remote", "add", "origin", fetch_url], cwd=work)
    else:
        git(["remote", "set-url", "origin", fetch_url], cwd=work)
    git(["config", "--unset-all", "remote.origin.pushurl"], cwd=work, allow_failure=True)
    if push_url != fetch_url:
        git(["remote", "set-url", "--push", "origin", push_url], cwd=work)

    git(["fetch", "-q", "--prune", "origin"], cwd=work, env=env)
    branch = branch or default_branch(work, env)
    base = remote_commit(work, branch)
    if base:
        git(["checkout", "-q", "-f", "-B", branch, base], cwd=work)
    else:
        # A new branch without history
        git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=work)
        git(["update-ref", "-d", f"refs/heads/{branch}"], cwd=work, allow_failure=True)
        git(["read-tree", "--empty"], cwd=work)
    git(["clean", "-q", "-f", "-d", "-x"], cwd=work)
    return branch, base


# --- Putting the new files on top ---------------------------------------------------

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


def sync(work, sources, prune=False, preserve=(), protected=()):
    """Copy every file of the sources [(folder, destination[, executables])] into work, overwriting changed files.

    With prune, files in work that no source has are deleted, except .git,
    paths matching a preserve regex and paths under a protected folder.
    Returns (written, removed), lists of relative paths.
    """
    patterns = [re.compile(p) for p in preserve]
    produced = set()
    written = []
    for source, destination, *_ in sources:
        destination = normalise(destination)
        for root, dirs, files in os.walk(source):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted(files):
                relative = normalise(os.path.join(destination, os.path.relpath(os.path.join(root, name), source)))
                produced.add(relative)
                target = os.path.join(work, *relative.split("/"))
                if os.path.isfile(target) and filecmp.cmp(os.path.join(root, name), target, shallow=False) \
                        and (os.stat(target).st_mode & 0o111) == (os.stat(os.path.join(root, name)).st_mode & 0o111):
                    continue
                make_parents(work, relative)
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                shutil.copyfile(os.path.join(root, name), target)
                shutil.copymode(os.path.join(root, name), target)
                written.append(relative)

    removed = []
    if prune:
        keep_folders = [normalise(p) for p in protected]
        for root, dirs, files in os.walk(work, topdown=True):
            if os.path.abspath(root) == os.path.abspath(work):
                dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                relative = normalise(os.path.relpath(os.path.join(root, name), work))
                if relative in produced or preserved(relative, patterns) \
                        or any(under(relative, folder) for folder in keep_folders):
                    continue
                os.remove(os.path.join(root, name))
                removed.append(relative)
        # Folders the deletions left empty
        for root, dirs, files in os.walk(work, topdown=False):
            relative = normalise(os.path.relpath(root, work))
            if relative and relative != ".git" and not relative.startswith(".git/") and not os.listdir(root):
                os.rmdir(root)
    return written, sorted(removed)


def executable_modes(sources):
    """{path in work: executable?} for the files of the sources that come with a set of executables.

    A source's executables are the paths (relative to its folder) that were
    executable in the test container; sources without that set (None) are left
    to the file system.
    """
    modes = {}
    for source, destination, *rest in sources:
        executables = rest[0] if rest else None
        if executables is None:
            continue
        destination = normalise(destination)
        for root, dirs, files in os.walk(source):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                inner = normalise(os.path.relpath(os.path.join(root, name), source))
                modes[normalise(f"{destination}/{inner}")] = inner in executables
    return modes


def make_parents(work, relative):
    """Create the folders of relative inside work, replacing files that are in the way."""
    current = work
    for part in relative.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.isfile(current) or os.path.islink(current):
            os.remove(current)
        if not os.path.isdir(current):
            os.mkdir(current)


# --- Committing and pushing ---------------------------------------------------------

def set_modes(work, modes):
    """Make the staged files' executable bits match modes {path: executable?}.

    git add takes them from the file system, which on Windows (core.filemode=false)
    has none: new files would be 100644 and existing ones keep their old mode.
    """
    if not modes:
        return
    changes = {"+x": [], "-x": []}
    for entry in git(["ls-files", "--stage", "-z"], cwd=work).split("\0"):
        info, _, path = entry.partition("\t")
        mode = info.split(" ")[0]
        if modes.get(path) is True and mode == "100644":
            changes["+x"].append(path)
        elif modes.get(path) is False and mode == "100755":
            changes["-x"].append(path)
    for flag, paths in changes.items():
        for i in range(0, len(paths), 100):  # keep the command line short (Windows)
            git(["update-index", f"--chmod={flag}", "--", *paths[i:i + 100]], cwd=work)


def commit(work, username, email, message, sign=False, modes=None):
    """Commit everything in work, with the executable bits of modes. Returns False when nothing changed.

    Commits aren't signed unless sign is set: signing keys that ask for a
    passphrase or a touch can't do it from here.
    """
    git(["add", "-A"], cwd=work)
    set_modes(work, modes)
    if not git(["status", "--porcelain"], cwd=work).strip():
        return False
    options = ["-c", f"user.name={username}", "-c", f"user.email={email}"]
    if not sign:
        options += ["-c", "commit.gpgsign=false"]
    git([*options, "commit", "-q", "-m", message], cwd=work)
    return True


def change_summary(work):
    """git's '3 files changed, 10 insertions(+)' line for the last commit."""
    output = git(["show", "--stat", "--format=", "HEAD"], cwd=work, allow_failure=True) or ""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "no changes"


def deploy_tree(job, name, work, fetch_url, push_url, branch, sources, message, env, protected=()):
    """Fetch the upstream, put sources on top, commit and push. Returns 'pushed' or 'unchanged'.

    When the upstream moves while pushing, everything is done again once on
    top of the new upstream. Pushes are never forced.
    """
    config = job.config
    for attempt in (1, 2):
        branch, base = prepare(work, fetch_url, push_url, branch, env)
        _, removed = sync(work, sources, bool(config.get("prune")), config.get("preserve") or (), protected)
        if not commit(work, config["username"], config["email"], message, bool(config.get("sign")),
                      executable_modes(sources)):
            job.say(f"{name}: already up to date")
            return "unchanged"
        summary = change_summary(work)
        if removed:
            summary += f" ({len(removed)} removed by 'prune')"
        if job.dry_run:
            job.say(f"{name}: would push {summary} to {branch} (dry run, committed in {work})")
            return "pushed"
        job.say(f"{name}: pushing {summary} to {branch}")
        try:
            git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=work, env=env)
            return "pushed"
        except GitError:
            git(["fetch", "-q", "origin"], cwd=work, env=env, allow_failure=True)
            if attempt == 2 or remote_commit(work, branch) == base:
                raise
            job.say(f"{name}: the upstream changed while pushing, trying again on top of it")
    return "pushed"


# --- Configuration -----------------------------------------------------------------

def check_common(job, allowed, errors, rejected=()):
    """The checks the git and AUR generators share. rejected keys get their own error from the caller."""
    config = job.config
    lib.check_keys(job, set(allowed) | set(rejected), errors)
    lib.check_string(job, "username", errors, required=True)
    lib.check_string(job, "email", errors, required=True)
    lib.check_string(job, "message", errors)

    ssh_key = lib.check_string(job, "ssh-key", errors) if "ssh-key" in allowed else None
    password = lib.check_string(job, "password", errors) if "password" in allowed else None
    managed = lib.check_string(job, "managed-key", errors)
    if ssh_key and password:
        errors.append(f"set either {job.key('ssh-key')} or {job.key('password')}, not both")
    if managed and (ssh_key or password):
        errors.append(f"{job.key('managed-key')} names the key pkggen makes when neither 'ssh-key' nor 'password' "
                      "is set, so it can't be used with them")
    if managed and not lib.KEY_NAME_RE.match(managed):
        errors.append(f"{job.key('managed-key')} must only contain letters, digits, '.', '_' and '-'")
    for kind, nickname in (("ssh-key", ssh_key), ("password", password)):
        if nickname:
            problem = lib.secret_problem(kind, nickname)
            if problem:
                errors.append(f"{job.key(kind)}: {problem}")

    for key in ("prune", "sign"):
        if key in config and not isinstance(config[key], bool):
            errors.append(f"{job.key(key)} must be true or false")
    preserve = config.get("preserve")
    if preserve is not None:
        if not isinstance(preserve, list) or not all(isinstance(p, str) and p for p in preserve):
            errors.append(f"{job.key('preserve')} must be a list of regular expressions")
        else:
            for pattern in preserve:
                try:
                    re.compile(pattern)
                except re.error as e:
                    errors.append(f"{job.key('preserve')} has an invalid regular expression {pattern!r}: {e}")
        if not config.get("prune"):
            job.warn(f"{job.key('preserve')} only matters with 'prune: true': without it nothing is deleted")
    return ssh_key, password


def target_paths(job, names):
    """{target name: its folder in the upstream repository}."""
    template = job.config.get("path", DEFAULT_PATH)
    return {name: normalise(template.replace("{target}", name)) for name in names}


def check(job):
    errors = []
    config = job.config
    ssh_key, password = check_common(job, KEYS, errors)
    url = lib.check_string(job, "url", errors, required=True)
    lib.check_string(job, "branch", errors)
    if url:
        kind = url_kind(url)
        if password and kind != "https":
            errors.append(f"{job.key('password')} only works with https URLs, but {job.key('url')} is {url}")
        if (ssh_key or config.get("managed-key")) and kind != "ssh":
            errors.append(f"{job.key('ssh-key' if ssh_key else 'managed-key')} only works with SSH URLs "
                          f"(git@host:path or ssh://), but {job.key('url')} is {url}")

    path = config.get("path", DEFAULT_PATH)
    if not isinstance(path, str):
        errors.append(f"{job.key('path')} must be a string like \"{{target}}\"")
    else:
        if ".." in normalise(path).split("/") or os.path.isabs(path) or re.match(r"^[A-Za-z]:", path):
            errors.append(f"{job.key('path')} must be a folder inside the repository, got {path!r}")
        names = [t.name for t in job.targets] + job.skipped_targets
        paths = target_paths(job, names)
        folders = sorted(set(paths.values()))
        if len(paths) > 1 and len(folders) < len(paths):
            errors.append(f"{job.key('path')} puts several targets ({', '.join(names)}) in the same folder: "
                          "add {target} to it")
        else:
            nested = [(a, b) for a in folders for b in folders if a != b and under(b, a)]
            if nested:
                errors.append(f"{job.key('path')} puts one target's folder ({nested[0][1] or 'the root'}) inside "
                              f"another one's ({nested[0][0] or 'the root'}): add {{target}} to it")
    return errors


def main(job):
    config = job.config
    if job.packages:
        job.warn("-p doesn't apply to git deployments: they push whole repositories")
    targets = job.usable_targets()
    if not targets:
        if not job.skipped:
            raise lib.DeployError("nothing to deploy: no target has test output")
        return  # every target failed its tests: they're reported as skipped
    paths = target_paths(job, [t.name for t in targets] + job.skipped_targets + [s["name"] for s in job.skipped])
    url = config["url"]
    sources = [(t.path, paths[t.name], t.executables) for t in targets]
    # Targets that aren't deployed this time keep what's upstream, even with prune
    protected = [paths[name] for name in job.skipped_targets + [s["name"] for s in job.skipped]]
    env = auth_env(job, url)
    message = config.get("message") or f"Update packages ({', '.join(t.name for t in targets)})"
    names = ", ".join(t.name for t in targets)
    job.say(f"deploying {names} to {url}")
    outcome = deploy_tree(job, names, job.work_path, url, url, config.get("branch"), sources, message, env, protected)
    (job.pushed if outcome == "pushed" else job.unchanged).append(job.id)


if __name__ == "__main__":
    lib.run(main, check)
