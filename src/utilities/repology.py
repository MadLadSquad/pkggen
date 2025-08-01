#!/usr/bin/env python3
import os
import yaml
import json
import argparse
import requests
from packaging.version import parse as parse_version

def main(package, is_raw, include_outdated):
    generators_path = os.getenv("PKGGEN_GENERATORS_PATH", os.getcwd())
    with open(os.path.join(generators_path, "distributions", "distributions.yaml"), "r") as stream:
        try:
            distributions = yaml.safe_load(stream)["distributions"]

            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
            }
            output = []

            response = requests.get(f"https://repology.org/api/v1/project/{package}", headers=headers)
            if response.status_code == 200:
                data = response.json()
                for item in data:
                    for distribution in distributions:
                        b_skip = False
                        for release in distribution["rp-names"]:
                            if item["repo"] == release and (
                                    ("binname" in item and item["binname"] == package) or 
                                    ("srcname" in item and item["srcname"] == package) or 
                                    ("visiblename" in item and item["visiblename"] == package)
                                ):

                                output.append({
                                    "distribution":         release,
                                    "distribution_type":    "supported",
                                    "srcname":              item["srcname"] if "srcname" in item else "",
                                    "binname":              item["binname"] if "binname" in item else "",
                                    "repository":           item["subrepo"] if "subrepo" in item else "",
                                    "version":              item["version"],
                                    "status":               item["status"],
                                    "summary":              item["summary"],
                                })
                                b_skip = True
                                break

                        if b_skip:
                            continue

                        if include_outdated:
                            if "rp-names-wildcards" in distribution:
                                for release in distribution["rp-names-wildcards"]:
                                    if release in item["repo"] and (
                                            ("binname" in item and item["binname"] == package) or 
                                            ("srcname" in item and item["srcname"] == package) or 
                                            ("visiblename" in item and item["visiblename"] == package)
                                        ):

                                        output.append({
                                            "distribution":         item["repo"],
                                            "distribution_type":    "discontinued",
                                            "srcname":              item["srcname"] if "srcname" in item else "None",
                                            "binname":              item["binname"] if "binname" in item else "None",
                                            "repository":           item["subrepo"] if "subrepo" in item else "Default",
                                            "version":              item["version"],
                                            "status":               item["status"],
                                            "summary":              item["summary"],
                                        })
                
                output.sort(key=lambda x: parse_version(x["version"]), reverse=True)
                output.sort(key=lambda x: x["distribution_type"] != "supported")
                if is_raw:
                    print(json.dumps(output))
                    return
                
                b_first = True
                max_width = 0

                def strings_do(f):
                    for item in output:
                        lines = [
                            f"Distribution: {item['distribution']}",
                            f"Distribution support state: {item['distribution_type']}",
                            f"Repository: {item['repository']}",
                            f"Source package name: {item['srcname']}",
                            f"Binary package name: {item['binname']}",
                            f"Version: {item['version']}",
                            f"Status: {item['status']}",
                            f"Summary: {item['summary']}"
                        ]
                        f(lines)
               
                def get_max_width(lines):
                    nonlocal max_width
                    max_width = max(max_width, max(len(line) for line in lines))
                
                def print_with_formatting(lines):
                    nonlocal b_first
                    nonlocal max_width

                    if b_first:
                        print("┏" + "━" * (max_width + 2) + "┓")
                        b_first = False
                    else:
                        print("┣" + "━" * (max_width + 2) + "┫")
                    
                    for line in lines:
                        print(f"┃ {line.ljust(max_width)} ┃")


                strings_do(get_max_width)
                strings_do(print_with_formatting)
                print("┗" + "━" * (max_width + 2) + "┛")

            else:
                if is_raw:
                    print(f'\\{ "error": "No such dependency", "response_code": {response.status_code} \\}')
                else:
                    print("No such dependency! Response code:", response.status_code)
        except yaml.YAMLError as exception:
            print("YAML parsing error: ")
            print(exception)


parser = argparse.ArgumentParser(
    prog="pkggen repology", 
    description="A utility script to query the repology API for dependency information across distributions",
    epilog="Copyright (c) MadLadSquad. Licensed under the terms of the MIT license."
)
parser.add_argument("package")
parser.add_argument("-j", "--json", help="Print the result of the query as a JSON object", action="store_true")
parser.add_argument("-i", "--include-outdated", help="Include versions in outdated distribution releases", action="store_true")

arguments = parser.parse_args()
main(arguments.package, arguments.json, arguments.include_outdated)
