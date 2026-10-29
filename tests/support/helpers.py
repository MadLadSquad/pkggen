"""Helpers to load generators in-process and to run them, or the CLI, as subprocesses."""
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

from tests.support import paths

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def _load(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered like a real import, so the module can find itself (the deployment lib passes itself to its hooks)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_generator(layer, name):
    """Import generators/<layer>/<name>.py with that layer's own lib.py (<layer>/_lib/lib.py) as `lib`.

    Every layer has a module called `lib`, so it's swapped into sys.modules only
    while the generator imports it. The generator keeps its reference afterwards.
    A fresh copy is loaded on every call, so module state never leaks between tests.
    """
    directory = paths.GENERATORS / layer
    saved = sys.modules.get("lib")
    lib = _load(f"pkggen_test_{layer}_lib", directory / "_lib" / "lib.py")
    if name == "lib":
        return lib
    sys.modules["lib"] = lib
    try:
        return _load(f"pkggen_test_{layer}_{name.replace('-', '_')}", directory / f"{name}.py")
    finally:
        if saved is None:
            sys.modules.pop("lib", None)
        else:
            sys.modules["lib"] = saved


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self):
        return json.loads(self.out)

    @property
    def err_plain(self):
        return strip_ansi(self.err)


def subprocess_env(extra=None):
    """The test environment, with the HTTP shim importable by every subprocess.

    A PYTHONPATH in extra goes in front of the shim's instead of replacing it.
    """
    extra = dict(extra or {})
    first = extra.pop("PYTHONPATH", None)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [first and str(first), str(paths.HTTP_SHIM),
                                                      env.get("PYTHONPATH")]))
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
    return env


def run(args, stdin=None, cwd=None, env=None, timeout=300):
    process = subprocess.run(
        [sys.executable, *map(str, args)],
        input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
        capture_output=True, cwd=cwd, env=subprocess_env(env), timeout=timeout,
    )
    return Result(process.returncode, process.stdout.decode("utf-8", "replace"),
                  process.stderr.decode("utf-8", "replace"))


def generator_env(layer, env=None):
    """What pkggen adds to a generator's environment: its layer's lib on PYTHONPATH (see utils.generator_env())."""
    return {"PYTHONPATH": str(paths.GENERATORS / layer / "_lib"), **(env or {})}


def run_generator(layer, name, payload, cwd=None, env=None):
    """Run generators/<layer>/<name>.py with payload (a dict, or raw str/bytes) on stdin, as pkggen does."""
    stdin = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return run([paths.GENERATORS / layer / f"{name}.py"], stdin=stdin, cwd=cwd, env=generator_env(layer, env))


def run_cli(*args, cwd=None, env=None, timeout=300):
    """Run `pkggen <args>` straight from the source tree."""
    return run([paths.CLI, *args], cwd=cwd, env=env, timeout=timeout)
