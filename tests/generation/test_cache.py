"""The fetch generators' HTTP cache: conditional requests with the ETag/Last-Modified of earlier answers."""
import json

import pytest
import responses

from tests.support.example import checksums
from tests.support.helpers import load_generator

API = "https://api.example.com/repos/me/project/releases"
DOWNLOAD = "https://downloads.example.com/project-1.0.tar.xz"


@pytest.fixture
def glib():
    return load_generator("generation", "lib")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "cache"
    monkeypatch.setenv("PKGGEN_CACHE_DIR", str(path))
    return path


def conditional(http, url, body, etag, content_type="application/json", extra=None):
    """Answer 200 with an ETag, or 304 when the request has it in If-None-Match. Returns the requests seen."""
    seen = []

    def answer(request):
        seen.append(dict(request.headers))
        if request.headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag, "X-RateLimit-Remaining": str(60 - len(seen))}, b""
        return 200, {"ETag": etag, "Content-Type": content_type, "Link": '<next>; rel="next"',
                     "X-RateLimit-Remaining": str(60 - len(seen)), **(extra or {})}, body
    http.add_callback(responses.GET, url, callback=answer)
    return seen


def test_api_answers_are_reused_when_unchanged(glib, http, cache):
    seen = conditional(http, API, json.dumps([{"tag_name": "v1.0"}]).encode(), '"v1"')
    first = glib.session().get(API, headers={"Accept": "application/vnd.github+json"}, timeout=5)
    second = glib.session().get(API, headers={"Accept": "application/vnd.github+json"}, timeout=5)
    assert "If-None-Match" not in seen[0] and seen[1]["If-None-Match"] == '"v1"'
    assert second.status_code == 200 and second.json() == first.json() == [{"tag_name": "v1.0"}]
    # The cached headers (pagination) with the fresh rate limit
    assert second.headers["Link"] == '<next>; rel="next"'
    assert second.headers["X-RateLimit-Remaining"] == "58"


def test_the_cache_key_includes_the_token(glib, http, cache):
    seen = conditional(http, API, b"[]", '"v1"')
    glib.session().get(API, headers={"Authorization": "Bearer one"}, timeout=5)
    glib.session().get(API, headers={"Authorization": "Bearer two"}, timeout=5)
    assert "If-None-Match" not in seen[1]


def test_nothing_is_cached_without_the_cache_folder(glib, http, monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_CACHE_DIR", raising=False)
    seen = conditional(http, API, b"[]", '"v1"')
    glib.session().get(API, timeout=5)
    glib.session().get(API, timeout=5)
    assert all("If-None-Match" not in headers for headers in seen)


def test_unchanged_artifacts_arent_downloaded_again(glib, http, cache):
    data = b"tarball " * 1000
    seen = conditional(http, DOWNLOAD, data, '"t1"', content_type="application/x-xz",
                       extra={"Last-Modified": "Mon, 05 Oct 2026 10:00:00 GMT"})
    first = glib.download_and_hash(glib.session(), DOWNLOAD)
    second = glib.download_and_hash(glib.session(), DOWNLOAD)
    assert first == second == {"size": len(data), "checksums": checksums(data),
                               "last_modified": "2026-10-05T10:00:00+00:00"}
    assert seen[1]["If-None-Match"] == '"t1"'


def test_a_changed_artifact_is_hashed_again(glib, http, cache):
    answers = iter([(b"one", '"1"'), (b"two", '"2"')])

    def answer(request):
        body, etag = next(answers)
        return 200, {"ETag": etag}, body
    http.add_callback(responses.GET, DOWNLOAD, callback=answer)
    first = glib.download_and_hash(glib.session(), DOWNLOAD)
    second = glib.download_and_hash(glib.session(), DOWNLOAD)
    assert first["checksums"] == checksums(b"one") and second["checksums"] == checksums(b"two")


def test_a_broken_cache_entry_is_ignored(glib, http, cache):
    conditional(http, API, b"[1]", '"v1"')
    glib.session().get(API, timeout=5)
    for path in cache.rglob("*.json"):
        path.write_text("not json")
    assert glib.session().get(API, timeout=5).json() == [1]


def test_generate_points_fetch_generators_at_pkggen_build(monkeypatch, tmp_path):
    import generate
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    monkeypatch.delenv("PKGGEN_CACHE_DIR", raising=False)
    assert generate.fetch_environment()["PKGGEN_CACHE_DIR"] == str(tmp_path / "pkggen-build" / "cache")
    # The user's own folder, or "" to turn the cache off, is passed on as it is
    monkeypatch.setenv("PKGGEN_CACHE_DIR", "")
    assert generate.fetch_environment()["PKGGEN_CACHE_DIR"] == ""


REPOLOGY = "https://repology.example.com/api/v1/project/zlib"


def max_age_answer(http, body=b"[1]", max_age="3600"):
    """Answer 200 with Cache-Control: max-age and no validators (like repology). Returns the requests seen."""
    seen = []

    def answer(request):
        seen.append(dict(request.headers))
        return 200, {"Cache-Control": f"max-age={max_age}", "Content-Type": "application/json"}, body
    http.add_callback(responses.GET, REPOLOGY, callback=answer)
    return seen


def test_max_age_answers_are_reused_without_asking(glib, http, cache, monkeypatch):
    seen = max_age_answer(http)
    monkeypatch.setattr(glib, "now", lambda: 1000.0)
    assert glib.session().get(REPOLOGY, timeout=5).json() == [1]
    monkeypatch.setattr(glib, "now", lambda: 1000.0 + 3599)
    second = glib.session().get(REPOLOGY, timeout=5)
    assert second.json() == [1] and second.reason == "OK (cached)"
    assert len(seen) == 1


def test_expired_max_age_answers_are_asked_for_again(glib, http, cache, monkeypatch):
    seen = max_age_answer(http)
    monkeypatch.setattr(glib, "now", lambda: 1000.0)
    glib.session().get(REPOLOGY, timeout=5)
    monkeypatch.setattr(glib, "now", lambda: 1000.0 + 3600)
    glib.session().get(REPOLOGY, timeout=5)
    assert len(seen) == 2


def test_an_expired_answer_is_used_when_the_network_fails(glib, http, cache, monkeypatch, capsys):
    max_age_answer(http)
    monkeypatch.setattr(glib, "now", lambda: 1000.0)
    glib.session().get(REPOLOGY, timeout=5)
    http.replace(responses.GET, REPOLOGY, body=__import__("requests").ConnectionError("offline"))
    monkeypatch.setattr(glib, "now", lambda: 100000.0)
    stale = glib.session().get(REPOLOGY, timeout=5)
    assert stale.json() == [1] and stale.reason == "OK (stale)"
    assert "using its answer from 1970-01-01 00:16 UTC" in capsys.readouterr().err


def test_network_errors_without_a_cached_answer_are_raised(glib, http, cache):
    import requests
    http.add(responses.GET, REPOLOGY, body=requests.ConnectionError("offline"))
    with pytest.raises(requests.ConnectionError):
        glib.session().get(REPOLOGY, timeout=5)


@pytest.mark.parametrize("header", ["no-store", "max-age=0", "no-cache, max-age=60"])
def test_answers_that_mustnt_be_reused_arent_kept(glib, http, cache, header):
    seen = []

    def answer(request):
        seen.append(1)
        return 200, {"Cache-Control": header}, b"[]"
    http.add_callback(responses.GET, REPOLOGY, callback=answer)
    glib.session().get(REPOLOGY, timeout=5)
    glib.session().get(REPOLOGY, timeout=5)
    assert len(seen) == 2 and not list(cache.rglob("*.json"))
