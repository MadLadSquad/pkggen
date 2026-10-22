"""`pkggen repology <project>`: what a dependency is called, and which version it has, in every release.

The lookup is the dependency generator's (generators/dependencies/repology.py),
so it shows exactly what `pkggen generate` works with. In a folder with a
pkggen.yaml its answers are kept in pkggen-build/cache, like generate's.
"""
import json
import os
import sys

import dependencies
import generate
import utils


def release_rows(projects, name, distributions, include_outdated):
    """One row per package, current releases first (in distributions.yaml order), then older ones."""
    supported = [rp for dist in distributions for rp in dist.get("rp-names") or []]
    found = projects.get(name) or {}
    rows = []
    for rp in supported + sorted(set(found) - set(supported)):
        for package in found.get(rp) or []:
            current = rp in supported
            if not current and not include_outdated:
                continue
            rows.append({
                "distribution": rp,
                "distribution_type": "supported" if current else "discontinued",
                "srcname": package.get("srcname") or "",
                "binname": package.get("binname") or "",
                "binnames": package.get("binnames") or [],
                "repository": package.get("subrepo") or "",
                "version": package.get("version") or "",
                "status": package.get("status") or "",
                "summary": package.get("summary") or "",
            })
    return rows


def environment():
    """Cache the answers in pkggen-build/cache inside a pkggen project, like `pkggen generate` does."""
    if generate.CACHE_ENV in os.environ or os.path.isfile(os.path.join(utils.get_run_path(), "pkggen.yaml")):
        return generate.fetch_environment()
    env = dict(os.environ)
    env[generate.CACHE_ENV] = ""
    return env


def print_table(rows):
    blocks = [[
        f"Distribution: {row['distribution']}",
        f"Distribution support state: {row['distribution_type']}",
        f"Repository: {row['repository'] or 'default'}",
        f"Source package name: {row['srcname'] or '-'}",
        f"Binary package names: {', '.join(row['binnames']) or row['binname'] or '-'}",
        f"Version: {row['version']}",
        f"Status: {row['status']}",
        f"Summary: {row['summary']}",
    ] for row in rows]
    width = max(len(line) for block in blocks for line in block)
    for i, block in enumerate(blocks):
        print(("┏" if i == 0 else "┣") + "━" * (width + 2) + ("┓" if i == 0 else "┫"))
        for line in block:
            print(f"┃ {line.ljust(width)} ┃")
    print("┗" + "━" * (width + 2) + "┛")


def query_repology(name, is_raw, include_outdated):
    """Print what repology knows about a project. Returns the exit code."""
    distributions = utils.load_distributions()
    rp_names = [rp for dist in distributions for rp in dist.get("rp-names") or []]
    wildcards = [w for dist in distributions for w in dist.get("rp-names-wildcards") or []] if include_outdated else []
    try:
        projects, warnings = dependencies.run_generator([name], rp_names, wildcards, env=environment())
    except RuntimeError as e:
        raise utils.GenericError(str(e))
    for line in warnings:
        print(f"\x1b[33m{line}\x1b[0m", file=sys.stderr)

    rows = release_rows(projects, name, distributions, include_outdated)
    if not rows:
        if is_raw:
            print(json.dumps({"error": "No such dependency", "project": name}))
        else:
            print(f"repology has no package of '{name}' in the releases pkggen supports. Look its project name up at "
                  f"https://repology.org/projects/?search={name}")
        return 1
    if is_raw:
        print(json.dumps(rows))
    else:
        print_table(rows)
    return 0
