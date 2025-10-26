#!/usr/bin/env python3

"""
The pkggen URL generator:

Input:
    {
        "name": "pkgname",
        "url-generator":
        {
            // The URL to fetch. It supports template parameters with the following syntax {parameter}
            // 
            // Acceptable template parameters: 
            // 1. pkgname
            // 2. version(only if the version field is already defined)
            "url": "https://example.com/",

            // A list of accepted hashes for locked artifacts
            "hash-locks": [ "hash", "hash", "hash" ],

            // Optional: A version for artifacts that are not updated frequently but don't have a 
            // version in the URL. Must not be defined while "transforms" is also defined.
            // Make sure to lock the version with the "hash-locks" field for strict version-locking
            "version": "1.0.0.0",
            
            // Optional: An array of regex replace pairs to extract the version from a URL.
            // The first element of a pair is the search regex, while the second is the replace string.
            // These instructions are applied sequentially. Must not be defined while "transforms"
            // is also defined.
            "transforms": [
                [ "https.*tag/v", "" ],
                [ "", "" ]
            ],
            
            // HTTP headers to be applied when fetching
            "headers":
            {
                "User-Agent": "My user agent",
            }
        }
    }

Output:
    {
        version: "1.0.0.0",
        "tarball-urls": 
        [
            {
                "url": "https://example.com",
                "size": "1234", //Optional
                "checksums":
                {
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
            }
        ]
    }

"""

import json
import requests
import re
import lib


def generate(x):
    data = json.loads(x)
    
    pkgname = data["name"]

    if "url-generator" not in data:
        raise lib.TinyError(f"No object named \"url-generator\" found inside the \"{pkgname}\" package's metadata!")
    urldata = data["url-generator"]

    if "url" not in urldata:
        raise lib.TinyError(f"No string entry named \"url\" found inside the \"{pkgname}\" package's metadata!")
    
    locks = urldata["hash-locks"] if "hash-locks" in urldata else None

    version = urldata["version"] if "version" in urldata else None
    transforms = urldata["transforms"] if "transforms" in urldata else None

    url = urldata["url"].format(pkgname=pkgname, version=version if version != None else "")

    if transforms != None and version != None:
        raise lib.TinyError(f"The URL generator does not support having both \"version\" and \"transform\" keys at the same time.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
    }
    headers = urldata["headers"] if "headers" in urldata else headers
    
    print(f"URL generator - Generating package: {pkgname}")

    response = requests.get(url, headers=headers, timeout=10, stream=True)
    response.raise_for_status()
    size = int(response.headers.get("content-length", 0))

    if response.status_code == 200:
        buf = lib.download_to_buffer(response, size, url)
        
        result = {
            "tarball-urls": [
                {
                    "url": url,
                    "checksums": {}
                }
            ]
        }
        lib.calculate_hashes(result["tarball-urls"][0]["checksums"], buf.getvalue())
        
        if locks != None:
            is_matching_lock = False
            for val in result["tarball-urls"][0]["checksums"].values():
                for h in locks:
                    if val == h:
                        is_matching_lock = True
                        break

                if is_matching_lock:
                    break

            if not is_matching_lock:
                raise lib.TinyError(f"Checksums calculated for URL({urldata['url']}) do not match any locked checksums!")

        if version != None:
            result["version"] = version
        elif transforms != None:
            urltmp = url

            for transform in transforms:
                if type(transform) == list and len(transform) >= 2:
                    urltmp = re.sub(transform[0], transform[1], urltmp)
                else:
                    raise lib.TinyError(f"Elements of the transforms array must be arrays with 2 elements!")

            result["version"] = urltmp

        if size != 0:
            result["tarball-urls"][0]["size"] = size

        print(result)
    else:
        raise lib.TinyError(f"Failed to fetch file with URL: {urldata['url']}. HTTP Response: {response.status_code} {response.reason}.")
        

generate("""
{
    "name": "untitled-exec",
    "url-generator":
    {
        "url": "https://github.com/MadLadSquad/UImGuiSDL/releases/tag/v1.0.0.0",
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0.1 Safari/605.1.15"
        },
        "transforms": [
            [ "https.*tag/v", "" ], 
            [ "\\\\.", "/" ]
        ],
        "size": "363"
    }
}
""")
