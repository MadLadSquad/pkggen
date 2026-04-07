#!/usr/bin/env python3
import os
import yaml
import json
import utils
import subprocess
import sys

def get_distributions():
    generators_path = utils.get_generators_path()
    repo_generators_path = os.path.join(generators_path, "repositories")
    dist_file = os.path.join(generators_path, "distributions", "distributions.yaml")

    if not os.path.exists(dist_file):
        return []

    with open(dist_file, "r") as stream:
        try:
            data = yaml.safe_load(stream)
            distributions_list = data.get("distributions", [])
            valid_dists = []
            
            for dist_dict in distributions_list:
                dist_name = list(dist_dict.keys())[0]
                if os.path.exists(os.path.join(repo_generators_path, f"{dist_name}.py")):
                    valid_dists.append(dist_name)
            return valid_dists
        except yaml.YAMLError:
            return []

def gen_repo(distribution, output_dir=None):
    generators_path = utils.get_generators_path()
    
    repo_generators_path = os.path.join(generators_path, "repositories")
    generator_path = os.path.join(repo_generators_path, f"{distribution}.py")
    
    if not os.path.exists(generator_path):
        raise utils.GenericError(f"Couldn't find repository generator for {distribution}!")

    pkggen = utils.get_pkggen_config()
    if pkggen is None:
        raise utils.GenericError("Couldn't load pkggen.yaml from the default path!")

    # Set working directory if output_dir is provided
    cwd = output_dir if output_dir else os.getcwd()
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    js = json.dumps(pkggen)
    
    result = subprocess.run(
        [sys.executable, generator_path],
        input=js,
        text=True,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        cwd=cwd
    )
    
    if result.returncode != 0:
        error_msg = (
            f"\nError encountered when running the repository generator for {distribution}\n"
            f"Generator: {generator_path}\n"
            f"Exit code: {result.returncode}\n"
        )
        print(error_msg, file=sys.stderr)
        raise utils.GenericError(f"Errors encountered when running the generator for {distribution}!")
    
    return result.stdout
