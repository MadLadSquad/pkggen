#!/usr/bin/env python3
"""
The pkggen GitHub fetch generator.

Input (one package from pkggen.yaml, as JSON on stdin):
    {
        "name": "pkgname",
        "github":
        {
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

            // Optional, releases only: release assets to download. When set, ONLY these
            // files are downloaded and a missing one is an error.
            // Placeholders: {version}, {pkgname}, {github_user}, {github_repo}
            "artifacts": [ "pkgname-{version}.tar.xz", "resources.tar.xz" ],

            // Optional, releases only: also download GitHub's auto-generated source
            // tarball when "artifacts" is set. Defaults to false.
            "include-source": false,

            // Optional: lock to an exact tag name (releases/tags) or commit hash (commits).
            // Can't be combined with "select".
            "version": "v1.0.0.0",

            // Optional: also accept draft releases / pre-releases. Default to false.
            "include-drafts": false,
            "include-pre-releases": false,

            // Optional, for GitHub Enterprise. Default to github.com and api.github.com
            "domain": "github.com",
            "api-domain": "api.github.com"
        }
    }

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
                "directory": "repo-1.0.0.0",         // folder it most likely extracts to
                "size": 1234,
                "checksums": { "md5", "sha1", "sha2_224" ... "sha3_512", "blake2b", "blake2s" }
            }
        ],
        // Repository metadata from the GitHub API, only fetched for keys the
        // package doesn't already define
        "exports": { "description": "...", "license": "MIT", "homepage": "..." }
    }
"""
import re
from datetime import datetime
from urllib.parse import quote

import requests

import lib

QUERIES = {
    "release": "releases", "releases": "releases",
    "tag": "tags", "tags": "tags",
    "commit": "commits", "commits": "commits",
}
PER_PAGE = 100


class GitHub:
    def __init__(self, data, pkgname, position):
        if not isinstance(data, dict):
            raise lib.TinyError("'github' must be a mapping")
        for key in ("user", "repo", "query"):
            if not data.get(key):
                raise lib.TinyError(f"no GitHub '{key}' provided")

        self.pkgname = pkgname
        self.position = position
        self.user = data["user"]
        self.repo = data["repo"]
        self.slug = f"{self.user}/{self.repo}"

        self.query = QUERIES.get(str(data["query"]).lower())
        if self.query is None:
            raise lib.TinyError(f"invalid GitHub query {data['query']!r}, expected one of: release, tag, commit")

        self.version = data.get("version")
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

        self.include_drafts = lib.to_bool(data.get("include-drafts"))
        self.include_pre_releases = lib.to_bool(data.get("include-pre-releases"))
        self.include_source = lib.to_bool(data.get("include-source"))

        self.domain = data.get("domain") or "github.com"
        self.api_domain = data.get("api-domain") or "api.github.com"

        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        key = lib.load_secrets().get("github_key")
        # Sent to GitHub only: requests drops it on redirects to other hosts
        self.auth = {"Authorization": f"Bearer {key}"} if key else {}
        self.session = lib.session()

    def api(self, path, what, **params):
        url = f"https://{self.api_domain}/repos/{self.user}/{self.repo}{path}"
        response = self.session.get(url, headers={**self.headers, **self.auth}, params=params, timeout=lib.TIMEOUT)
        if response.status_code != 200:
            raise lib.http_error(response, what)
        return response.json()

    def download(self, url, alias, kind, directory):
        result = lib.download_and_hash(self.session, url, headers=self.auth, position=self.position, label=alias)
        result.pop("last_modified", None)
        return {"url": url, "alias": alias, "type": kind, "directory": directory, **result}

    def source_archive(self, ref, version):
        """URL, alias and top-level folder of GitHub's auto-generated tarball for ref."""
        url = f"https://{self.domain}/{self.user}/{self.repo}/archive/{quote(ref, safe='/')}.tar.gz"
        # GitHub names the folder <repo>-<ref>, without "refs/tags/", with a leading "v" before
        # a digit dropped and with "/" turned into "-"
        name = ref.removeprefix("refs/tags/")
        if re.match(r"v\d", name):
            name = name[1:]
        return url, f"{self.pkgname}-{version}.tar.gz", f"{self.repo}-{name.replace('/', '-')}"


def to_date_version(date):
    return datetime.fromisoformat(date).strftime("%Y%m%d")


def generate_commit(gh):
    if gh.version is not None:
        commit = gh.api(f"/commits/{gh.version}", f"find commit {gh.version!r} in {gh.slug}")
    else:
        commits = gh.api("/commits", f"list commits of {gh.slug}", per_page=1)
        if not commits:
            raise lib.TinyError(f"{gh.slug} has no commits")
        commit = commits[0]

    sha = commit["sha"]
    date = lib.iso_date(commit["commit"]["committer"]["date"])
    version = to_date_version(date)
    url, alias, directory = gh.source_archive(sha, version)
    return {
        "version": version,
        "date": date,
        "commit": sha,
        "artifacts": [gh.download(url, alias, "source", directory)],
    }


def release_allowed(gh, release):
    if release.get("draft") and not gh.include_drafts:
        return False
    if release.get("prerelease") and not gh.include_pre_releases:
        return False
    return True


def find_tag_or_release(gh):
    # Releases have a free-form title in "name"; the tag is in "tag_name"
    key = "tag_name" if gh.query == "releases" else "name"
    page = 1
    while True:
        entries = gh.api(f"/{gh.query}", f"list {gh.query} of {gh.slug}", page=page, per_page=PER_PAGE)
        for entry in entries:
            tag = entry.get(key) or ""
            if gh.version is not None:
                if tag == gh.version:
                    return entry, tag
                continue
            if gh.query == "releases" and not release_allowed(gh, entry):
                continue
            if gh.select is not None and not gh.select.search(tag):
                continue
            return entry, tag

        if len(entries) < PER_PAGE:
            break
        page += 1

    kind = gh.query[:-1]
    if gh.version is not None:
        criteria = f"named {gh.version!r}"
    else:
        parts = []
        if gh.select is not None:
            parts.append(f"matching {gh.select.pattern!r}")
        if gh.query == "releases":
            if not gh.include_drafts:
                parts.append("excluding drafts")
            if not gh.include_pre_releases:
                parts.append("excluding pre-releases")
        criteria = ", ".join(parts) or "at all"
    raise lib.TinyError(f"no {kind} found in {gh.slug} {criteria}")


def format_artifact(gh, pattern, version):
    try:
        return pattern.format(pkgname=gh.pkgname, version=version, github_user=gh.user, github_repo=gh.repo)
    except (KeyError, IndexError) as e:
        raise lib.TinyError(f"artifact pattern {pattern!r} uses an unknown placeholder {e}. "
                            "Allowed: {version}, {pkgname}, {github_user}, {github_repo}")


def generate_tag_or_release(gh):
    entry, tag = find_tag_or_release(gh)
    version = lib.apply_transforms(gh.transforms, tag)
    if not version:
        raise lib.TinyError(f"the transforms turned tag {tag!r} into an empty version")

    downloads = []
    if gh.artifacts is None or gh.include_source:
        url, alias, directory = gh.source_archive(f"refs/tags/{tag}", version)
        downloads.append((url, alias, "source", directory))

    if gh.artifacts is not None:
        assets = {asset["name"]: asset for asset in entry.get("assets", [])}
        wanted = [format_artifact(gh, pattern, version) for pattern in gh.artifacts]
        missing = [name for name in wanted if name not in assets]
        if missing:
            available = ", ".join(sorted(assets)) or "none"
            raise lib.TinyError(f"release {tag!r} of {gh.slug} has no asset named {', '.join(missing)} "
                                f"(available: {available})")
        # Listed artifacts come first, so artifacts[0] is the first file the user asked for
        downloads = [(assets[name]["browser_download_url"], name, "asset", lib.archive_stem(name))
                     for name in wanted] + downloads

    if gh.query == "releases":
        date = lib.iso_date(entry.get("published_at") or entry.get("created_at"))
    else:
        # Tag entries carry no date, so use the date of the commit they point to
        sha = entry["commit"]["sha"]
        commit = gh.api(f"/commits/{sha}", f"look up commit {sha} of tag {tag!r} in {gh.slug}")
        date = lib.iso_date(commit["commit"]["committer"]["date"])

    result = {
        "version": version,
        "tag": tag,
        "artifacts": [gh.download(*download) for download in downloads],
    }
    if date:
        result["date"] = date
    return result


def get_exports(gh, package):
    wanted = [key for key in ("description", "license", "homepage") if not package.get(key)]
    if not wanted:
        return {}

    try:
        data = gh.api("", f"fetch repository metadata of {gh.slug}")
    except (lib.TinyError, requests.RequestException) as e:
        lib.warn(f"[{gh.pkgname}] {e}. Continuing without exported metadata")
        return {}

    license_id = (data.get("license") or {}).get("spdx_id")
    found = {
        "description": data.get("description"),
        "license": license_id if license_id and license_id != "NOASSERTION" else None,
        "homepage": data.get("homepage") or data.get("html_url"),
    }
    return {key: found[key] for key in wanted if found[key]}


def generate(package):
    if "github" not in package:
        raise lib.TinyError("no 'github' block found in the package metadata")

    gh = GitHub(package["github"], package["name"], int(package.get("position", 0)))
    if gh.query == "commits":
        result = generate_commit(gh)
    else:
        result = generate_tag_or_release(gh)
    result["exports"] = get_exports(gh, package)
    return result


if __name__ == "__main__":
    lib.run(generate)
