#!/usr/bin/env python3
"""
The pkggen URL fetch generator.

Input (one package from pkggen.yaml, as JSON on stdin):
    {
        "name": "pkgname",
        "url-generator":
        {
            // The URL to fetch. Placeholders: {pkgname}, and {version} when "version" is set
            "url": "https://example.com/{pkgname}-{version}.tar.gz",

            // Exactly one of "version" or "transforms" is required:
            //
            // A fixed version, for artifacts that are rarely updated and have no version in
            // their URL. Pair it with "hash-locks" for strict version locking.
            "version": "1.0.0.0",
            //
            // Or [pattern, replacement] regex pairs, applied in order to the URL to
            // extract the version from it.
            "transforms": [ [ "https.*tag/v", "" ] ],

            // Optional: accepted checksums (any algorithm). The download must match one.
            "hash-locks": [ "hash", "hash" ],

            // Optional: distfile name. Placeholders: {pkgname}, {version}.
            // Defaults to the last part of the URL path.
            "alias": "{pkgname}-{version}.tar.gz",

            // Optional: extra HTTP headers, merged over the defaults
            "headers": { "User-Agent": "My user agent" }
        }
    }

Output (JSON on stdout):
    {
        "version": "1.0.0.0",
        "date": "2026-01-01T00:00:00+00:00",   // from Last-Modified, when the server sends it
        "artifacts": [
            {
                "url": "https://example.com/...",
                "alias": "pkgname-1.0.0.0.tar.gz",
                "type": "source",
                "directory": "pkgname-1.0.0.0",   // folder it most likely extracts to
                "size": 1234,
                "checksums": { "md5", "sha1", "sha2_224" ... "sha3_512", "blake2b", "blake2s" }
            }
        ],
        "exports": {}
    }
"""
import posixpath
from urllib.parse import unquote, urlparse

import requests

import lib

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
}


def fill(template, key, **values):
    try:
        return str(template).format(**values)
    except (KeyError, IndexError) as e:
        allowed = ", ".join("{" + name + "}" for name in values)
        raise lib.TinyError(f"'{key}' uses an unknown placeholder {e}. Allowed: {allowed}")


def generate(package):
    pkgname = package["name"]
    config = package.get("url-generator")
    if not isinstance(config, dict):
        raise lib.TinyError("no 'url-generator' block found in the package metadata")
    if not config.get("url"):
        raise lib.TinyError("'url-generator' has no 'url'")

    version = config.get("version")
    transforms = config.get("transforms")
    if version is not None and transforms is not None:
        raise lib.TinyError("'version' and 'transforms' can't be used together")
    if version is None and transforms is None:
        raise lib.TinyError("set either 'version' or 'transforms' so the package gets a version")
    if version is None and "{version}" in str(config["url"]):
        raise lib.TinyError("the URL uses {version}, which needs a fixed 'version'")

    if version is not None:
        version = str(version)
        url = fill(config["url"], "url", pkgname=pkgname, version=version)
    else:
        url = fill(config["url"], "url", pkgname=pkgname)
        version = lib.apply_transforms(transforms, url)
        if not version:
            raise lib.TinyError(f"the transforms turned {url} into an empty version")

    if config.get("alias"):
        alias = fill(config["alias"], "alias", pkgname=pkgname, version=version)
    else:
        alias = posixpath.basename(unquote(urlparse(url).path)) or f"{pkgname}-{version}"

    headers = {**DEFAULT_HEADERS, **(config.get("headers") or {})}
    artifact = lib.download_and_hash(requests.Session(), url, headers=headers,
                                     position=int(package.get("position", 0)), label=alias)

    locks = config.get("hash-locks")
    if locks is not None:
        locks = {str(h).lower() for h in (locks if isinstance(locks, list) else [locks])}
        if not locks.intersection(artifact["checksums"].values()):
            raise lib.TinyError(f"{url} doesn't match any of the 'hash-locks' "
                                f"(its sha2_256 is {artifact['checksums']['sha2_256']})")

    date = artifact.pop("last_modified", None)
    result = {
        "version": version,
        "artifacts": [{"url": url, "alias": alias, "type": "source", "directory": lib.archive_stem(alias), **artifact}],
        "exports": {},
    }
    if date:
        result["date"] = date
    return result


if __name__ == "__main__":
    lib.run(generate)
