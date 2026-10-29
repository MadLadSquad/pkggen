#!/usr/bin/env python3
# Helpers shared by every generator layer and by pkggen itself.
#
# Every layer's lib.py loads this file by path, as the module `pkggen_common`
# (a plain name like `common` could shadow, or be shadowed by, another
# module), and re-exports everything in __all__, so generators get these as
# `lib.<name>`. pkggen's core loads the copy it ships with (utils.common).
# Only the standard library may be imported here: every layer, and the core
# before its dependencies are checked, can import it.
import importlib.util
import os
import re
import stat
import sys

__all__ = ("ANSI_RE", "ERROR_PREFIX_RE", "config_dir", "secrets_path", "ensure_secrets_file", "shquote", "to_bool",
           "as_list", "words", "generator_dirs", "find_all", "load_generator")

# The generators folder this file belongs to
HERE = os.path.dirname(os.path.abspath(__file__))
# Every generators folder, highest priority first: pkggen sets it for the generators it runs
DIRS_ENV = "PKGGEN_GENERATORS_DIRS"
# The folder in each layer that holds its lib.py. pkggen puts the base folder's on PYTHONPATH, so `import lib`
# works from any generators folder. It isn't the layer folder itself: generator names like github.py or git.py
# would hide the Python packages of the same name.
LIB_FOLDER = "_lib"

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


# --- Generators folders ----------------------------------------------------------

def default_generator_dirs():
    """The user's and the system's generators folders, highest priority first (they may not exist).

    $XDG_DATA_HOME/pkggen/generators (~/.local/share/...), then every $XDG_DATA_DIRS
    entry's (/usr/local/share, then /usr/share); %LOCALAPPDATA% then %PROGRAMDATA%
    on Windows. pkggen's own generators come after them.
    """
    if os.name == "nt":
        user = os.getenv("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        system = [os.getenv("PROGRAMDATA") or "C:\\ProgramData"]
    else:
        user = os.getenv("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        # The XDG spec: unset or empty means /usr/local/share:/usr/share, and relative entries are ignored
        system = [d for d in (os.getenv("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
                  if os.path.isabs(d)]
    return [os.path.join(d, "pkggen", "generators") for d in [user, *system]]


def generator_dirs():
    """Every generators folder, highest priority first. The last one is the base, whose libraries are used.

    Generators get the list from pkggen (PKGGEN_GENERATORS_DIRS); run by hand, it's this file's folder.
    """
    dirs = [d for d in (os.getenv(DIRS_ENV) or "").split(os.pathsep) if d]
    return dirs or [HERE]


def find_all(*parts, dirs=None):
    """Every existing <generators folder>/<parts...>, highest priority first.

    find_all("deployment", "hooks", "debian.py")[0] is the one to use; any others are shadowed by it.
    """
    found = []
    for folder in generator_dirs() if dirs is None else dirs:
        path = os.path.join(folder, *parts)
        if os.path.exists(path) and path not in found:
            found.append(path)
    return found


def load_generator(layer, name):
    """Import another generator of the caller's layer as a module: lib.load_generator("deployment", "git").

    It comes from the highest-priority generators folder that has it. Its `import lib`
    gets the caller's lib, so only generators of the same layer can be loaded.
    """
    found = find_all(layer, f"{name}.py")
    if not found:
        raise ImportError(f"there's no {layer} generator called '{name}' in {', '.join(generator_dirs())}")
    module_name = f"pkggen_generator_{layer}_{re.sub(r'[^A-Za-z0-9_]', '_', name)}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, found[0])
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[module_name]
            raise
    return sys.modules[module_name]


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
