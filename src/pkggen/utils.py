#!/usr/bin/env python3
import importlib.util
import os
import re
import yaml
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# pkggen's own generators: the copy an installed pkggen ships with, the checkout's generators/ (editable
# installs, running from src/), or <prefix>/share/pkggen/generators, where distribution packages may put them
# (/usr/share/pkggen/generators for the system's Python)
INSTALLED_GENERATORS = (os.path.join(HERE, "generators"), os.path.join(HERE, os.pardir, os.pardir, "generators"),
                        os.path.join(sys.prefix, "share", "pkggen", "generators"))
COMMON_MODULE = "pkggen_common"
# Replaces the list of generators folders (os.pathsep-separated, highest priority first)
PATH_ENV = "PKGGEN_GENERATORS_PATH"
# A generator's or a distribution's name: a file name in a generators folder, never a path
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
YELLOW, RESET = "\x1b[33m", "\x1b[0m"


def load_common():
    """generators/common.py, the helpers pkggen shares with the generators' lib.py files.

    Always the copy pkggen ships with, not PKGGEN_GENERATORS_PATH's: pkggen's
    own behaviour doesn't change with the generators it runs. It's registered
    as `pkggen_common`, the name the generators' lib.py files load it under.
    """
    if COMMON_MODULE in sys.modules:
        return sys.modules[COMMON_MODULE]
    for folder in INSTALLED_GENERATORS:
        path = os.path.join(folder, "common.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(COMMON_MODULE, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[COMMON_MODULE] = module
            spec.loader.exec_module(module)
            return module
    raise ImportError(f"pkggen's generators/common.py is missing (looked in {', '.join(INSTALLED_GENERATORS)}): "
                      "reinstall pkggen")


common = load_common()
ANSI_RE = common.ANSI_RE
ERROR_PREFIX_RE = common.ERROR_PREFIX_RE


def create_secrets_file():
    """Create secrets.yaml (readable only by its owner) if it doesn't exist. Returns its path."""
    return common.ensure_secrets_file()[0]


_warned = set()


def warn_once(message):
    """Print a warning on stderr, once per run."""
    if message not in _warned:
        _warned.add(message)
        print(f"{YELLOW}Warning: {message}{RESET}", file=sys.stderr)


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# --- Generators folders --------------------------------------------------------------

def builtin_generators_dir():
    """The generators pkggen ships with (see INSTALLED_GENERATORS), or None."""
    for path in INSTALLED_GENERATORS:
        if os.path.isfile(os.path.join(path, "distributions", "distributions.yaml")):
            return os.path.abspath(path)
    return None


def generator_dirs():
    """Every generators folder, highest priority first.

    PKGGEN_GENERATORS_PATH replaces the list. Otherwise it's the user's folder,
    the system's folders (common.default_generator_dirs(), only those that exist)
    and pkggen's own generators. The last folder is the base: every generator
    imports its layer's lib.py from it, and pkggen's generators are there.
    A generator, hook, template folder or distribution in an earlier folder
    replaces the one of the same name in later folders, with a warning.
    """
    custom = [p for p in (os.getenv(PATH_ENV) or "").split(os.pathsep) if p]
    if custom:
        dirs = [os.path.abspath(os.path.expanduser(p)) for p in custom]
    else:
        builtin = builtin_generators_dir() or os.path.abspath(os.getcwd())
        dirs = [os.path.abspath(p) for p in common.default_generator_dirs() if os.path.isdir(p)]
        dirs = [p for p in dirs if not same_path(p, builtin)] + [builtin]
    unique = []
    for path in dirs:
        if not any(same_path(path, seen) for seen in unique):
            unique.append(path)
    return unique


def base_generators_dir():
    return generator_dirs()[-1]


def shown_dirs():
    return ", ".join(generator_dirs())


def find_shared(*parts, kind):
    """<generators folder>/<parts...> from the highest-priority folder that has it, or None.

    `kind` describes it for the warning when it replaces others ("the packaging generator 'arch'").
    """
    found = common.find_all(*parts, dirs=generator_dirs())
    if len(found) > 1:
        warn_once(f"{kind} in {found[0]} replaces {', '.join(found[1:])}")
    return found[0] if found else None


def find_generator(layer, name):
    """generators/<layer>/<name>.py from the highest-priority folder that has it, or None."""
    if not isinstance(name, str) or not NAME_RE.match(name) or name == "lib":
        return None
    path = find_shared(layer, f"{name}.py", kind=f"the {layer} generator '{name}'")
    if path is None or not os.path.isfile(path):
        return None
    folder = os.path.dirname(path)
    if os.path.isfile(os.path.join(folder, "lib.py")):
        # The script's folder comes first on sys.path, so it would hide pkggen's lib
        warn_once(f"{os.path.join(folder, 'lib.py')} hides pkggen's lib from the generators next to it: rename it "
                  "(lib.py is reserved)")
    return path


def list_generators(layer):
    """The names of every generator of a layer, in every generators folder, sorted."""
    names = set()
    for folder in generator_dirs():
        directory = os.path.join(folder, layer)
        if os.path.isdir(directory):
            names.update(entry.name[:-3] for entry in os.scandir(directory)
                         if entry.is_file() and entry.name.endswith(".py") and entry.name != "lib.py"
                         and NAME_RE.match(entry.name[:-3]))
    return sorted(names)


def has_generator(layer, name):
    """Whether a layer has a generator called name (without the warnings of find_generator())."""
    return isinstance(name, str) and name in list_generators(layer)


def generator_env(layer, base=None):
    """The environment a layer's generators run in: `base` (default os.environ) plus what they need.

    PKGGEN_GENERATORS_DIRS lists the generators folders (common.generator_dirs()
    reads it), and the base folder's <layer>/_lib goes first on PYTHONPATH, so
    `import lib` works from every folder. Output is read as UTF-8, also where
    the locale's code page isn't (Windows).
    """
    env = dict(os.environ if base is None else base)
    dirs = generator_dirs()
    env[common.DIRS_ENV] = os.pathsep.join(dirs)
    lib_dir = os.path.join(dirs[-1], layer, common.LIB_FOLDER)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [lib_dir, env.get("PYTHONPATH")]))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def check_distribution_entries(distributions, dist_file):
    for dist in distributions:
        if not isinstance(dist, dict) or not isinstance(dist.get("name"), str) or not NAME_RE.match(dist["name"]):
            raise GenericError(f"Every entry in {dist_file} needs a 'name' key (a name, not a path)!")
        targets = dist.get("targets")
        if targets is not None and not (isinstance(targets, dict) and all(
                rp in (dist.get("rp-names") or []) and isinstance(names, list) and names
                and all(isinstance(n, str) and n for n in names) for rp, names in targets.items())):
            raise GenericError(f"'targets' of {dist['name']} in {dist_file} must map its rp-names to lists of "
                               "test target names")


def load_distributions():
    """Every distribution of every generators folder's distributions/distributions.yaml.

    An entry replaces the one of the same name in lower-priority folders (with a warning) and keeps its place.
    """
    files = common.find_all("distributions", "distributions.yaml", dirs=generator_dirs())
    if not files:
        raise GenericError(f"Couldn't find the distributions list (distributions/distributions.yaml) in "
                           f"{shown_dirs()}! Is {PATH_ENV} set?")
    merged, origin = {}, {}
    for dist_file in reversed(files):  # lowest priority first
        try:
            with open(dist_file, "r", encoding="utf-8") as stream:
                data = yaml.safe_load(stream) or {}
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as e:
            raise GenericError(f"Couldn't parse {dist_file}: {e}")
        distributions = data.get("distributions") if isinstance(data, dict) else None
        if not isinstance(distributions, (list, type(None))):
            raise GenericError(f"'distributions' in {dist_file} must be a list")
        check_distribution_entries(distributions or [], dist_file)
        for dist in distributions or []:
            name = dist["name"]
            if name in origin and origin[name] != dist_file:
                warn_once(f"the distribution '{name}' in {dist_file} replaces the one in {origin[name]}")
            merged[name], origin[name] = dist, dist_file
    return list(merged.values())

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
