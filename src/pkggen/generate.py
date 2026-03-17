#!/usr/bin/env python3
import os
import yaml
import json
import utils
import subprocess
import sys

def generate_packages(packages, generator):
    results = []
    for package in packages:
        js = json.dumps(package)

        result = subprocess.run(
            [ sys.executable, generator ],
            input=js,
            text=True,
            capture_output = True
        )
        if result.returncode != 0:
            print("Error encountered when running the generator!")

            print("Failed generator stdin:")
            print(result.stdout)

            print("Failed generator stderr:")
            print(result.stderr)

            raise GenericError("Errors encountered when runnning the generator!")

        results.append(result.stdout)

    return results

def generate():
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
                found_generator = False
                generator = ""
                for gen in generators:
                    if gen == generation_level["generator"] + ".py":
                        generator = gen
                        found_generator = True
                        break
                        
                # TODO: Pass ready data
                if found_generator:
                    return generate_packages(generation_level["packages"], os.path.join(generator_files, generator))

                raise GenericError("Couldn't find template generator!")
            else:
                raise GenericError("Couldn't find a generator or packages key in the generator metadata!")
    else:
        raise GenericError("Couldn't load pkggen.yaml from the default path!")
