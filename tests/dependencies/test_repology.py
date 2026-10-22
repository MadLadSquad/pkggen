"""The repology dependency generator: looks projects up, trims them to pkggen's releases, throttles and caches."""
import json

import pytest
import requests
import responses

from tests.support.helpers import load_generator, run_generator
from tests.support.http_shim.pkggen_test_http import Routes

API = "https://repology.org/api/v1/project/"

GLFW = [
    {"repo": "arch", "subrepo": "extra", "srcname": "glfw", "binname": "glfw", "visiblename": "glfw",
     "version": "3.5.1", "status": "newest", "summary": "A library", "maintainers": ["x@archlinux.org"]},
    {"repo": "debian_13", "subrepo": "trixie/main", "srcname": "glfw3", "binnames": ["libglfw3", "libglfw3-dev"],
     "visiblename": "glfw3", "version": "3.4", "status": "outdated"},
    {"repo": "debian_9", "srcname": "glfw3", "binnames": ["libglfw3"], "version": "3.2", "status": "legacy"},
    {"repo": "freebsd", "srcname": "graphics/glfw", "version": "3.5.1", "status": "newest"},
]


@pytest.fixture
def repology(monkeypatch):
    module = load_generator("dependencies", "repology")
    module.Throttle.last = None
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    module.sleeps = sleeps
    return module


def job(*projects, **extra):
    return {"projects": list(projects), "repositories": ["arch", "debian_13", "fedora_44"],
            "user_agent": "pkggen/test", **extra}


def test_projects_are_trimmed_to_known_releases(repology, http):
    http.add(responses.GET, API + "glfw", json=GLFW)
    result = repology.main(job("glfw"))
    assert set(result["projects"]["glfw"]) == {"arch", "debian_13"}
    assert result["projects"]["glfw"]["arch"] == [{
        "repo": "arch", "subrepo": "extra", "srcname": "glfw", "binname": "glfw", "binnames": [],
        "visiblename": "glfw", "version": "3.5.1", "status": "newest", "summary": "A library"}]
    assert result["projects"]["glfw"]["debian_13"][0]["binnames"] == ["libglfw3", "libglfw3-dev"]
    assert result["missing"] == []
    assert http.calls[0].request.headers["User-Agent"] == "pkggen/test"


def test_wildcards_keep_older_releases(repology, http):
    http.add(responses.GET, API + "glfw", json=GLFW)
    result = repology.main(job("glfw", wildcards=["debian"]))
    assert set(result["projects"]["glfw"]) == {"arch", "debian_13", "debian_9"}


@pytest.mark.parametrize("status, body", [(404, {}), (200, []), (200, [{"repo": "freebsd"}])])
def test_unknown_projects_are_missing(repology, http, status, body):
    http.add(responses.GET, API + "nope", json=body, status=status)
    assert repology.main(job("nope")) == {"projects": {"nope": {}}, "missing": ["nope"], "warnings": []}


def test_names_are_quoted(repology, http):
    http.add(responses.GET, API + "lib%2Fodd", json=[])
    repology.main(job("lib/odd"))
    assert http.calls[0].request.url == API + "lib%2Fodd"


def test_http_errors_name_the_project(repology, http):
    http.add(responses.GET, API + "glfw", status=500, json={"error": "boom"})
    with pytest.raises(repology.lib.TinyError, match="look 'glfw' up on repology: HTTP 500"):
        repology.main(job("glfw"))


def test_rate_limited_requests_are_retried_once(repology, http):
    http.add(responses.GET, API + "glfw", status=429, headers={"Retry-After": "3"})
    http.add(responses.GET, API + "glfw", json=GLFW)
    assert repology.main(job("glfw"))["projects"]["glfw"]
    assert 3 in repology.sleeps


def test_requests_are_spaced_out(repology, http, monkeypatch):
    for name in ("a", "b", "c"):
        http.add(responses.GET, API + name, json=[])
    # a ends at 0.0; b is asked for at 0.2 and ends at 1.0; c is asked for at 2.5
    clock = iter([0.0, 0.2, 1.0, 2.5, 2.6])
    monkeypatch.setattr(repology.time, "monotonic", lambda: next(clock))
    repology.main(job("a", "b", "c", "a"))  # duplicates are looked up once
    assert len(http.calls) == 3
    # Only b came less than a second after the request before it
    assert repology.sleeps == [pytest.approx(0.8)]


def test_cached_answers_dont_wait_or_ask_again(repology, http, tmp_path, monkeypatch):
    monkeypatch.setenv("PKGGEN_CACHE_DIR", str(tmp_path))
    http.add(responses.GET, API + "glfw", json=GLFW, headers={"Cache-Control": "max-age=3600"})
    first = repology.main(job("glfw"))
    repology.Throttle.last = None
    second = repology.main(job("glfw"))
    assert first == second and len(http.calls) == 1 and repology.sleeps == []


def test_offline_with_an_expired_answer(repology, http, tmp_path, monkeypatch):
    monkeypatch.setenv("PKGGEN_CACHE_DIR", str(tmp_path))
    http.add(responses.GET, API + "glfw", json=GLFW, headers={"Cache-Control": "max-age=60"})
    fetch = repology.lib.fetch
    monkeypatch.setattr(fetch, "now", lambda: 1000.0)
    repology.main(job("glfw"))
    http.replace(responses.GET, API + "glfw", body=requests.ConnectionError("offline"))
    monkeypatch.setattr(fetch, "now", lambda: 5000.0)
    assert repology.main(job("glfw"))["projects"]["glfw"]


def test_subprocess_contract(http_subprocess):
    calls = http_subprocess(Routes().json(API + "glfw", GLFW).json(API + "nope", [], status=404))
    result = run_generator("dependencies", "repology", job("glfw", "nope"))
    assert result.code == 0, result.err
    assert set(result.json["projects"]["glfw"]) == {"arch", "debian_13"}
    assert result.json["missing"] == ["nope"]
    assert [c["url"] for c in calls()] == [API + "glfw", API + "nope"]


@pytest.mark.parametrize("payload, message", [
    ("not json", "couldn't parse the job JSON"),
    ({"projects": "glfw"}, "expected a JSON object with a 'projects' list"),
])
def test_bad_jobs(payload, message):
    result = run_generator("dependencies", "repology", payload if isinstance(payload, str) else json.dumps(payload))
    assert result.code == 1 and f"Error [dependencies]: {message}" in result.err_plain


def test_network_errors_are_one_line(http_subprocess):
    http_subprocess(Routes())
    result = run_generator("dependencies", "repology", job("glfw"))
    assert result.code == 1
    assert "Error [dependencies]: network error" in result.err_plain and "Traceback" not in result.err


def test_version_sort_key():
    lib = load_generator("dependencies", "lib")
    from packaging.version import parse
    assert lib.version_sort_key("1.2.13.dfsg") == parse("1.2.13")
    assert lib.version_sort_key("abc") == parse("0")
    assert lib.version_sort_key("2:1.25") == parse("2")  # not PEP 440: the leading number
