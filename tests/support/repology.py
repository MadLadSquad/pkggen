"""Recorded repology answers (tests/support/data/repology.json, see record_repology.py)."""
import json
from functools import lru_cache

from tests.support import paths

API = "https://repology.org/api/v1/project/"


@lru_cache(maxsize=None)
def recorded():
    with open(paths.REPO / "tests" / "support" / "data" / "repology.json", encoding="utf-8") as f:
        return json.load(f)


def project(name):
    """Repology's answer for a project, as the API gives it."""
    return [dict(p) for p in recorded()[name]]


def releases(name, rp_names=None):
    """A project's packages per release, as `pkggen generate` passes them to packaging generators."""
    result = {}
    for package in project(name):
        package.setdefault("binnames", [])
        if rp_names is None or package["repo"] in rp_names:
            result.setdefault(package["repo"], []).append(package)
    return result


def dep(name, op=None, version=None, rp_names=None, **extra):
    """One entry of a packaging job's `dependencies`."""
    return {"name": name, "op": op, "version": version, "local": None,
            "releases": releases(name, rp_names) if name in recorded() else {}, **extra}


def routes(routes_builder, names=None):
    """Add the recorded projects to a tests.support.http_shim Routes."""
    for name in names or recorded():
        routes_builder.json(API + name, recorded()[name], headers={"Cache-Control": "max-age=3600"})
    return routes_builder
