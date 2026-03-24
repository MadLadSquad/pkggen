#!/usr/bin/env python3
import os
import yaml
import json
import utils
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

def generate_packages(packages, package_filter, generator):
    # Pre-filter packages
    if package_filter is not None:
        packages = [p for p in packages if p.get("name") in package_filter]

    if not packages:
        return []

    def worker_task(package, position):
        # Inject position for the progress bar
        package["position"] = position
        js = json.dumps(package)

        # We capture stdout to get the result, but let stderr go to sys.stderr 
        # so that tqdm progress bars are visible and correctly positioned.
        result = subprocess.run(
            [ sys.executable, generator ],
            input=js,
            text=True,
            stdout=subprocess.PIPE,
            stderr=sys.stderr
        )

        if result.returncode != 0:
            error_msg = (
                f"\nError encountered when running the generator for package: {package.get('name', 'unknown')}\n"
                f"Generator: {generator}\n"
                f"Exit code: {result.returncode}\n"
            )
            print(error_msg, file=sys.stderr)
            raise utils.GenericError(f"Errors encountered when running the generator for {package.get('name')}!")

        return result.stdout

    # Using a limited number of workers to avoid overwhelming the system and network
    with ThreadPoolExecutor(max_workers=min(len(packages), 10)) as executor:
        # Use list comprehension with enumerate to pass position
        futures = [executor.submit(worker_task, pkg, i) for i, pkg in enumerate(packages)]
        results = [f.result() for f in futures]
    
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
