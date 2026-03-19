#!/usr/bin/env python3
import os
import yaml
import json
import utils
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

def generate_packages(packages, package_filter, generator):
    results = []

    def worker_task(package, pkg_filter):
        if pkg_filter != None and package.get("name") not in pkg_filter:
            return None
        
        js = json.dumps(package)

        result = subprocess.run(
            [ sys.executable, generator ],
            input=js,
            text=True,
            capture_output = True
        )

        if result.returncode != 0:
            print("Error encountered when running the generator!")

            print("Failed generator stdout: ")
            print(result.stdout)

            print("Failed generator stderr: ")
            print(result.stderr)

            raise utils.GenericError("Errors encountered when running the generator!")

        return result.stdout

    with ThreadPoolExecutor() as executor:
        results = list(executor.map(lambda x: worker_task(x, package_filter), packages))
    
    return [res for res in results if res is not None] 

def generate(package_filter=None):
    utils.create_secrets_file()
    generators_path = utils.get_generators_path()
    generator_files = os.path.join(generators_path, "generation")
    generators = []

    for entry in os.scandir(generator_files):
        if entry.is_file() and entry.name != "lib.py":
            generators.append(entry.name)


    pkggen = utils.get_pkggen_config()
    if pkggen != None:
        for key, generation_level in pkggen.items():
            if "generator" in generation_level and "packages" in generation_level:
                packages = generation_level["packages"]
                
                if not packages:
                    continue

                found_generator = False
                generator = ""
                for gen in generators:
                    if gen == generation_level["generator"] + ".py":
                        generator = gen
                        found_generator = True
                        break
                        
                # TODO: Pass ready data
                if found_generator:
                    return generate_packages(packages, package_filter, os.path.join(generator_files, generator))

                raise utils.GenericError("Couldn't find template generator!")
            else:
                raise utils.GenericError("Couldn't find a generator or packages key in the generator metadata!")
    else:
        raise utils.GenericError("Couldn't load pkggen.yaml from the default path!")
