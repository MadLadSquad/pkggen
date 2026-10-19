import pytest
from responses import matchers

from tests.support.example import archive_bytes, checksums
from tests.support.helpers import load_generator, run_generator
from tests.support.http_shim.pkggen_test_http import Routes

API = "https://gitlab.com/api/v4/projects/u%2Fr"
RELEASES_QUERY = {"order_by": "released_at", "sort": "desc", "page": "1", "per_page": "100"}
TAGS_QUERY = {"order_by": "updated", "sort": "desc", "page": "1", "per_page": "100"}


@pytest.fixture
def gl():
    return load_generator("generation", "gitlab")


def package(**gitlab):
    return {"name": "pkg", "gitlab": {"user": "u", "repo": "r", "query": "releases", **gitlab}}


def release(tag, links=(), released="2026-04-05T06:07:08.000Z", upcoming=False):
    return {"tag_name": tag, "name": f"Release {tag}", "released_at": released, "upcoming_release": upcoming,
            "assets": {"links": list(links)}}


def link(name, url=None, direct=None):
    entry = {"name": name, "url": url or f"https://gitlab.com/u/r/-/uploads/abc/{name}"}
    if direct is not False:
        entry["direct_asset_url"] = direct or f"https://gitlab.com/u/r/-/releases/v1/downloads/{name}"
    return entry


def add_file(http, url):
    data = archive_bytes(url)
    http.add("GET", url, body=data)
    return data


def add_metadata(http, api=API, **data):
    body = {"description": "GitLab description", "license": {"key": "mit"}, "web_url": "https://gitlab.com/u/r", **data}
    http.add("GET", api, json=body, match=[matchers.query_param_matcher({"license": "true"})])


# --- Releases ----------------------------------------------------------------------------

def test_release_with_artifacts(gl, http):
    http.add("GET", f"{API}/releases", json=[release("v1", [link("pkg-1.tar.xz"), link("notes.txt")])],
             match=[matchers.query_param_matcher(RELEASES_QUERY)])
    url = "https://gitlab.com/u/r/-/releases/v1/downloads/pkg-1.tar.xz"
    data = add_file(http, url)
    add_metadata(http)
    result = gl.generate(package(artifacts=["{pkgname}-{version}.tar.xz"], transforms=[["^v", ""]]))
    assert result == {
        "version": "1",
        "tag": "v1",
        "date": "2026-04-05T06:07:08+00:00",
        "artifacts": [{"url": url, "alias": "pkg-1.tar.xz", "type": "asset", "directory": "pkg-1",
                       "size": len(data), "checksums": checksums(data)}],
        "exports": {"description": "GitLab description", "license": "MIT", "homepage": "https://gitlab.com/u/r"},
    }


def test_asset_link_without_direct_url(gl, http):
    http.add("GET", f"{API}/releases", json=[release("v1", [link("a.zip", url="https://cdn.example.com/a.zip",
                                                                   direct=False)])])
    add_file(http, "https://cdn.example.com/a.zip")
    add_metadata(http)
    assert gl.generate(package(artifacts=["a.zip"]))["artifacts"][0]["url"] == "https://cdn.example.com/a.zip"


@pytest.mark.parametrize("tag, url_ref, directory", [
    ("v1.0", "v1.0/r-v1.0", "r-v1.0"),
    ("release/1.0", "release/1.0/r-release-1.0", "r-release-1.0"),
])
def test_source_tarball(gl, http, tag, url_ref, directory):
    http.add("GET", f"{API}/releases", json=[release(tag)])
    url = f"https://gitlab.com/u/r/-/archive/{url_ref}.tar.gz"
    add_file(http, url)
    add_metadata(http)
    artifact = gl.generate(package())["artifacts"][0]
    assert (artifact["url"], artifact["alias"], artifact["directory"], artifact["type"]) == \
        (url, f"pkg-{tag}.tar.gz", directory, "source")


def test_include_source(gl, http):
    http.add("GET", f"{API}/releases", json=[release("v1", [link("a.zip")])])
    add_file(http, "https://gitlab.com/u/r/-/releases/v1/downloads/a.zip")
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    add_metadata(http)
    result = gl.generate(package(artifacts=["a.zip"], **{"include-source": True}))
    assert [a["type"] for a in result["artifacts"]] == ["asset", "source"]


def test_upcoming_releases(gl, http):
    releases = [release("v2", upcoming=True), release("v1")]
    http.add("GET", f"{API}/releases", json=releases)
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    add_file(http, "https://gitlab.com/u/r/-/archive/v2/r-v2.tar.gz")
    add_metadata(http)
    assert gl.generate(package())["tag"] == "v1"
    assert gl.generate(package(**{"include-upcoming": "yes"}))["tag"] == "v2"


def test_drafts_and_pre_releases_are_ignored_with_a_warning(gl, http, capsys):
    http.add("GET", f"{API}/releases", json=[release("v1")])
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    add_metadata(http)
    gl.generate(package(**{"include-drafts": True, "include-pre-releases": True}))
    err = capsys.readouterr().err
    assert "GitLab has no drafts or pre-releases, ignoring 'include-drafts'" in err
    assert "ignoring 'include-pre-releases'" in err


@pytest.mark.parametrize("config, message", [
    ({}, "no release found in u/r excluding upcoming releases"),
    ({"include-upcoming": True, "select": "^x"}, "no release found in u/r matching '^x'"),
    ({"version": "v9"}, "no release found in u/r named 'v9'"),
])
def test_nothing_found(gl, http, config, message):
    http.add("GET", f"{API}/releases", json=[release("v2", upcoming=True)])
    with pytest.raises(gl.lib.TinyError) as info:
        gl.generate(package(**config))
    assert str(info.value) == message


def test_missing_asset(gl, http):
    http.add("GET", f"{API}/releases", json=[release("v1", [link("b.zip")])])
    with pytest.raises(gl.lib.TinyError, match=r"has no asset named r-u.zip \(available: b.zip\)"):
        gl.generate(package(artifacts=["{gitlab_repo}-{gitlab_user}.zip"]))


def test_pagination(gl, http):
    http.add("GET", f"{API}/releases", json=[release(f"v0.{i}") for i in range(100)],
             match=[matchers.query_param_matcher(RELEASES_QUERY)])
    http.add("GET", f"{API}/releases", json=[release("v9")],
             match=[matchers.query_param_matcher({**RELEASES_QUERY, "page": "2"})])
    add_file(http, "https://gitlab.com/u/r/-/archive/v9/r-v9.tar.gz")
    add_metadata(http)
    assert gl.generate(package(version="v9"))["tag"] == "v9"


# --- Tags and commits ---------------------------------------------------------------------------

def test_tags(gl, http):
    http.add("GET", f"{API}/repository/tags", json=[
        {"name": "nightly", "commit": {"committed_date": "2026-01-01T00:00:00Z"}},
        {"name": "v3.0", "commit": {"committed_date": "2026-01-01T02:00:00+02:00"}},
    ], match=[matchers.query_param_matcher(TAGS_QUERY)])
    add_file(http, "https://gitlab.com/u/r/-/archive/v3.0/r-v3.0.tar.gz")
    add_metadata(http)
    result = gl.generate(package(query="tags", select=r"^v"))
    assert (result["tag"], result["date"]) == ("v3.0", "2026-01-01T00:00:00+00:00")


def test_latest_commit(gl, http):
    http.add("GET", f"{API}/repository/commits", json=[{"id": "c0ffee", "committed_date": "2026-05-06T07:08:09Z"}],
             match=[matchers.query_param_matcher({"per_page": "1"})])
    add_file(http, "https://gitlab.com/u/r/-/archive/c0ffee/r-c0ffee.tar.gz")
    add_metadata(http)
    result = gl.generate(package(query="commits"))
    assert (result["version"], result["commit"]) == ("20260506", "c0ffee")
    assert result["artifacts"][0]["directory"] == "r-c0ffee"


def test_locked_commit(gl, http):
    http.add("GET", f"{API}/repository/commits/abc", json={"id": "abc", "committed_date": "2026-05-06T07:08:09Z"})
    add_file(http, "https://gitlab.com/u/r/-/archive/abc/r-abc.tar.gz")
    add_metadata(http)
    assert gl.generate(package(query="commit", version="abc"))["commit"] == "abc"


def test_no_commits(gl, http):
    http.add("GET", f"{API}/repository/commits", json=[])
    with pytest.raises(gl.lib.TinyError, match="u/r has no commits"):
        gl.generate(package(query="commits"))


# --- Instances, subgroups and tokens -------------------------------------------------------------

def test_subgroups_and_self_hosted_path_prefix(gl, http, secrets):
    secrets({"gitlab_key": "dotcom-token", "gitlab_keys": {"example.com/gitlab": "own-token"}})
    api = "https://example.com/gitlab/api/v4/projects/group%2Fsub%2Fr"
    http.add("GET", f"{api}/releases", json=[release("v1", [link("a.zip", direct="https://example.com/gitlab/a.zip"),
                                                          link("b.zip", direct="https://github.com/b.zip")])])
    add_file(http, "https://example.com/gitlab/a.zip")
    add_file(http, "https://github.com/b.zip")
    add_metadata(http, api=api, license={"key": "lgpl-3.0"}, web_url="https://example.com/gitlab/group/sub/r")
    result = gl.generate(package(user="/group/sub/", domain="example.com/gitlab/", artifacts=["a.zip", "b.zip"]))
    assert result["exports"]["license"] == "LGPL-3.0"
    auth = {call.request.url: call.request.headers.get("Authorization") for call in http.calls}
    # The instance's own token, and never to other hosts
    assert auth["https://example.com/gitlab/a.zip"] == "Bearer own-token"
    assert auth["https://github.com/b.zip"] is None
    assert auth[f"{api}/releases?order_by=released_at&sort=desc&page=1&per_page=100"] == "Bearer own-token"


def test_gitlab_key_is_only_for_gitlab_com(gl, http, secrets):
    secrets({"gitlab_key": "dotcom-token"})
    api = "https://git.example.org/api/v4/projects/u%2Fr"
    http.add("GET", f"{api}/releases", status=404, json={"message": "404 Project Not Found"})
    with pytest.raises(gl.lib.TinyError) as info:
        gl.generate(package(domain="git.example.org"))
    assert "Bearer" not in str(http.calls[0].request.headers)
    assert "a 'gitlab_keys' entry for git.example.org" in str(info.value)


def test_gitlab_key_is_sent_to_gitlab_com(gl, http, secrets):
    secrets({"gitlab_key": "dotcom-token"})
    http.add("GET", f"{API}/releases", json=[release("v1")])
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    add_metadata(http)
    gl.generate(package())
    assert all(call.request.headers["Authorization"] == "Bearer dotcom-token" for call in http.calls)


def test_invalid_gitlab_keys(gl, secrets):
    secrets({"gitlab_keys": ["token"]})
    with pytest.raises(gl.lib.TinyError, match="'gitlab_keys' in secrets.yaml must be a mapping"):
        gl.generate(package())


# --- Exports ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("key, spdx", [("mit", "MIT"), ("GPL-3.0", "GPL-3.0"), ("bsd-3-clause", "BSD-3-Clause"),
                                       ("other", None), (None, None)])
def test_license_mapping(gl, http, key, spdx):
    http.add("GET", f"{API}/releases", json=[release("v1")])
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    add_metadata(http, license={"key": key} if key else None)
    assert gl.generate(package())["exports"].get("license") == spdx


def test_metadata_failure_is_a_warning(gl, http, capsys):
    http.add("GET", f"{API}/releases", json=[release("v1")])
    add_file(http, "https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz")
    http.add("GET", API, status=403, json={"message": "403 Forbidden"})
    assert gl.generate({**package(), "description": "own"})["exports"] == {}
    assert "couldn't fetch project metadata of u/r: HTTP 403" in capsys.readouterr().err


@pytest.mark.parametrize("pkg, message", [
    ({"name": "pkg"}, "no 'gitlab' block found in the package metadata"),
    ({"name": "pkg", "gitlab": []}, "'gitlab' must be a mapping"),
    ({"name": "pkg", "gitlab": {"user": "u", "repo": "r"}}, "no GitLab 'query' provided"),
    (package(query="mr"), "invalid GitLab query 'mr'"),
    (package(artifacts={"a": 1}), "'artifacts' must be a list"),
    (package(version="v1", select="v"), "'version' and 'select' can't be used together"),
    (package(select="[", version=None), "invalid 'select' regex '['"),
])
def test_config_errors(gl, pkg, message):
    with pytest.raises(gl.lib.TinyError) as info:
        gl.generate(pkg)
    assert message in str(info.value)


def test_subprocess_contract(http_subprocess):
    http_subprocess(Routes()
                    .json(f"{API}/releases", [release("v1")], query=RELEASES_QUERY)
                    .body("https://gitlab.com/u/r/-/archive/v1/r-v1.tar.gz", b"tarball")
                    .json(API, {"description": "d", "license": {"key": "mit"}, "web_url": "w"}, query={"license": "true"}))
    result = run_generator("generation", "gitlab", package(transforms=[["^v", ""]]))
    assert result.code == 0, result.err
    assert result.json["version"] == "1"
    assert result.json["artifacts"][0]["checksums"] == checksums(b"tarball")
    assert result.json["exports"] == {"description": "d", "license": "MIT", "homepage": "w"}
