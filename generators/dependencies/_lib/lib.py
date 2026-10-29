#!/usr/bin/env python3
# Shared helpers for dependency generators.
#
# Contract: a dependency generator reads one job (JSON) on stdin:
#   {projects: [names], repositories: [rp-names], wildcards: [prefixes], user_agent}
# and prints {projects: {name: {rp-name: [package]}}, missing: [names], warnings}
# on stdout, where a package is {repo, subrepo, srcname, binname, binnames,
# visiblename, version, status, summary}. `wildcards` (optional) also keeps
# the releases whose name contains one of them (older releases, for
# `pkggen repology --include-outdated`). Progress and errors go to stderr.
import importlib.util
import json
import os
import re
import sys
import traceback

import requests
from packaging.version import InvalidVersion, parse as parse_version

# generators/common.py: the helpers every layer shares (shquote, to_bool, secrets_path, ...), as lib.<name>.
# Loaded by path once per process, under a name no other module has. This file is generators/<layer>/_lib/lib.py.
if "pkggen_common" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("pkggen_common", os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "common.py"))
    sys.modules["pkggen_common"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["pkggen_common"])
from pkggen_common import *  # noqa: E402,F401,F403
from pkggen_common import LIB_FOLDER  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_KEYS = ("repo", "subrepo", "srcname", "binname", "binnames", "visiblename", "version", "status", "summary")


def load_fetch_lib():
    """The fetch generators' lib.py, for its HTTP cache (loaded by path: every layer's module is called `lib`)."""
    path = os.path.join(os.path.dirname(os.path.dirname(HERE)), "generation", LIB_FOLDER, "lib.py")
    spec = importlib.util.spec_from_file_location("pkggen_generation_lib", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load_fetch_lib()
DEBUG = fetch.DEBUG
TIMEOUT = fetch.TIMEOUT
TinyError = fetch.TinyError
log = fetch.log
warn = fetch.warn


def version_sort_key(version):
    """Distribution versions ("1.2.13.dfsg", "1.3_p1") often aren't PEP 440: fall back to the leading numeric part."""
    try:
        return parse_version(str(version))
    except InvalidVersion:
        match = re.match(r"\d+(\.\d+)*", str(version))
        return parse_version(match.group(0) if match else "0")


def trim(packages, repositories, wildcards=()):
    """Group a project's packages by release, keeping only the releases pkggen knows (and the wildcard ones)."""
    known = set(repositories)
    result = {}
    for package in packages:
        repo = package.get("repo")
        if not isinstance(repo, str):
            continue
        if repo not in known and not any(w in repo for w in wildcards):
            continue
        entry = {key: package.get(key) for key in PACKAGE_KEYS}
        entry["binnames"] = list(entry["binnames"] or [])
        entry["version"] = str(entry["version"] or "")
        result.setdefault(repo, []).append(entry)
    return result


def run(main):
    """Read the job from stdin, call main(job) and print its result as JSON.

    Expected errors become a single "Error [dependencies]: message" line on
    stderr and exit code 1. Set PKGGEN_DEBUG=1 to also get a traceback.
    """
    try:
        job = json.loads(sys.stdin.buffer.read())
        if not isinstance(job, dict) or not isinstance(job.get("projects"), list):
            raise TinyError("expected a JSON object with a 'projects' list on stdin")
        result = main(job)
    except json.JSONDecodeError as e:
        fetch.fail("dependencies", f"couldn't parse the job JSON: {e}")
    except TinyError as e:
        if DEBUG:
            traceback.print_exc()
        fetch.fail("dependencies", str(e))
    except requests.RequestException as e:
        if DEBUG:
            traceback.print_exc()
        fetch.fail("dependencies", f"network error: {e}")
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        fetch.fail("dependencies", f"unexpected {type(e).__name__}: {e} (set PKGGEN_DEBUG=1 for a traceback)")

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
