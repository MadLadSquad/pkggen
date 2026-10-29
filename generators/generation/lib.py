#!/usr/bin/env python3
# Shared helpers for fetch generators.
#
# Contract: a fetch generator reads one package (JSON) on stdin and prints one
# JSON result on stdout. Everything else (progress bars, messages, errors) goes
# to stderr, so stdout always stays parseable.
import importlib.util
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import yaml
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from tqdm import tqdm

# generators/common.py: the helpers every layer shares (shquote, to_bool, secrets_path, ...), as lib.<name>.
# Loaded by path once per process, under a name no other module has
if "pkggen_common" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "pkggen_common", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common.py"))
    sys.modules["pkggen_common"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["pkggen_common"])
from pkggen_common import *  # noqa: E402,F401,F403

DEBUG = os.getenv("PKGGEN_DEBUG") == "1"
TIMEOUT = 30
CHUNK_SIZE = 1 << 16

# Output key -> hashlib algorithm name
HASHES = (
    ("md5", "md5"),
    ("sha1", "sha1"),
    ("sha2_224", "sha224"),
    ("sha3_224", "sha3_224"),
    ("sha2_256", "sha256"),
    ("sha3_256", "sha3_256"),
    ("sha2_384", "sha384"),
    ("sha3_384", "sha3_384"),
    ("sha2_512", "sha512"),
    ("sha3_512", "sha3_512"),
    ("blake2b", "blake2b"),  # 64-byte digest by default
    ("blake2s", "blake2s"),  # 32-byte digest by default
)


class TinyError(Exception):
    """An expected error. Reported as a single line, without a traceback."""


# --- HTTP cache --------------------------------------------------------------
#
# `pkggen generate` sets PKGGEN_CACHE_DIR (pkggen-build/cache by default). API
# answers and artifact checksums are kept there with their ETag/Last-Modified,
# and requested again conditionally: an unchanged answer is a 304, which
# GitHub doesn't count against the rate limit, and an unchanged artifact isn't
# downloaded again. Answers with `Cache-Control: max-age` (repology's) are
# also kept, and reused without asking until they're that old; when the
# network fails, an expired one is used with a warning. Without the variable
# nothing is cached.

CACHE_ENV = "PKGGEN_CACHE_DIR"
# Request headers that change the answer, so they're part of the cache key
CACHE_KEY_HEADERS = ("Accept", "Authorization", "PRIVATE-TOKEN")


def cache_dir():
    return os.getenv(CACHE_ENV) or None


def cache_path(kind, key):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir(), kind, digest[:2], f"{digest}.json")


def read_cache(kind, key):
    try:
        with open(cache_path(kind, key), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_cache(kind, key, entry):
    path = cache_path(kind, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, path)
    except OSError as e:
        warn(f"couldn't write the HTTP cache ({e}), continuing without it")


def validators(headers):
    """The ETag/Last-Modified of a response, which a later request can be made conditional on."""
    return {key: headers[name] for key, name in (("etag", "ETag"), ("last_modified", "Last-Modified"))
            if headers.get(name)}


def max_age(headers):
    """How many seconds an answer may be reused without asking again (Cache-Control: max-age), or None."""
    directives = [d.strip().lower() for d in (headers.get("Cache-Control") or "").split(",")]
    if "no-store" in directives or "no-cache" in directives:
        return None
    for directive in directives:
        name, _, value = directive.partition("=")
        if name.strip() == "max-age" and value.strip().strip('"').isdigit():
            return int(value.strip().strip('"')) or None
    return None


def now():
    return time.time()


def cached_response(entry, request, reason):
    response = requests.Response()
    response.status_code = 200
    response.reason = reason
    response._content = entry["body"].encode("utf-8")
    response.encoding = "utf-8"
    response.headers = CaseInsensitiveDict(entry["headers"])
    response.url = request.url
    response.request = request
    return response


def conditional_headers(entry):
    headers = {}
    if entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    if entry.get("last_modified"):
        headers["If-Modified-Since"] = entry["last_modified"]
    return headers


class CachingAdapter(HTTPAdapter):
    """Makes GET requests (not streamed downloads) conditional on the cached answer.

    A 304 is turned back into the cached 200, with the fresh rate limit headers.
    An answer with `Cache-Control: max-age` is returned from the cache, without
    a request, until it expires; after that it's still used when the request
    fails to connect.
    """

    def send(self, request, stream=False, **kwargs):
        if request.method != "GET" or stream or not cache_dir():
            return super().send(request, stream=stream, **kwargs)
        key = "\n".join([request.url] + [f"{name}: {request.headers.get(name, '')}" for name in CACHE_KEY_HEADERS])
        entry = read_cache("http", key)
        if entry and entry.get("max_age") and 0 <= now() - entry.get("stored", 0) < entry["max_age"]:
            return cached_response(entry, request, "OK (cached)")
        if entry:
            request.headers.update(conditional_headers(entry))
        try:
            response = super().send(request, stream=stream, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            if not (entry and entry.get("max_age")):
                raise
            stored = datetime.fromtimestamp(entry.get("stored", 0), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            warn(f"couldn't reach {request.url} ({type(e).__name__}), using its answer from {stored}")
            return cached_response(entry, request, "OK (stale)")

        if response.status_code == 304 and entry:
            cached = cached_response(entry, request, "OK (cached)")
            for name, value in response.headers.items():
                if name.lower().startswith(("x-ratelimit", "ratelimit")):
                    cached.headers[name] = value
            cached.url = response.url
            cached.connection = response.connection
            if entry.get("max_age"):
                write_cache("http", key, {**entry, "stored": now()})
            return cached

        lifetime = max_age(response.headers)
        if response.status_code == 200 and (validators(response.headers) or lifetime):
            try:
                body = response.content.decode("utf-8")
            except UnicodeDecodeError:
                return response  # only text answers are cached
            entry = {**validators(response.headers), "headers": dict(response.headers), "body": body}
            if lifetime:
                entry.update(max_age=lifetime, stored=now())
            write_cache("http", key, entry)
        return response


def session():
    """A requests session with the HTTP cache. Fetch generators use it for every request."""
    result = requests.Session()
    adapter = CachingAdapter()
    result.mount("https://", adapter)
    result.mount("http://", adapter)
    return result


def readinput():
    return sys.stdin.buffer.read()


def log(message):
    tqdm.write(message, file=sys.stderr)


def warn(message):
    log(f"\x1b[33mWarning: {message}\x1b[0m")


def load_secrets():
    path, created = ensure_secrets_file()  # noqa: F405 (from pkggen_common)
    if created:
        warn(f"{path} was requested but not found, so it was created (readable only by you)")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            result = yaml.safe_load(stream)
    except yaml.YAMLError as e:
        raise TinyError(f"couldn't parse {path}: {e}")
    except UnicodeDecodeError as e:
        raise TinyError(f"{path} isn't UTF-8: {e}")
    if result is not None and not isinstance(result, dict):
        raise TinyError(f"{path} must contain a mapping of secret names to values")
    return result or {}


def apply_transforms(transforms, value):
    """Apply [pattern, replacement] regex pairs to value, in order."""
    if transforms is None:
        return value
    if not isinstance(transforms, list):
        raise TinyError("'transforms' must be a list of [pattern, replacement] pairs")
    for transform in transforms:
        if not isinstance(transform, list) or len(transform) != 2:
            raise TinyError(f"every transform must be a [pattern, replacement] pair, got {transform!r}")
        try:
            value = re.sub(transform[0], transform[1], value)
        except re.error as e:
            raise TinyError(f"invalid transform regex {transform[0]!r}: {e}")
    return value


def http_error(response, what, secret_hint="a 'github_key'"):
    """Build a descriptive TinyError for a failed HTTP response.

    secret_hint names the secret that would help, e.g. "a 'gitlab_key'".
    """
    message = f"couldn't {what}: HTTP {response.status_code} {response.reason} ({response.url})"
    try:
        body = response.json()
        api_message = body.get("message") or body.get("error")
        if api_message:
            message += f": {api_message}"
    except (ValueError, AttributeError):
        pass

    # GitHub sends X-RateLimit-*, GitLab sends RateLimit-*
    headers = response.headers
    remaining = headers.get("X-RateLimit-Remaining") or headers.get("RateLimit-Remaining")
    if response.status_code == 429 or (response.status_code == 403 and remaining == "0"):
        reset = headers.get("X-RateLimit-Reset") or headers.get("RateLimit-Reset")
        when = datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S") if reset and reset.isdigit() else "later"
        message += f". API rate limit exceeded, it resets at {when}. Add {secret_hint} to {secrets_path()} to raise the limit"
    elif response.status_code == 404:
        message += f". Check that it exists. Private repositories also need {secret_hint} in secrets.yaml"
    return TinyError(message)


def iso_date(value):
    """Normalise an ISO 8601 or HTTP date to an ISO 8601 UTC string, or None."""
    if not value or not isinstance(value, str):
        return None
    # HTTP dates can contain a "T" too (GMT, Tue, Thu, Oct), so try ISO 8601 first
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat()


ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar.lz", ".tar.lzma",
                    ".tgz", ".txz", ".tbz2", ".tar", ".zip", ".7z")


def archive_stem(filename):
    """Best guess of the folder an archive extracts to: its name without the archive suffix."""
    for suffix in ARCHIVE_SUFFIXES:
        if filename.lower().endswith(suffix):
            return filename[:-len(suffix)]
    return filename


def short_name(name, width=30):
    return name if len(name) <= width else name[:width - 3] + "..."


def download_and_hash(session, url, headers=None, position=0, label=None, secret_hint="a 'github_key'"):
    """Stream url, hashing it on the fly.

    Returns {"size", "checksums"} plus "last_modified" (ISO 8601) when the
    server sends a Last-Modified header.

    Bytes are hashed exactly as served (no transparent decompression), which
    is what package managers see when they download the same URL.
    """
    request_headers = {"Accept-Encoding": "identity", **(headers or {})}
    # The artifact's checksums from an earlier run, used again when the server says it hasn't changed
    cached = read_cache("downloads", url) if cache_dir() else None
    if cached:
        request_headers.update(conditional_headers(cached))
    try:
        response = session.get(url, headers=request_headers, timeout=TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise TinyError(f"couldn't download {url}: {e}")

    with response:
        if response.status_code == 304 and cached:
            return dict(cached["result"])
        if response.status_code != 200:
            raise http_error(response, f"download {url}", secret_hint)

        expected = int(response.headers.get("content-length") or 0)
        last_modified = iso_date(response.headers.get("last-modified"))
        hashers = {key: hashlib.new(algorithm, usedforsecurity=False) for key, algorithm in HASHES}
        size = 0
        desc = f"Downloading {short_name(label or url.rsplit('/', 1)[-1])}"
        try:
            with tqdm(total=expected or None, unit="B", unit_scale=True, desc=desc,
                      position=position, leave=False, file=sys.stderr) as bar:
                for chunk in response.raw.stream(CHUNK_SIZE, decode_content=False):
                    if not chunk:
                        continue
                    for hasher in hashers.values():
                        hasher.update(chunk)
                    size += len(chunk)
                    bar.update(len(chunk))
        except Exception as e:
            raise TinyError(f"download of {url} was interrupted: {e}")

    if expected and size != expected:
        raise TinyError(f"download of {url} is truncated: got {size} of {expected} bytes")
    result = {"size": size, "checksums": {key: h.hexdigest() for key, h in hashers.items()}}
    if last_modified:
        result["last_modified"] = last_modified
    if cache_dir() and validators(response.headers):
        write_cache("downloads", url, {**validators(response.headers), "result": result})
    return result


def fail(name, message):
    print(f"\x1b[31mError [{name}]: {message}\x1b[0m", file=sys.stderr)
    sys.exit(1)


def run(generate):
    """Read the package from stdin, call generate(package) and print its result as JSON.

    Expected errors become a single "Error [package]: message" line on stderr
    and exit code 1. Set PKGGEN_DEBUG=1 to also get a traceback.
    """
    name = "unknown"
    try:
        data = json.loads(readinput())
        if not isinstance(data, dict):
            raise TinyError("expected a JSON object describing the package on stdin")
        name = data.get("name") or name
        if not data.get("name"):
            raise TinyError("the package has no 'name'")
        result = generate(data)
    except json.JSONDecodeError as e:
        fail(name, f"couldn't parse the package JSON: {e}")
    except TinyError as e:
        if DEBUG:
            traceback.print_exc()
        fail(name, str(e))
    except requests.RequestException as e:
        if DEBUG:
            traceback.print_exc()
        fail(name, f"network error: {e}")
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        fail(name, f"unexpected {type(e).__name__}: {e} (set PKGGEN_DEBUG=1 for a traceback)")

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
