#!/usr/bin/env python3

"""
The pkggen URL generator:

Input:
    name:
    url-generator:
        url: "https://example.com/"
        hash-locks: [ hash, hash, hash ]
        headers:
            User-Agent: "My user agent"

Output:
    {
        "url": "https://example.com",
        "size": "1234"
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
        },
    }

"""

import json
import requests
import platform
import os
import hashlib
from tqdm import tqdm
from io import BytesIO


class TinyError(Exception):
    def __init__(self, message):
        super().__init__("\x1b[31m" + message + "\x1b[0m")
        self.args = (message, )
        self.__traceback__ = None

def calculate_hash(obj, data, algorithm, key):
    if key != "blake2b" or key != "blake2s":
        h = hashlib.new(algorithm, usedforsecurity=False)
    else:
        h = hashlib.blake2b(digest_size=64) if key == "blake2b" else hashlib.blake2s(digest_size=32)
    h.update(data)
    obj[key] = h.hexdigest()

def calculate_hashes(obj, data):
    calculate_hash(obj, data, "md5", "md5")

    calculate_hash(obj, data, "sha1", "sha1")

    calculate_hash(obj, data, "sha224", "sha2-224")
    calculate_hash(obj, data, "sha3_224", "sha3-224")

    calculate_hash(obj, data, "sha256", "sha2-256")
    calculate_hash(obj, data, "sha3_256", "sha3-256")
    
    calculate_hash(obj, data, "sha384", "sha2-384")
    calculate_hash(obj, data, "sha3_384", "sha3-384")

    calculate_hash(obj, data, "sha512", "sha2-512")
    calculate_hash(obj, data, "sha3_512", "sha3-512")

    calculate_hash(obj, data, "blake2b", "blake2b")
    calculate_hash(obj, data, "blake2s", "blake2s")

def generate(x):
    data = json.loads(x)
    
    pkgname = data["name"]
    if "url-generator" not in data:
        raise TinyError(f"No object named \"url-generator\" found inside the \"{pkgname}\" package's metadata!")
    urldata = data["url-generator"]

    if "url" not in urldata:
        raise TinyError(f"No string entry named \"url\" found inside the \"{pkgname}\" package's metadata!")
    
    locks = urldata["hash-locks"] if "hash-locks" in urldata else None

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15',
    }
    headers = urldata["headers"] if "headers" in urldata else headers

    urldata = data["url-generator"]
    
    print(f"URL generator - Generating package: {pkgname}")

    response = requests.get(urldata["url"], headers=headers, timeout=10, stream=True)
    response.raise_for_status()
    size = int(response.headers.get("content-length", 0))

    if response.status_code == 200:
        buf = BytesIO()

        with tqdm(total=size if size > 0 else None, unit="B", unit_scale=True, desc=f"Downloading {urldata['url']}: ") as pbar:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                buf.write(chunk)
                pbar.update(len(chunk))

        result = {
            "url": urldata["url"],
            "checksums": {}
        }
        calculate_hashes(result["checksums"], buf.getvalue())
        
        if locks != None:
            is_matching_lock = False
            for val in result["checksums"].values():
                for h in locks:
                    if val == h:
                        is_matching_lock = True
                        break

                if is_matching_lock:
                    break

            if not is_matching_lock:
                raise TinyError(f"Checksums calculated for URL({urldata['url']}) do not match any locked checksums!")

        if size != 0:
            result["size"] = size

        print(result)
    else:
        raise TinyError(f"Failed to fetch file with URL: {urldata['url']}. HTTP Response: {response.status_code} {response.reason}.")
        

generate("""
{
    "name": "untitled-exec",
    "url-generator":
    {
        "url": "https://example.com/",
        "hash-locks": 
        [
            "6f5635035f36ad500b4fc4bb7816bb72ef5594e1bcae44fa074c5e988fc4c0fe"
        ],
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0.1 Safari/605.1.15"
        }
    }
}
""")
