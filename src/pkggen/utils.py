#!/usr/bin/env python3
import os
import yaml
import sys

def create_secrets_file():
    if os.name == 'nt':
        base_dir = os.getenv('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
    else:
        base_dir = os.getenv('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    config_dir = os.path.join(base_dir, 'pkggen')
    os.makedirs(config_dir, exist_ok=True)
    secrets_file = os.path.join(config_dir, 'secrets.yaml')
    if not os.path.exists(secrets_file):
        with open(secrets_file, 'w') as f:
            f.write('')
    return secrets_file

HERE = os.path.dirname(os.path.abspath(__file__))
# Where the generators are when PKGGEN_GENERATORS_PATH isn't set: the copy an
# installed pkggen ships with, else the checkout's generators/ (editable installs)
BUNDLED_GENERATORS = (os.path.join(HERE, "generators"), os.path.join(HERE, os.pardir, os.pardir, "generators"))

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
        with open(dist_file, "r") as stream:
            data = yaml.safe_load(stream) or {}
    except FileNotFoundError:
        raise GenericError(f"Couldn't find the distributions list at {dist_file}! Is PKGGEN_GENERATORS_PATH set?")
    except yaml.YAMLError as e:
        raise GenericError(f"Couldn't parse {dist_file}: {e}")

    distributions = data.get("distributions") or []
    for dist in distributions:
        if not isinstance(dist, dict) or "name" not in dist:
            raise GenericError(f"Every entry in {dist_file} needs a 'name' key!")
    return distributions

def get_run_path():
    """The directory containing pkggen.yaml."""
    return os.path.abspath(os.getenv("PKGGEN_RUN_PATH", os.getcwd()))

def get_pkggen_config():
    pkggen_config = os.path.join(get_run_path(), "pkggen.yaml")
    try:
        with open(pkggen_config, "r") as stream:
            return yaml.safe_load(stream)
    except FileNotFoundError:
        raise GenericError(f"Couldn't find {pkggen_config}! Run pkggen from its directory or set PKGGEN_RUN_PATH.")
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
