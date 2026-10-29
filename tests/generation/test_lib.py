import hashlib
import json
import textwrap

import pytest
import requests

from tests.support.example import checksums
from tests.support.helpers import load_generator, run


@pytest.fixture
def lib():
    return load_generator("generation", "lib")


# --- Small helpers -----------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, False), ("true", True), ("Yes", True), ("on", True), ("0", False), ("", False), (1, True), ([], False),
])
def test_to_bool(lib, value, expected):
    assert lib.to_bool(value) is expected


def test_to_bool_default(lib):
    assert lib.to_bool(None, True) is True


def test_apply_transforms(lib):
    assert lib.apply_transforms(None, "v1.0") == "v1.0"
    assert lib.apply_transforms([["^v", ""], ["-", "."]], "v1-2-3") == "1.2.3"
    assert lib.apply_transforms([[r"release-(\d+)", r"\1.0"]], "release-7") == "7.0"


@pytest.mark.parametrize("transforms, message", [
    ("^v", "'transforms' must be a list"),
    ([["^v"]], "every transform must be a [pattern, replacement] pair"),
    (["^v", ""], "every transform must be a [pattern, replacement] pair"),
    ([["(", ""]], "invalid transform regex '('"),
])
def test_apply_transforms_errors(lib, transforms, message):
    with pytest.raises(lib.TinyError) as info:
        lib.apply_transforms(transforms, "v1")
    assert message in str(info.value)


@pytest.mark.parametrize("value, expected", [
    ("2026-01-02T03:04:05Z", "2026-01-02T03:04:05+00:00"),
    ("2026-01-02T05:04:05+02:00", "2026-01-02T03:04:05+00:00"),
    ("2026-01-02T03:04:05", "2026-01-02T03:04:05+00:00"),
    ("Fri, 02 Jan 2026 03:04:05 GMT", "2026-01-02T03:04:05+00:00"),
    ("Thu, 01 Oct 2026 03:04:05 GMT", "2026-10-01T03:04:05+00:00"),
    ("Fri, 02 Jan 2026 05:04:05 +0200", "2026-01-02T03:04:05+00:00"),
    ("not a date", None),
    ("Tnope", None),
    ("", None),
    (None, None),
    (20260102, None),
])
def test_iso_date(lib, value, expected):
    assert lib.iso_date(value) == expected


@pytest.mark.parametrize("name, stem", [
    ("pkg-1.0.tar.gz", "pkg-1.0"), ("pkg-1.0.TAR.XZ", "pkg-1.0"), ("pkg.tgz", "pkg"), ("pkg-1.0.zip", "pkg-1.0"),
    ("pkg.tar.zst", "pkg"), ("pkg.7z", "pkg"), ("pkg.AppImage", "pkg.AppImage"), ("pkg.tar", "pkg"),
])
def test_archive_stem(lib, name, stem):
    assert lib.archive_stem(name) == stem


def test_short_name(lib):
    assert lib.short_name("a" * 30) == "a" * 30
    assert lib.short_name("a" * 31) == "a" * 27 + "..."


# --- Secrets -------------------------------------------------------------------------

def test_load_secrets_creates_missing_file(lib, isolated_env, capsys):
    assert lib.load_secrets() == {}
    assert (isolated_env / "pkggen" / "secrets.yaml").is_file()
    assert "was requested but not found" in capsys.readouterr().err


def test_load_secrets(lib, secrets):
    secrets({"github_key": "abc"})
    assert lib.load_secrets() == {"github_key": "abc"}


@pytest.mark.parametrize("content, message", [("a: [", "couldn't parse"), ("- a\n", "must contain a mapping")])
def test_load_secrets_errors(lib, secrets, content, message):
    secrets(content)
    with pytest.raises(lib.TinyError, match=message):
        lib.load_secrets()


def test_secrets_path_on_windows(lib, monkeypatch, tmp_path):
    monkeypatch.setattr(lib.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert lib.secrets_path() == str(tmp_path / "pkggen" / "secrets.yaml")


def test_load_secrets_utf8_and_permissions(lib, isolated_env, secrets):
    import os
    lib.load_secrets()
    path = isolated_env / "pkggen" / "secrets.yaml"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        lib.load_secrets()
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes("github_key: \"Jürgen-🔑\"\n".encode("utf-8"))
    assert lib.load_secrets() == {"github_key": "Jürgen-🔑"}
    path.write_bytes(b"github_key: \xff\n")
    with pytest.raises(lib.TinyError, match="isn't UTF-8"):
        lib.load_secrets()


# --- http_error ------------------------------------------------------------------------

def fake_response(http, status, json_body=None, headers=None, body=""):
    url = "https://api.example.com/thing"
    if json_body is not None:
        http.add("GET", url, status=status, json=json_body, headers=headers or {})
    else:
        http.add("GET", url, status=status, body=body, headers=headers or {})
    return requests.get(url)


def test_http_error_includes_api_message(lib, http):
    error = lib.http_error(fake_response(http, 500, {"message": "Server exploded"}), "list releases")
    assert str(error) == ("couldn't list releases: HTTP 500 Internal Server Error (https://api.example.com/thing): "
                          "Server exploded")


def test_http_error_with_non_json_body(lib, http):
    assert str(lib.http_error(fake_response(http, 502, body="<html>"), "x")).endswith("(https://api.example.com/thing)")


def test_http_error_404_hint(lib, http):
    error = str(lib.http_error(fake_response(http, 404, {"error": "404 Project Not Found"}), "x", "a 'gitlab_key'"))
    assert ": 404 Project Not Found. Check that it exists. Private repositories also need a 'gitlab_key'" in error


def test_http_error_github_rate_limit(lib, http, isolated_env):
    response = fake_response(http, 403, {"message": "API rate limit exceeded"},
                             headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1792411200"})
    error = str(lib.http_error(response, "x"))
    assert "API rate limit exceeded, it resets at " in error
    assert "later" not in error
    assert f"Add a 'github_key' to {isolated_env / 'pkggen' / 'secrets.yaml'}" in error


def test_http_error_gitlab_rate_limit(lib, http):
    error = str(lib.http_error(fake_response(http, 429, {}, headers={"RateLimit-Remaining": "0"}), "x", "a 'gitlab_key'"))
    assert "it resets at later. Add a 'gitlab_key'" in error


def test_http_error_plain_403_is_not_a_rate_limit(lib, http):
    error = str(lib.http_error(fake_response(http, 403, {}, headers={"X-RateLimit-Remaining": "12"}), "x"))
    assert "rate limit" not in error


# --- download_and_hash -------------------------------------------------------------------

URL = "https://downloads.example.com/pkg-1.0.tar.gz"
DATA = b"0123456789abcdef" * 10000  # bigger than one chunk


def test_download_and_hash(lib, http):
    http.add("GET", URL, body=DATA, headers={"Content-Length": str(len(DATA))})
    result = lib.download_and_hash(requests.Session(), URL, headers={"Authorization": "Bearer t"})
    assert result == {"size": len(DATA), "checksums": checksums(DATA)}
    assert set(result["checksums"]) == {key for key, _ in lib.HASHES}
    sent = http.calls[0].request.headers
    assert sent["Accept-Encoding"] == "identity"
    assert sent["Authorization"] == "Bearer t"


def test_download_reports_last_modified(lib, http):
    http.add("GET", URL, body=DATA, headers={"Last-Modified": "Fri, 02 Jan 2026 03:04:05 GMT"})
    assert lib.download_and_hash(requests.Session(), URL)["last_modified"] == "2026-01-02T03:04:05+00:00"


def test_download_hashes_bytes_as_served(lib, http):
    # A gzip Content-Encoding must not be decoded: package managers hash the raw file
    http.add("GET", URL, body=DATA, headers={"Content-Encoding": "gzip"})
    result = lib.download_and_hash(requests.Session(), URL)
    assert result["checksums"]["sha2_256"] == hashlib.sha256(DATA).hexdigest()
    assert "last_modified" not in result


def test_download_error_status(lib, http):
    http.add("GET", URL, status=404, json={"message": "Not Found"})
    with pytest.raises(lib.TinyError, match=r"couldn't download .*HTTP 404.*Not Found. Check that it exists"):
        lib.download_and_hash(requests.Session(), URL)


def test_download_connection_error(lib, http):
    http.add("GET", URL, body=requests.ConnectionError("refused"))
    with pytest.raises(lib.TinyError, match=f"couldn't download {URL}: refused"):
        lib.download_and_hash(requests.Session(), URL)


def test_download_truncated(lib, http):
    http.add("GET", URL, body=DATA, headers={"Content-Length": str(len(DATA) + 10)}, auto_calculate_content_length=False)
    with pytest.raises(lib.TinyError, match=f"download of {URL} (is truncated|was interrupted)"):
        lib.download_and_hash(requests.Session(), URL)


# --- run() ---------------------------------------------------------------------------------

@pytest.fixture
def script(tmp_path):
    """A fetch generator whose generate() behaves according to package["mode"]."""
    from tests.support import paths
    path = tmp_path / "fake.py"
    path.write_text(textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(paths.GENERATORS / "generation")!r})
        import lib, requests

        def generate(package):
            mode = package.get("mode")
            if mode == "tiny":
                raise lib.TinyError("expected problem")
            if mode == "network":
                raise requests.ConnectionError("no route")
            if mode == "bug":
                raise ValueError("oops")
            print("progress goes to stderr", file=sys.stderr)
            return {{"version": "1", "artifacts": [], "echo": package}}

        lib.run(generate)
    """))
    return path


def test_run_prints_result(script):
    result = run([script], stdin=json.dumps({"name": "p", "x": [1]}))
    assert result.code == 0
    assert result.json == {"version": "1", "artifacts": [], "echo": {"name": "p", "x": [1]}}
    assert result.out.count("\n") == 1
    assert "progress goes to stderr" in result.err


@pytest.mark.parametrize("stdin, message", [
    ("{bad", "Error [unknown]: couldn't parse the package JSON"),
    ("[1, 2]", "Error [unknown]: expected a JSON object"),
    ("{}", "Error [unknown]: the package has no 'name'"),
    ('{"name": "p", "mode": "tiny"}', "Error [p]: expected problem"),
    ('{"name": "p", "mode": "network"}', "Error [p]: network error: no route"),
    ('{"name": "p", "mode": "bug"}', "Error [p]: unexpected ValueError: oops (set PKGGEN_DEBUG=1 for a traceback)"),
])
def test_run_errors(script, stdin, message):
    result = run([script], stdin=stdin)
    assert result.code == 1
    assert result.out == ""
    assert result.err_plain.strip().startswith(message)
    assert len(result.err_plain.strip().splitlines()) == 1
    assert "Traceback" not in result.err


def test_run_debug_prints_traceback(script):
    result = run([script], stdin='{"name": "p", "mode": "tiny"}', env={"PKGGEN_DEBUG": "1"})
    assert result.code == 1
    assert "Traceback" in result.err
    assert "Error [p]: expected problem" in result.err_plain
