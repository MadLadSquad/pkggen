#!/usr/bin/env python3
# `pkggen gen-repo <dist>`: run the distribution's repository generator once,
# then copy the optional `repositories.<dist>.seed-files` folder into the result.
import os
import json
import shutil
from . import utils
import subprocess
import sys

SEED_KEY = "seed-files"

def get_repo_generators_path():
    return os.path.join(utils.get_generators_path(), "repositories")

def get_distributions():
    # Only offer distributions that also have a repository generator
    repo_generators_path = get_repo_generators_path()
    return [
        dist["name"] for dist in utils.load_distributions()
        if os.path.exists(os.path.join(repo_generators_path, f"{dist['name']}.py"))
    ]

def seed_folder(distribution, config):
    """The absolute `repositories.<dist>.seed-files` folder, or None. Fails if it's set but missing."""
    block = ((config or {}).get("repositories") or {}).get(distribution)
    value = block.get(SEED_KEY) if isinstance(block, dict) else None
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise utils.GenericError(f"'repositories.{distribution}.{SEED_KEY}' must be the path of a folder")
    path = os.path.abspath(os.path.join(utils.get_run_path(), os.path.expanduser(value)))
    if not os.path.isdir(path):
        raise utils.GenericError(f"'repositories.{distribution}.{SEED_KEY}' is {value}, but "
                                 f"{utils.display_path(path)} isn't a folder")
    return path


def copy_seed_files(source, destination, force):
    """Copy every file under source into destination, byte for byte.

    Existing files are kept unless force: like the repository generators, the
    repository is the user's once it exists. Returns (copied, kept) counts.
    """
    copied = kept = 0
    for directory, dirs, files in os.walk(source):
        dirs.sort()
        for name in sorted(files):
            src = os.path.join(directory, name)
            rel = os.path.relpath(src, source)
            dst = os.path.join(destination, rel)
            if os.path.exists(dst) and not force:
                kept += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return copied, kept


def gen_repo(distribution, output_dir=None, force=False):
    generator_path = os.path.join(get_repo_generators_path(), f"{distribution}.py")
    if not os.path.exists(generator_path):
        raise utils.GenericError(f"Couldn't find repository generator for {distribution}!")

    pkggen = utils.get_pkggen_config()
    if pkggen is None:
        raise utils.GenericError("Couldn't load pkggen.yaml from the default path!")

    seeds = seed_folder(distribution, pkggen)
    cwd = output_dir if output_dir else utils.get_repo_path(distribution, pkggen)
    os.makedirs(cwd, exist_ok=True)
    print(f"Generating the {distribution} repository in {utils.display_path(cwd)}", file=sys.stderr)

    # Generators report progress on stderr, so stdout is not captured
    result = subprocess.run(
        [sys.executable, generator_path],
        input=json.dumps(pkggen),
        text=True,
        cwd=cwd,
        env={**os.environ, "PKGGEN_FORCE": "1" if force else "0"},
    )

    if result.returncode != 0:
        error_msg = (
            f"\nError encountered when running the repository generator for {distribution}\n"
            f"Generator: {generator_path}\n"
            f"Exit code: {result.returncode}\n"
        )
        print(error_msg, file=sys.stderr)
        raise utils.GenericError(f"Errors encountered when running the generator for {distribution}!")

    if seeds:
        copied, kept = copy_seed_files(seeds, cwd, force)
        message = f"Copied {copied} seed file{'' if copied == 1 else 's'} from {utils.display_path(seeds)}"
        if kept:
            message += f" ({kept} already there, kept: use --force to overwrite them)"
        print(message, file=sys.stderr)
