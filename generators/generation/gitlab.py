#!/usr/bin/env python3
"""
The pkggen GitLab fetch generator. Mirrors the GitHub generator: same keys, queries and output.

Input (one package from pkggen.yaml, as JSON on stdin):
    {
        "name": "pkgname",
        "gitlab":
        {
            // The namespace may contain subgroups, e.g. "group/subgroup"
            "user": "user",
            "repo": "repo",

            // One of: release(s), tag(s) or commit(s).
            // Releases and tags take their version from the tag name (after "transforms").
            // Commits use the committer date as the version, formatted as YYYYMMDD.
            "query": "releases",

            // Optional: only accept tag names matching this regex
            "select": "^v\\d+\\.\\d+\\.\\d+$",

            // Optional: [pattern, replacement] regex pairs applied in order to the
            // tag name to turn it into a version
            "transforms": [ [ "^v", "" ] ],

            // Optional, releases only: release asset links to download. When set, ONLY
            // these files are downloaded and a missing one is an error.
            // Placeholders: {version}, {pkgname}, {gitlab_user}, {gitlab_repo}
            "artifacts": [ "pkgname-{version}.tar.xz", "resources.tar.xz" ],

            // Optional, releases only: also download GitLab's auto-generated source
            // tarball when "artifacts" is set. Defaults to false.
            "include-source": false,

            // Optional: lock to an exact tag name (releases/tags) or commit hash (commits).
            // Can't be combined with "select".
            "version": "v1.0.0.0",

            // Optional: also accept upcoming releases (release date in the future).
            // Defaults to false. GitLab has no drafts or pre-releases, so the GitHub
            // generator's "include-drafts" and "include-pre-releases" are ignored.
            "include-upcoming": false,

            // Optional, for self-hosted instances. May include a path prefix
            // ("example.com/gitlab"). Defaults to gitlab.com
            "domain": "gitlab.com"
        }
    }

Secrets (secrets.yaml): "gitlab_key" is used for gitlab.com; other instances take their
token from "gitlab_keys: { <domain>: <token> }", so a gitlab.com token never leaves gitlab.com.

Output (JSON on stdout):
    {
        "version": "1.0.0.0",
        "date": "2026-01-01T00:00:00+00:00",   // release/commit date, ISO 8601 UTC
        "tag": "v1.0.0.0",            // releases and tags only
        "commit": "<sha>",            // commits only
        "artifacts": [
            {
                "url": "https://...",
                "alias": "pkgname-1.0.0.0.tar.gz",   // unique distfile name
                "type": "source",                    // "source" or "asset"
                "directory": "repo-v1.0.0.0",        // folder it most likely extracts to
                "size": 1234,
                "checksums": { "md5", "sha1", "sha2_224" ... "sha3_512", "blake2b", "blake2s" }
            }
        ],
        // Project metadata from the GitLab API, only fetched for keys the
        // package doesn't already define
        "exports": { "description": "...", "license": "MIT", "homepage": "..." }
    }
"""
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

import requests

import lib

QUERIES = {
    "release": "releases", "releases": "releases",
    "tag": "tags", "tags": "tags",
    "commit": "commits", "commits": "commits",
}
PER_PAGE = 100
DEFAULT_DOMAIN = "gitlab.com"

# GitLab reports Licensee keys; map them to the SPDX ids GitHub reports for the same
# licenses, so the packaging filters (spdx, debian_license, gentoo_license) apply unchanged
LICENSES = {
    "0bsd": "0BSD", "afl-3.0": "AFL-3.0", "agpl-3.0": "AGPL-3.0", "apache-2.0": "Apache-2.0",
    "artistic-2.0": "Artistic-2.0", "blueoak-1.0.0": "BlueOak-1.0.0",
    "bsd-2-clause": "BSD-2-Clause", "bsd-2-clause-patent": "BSD-2-Clause-Patent",
    "bsd-3-clause": "BSD-3-Clause", "bsd-3-clause-clear": "BSD-3-Clause-Clear",
    "bsd-4-clause": "BSD-4-Clause", "bsl-1.0": "BSL-1.0",
    "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0", "cc0-1.0": "CC0-1.0",
    "cecill-2.1": "CECILL-2.1",
    "cern-ohl-p-2.0": "CERN-OHL-P-2.0", "cern-ohl-s-2.0": "CERN-OHL-S-2.0", "cern-ohl-w-2.0": "CERN-OHL-W-2.0",
    "ecl-2.0": "ECL-2.0", "epl-1.0": "EPL-1.0", "epl-2.0": "EPL-2.0", "eupl-1.1": "EUPL-1.1", "eupl-1.2": "EUPL-1.2",
    "gfdl-1.3": "GFDL-1.3", "gpl-2.0": "GPL-2.0", "gpl-3.0": "GPL-3.0", "isc": "ISC",
    "lgpl-2.1": "LGPL-2.1", "lgpl-3.0": "LGPL-3.0", "lppl-1.3c": "LPPL-1.3c",
    "mit": "MIT", "mit-0": "MIT-0", "mpl-2.0": "MPL-2.0", "ms-pl": "MS-PL", "ms-rl": "MS-RL",
    "mulanpsl-2.0": "MulanPSL-2.0", "ncsa": "NCSA", "odbl-1.0": "ODbL-1.0", "ofl-1.1": "OFL-1.1",
    "osl-3.0": "OSL-3.0", "postgresql": "PostgreSQL", "unlicense": "Unlicense", "upl-1.0": "UPL-1.0",
    "vim": "Vim", "wtfpl": "WTFPL", "zlib": "Zlib",
}


class GitLab:
    def __init__(self, data, pkgname, position):
        if not isinstance(data, dict):
            raise lib.TinyError("'gitlab' must be a mapping")
        for key in ("user", "repo", "query"):
            if not data.get(key):
                raise lib.TinyError(f"no GitLab '{key}' provided")

        self.pkgname = pkgname
        self.position = position
        self.user = str(data["user"]).strip("/")
        self.repo = str(data["repo"]).strip("/")
        self.slug = f"{self.user}/{self.repo}"

        self.query = QUERIES.get(str(data["query"]).lower())
        if self.query is None:
            raise lib.TinyError(f"invalid GitLab query {data['query']!r}, expected one of: release, tag, commit")

        self.version = data.get("version")
        # YAML reads `version: 1.10` as the number 1.1: converting it back could pick the wrong tag
        if self.version is not None and not isinstance(self.version, str):
            raise lib.TinyError(f"'version' must be the exact tag name or commit hash in quotes, like "
                                f"version: \"{self.version}\" (YAML reads unquoted numbers as numbers: "
                                "1.10 becomes 1.1)")
        self.transforms = data.get("transforms")
        self.artifacts = data.get("artifacts")
        if self.artifacts is not None and not isinstance(self.artifacts, list):
            raise lib.TinyError("'artifacts' must be a list of file names")
        if self.artifacts is not None and self.query != "releases":
            lib.warn(f"'artifacts' only works with release queries, ignoring it for {self.query}")
            self.artifacts = None

        self.select = None
        if data.get("select") is not None:
            if self.version is not None:
                raise lib.TinyError("'version' and 'select' can't be used together")
            try:
                self.select = re.compile(data["select"])
            except re.error as e:
                raise lib.TinyError(f"invalid 'select' regex {data['select']!r}: {e}")

        for key in ("include-drafts", "include-pre-releases"):
            if lib.to_bool(data.get(key)):
                lib.warn(f"[{pkgname}] GitLab has no drafts or pre-releases, ignoring '{key}'. "
                         "Use 'select' to filter tags by name, or 'include-upcoming' for future releases")
        self.include_upcoming = lib.to_bool(data.get("include-upcoming"))
        self.include_source = lib.to_bool(data.get("include-source"))

        self.domain = str(data.get("domain") or DEFAULT_DOMAIN).strip("/")
        self.host = urlsplit(f"https://{self.domain}").netloc
        self.api_base = f"https://{self.domain}/api/v4/projects/{quote(self.slug, safe='')}"

        secrets = lib.load_secrets()
        keys = secrets.get("gitlab_keys") or {}
        if not isinstance(keys, dict):
            raise lib.TinyError("'gitlab_keys' in secrets.yaml must be a mapping of domains to tokens")
        key = keys.get(self.domain)
        if key is None and self.domain == DEFAULT_DOMAIN:
            key = secrets.get("gitlab_key")
        if self.domain == DEFAULT_DOMAIN:
            self.secret_hint = "a 'gitlab_key'"
        else:
            self.secret_hint = f"a 'gitlab_keys' entry for {self.domain}"
        # Sent to this instance only: requests drops it on redirects to other hosts
        self.auth = {"Authorization": f"Bearer {key}"} if key else {}
        self.session = lib.session()

    def api(self, path, what, **params):
        url = f"{self.api_base}{path}"
        response = self.session.get(url, headers=self.auth, params=params, timeout=lib.TIMEOUT)
        if response.status_code != 200:
            raise lib.http_error(response, what, self.secret_hint)
        return response.json()

    def download(self, url, alias, kind, directory):
        # Release links can point anywhere: only send the token to this instance
        headers = self.auth if urlsplit(url).netloc == self.host else {}
        result = lib.download_and_hash(self.session, url, headers=headers, position=self.position, label=alias,
                                       secret_hint=self.secret_hint)
        result.pop("last_modified", None)
        return {"url": url, "alias": alias, "type": kind, "directory": directory, **result}

    def source_archive(self, ref, version):
        """URL, alias and top-level folder of GitLab's auto-generated tarball for ref."""
        # GitLab names the folder after the file only when the file is called <repo>-<ref>
        # (with "/" turned into "-"), otherwise it appends the commit hash. A leading "v" is kept
        name = f"{self.repo}-{ref.replace('/', '-')}"
        url = f"https://{self.domain}/{self.slug}/-/archive/{quote(ref, safe='/')}/{quote(name)}.tar.gz"
        return url, f"{self.pkgname}-{version}.tar.gz", name


def to_date_version(date):
    return datetime.fromisoformat(date).strftime("%Y%m%d")


def generate_commit(gl):
    if gl.version is not None:
        commit = gl.api(f"/repository/commits/{quote(str(gl.version), safe='')}",
                        f"find commit {gl.version!r} in {gl.slug}")
    else:
        commits = gl.api("/repository/commits", f"list commits of {gl.slug}", per_page=1)
        if not commits:
            raise lib.TinyError(f"{gl.slug} has no commits")
        commit = commits[0]

    sha = commit["id"]
    date = lib.iso_date(commit["committed_date"])
    version = to_date_version(date)
    url, alias, directory = gl.source_archive(sha, version)
    return {
        "version": version,
        "date": date,
        "commit": sha,
        "artifacts": [gl.download(url, alias, "source", directory)],
    }


def release_allowed(gl, release):
    if release.get("upcoming_release") and not gl.include_upcoming:
        return False
    return True


def find_tag_or_release(gl):
    # Releases have a free-form title in "name"; the tag is in "tag_name"
    if gl.query == "releases":
        path, key, order = "/releases", "tag_name", "released_at"
    else:
        path, key, order = "/repository/tags", "name", "updated"
    page = 1
    while True:
        entries = gl.api(path, f"list {gl.query} of {gl.slug}",
                         order_by=order, sort="desc", page=page, per_page=PER_PAGE)
        for entry in entries:
            tag = entry.get(key) or ""
            if gl.version is not None:
                if tag == gl.version:
                    return entry, tag
                continue
            if gl.query == "releases" and not release_allowed(gl, entry):
                continue
            if gl.select is not None and not gl.select.search(tag):
                continue
            return entry, tag

        if len(entries) < PER_PAGE:
            break
        page += 1

    kind = gl.query[:-1]
    if gl.version is not None:
        criteria = f"named {gl.version!r}"
    else:
        parts = []
        if gl.select is not None:
            parts.append(f"matching {gl.select.pattern!r}")
        if gl.query == "releases" and not gl.include_upcoming:
            parts.append("excluding upcoming releases")
        criteria = ", ".join(parts) or "at all"
    raise lib.TinyError(f"no {kind} found in {gl.slug} {criteria}")


def format_artifact(gl, pattern, version):
    try:
        return pattern.format(pkgname=gl.pkgname, version=version, gitlab_user=gl.user, gitlab_repo=gl.repo)
    except (KeyError, IndexError) as e:
        raise lib.TinyError(f"artifact pattern {pattern!r} uses an unknown placeholder {e}. "
                            "Allowed: {version}, {pkgname}, {gitlab_user}, {gitlab_repo}")


def generate_tag_or_release(gl):
    entry, tag = find_tag_or_release(gl)
    version = lib.apply_transforms(gl.transforms, tag)
    if not version:
        raise lib.TinyError(f"the transforms turned tag {tag!r} into an empty version")

    downloads = []
    if gl.artifacts is None or gl.include_source:
        url, alias, directory = gl.source_archive(tag, version)
        downloads.append((url, alias, "source", directory))

    if gl.artifacts is not None:
        links = {link["name"]: link for link in (entry.get("assets") or {}).get("links", [])}
        wanted = [format_artifact(gl, pattern, version) for pattern in gl.artifacts]
        missing = [name for name in wanted if name not in links]
        if missing:
            available = ", ".join(sorted(links)) or "none"
            raise lib.TinyError(f"release {tag!r} of {gl.slug} has no asset named {', '.join(missing)} "
                                f"(available: {available})")
        # Listed artifacts come first, so artifacts[0] is the first file the user asked for.
        # direct_asset_url is the permanent /-/releases/<tag>/downloads/<file> link
        downloads = [(links[name].get("direct_asset_url") or links[name]["url"], name, "asset",
                      lib.archive_stem(name)) for name in wanted] + downloads

    if gl.query == "releases":
        date = lib.iso_date(entry.get("released_at") or entry.get("created_at"))
    else:
        # Unlike GitHub, tag entries already carry their commit's date
        date = lib.iso_date((entry.get("commit") or {}).get("committed_date"))

    result = {
        "version": version,
        "tag": tag,
        "artifacts": [gl.download(*download) for download in downloads],
    }
    if date:
        result["date"] = date
    return result


def get_exports(gl, package):
    wanted = [key for key in ("description", "license", "homepage") if not package.get(key)]
    if not wanted:
        return {}

    try:
        data = gl.api("", f"fetch project metadata of {gl.slug}", license="true")
    except (lib.TinyError, requests.RequestException) as e:
        lib.warn(f"[{gl.pkgname}] {e}. Continuing without exported metadata")
        return {}

    license_key = str((data.get("license") or {}).get("key") or "").lower()
    found = {
        "description": data.get("description"),
        "license": LICENSES.get(license_key),
        "homepage": data.get("web_url"),
    }
    return {key: found[key] for key in wanted if found[key]}


def generate(package):
    if "gitlab" not in package:
        raise lib.TinyError("no 'gitlab' block found in the package metadata")

    gl = GitLab(package["gitlab"], package["name"], int(package.get("position", 0)))
    if gl.query == "commits":
        result = generate_commit(gl)
    else:
        result = generate_tag_or_release(gl)
    result["exports"] = get_exports(gl, package)
    return result


if __name__ == "__main__":
    lib.run(generate)
