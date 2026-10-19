import pytest

from tests.support.example import checksums
from tests.support.helpers import load_generator, run_generator
from tests.support.http_shim.pkggen_test_http import Routes

DATA = b"archive bytes" * 100


@pytest.fixture
def url_gen():
    return load_generator("generation", "url")


def package(**config):
    return {"name": "tool", "url-generator": config}


def test_fixed_version(url_gen, http):
    http.add("GET", "https://example.com/tool-2.0.tar.gz", body=DATA)
    result = url_gen.generate(package(url="https://example.com/{pkgname}-{version}.tar.gz", version=2.0))
    assert result == {
        "version": "2.0",
        "artifacts": [{"url": "https://example.com/tool-2.0.tar.gz", "alias": "tool-2.0.tar.gz", "type": "source",
                       "directory": "tool-2.0", "size": len(DATA), "checksums": checksums(DATA)}],
        "exports": {},
    }
    assert "Mozilla" in http.calls[0].request.headers["User-Agent"]


def test_version_from_transforms(url_gen, http):
    http.add("GET", "https://example.com/releases/v3.1/tool%20linux.zip", body=DATA)
    result = url_gen.generate(package(url="https://example.com/releases/v3.1/tool%20linux.zip",
                                      transforms=[[r".*/v([^/]+)/.*", r"\1"]]))
    assert result["version"] == "3.1"
    # The default alias is the unquoted last path segment
    assert result["artifacts"][0]["alias"] == "tool linux.zip"
    assert result["artifacts"][0]["directory"] == "tool linux"


def test_alias_and_headers(url_gen, http):
    http.add("GET", "https://example.com/download", body=DATA)
    result = url_gen.generate(package(url="https://example.com/download", version="1",
                                      alias="{pkgname}-{version}.tar.xz", headers={"User-Agent": "pkggen", "X-A": "b"}))
    assert result["artifacts"][0]["alias"] == "tool-1.tar.xz"
    sent = http.calls[0].request.headers
    assert sent["User-Agent"] == "pkggen" and sent["X-A"] == "b"


def test_alias_falls_back_when_url_has_no_file_name(url_gen, http):
    http.add("GET", "https://example.com/", body=DATA)
    assert url_gen.generate(package(url="https://example.com/", version="1"))["artifacts"][0]["alias"] == "tool-1"


@pytest.mark.parametrize("locks", [[checksums(DATA)["sha2_512"]], checksums(DATA)["md5"].upper(),
                                   ["0000", checksums(DATA)["blake2s"]]])
def test_hash_locks_match(url_gen, http, locks):
    http.add("GET", "https://example.com/a.tar.gz", body=DATA)
    assert url_gen.generate(package(url="https://example.com/a.tar.gz", version="1", **{"hash-locks": locks}))


def test_hash_locks_mismatch(url_gen, http):
    http.add("GET", "https://example.com/a.tar.gz", body=DATA)
    with pytest.raises(url_gen.lib.TinyError, match=f"doesn't match any of the 'hash-locks' \\(its sha2_256 is "
                                                    f"{checksums(DATA)['sha2_256']}\\)"):
        url_gen.generate(package(url="https://example.com/a.tar.gz", version="1", **{"hash-locks": ["0000"]}))


def test_date_from_last_modified(url_gen, http):
    http.add("GET", "https://example.com/a.tar.gz", body=DATA, headers={"Last-Modified": "Fri, 02 Jan 2026 03:04:05 GMT"})
    assert url_gen.generate(package(url="https://example.com/a.tar.gz", version="1"))["date"] == \
        "2026-01-02T03:04:05+00:00"


def test_no_date_without_last_modified(url_gen, http):
    http.add("GET", "https://example.com/a.tar.gz", body=DATA)
    assert "date" not in url_gen.generate(package(url="https://example.com/a.tar.gz", version="1"))


@pytest.mark.parametrize("pkg, message", [
    ({"name": "tool"}, "no 'url-generator' block found"),
    ({"name": "tool", "url-generator": "https://x"}, "no 'url-generator' block found"),
    (package(version="1"), "'url-generator' has no 'url'"),
    (package(url="https://x/a", version="1", transforms=[]), "'version' and 'transforms' can't be used together"),
    (package(url="https://x/a"), "set either 'version' or 'transforms'"),
    (package(url="https://x/{version}", transforms=[["a", "b"]]), "the URL uses {version}, which needs a fixed 'version'"),
    (package(url="https://x/{nope}", version="1"), "'url' uses an unknown placeholder 'nope'. Allowed: {pkgname}, {version}"),
    (package(url="https://x/a", version="1", alias="{arch}"), "'alias' uses an unknown placeholder 'arch'"),
    (package(url="https://x/a", transforms=[[".*", ""]]), "the transforms turned https://x/a into an empty version"),
])
def test_errors(url_gen, pkg, message):
    with pytest.raises(url_gen.lib.TinyError) as info:
        url_gen.generate(pkg)
    assert message in str(info.value)


def test_subprocess_contract(http_subprocess):
    calls = http_subprocess(Routes().body("https://example.com/tool-1.tar.gz", DATA))
    result = run_generator("generation", "url", {**package(url="https://example.com/{pkgname}-{version}.tar.gz",
                                                           version="1"), "position": 3})
    assert result.code == 0, result.err
    assert result.json["artifacts"][0]["checksums"] == checksums(DATA)
    assert [call["url"] for call in calls()] == ["https://example.com/tool-1.tar.gz"]


def test_subprocess_unmatched_request_fails_offline(http_subprocess):
    http_subprocess(Routes())
    result = run_generator("generation", "url", package(url="https://example.com/a.tar.gz", version="1"))
    assert result.code == 1
    assert result.err_plain.startswith("Error [tool]: couldn't download https://example.com/a.tar.gz")
