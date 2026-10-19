#!/usr/bin/env python3
import os
import json
import utils
import subprocess
import sys

def get_repo_generators_path():
    return os.path.join(utils.get_generators_path(), "repositories")

def get_distributions():
    # Only offer distributions that also have a repository generator
    repo_generators_path = get_repo_generators_path()
    return [
        dist["name"] for dist in utils.load_distributions()
        if os.path.exists(os.path.join(repo_generators_path, f"{dist['name']}.py"))
    ]

def gen_repo(distribution, output_dir=None, force=False):
    generator_path = os.path.join(get_repo_generators_path(), f"{distribution}.py")
    if not os.path.exists(generator_path):
        raise utils.GenericError(f"Couldn't find repository generator for {distribution}!")

    pkggen = utils.get_pkggen_config()
    if pkggen is None:
        raise utils.GenericError("Couldn't load pkggen.yaml from the default path!")

    cwd = output_dir if output_dir else utils.get_repo_path(distribution, pkggen)
    os.makedirs(cwd, exist_ok=True)
    print(f"Generating the {distribution} repository in {utils.display_path(cwd)}", file=sys.stderr)

    # Generators report progress on stderr, so stdout is not captured
    result = subprocess.run(
        [sys.executable, generator_path],
        input=json.dumps(pkggen),
        text=True,
        cwd=cwd,
        env={**os.environ, "PKGGEN_FORCE": "1" if force else "0"},
    )

    if result.returncode != 0:
        error_msg = (
            f"\nError encountered when running the repository generator for {distribution}\n"
            f"Generator: {generator_path}\n"
            f"Exit code: {result.returncode}\n"
        )
        print(error_msg, file=sys.stderr)
        raise utils.GenericError(f"Errors encountered when running the generator for {distribution}!")
