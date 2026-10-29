#!/usr/bin/env python3
import importlib.util
import os
import yaml
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Where the generators are when PKGGEN_GENERATORS_PATH isn't set: the copy an
# installed pkggen ships with, else the checkout's generators/ (editable installs)
BUNDLED_GENERATORS = (os.path.join(HERE, "generators"), os.path.join(HERE, os.pardir, os.pardir, "generators"))
COMMON_MODULE = "pkggen_common"


def load_common():
    """generators/common.py, the helpers pkggen shares with the generators' lib.py files.

    Always the copy pkggen ships with, not PKGGEN_GENERATORS_PATH's: pkggen's
    own behaviour doesn't change with the generators it runs. It's registered
    as `pkggen_common`, the name the generators' lib.py files load it under.
    """
    if COMMON_MODULE in sys.modules:
        return sys.modules[COMMON_MODULE]
    for folder in BUNDLED_GENERATORS:
        path = os.path.join(folder, "common.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(COMMON_MODULE, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[COMMON_MODULE] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"pkggen's generators/common.py is missing (looked in {', '.join(BUNDLED_GENERATORS)}): "
                      "reinstall pkggen")


common = load_common()
ANSI_RE = common.ANSI_RE
ERROR_PREFIX_RE = common.ERROR_PREFIX_RE


def create_secrets_file():
    """Create secrets.yaml (readable only by its owner) if it doesn't exist. Returns its path."""
    return common.ensure_secrets_file()[0]


def get_generators_path():
    # Absolute, so generators still resolve when run from another working directory
    custom = os.getenv("PKGGEN_GENERATORS_PATH")
    if custom:
        return os.path.abspath(custom)
    for path in BUNDLED_GENERATORS:
        if os.path.isfile(os.path.join(path, "distributions", "distributions.yaml")):
            return os.path.abspath(path)
    return os.path.abspath(os.getcwd())

def load_distributions():
    dist_file = os.path.join(get_generators_path(), "distributions", "distributions.yaml")
    try:
        with open(dist_file, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except FileNotFoundError:
        raise GenericError(f"Couldn't find the distributions list at {dist_file}! Is PKGGEN_GENERATORS_PATH set?")
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise GenericError(f"Couldn't parse {dist_file}: {e}")

    distributions = data.get("distributions") or []
    for dist in distributions:
        if not isinstance(dist, dict) or "name" not in dist:
            raise GenericError(f"Every entry in {dist_file} needs a 'name' key!")
        targets = dist.get("targets")
        if targets is not None and not (isinstance(targets, dict) and all(
                rp in (dist.get("rp-names") or []) and isinstance(names, list) and names
                and all(isinstance(n, str) and n for n in names) for rp, names in targets.items())):
            raise GenericError(f"'targets' of {dist['name']} in {dist_file} must map its rp-names to lists of "
                               "test target names")
    return distributions

def target_aliases(dist):
    """{target name: rp-name} of a distribution's `targets` in distributions.yaml.

    A release repology lists once can be tested as several targets (Void's
    glibc and musl builds are both void_x86_64): their names can be used
    wherever an rp-name can.
    """
    return {name: rp for rp, names in (dist.get("targets") or {}).items() for name in names}

def release_names(dist):
    """A distribution's rp-names, then the target names of its `targets`."""
    return list(dist.get("rp-names") or []) + list(target_aliases(dist))

def get_run_path():
    """The directory containing pkggen.yaml."""
    return os.path.abspath(os.getenv("PKGGEN_RUN_PATH", os.getcwd()))

def get_pkggen_config():
    pkggen_config = os.path.join(get_run_path(), "pkggen.yaml")
    try:
        # YAML files are UTF-8, whatever the locale's code page is (often cp1252 on Windows)
        with open(pkggen_config, "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except FileNotFoundError:
        raise GenericError(f"Couldn't find {pkggen_config}! Run pkggen from its directory or set PKGGEN_RUN_PATH.")
    except UnicodeDecodeError as e:
        raise GenericError(f"Couldn't parse {pkggen_config}: it isn't UTF-8 ({e})")
    except yaml.YAMLError as e:
        raise GenericError(f"Couldn't parse {pkggen_config}: {e}")

# Repositories live here unless `repositories.<dist>.path` says otherwise
DEFAULT_BUILD_DIR = "pkggen-build"

def get_repo_path(distribution, config):
    """Absolute path of a distribution's repository, relative paths being relative to pkggen.yaml."""
    block = ((config or {}).get("repositories") or {}).get(distribution)
    custom = block.get("path") if isinstance(block, dict) else None
    path = os.path.expanduser(str(custom)) if custom else os.path.join(DEFAULT_BUILD_DIR, distribution)
    return os.path.abspath(os.path.join(get_run_path(), path))

def display_path(path):
    """Path relative to the pkggen.yaml directory when it's inside it, for messages."""
    try:
        rel = os.path.relpath(path, get_run_path())
    except ValueError:  # different drive on Windows
        return path
    return path if rel.startswith("..") else rel

class GenericError(Exception):
    def __init__(self, message):
        super().__init__("\x1b[31m" + message + "\x1b[0m")
        self.args = (message, )
        self.__traceback__ = None
