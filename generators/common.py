#!/usr/bin/env python3
# Helpers shared by every generator layer and by pkggen itself.
#
# Every layer's lib.py loads this file by path, as the module `pkggen_common`
# (a plain name like `common` could shadow, or be shadowed by, another
# module), and re-exports everything in __all__, so generators get these as
# `lib.<name>`. pkggen's core loads the copy it ships with (utils.common).
# Only the standard library may be imported here: every layer, and the core
# before its dependencies are checked, can import it.
import os
import re
import stat

__all__ = ("ANSI_RE", "ERROR_PREFIX_RE", "config_dir", "secrets_path", "ensure_secrets_file", "shquote", "to_bool",
           "as_list", "words")

# ANSI color codes, which generators put around their messages
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# The "Error: " or "Error [<package>]: " that generators start their error lines with
ERROR_PREFIX_RE = re.compile(r"^Error( \[[^\]]*\])?: ", re.MULTILINE)


# --- Configuration folder and secrets ----------------------------------------

def config_dir():
    """pkggen's configuration folder: $XDG_CONFIG_HOME/pkggen, or %APPDATA%\\pkggen on Windows."""
    if os.name == "nt":
        base_dir = os.getenv("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    else:
        base_dir = os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base_dir, "pkggen")


def secrets_path():
    return os.path.join(config_dir(), "secrets.yaml")


def ensure_secrets_file():
    """Create an empty secrets.yaml if there's none. Returns (its path, whether it was created).

    It holds tokens and key passphrases, so only its owner may read it (0600,
    like the SSH keys pkggen manages). Files older versions of pkggen created
    readable by everyone are restricted too.
    """
    path = secrets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        # O_EXCL: never truncate a file another process created in the meantime
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        created = True
    except FileExistsError:
        created = False
    if os.name != "nt":  # Windows has no such permission bits: %APPDATA% is per user already
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                os.chmod(path, mode & 0o700)
        except OSError:
            pass  # e.g. someone else's file: reading it still works
    return path, created


# --- Values from pkggen.yaml ----------------------------------------------------

def shquote(value):
    """Quote for sh and bash with single quotes: it's -> 'it'\\''s'."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def to_bool(value, default=False):
    """A pkggen.yaml flag: true/false, or a string like "yes", "on", "1". None is the default."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def as_list(value):
    """Turn None, a single value or a list into a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def words(value):
    """A list of names written as a list or as one space-separated string, as strings: "a b" -> ["a", "b"]."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]
