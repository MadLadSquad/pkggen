#!/usr/bin/env python3
import sys
import os
import json
import re
import requests
from packaging.version import parse as parse_version, InvalidVersion

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils

def version_sort_key(version):
    # Distro versions (e.g. "1.2.13.dfsg", "1.3_p1") often aren't PEP 440, so fall back to the leading numeric part
    try:
        return parse_version(version)
    except InvalidVersion:
        match = re.match(r"\d+(\.\d+)*", version)
        return parse_version(match.group(0) if match else "0")

def query_repology(package, is_raw, include_outdated):
    distributions = utils.load_distributions()
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
                            "summary":              item.get("summary", ""),
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
                                    "summary":              item.get("summary", ""),
                                })

        output.sort(key=lambda x: version_sort_key(x["version"]), reverse=True)
        output.sort(key=lambda x: x["distribution_type"] != "supported")
        if is_raw:
            print(json.dumps(output))
            return

        b_first = True
        max_width = 0

        def strings_do(f):
            for i in output:
                lines = [
                    f"Distribution: {i['distribution']}",
                    f"Distribution support state: {i['distribution_type']}",
                    f"Repository: {i['repository']}",
                    f"Source package name: {i['srcname']}",
                    f"Binary package name: {i['binname']}",
                    f"Version: {i['version']}",
                    f"Status: {i['status']}",
                    f"Summary: {i['summary']}"
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
            print(json.dumps({"error": "No such dependency", "response_code": response.status_code}))
        else:
            print("No such dependency! Response code:", response.status_code)
