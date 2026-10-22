#!/usr/bin/env python3
# Pushes every tested PKGBUILD folder (pkggen-build/test/arch/<name>/) to its
# own AUR repository, one package after the other.
#
# The AUR only accepts SSH pushes, so pkggen makes an SSH key called 'aur' on
# first use (unless `ssh-key` names one) and asks the user to add it to their
# AUR account. Repositories are fetched anonymously over https.
import importlib.util
import os
import re

import lib


def load_git_generator():
    """The git deployment generator next to this file (by path: `import git` could find GitPython)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git.py")
    spec = importlib.util.spec_from_file_location("pkggen_deployment_git", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


git = load_git_generator()

KEYS = {"generator", "username", "email", "ssh-key", "managed-key", "targets", "prune", "preserve", "message",
        "sign"}
BRANCH = "master"  # the only branch the AUR accepts
DEFAULT_MESSAGE = "Update to {version}"
NAME_RE = re.compile(r"^[a-z0-9@._+-]+$")


def urls(pkgbase):
    """(fetch URL, push URL). PKGGEN_TEST_AUR_URL (with {pkgbase}) replaces both in tests."""
    test = os.getenv("PKGGEN_TEST_AUR_URL")
    if test:
        url = test.replace("{pkgbase}", pkgbase)
        return url, url
    return f"https://aur.archlinux.org/{pkgbase}.git", f"ssh://aur@aur.archlinux.org/{pkgbase}.git"


def read_srcinfo(path):
    """The pkgbase block of a .SRCINFO as {key: [values]}."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "pkgname":
                break  # the split packages' blocks follow
            values.setdefault(key, []).append(value)
    return values


def full_version(srcinfo):
    version = f"{srcinfo['pkgver'][0]}-{(srcinfo.get('pkgrel') or ['1'])[0]}"
    epoch = (srcinfo.get("epoch") or [None])[0]
    return f"{epoch}:{version}" if epoch else version


def package_folders(target):
    """{folder name: path} of the PKGBUILD folders in a target's output (not the pacman repository)."""
    folders = {}
    for entry in sorted(os.scandir(target.path), key=lambda e: e.name):
        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "PKGBUILD")):
            folders[entry.name] = entry.path
    return folders


def check_folder(path):
    """Why the AUR would reject this folder, or None. Returns (problem, srcinfo)."""
    srcinfo_path = os.path.join(path, ".SRCINFO")
    if not os.path.isfile(srcinfo_path):
        return (".SRCINFO is missing, and the AUR rejects pushes without it. Add a .SRCINFO.tmpl to the "
                "package's templates (packages/arch/<name>/), like the default one"), None
    folders = sorted(e.name for e in os.scandir(path) if e.is_dir())
    if folders:
        return f"the AUR doesn't accept folders, but it has {', '.join(folders)}", None
    srcinfo = read_srcinfo(srcinfo_path)
    if not srcinfo.get("pkgbase") or not srcinfo.get("pkgver"):
        return ".SRCINFO has no pkgbase or pkgver", None
    if not NAME_RE.match(srcinfo["pkgbase"][0]):
        return f"{srcinfo['pkgbase'][0]!r} isn't a valid AUR package name", None
    return None, srcinfo


def key_hint(job):
    user = job.config.get("username") or "<you>"
    return (f"to your AUR account (https://aur.archlinux.org/account/{user}/edit, under "
            "'SSH Public Key', then save with your password)")


def check(job):
    errors = []
    rejected = ("url", "branch", "password", "path")
    git.check_common(job, KEYS, errors, rejected)
    for key in rejected:
        if key in job.config:
            errors.append(f"{job.key(key)} can't be set for AUR deployments" +
                          (": the AUR only accepts SSH keys" if key == "password" else ""))
    return errors


def main(job):
    config = job.config
    targets = job.targets
    if not targets:
        raise lib.DeployError("nothing to deploy: the arch target has no test output")
    if len(targets) > 1:
        job.warn(f"the AUR gets the PKGBUILDs of {targets[0].name}, the other targets are ignored")
    target = targets[0]

    folders = package_folders(target)
    if job.packages:
        missing = [p for p in job.packages if p not in folders]
        for name in missing:
            job.skipped.append({"name": name, "reason": f"{target.name} has no PKGBUILD folder for it"})
        folders = {name: path for name, path in folders.items() if name in job.packages}

    chosen = {}
    for name, path in folders.items():
        status = target.packages.get(name)
        if not job.skip_tests and not (status and status.get("ok")):
            reason = "it failed its tests on " + target.name if status else f"it wasn't tested on {target.name}"
            job.skipped.append({"name": name, "reason": f"{reason} (--skip-tests deploys it anyway)"})
            continue
        chosen[name] = path
    if not chosen:
        if not job.skipped:
            raise lib.DeployError(f"{target.name} has no PKGBUILD folders to deploy")
        return

    # One key for every package. A new key is announced once, before the first push
    env = None
    for name, path in chosen.items():
        problem, srcinfo = check_folder(path)
        if problem:
            job.failed.append({"name": name, "message": problem})
            job.say(f"✗ {name}: {problem}")
            continue
        pkgbase = srcinfo["pkgbase"][0]
        version = full_version(srcinfo)
        fetch_url, push_url = urls(pkgbase)
        try:
            if env is None:
                env = git.auth_env(job, push_url, default_key="aur", hint=key_hint(job))
            message = (config.get("message") or DEFAULT_MESSAGE).replace("{version}", version).replace("{name}", pkgbase)
            sources = [(name, path, "", target.executables_in(name))]
            outcome = git.deploy_tree(job, f"{pkgbase} {version}", os.path.join(job.work_path, pkgbase),
                                      fetch_url, push_url, BRANCH, sources, message, env)
        except lib.DeployError as e:
            if env is None:
                raise  # no key: no package can be pushed
            job.failed.append({"name": name, "message": str(e)})
            job.say(f"✗ {name}: {str(e).splitlines()[0]}")
            continue
        (job.pushed if outcome == "pushed" else job.unchanged).append(name)


if __name__ == "__main__":
    lib.run(main, check)
