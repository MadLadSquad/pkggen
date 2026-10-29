import pytest

from tests.support import example
from tests.support.example import archive_bytes, checksums, example_routes
from tests.support.helpers import load_generator, run_generator

API = "https://api.github.com/repos/u/r"
FIRST_PAGE = {"page": "1", "per_page": "100"}


@pytest.fixture
def gh():
    return load_generator("generation", "github")


def release(repo, tag, assets, **kwargs):
    return example.release(repo, tag, assets, downloads="https://github.com/u", **kwargs)


def package(**github):
    return {"name": "pkg", "github": {"user": "u", "repo": "r", "query": "releases", **github}}


def asset_url(tag, name):
    return f"https://github.com/u/r/releases/download/{tag}/{name}"


def add_releases(http, releases, page=FIRST_PAGE):
    http.add("GET", f"{API}/releases", json=releases, match=[query(page)])


def query(params):
    from responses import matchers
    return matchers.query_param_matcher(params)


def add_file(http, url, name=None):
    data = archive_bytes(name or url)
    http.add("GET", url, body=data)
    return data


def add_metadata(http, **data):
    body = {"description": "Upstream description", "license": {"spdx_id": "MIT"}, "homepage": "https://r.example.com",
            "html_url": "https://github.com/u/r", **data}
    http.add("GET", API, json=body)


# --- Releases ------------------------------------------------------------------------

def test_release_with_artifacts(gh, http):
    add_releases(http, [release("r", "v1.2.0", ["pkg-1.2.0.tar.xz", "other.zip"], published="2026-03-04T05:06:07Z")])
    data = add_file(http, asset_url("v1.2.0", "pkg-1.2.0.tar.xz"))
    add_metadata(http)

    result = gh.generate(package(artifacts=["{pkgname}-{version}.tar.xz"], transforms=[["^v", ""]]))
    assert result == {
        "version": "1.2.0",
        "tag": "v1.2.0",
        "date": "2026-03-04T05:06:07+00:00",
        "artifacts": [{"url": asset_url("v1.2.0", "pkg-1.2.0.tar.xz"), "alias": "pkg-1.2.0.tar.xz", "type": "asset",
                       "directory": "pkg-1.2.0", "size": len(data), "checksums": checksums(data)}],
        "exports": {"description": "Upstream description", "license": "MIT", "homepage": "https://r.example.com"},
    }
    api_headers = http.calls[0].request.headers
    assert api_headers["Accept"] == "application/vnd.github+json"
    assert api_headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert "Authorization" not in api_headers


def test_release_source_tarball(gh, http):
    add_releases(http, [release("r", "v2.0", [])])
    url = "https://github.com/u/r/archive/refs/tags/v2.0.tar.gz"
    data = add_file(http, url)
    add_metadata(http)
    result = gh.generate(package(transforms=[["^v", ""]]))
    assert result["artifacts"] == [{"url": url, "alias": "pkg-2.0.tar.gz", "type": "source", "directory": "r-2.0",
                                    "size": len(data), "checksums": checksums(data)}]


def test_include_source_puts_listed_artifacts_first(gh, http):
    add_releases(http, [release("r", "1.0", ["a.tar.gz", "b-1.0-r.zip"])])
    add_file(http, asset_url("1.0", "a.tar.gz"))
    add_file(http, asset_url("1.0", "b-1.0-r.zip"))
    add_file(http, "https://github.com/u/r/archive/refs/tags/1.0.tar.gz")
    add_metadata(http)
    result = gh.generate(package(artifacts=["b-{version}-{github_repo}.zip", "a.tar.gz"], **{"include-source": "yes"}))
    assert [(a["alias"], a["type"]) for a in result["artifacts"]] == [
        ("b-1.0-r.zip", "asset"), ("a.tar.gz", "asset"), ("pkg-1.0.tar.gz", "source")]


def test_drafts_and_pre_releases_are_skipped(gh, http):
    add_releases(http, [release("r", "3.0", [], draft=True), release("r", "2.0", [], prerelease=True),
                        release("r", "1.0", [])])
    add_file(http, "https://github.com/u/r/archive/refs/tags/1.0.tar.gz")
    add_metadata(http)
    assert gh.generate(package())["version"] == "1.0"


@pytest.mark.parametrize("flags, version", [({"include-drafts": True}, "3.0"), ({"include-pre-releases": "1"}, "2.0")])
def test_drafts_and_pre_releases_can_be_included(gh, http, flags, version):
    add_releases(http, [release("r", "3.0", [], draft=True), release("r", "2.0", [], prerelease=True)])
    add_file(http, f"https://github.com/u/r/archive/refs/tags/{version}.tar.gz")
    add_metadata(http)
    assert gh.generate(package(**flags))["version"] == version


def test_select(gh, http):
    add_releases(http, [release("r", "nightly", []), release("r", "v1.5.0", [])])
    add_file(http, "https://github.com/u/r/archive/refs/tags/v1.5.0.tar.gz")
    add_metadata(http)
    assert gh.generate(package(select=r"^v\d"))["tag"] == "v1.5.0"


def test_version_lock_on_a_later_page(gh, http):
    add_releases(http, [release("r", f"v0.{i}", []) for i in range(100)])
    add_releases(http, [release("r", "v9.9", [])], page={"page": "2", "per_page": "100"})
    add_file(http, "https://github.com/u/r/archive/refs/tags/v9.9.tar.gz")
    add_metadata(http)
    assert gh.generate(package(version="v9.9"))["tag"] == "v9.9"


@pytest.mark.parametrize("value", [1.1, 2024, True])
def test_unquoted_version_is_an_error(gh, value):
    # YAML reads `version: 1.10` as 1.1: matching str(1.1) could pick the wrong tag
    with pytest.raises(gh.lib.TinyError, match="must be the exact tag name or commit hash in quotes"):
        gh.generate(package(version=value))


@pytest.mark.parametrize("config, message", [
    ({}, "no release found in u/r excluding drafts, excluding pre-releases"),
    ({"select": "^x", "include-drafts": True, "include-pre-releases": True}, "no release found in u/r matching '^x'"),
    ({"version": "v7"}, "no release found in u/r named 'v7'"),
])
def test_nothing_found(gh, http, config, message):
    add_releases(http, [release("r", "v1", [], draft=True), release("r", "v2", [], prerelease=True)])
    with pytest.raises(gh.lib.TinyError) as info:
        gh.generate(package(**config))
    assert str(info.value) == message


def test_missing_asset(gh, http):
    add_releases(http, [release("r", "1.0", ["z.zip", "a.zip"])])
    with pytest.raises(gh.lib.TinyError, match=r"release '1.0' of u/r has no asset named pkg-1.0.tar.xz "
                                               r"\(available: a.zip, z.zip\)"):
        gh.generate(package(artifacts=["{pkgname}-{version}.tar.xz"]))


def test_unknown_artifact_placeholder(gh, http):
    add_releases(http, [release("r", "1.0", [])])
    with pytest.raises(gh.lib.TinyError, match="uses an unknown placeholder 'arch'"):
        gh.generate(package(artifacts=["{arch}.zip"]))


def test_empty_version_after_transforms(gh, http):
    add_releases(http, [release("r", "v", [])])
    with pytest.raises(gh.lib.TinyError, match="the transforms turned tag 'v' into an empty version"):
        gh.generate(package(transforms=[["v", ""]]))


def test_api_error(gh, http):
    http.add("GET", f"{API}/releases", status=404, json={"message": "Not Found"})
    with pytest.raises(gh.lib.TinyError) as info:
        gh.generate(package())
    assert "couldn't list releases of u/r: HTTP 404" in str(info.value)
    assert "a 'github_key'" in str(info.value)


# --- Tags and commits ------------------------------------------------------------------

def commit(sha, date):
    return {"sha": sha, "commit": {"committer": {"date": date}}}


def test_tags(gh, http, capsys):
    http.add("GET", f"{API}/tags", json=[{"name": "v1.0", "commit": {"sha": "abc"}}], match=[query(FIRST_PAGE)])
    http.add("GET", f"{API}/commits/abc", json=commit("abc", "2025-12-31T23:00:00-02:00"))
    data = add_file(http, "https://github.com/u/r/archive/refs/tags/v1.0.tar.gz")
    add_metadata(http)
    result = gh.generate(package(query="tag", artifacts=["ignored.zip"]))
    assert result["date"] == "2026-01-01T01:00:00+00:00"
    assert result["version"] == "v1.0"
    assert result["artifacts"][0]["checksums"] == checksums(data)
    assert result["artifacts"][0]["directory"] == "r-1.0"
    assert "'artifacts' only works with release queries, ignoring it for tags" in capsys.readouterr().err


def test_latest_commit(gh, http):
    http.add("GET", f"{API}/commits", json=[commit("f00ba4", "2026-02-03T04:05:06Z")], match=[query({"per_page": "1"})])
    add_file(http, "https://github.com/u/r/archive/f00ba4.tar.gz")
    add_metadata(http)
    result = gh.generate(package(query="commits"))
    assert result["version"] == "20260203"
    assert result["commit"] == "f00ba4"
    assert result["date"] == "2026-02-03T04:05:06+00:00"
    assert "tag" not in result
    assert result["artifacts"][0]["alias"] == "pkg-20260203.tar.gz"
    assert result["artifacts"][0]["directory"] == "r-f00ba4"


def test_locked_commit(gh, http):
    http.add("GET", f"{API}/commits/deadbeef", json=commit("deadbeef", "2026-02-03T04:05:06Z"))
    add_file(http, "https://github.com/u/r/archive/deadbeef.tar.gz")
    add_metadata(http)
    assert gh.generate(package(query="commit", version="deadbeef"))["commit"] == "deadbeef"


def test_no_commits(gh, http):
    http.add("GET", f"{API}/commits", json=[])
    with pytest.raises(gh.lib.TinyError, match="u/r has no commits"):
        gh.generate(package(query="commits"))


@pytest.mark.parametrize("ref, directory", [
    ("refs/tags/v1.0", "r-1.0"), ("refs/tags/vNext", "r-vNext"), ("release/1.0", "r-release-1.0"), ("abc123", "r-abc123"),
])
def test_source_archive_names(gh, ref, directory):
    hub = gh.GitHub(package()["github"], "pkg", 0)
    url, alias, folder = hub.source_archive(ref, "1.0")
    assert url == f"https://github.com/u/r/archive/{ref}.tar.gz"
    assert alias == "pkg-1.0.tar.gz"
    assert folder == directory


# --- Exports ---------------------------------------------------------------------------------

def setup_release(http):
    add_releases(http, [release("r", "1.0", [])])
    add_file(http, "https://github.com/u/r/archive/refs/tags/1.0.tar.gz")


def test_exports_only_fill_unset_keys(gh, http):
    setup_release(http)
    add_metadata(http, license={"spdx_id": "NOASSERTION"}, homepage="")
    result = gh.generate({**package(), "description": "Mine"})
    assert result["exports"] == {"homepage": "https://github.com/u/r"}


def test_no_metadata_request_when_everything_is_set(gh, http):
    setup_release(http)
    result = gh.generate({**package(), "description": "d", "license": "MIT", "homepage": "h"})
    assert result["exports"] == {}
    assert all(call.request.url.split("?")[0] != API for call in http.calls)


def test_metadata_failure_is_a_warning(gh, http, capsys):
    setup_release(http)
    http.add("GET", API, status=500, json={})
    assert gh.generate(package())["exports"] == {}
    assert "[pkg] couldn't fetch repository metadata of u/r: HTTP 500" in capsys.readouterr().err


# --- Configuration ----------------------------------------------------------------------------

def test_token_and_enterprise_domains(gh, http, secrets):
    secrets({"github_key": "s3cret"})
    http.add("GET", "https://ghe.example.com/api/v3/repos/u/r/releases", json=[release("r", "1.0", [])])
    http.add("GET", "https://ghe.example.com/u/r/archive/refs/tags/1.0.tar.gz", body=b"x")
    http.add("GET", "https://ghe.example.com/api/v3/repos/u/r", json={})
    gh.generate(package(domain="ghe.example.com", **{"api-domain": "ghe.example.com/api/v3"}))
    assert len(http.calls) == 3
    assert all(call.request.headers["Authorization"] == "Bearer s3cret" for call in http.calls)


@pytest.mark.parametrize("pkg, message", [
    ({"name": "pkg"}, "no 'github' block found in the package metadata"),
    ({"name": "pkg", "github": "u/r"}, "'github' must be a mapping"),
    ({"name": "pkg", "github": {"repo": "r", "query": "releases"}}, "no GitHub 'user' provided"),
    ({"name": "pkg", "github": {"user": "u", "query": "releases"}}, "no GitHub 'repo' provided"),
    ({"name": "pkg", "github": {"user": "u", "repo": "r"}}, "no GitHub 'query' provided"),
    (package(query="branches"), "invalid GitHub query 'branches', expected one of: release, tag, commit"),
    (package(artifacts="a.zip"), "'artifacts' must be a list of file names"),
    (package(version="v1", select="^v"), "'version' and 'select' can't be used together"),
    (package(select="("), "invalid 'select' regex '('"),
])
def test_config_errors(gh, pkg, message):
    with pytest.raises(gh.lib.TinyError) as info:
        gh.generate(pkg)
    assert message in str(info.value)


# --- Subprocess contract -------------------------------------------------------------------------

def test_example_package_offline(http_subprocess, example_config):
    from pkggen import generate
    calls = http_subprocess(example_routes())
    # The first example package, with its group's defaults
    pkg = generate.load_packages(example_config, ["untitled-cli-parser"])[0][0]
    result = run_generator("generation", "github", {**pkg, "position": 0})
    assert result.code == 0, result.err
    data = archive_bytes("untitled-cli-parser-6.1.0.0.tar.xz")
    assert result.json == {
        "version": "6.1.0.0",
        "tag": "v6.1.0.0",
        "date": "2026-09-01T10:00:00+00:00",
        "artifacts": [{
            "url": "https://github.com/MadLadSquad/UntitledCLIParser/releases/download/v6.1.0.0/"
                   "untitled-cli-parser-6.1.0.0.tar.xz",
            "alias": "untitled-cli-parser-6.1.0.0.tar.xz", "type": "asset",
            "directory": "untitled-cli-parser-6.1.0.0", "size": len(data), "checksums": checksums(data)}],
        # The package sets its description, license and homepage, so the repository isn't asked
        "exports": {},
    }
    assert len(calls()) == 2
