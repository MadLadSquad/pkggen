#!/usr/bin/env python3
# Shared helpers for repository generators.
#
# Every repository generator receives the full pkggen.yaml as JSON on stdin
# and reads its own block from `repositories.<distribution>`. Files are written
# relative to the current working directory (the gen-repo output directory).
import json
import os
import sys

# Set by `pkggen gen-repo --force`. Without it, existing files are left alone.
FORCE = os.getenv("PKGGEN_FORCE") == "1"


def readinput():
    return sys.stdin.buffer.read()


def warn(message):
    print(f"\x1b[33mWarning: {message}\x1b[0m", file=sys.stderr)


def fail(message):
    print(f"\x1b[31mError: {message}\x1b[0m", file=sys.stderr)
    sys.exit(1)


def load_repo_config(distribution, required=()):
    """Return the `repositories.<distribution>` block of pkggen.yaml.

    Returns an empty dict if the block is missing and nothing is required.
    Exits with an error if the input is invalid or a required key is missing.
    """
    raw = readinput()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        fail(f"couldn't parse the pkggen configuration: {e}")

    repositories = (data or {}).get("repositories") or {}
    config = repositories.get(distribution)
    if config is None:
        if required:
            fail(f"pkggen.yaml has no 'repositories.{distribution}' block")
        return {}
    if not isinstance(config, dict):
        fail(f"'repositories.{distribution}' must be a mapping")

    missing = [key for key in required if not config.get(key)]
    if missing:
        fail(f"'repositories.{distribution}' is missing: {', '.join(missing)}")
    return config


def write_file(path, content):
    """Write a file, skipping it if it already exists unless FORCE is set."""
    exists = os.path.exists(path)
    if exists and not FORCE:
        warn(f"skipped {path} (already exists, use --force to overwrite)")
        return

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"{'overwrote' if exists else 'created'} {path}", file=sys.stderr)


def make_dirs(dirs, gitkeep=True):
    """Create directories, adding an empty .gitkeep so git tracks them."""
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)
        keep = os.path.join(directory, ".gitkeep")
        if gitkeep and not os.path.exists(keep):
            open(keep, "w").close()


def as_list(value):
    """Turn None, a single value or a list into a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def to_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def lines(*entries):
    """Join entries into newline-terminated file content."""
    return "\n".join(entries) + "\n"
