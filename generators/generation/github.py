#!/usr/bin/env python3
import lib
import requests
import re
import json
from datetime import datetime

"""
The pkggen GitHub generator:

Input:
    {
        "name": "pkgname",
        "github":
        {
            "user": "user",
            "repo": "repo",

            // A query can be one of either: tag, release or commit.
            // Note: version strings are calculated by the release/tag name when querying for releases
            // or tags. Versions strings for commits are formatted in a date format like this: YYYYMMDD.
            "query": "release",

            // Optional: filters tag or release names given a regex pattern
            "select": "^\\d+\\.\\d+\\.\\d+$"

            // Optional: An array of regex replace pairs to extract the version from a tag/release name.
            // The first element of a pair is the search regex, while the second is the replace string.
            // These instructions are applied sequentially.
            "transforms": [
                [ "v", "" ]
            ],

            // Optional: Release queries have access to this field that allows you to select
            // an array of templated artifacts.
            //
            // Acceptable template parameters: version, pkgname, github_user, github_repo
            "artifacts": [
                "pkgname-{version}.tar.xz",
                "resources.tar.xz"
            ],

            // Optional: Lock package to a release/tag version when querying by tags.
            // You can also use a commit hash to lock commit-based generators too
            "version": "v1.0.0.0",

            // Optional: Whether to also accept drafts as valid releases. Defaults to false
            "include-drafts": "true",

            // Optional: Whether to also accept pre-releases as valid releases. Defaults to false
            "include-pre-releases": "true",

            // An optional field for enterprise users which host their own GitHub enterprise
            // instance under a different domain. Defaults to github.com
            "domain": "github.com",

            // An optional field for enterprise users which host their own GitHub enterprise
            // instance under a different domain. Defaults to api.github.com
            "api-domain": "api.github.com"
        }
    }
    
Output:
    {
        "tarball-urls": [
            { 
                "url": "pkgname-version.tar.xz",
                "size": "1234",
                "checksums": {
                    "sha2-512": "hash",
                    "sha3-512": "hash",
                    
                    "sha2-384": "hash",
                    "sha3-384": "hash",

                    "sha2-256": "hash",
                    "sha3-256": "hash",

                    "sha2-224": "hash",
                    "sha3-224": "hash",

                    "sha1": "hash",

                    "md5": "hash",

                    "blake2s": "hash",
                    "blake2b": "hash",
                }
            },
            {
                "url": "resources.tar.xz",
                "size": "1234",
                "checksums": {
                    "sha2-512": "hash",
                    "sha3-512": "hash",

                    "sha2-384": "hash",
                    "sha3-384": "hash",

                    "sha2-256": "hash",
                    "sha3-256": "hash",

                    "sha2-224": "hash",
                    "sha3-224": "hash",

                    "sha1": "hash",

                    "md5": "hash",

                    "blake2s": "hash",
                    "blake2b": "hash",
                }
            },
        ],
        "version": "1.0.0.0",
    }

Additional metadata fetched from the GitHub API(only exported if not defined):

1. pkgdescription
1. pkglicense
1. pkghomepage

"""

class GitHubData:
    def __init__(self):
        self.user = ""
        self.repo = ""
        self.query = ""
    
        self.select = None
        self.transforms = None
        self.artifacts = None
        self.version = None

        self.include_drafts = False
        self.include_pre_releases = False

        self.domain = "github.com"
        self.api_domain = "api.github.com"

    def __init__(self, data, pkgname):
        def sanitise_query(query):
            result = query
            if result == "tag":
                result = "tags"
            elif result == "release":
                result = "releases"
            elif result == "commit":
                result = "commits"
    
            if result != "tags" and result != "commits" and result != "releases":
                raise lib.TinyError(f"Invalid GitHub query! The query field can only be set to one of the following: \"release\", \"tag\" or \"commit\"")
            return result

        if "user" not in data:
            raise lib.TinyError(f"No GitHub user provided for \"{pkgname}\"!")
        if "repo" not in data:
            raise lib.TinyError(f"No GitHub repo provided for \"{pkgname}\"!")
        if "query" not in data:
            raise lib.TinyError(f"No GitHub query provided for \"{pkgname}\"!")

        self.user = data["user"]
        self.repo = data["repo"]
        self.query = sanitise_query(data["query"])
        
        self.select = data["select"] if "select" in data else None
        self.transforms = data["transforms"] if "transforms" in data else None
        self.artifacts = data["artifacts"] if "artifacts" in data else None
        self.version = data["version"] if "version" in data else None

        self.include_drafts = data["include-drafts"] if "include-drafts" in data else False
        self.include_pre_releases = data["include-pre-releases"] if "include-pre-releases" in data else False

        self.domain = data["domain"] if "domain" in data else "github.com"
        self.api_domain = data["api-domain"] if "api-domain" in data else "api.github.com"

    
def transform_date(x):
    return datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%d")

def generate_artifact_data(tarball_urls, user, repo):
    result = []
    for tarball_url in tarball_urls:
        tarball_response = requests.get(tarball_url, timeout=10, stream=True)
        size = int(tarball_response.headers.get("content-length", 0))
    
        if tarball_response.status_code == 200:
            buf = lib.download_to_buffer(tarball_response, size, tarball_url)
            result.append({
                "url": tarball_url,
                "checksums": {}
            })

            lib.calculate_hashes(result[-1]["checksums"], buf.getvalue())
            if size != 0:
                result[-1]["size"] = size
        else:
            raise lib.TinyError(f"Invalid git commit hash for GitHub repository {user}/{repo}!")

    return result

def get_exports(github):
    result = {}
    response = requests.get(f"https://{github.api_domain}/repos/{github.user}/{github.repo}")
    if response.status_code == 200:
        data = response.json()

        description = data["description"] if "description" in data and data["description"] else None
        if description != None:
            result["description"] = description

        license_str = data["license"]["spdx_id"] if "license" in data and data["license"] != None and "spdx_id" in data["license"] else None
        if license_str != None:
            result["license"] = license_str

        homepage = data["homepage"] if "homepage" in data and data["homepage"] != None and data["homepage"] != "" else None
        if homepage != None:
            result["homepage"] = homepage

        return result


def generate_commit(github):
    result = {}
    api_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    if github.version != None:
        response = requests.get(f"https://{github.api_domain}/repos/{github.user}/{github.repo}/commits/{github.version}", headers=api_headers, timeout=10)
        if response.status_code == 200:
            data = response.json()

            result["version"] = transform_date(data["commit"]["committer"]["date"])
            result["tarball-urls"] = generate_artifact_data(
                [
                    f"https://{github.domain}/{github.user}/{github.repo}/archive/{data['sha']}.tar.gz"
                ],
                github.user,
                github.repo
            )
        else:
            raise lib.TinyError(f"Invalid git commit hash for GitHub repository {github.user}/{github.repo}!")
    else:
        response = requests.get(f"https://{github.api_domain}/repos/{github.user}/{github.repo}/commits", headers=api_headers, timeout=10)
        if response.status_code == 200:
            data = response.json()[0]
            result["version"] = transform_date(data["commit"]["committer"]["date"])
            result["tarball-urls"] = generate_artifact_data(
                [
                    f"https://{github.domain}/{github.user}/{github.repo}/archive/{data['sha']}.tar.gz"
                ],
                github.user,
                github.repo
            )
        else:
            raise lib.TinyError(f"Invalid git commit hash for GitHub repository {github.user}/{github.repo}!")
    result["exports"] = get_exports(github)
    return result

def apply_version_transforms(transforms, version):
    if transforms != None:
        for transform in transforms:
            if type(transform) == list and len(transform) >= 2:
                version = re.sub(transform[0], transform[1], version)
            else:
                raise lib.TinyError(f"Elements of the transforms array must be arrays with 2 elements!")
    return version

def generate_release_or_tag(pkgname, github):
    api_headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    page = 1
    result = {}

    while True:
        response = requests.get(f"https://{github.api_domain}/repos/{github.user}/{github.repo}/{github.query}?page={page}&per_page=100", headers=api_headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if len(data) == 0:
                raise lib.TinyError(f"Unable to find compatible version or the URL is invalid for GitHub repository {github.user}/{github.repo}")

            obj = {}


            if github.select != None:
                if github.version != None:
                    raise lib.TinyError(f"Cannot have both a \"version\" and a \"select\" key when generating a tag/release!")

                regex_filter = re.compile(github.select)

                def filter_fun(f, regex_filter, github):
                    result = True
                    if github.query == "releases":
                        if (not github.include_drafts and f["draft"]) or (not github.include_pre_releases and f["prerelease"]):
                            result = False
                    return result and regex_filter.search(f["name"])

                filtered = [f for f in data if filter_fun(f, regex_filter, github)]
                
                if len(filtered) > 0:
                    obj = filtered[0]
                    result["version"] = apply_version_transforms(github.transforms, obj["name"])
                else:
                    page += 1
                    continue
            else:
                if github.version == None:
                    if github.query == "releases":
                        is_compatible = True
                        for release in data:
                            if (not github.include_drafts and release["draft"]) or (not github.include_pre_releases and release["prerelease"]):
                                is_compatible = False
                            else:
                                is_compatible = True
                            
                            if is_compatible:
                                obj = release
                                break

                        if is_compatible == False:
                            page += 1
                            continue
                    else:
                        obj = data[0]

                    result["version"] = apply_version_transforms(github.transforms, obj["name"])
                else:
                    found_version = False
                    for d in data:
                        if github.version == d["name"]:
                            found_version = True
                            obj = d
                            result["version"] = apply_version_transforms(github.transforms, d["name"])
                            break

                    if found_version == False:
                        page += 1
                        continue


            if github.query == "tags":
                result["tarball-urls"] = generate_artifact_data(
                    [
                        obj["tarball_url"]
                    ],
                    github.user,
                    github.repo
                )
            elif github.query == "releases":
                if github.artifacts == None:
                    result["tarball-urls"] = generate_artifact_data(
                        [
                            obj["tarball_url"]
                        ],
                        github.user,
                        github.repo
                    )
                else:
                    new_artifacts = [ 
                        artifact.format(
                            pkgname=pkgname,
                            version=result["version"],
                            github_user = github.user,
                            github_repo = github.repo
                        )
                        for artifact in github.artifacts
                    ]
                    urls = [ obj["tarball_url"] ]

                    for asset in obj["assets"]:
                        for artifact in new_artifacts:
                            if artifact == asset["name"]:
                                urls.append(asset["browser_download_url"])

                    result["tarball-urls"] = generate_artifact_data(urls, github.user, github.repo)

            result["exports"] = get_exports(github)
            return result
        else:
            raise lib.TinyError(f"Unable to find compatible version or the URL is invalid for GitHub repository {github.user}/{github.repo}")

        page += 1
    

def generate(x):
    data = json.loads(x)

    pkgname = data["name"]
    if "github" not in data:
        raise lib.TinyError(f"No object named \"github\" found inside the \"{pkgname}\" package's metadata!")
   
    github_data = GitHubData(data["github"], pkgname)
    if github_data.query == "commits":
        print(generate_commit(github_data))
    elif github_data.query == "tags" or github_data.query == "releases":
        print(generate_release_or_tag(pkgname, github_data))



generate("""
{
    "name": "UntitledImGuiFramework",
    "github": {
        "_user": "MadLadSquad",
        "_repo": "UntitledImGuiFramework",
        "user": "microsoft",
        "repo": "monaco-editor",
        "query": "releases",
        "_select": "^v\\\\d+\\\\.\\\\d+\\\\.\\\\d+\\\\.\\\\d+$",
        "_version": "v1.3.2.0",
        "_transforms": [
            [ "v", "" ]
        ],
        "_artifacts": [
            "untitled-imgui-framework-{version}.tar.xz"
        ],
        "include-pre-releases": "true",
        "include-drafts": "true"
    }
}
""")


#generate("""
#{
#    "name": "UntitledImGuiFramework",
#    "github": {
#        "user": "MadLadSquad",
#        "repo": "UntitledImGuiFramework",
#        "query": "commits",
#        "version": "142e73823eb7af3b1e1f743ccb3756c5d3cb7e8b"
#    }
#}
#""")
