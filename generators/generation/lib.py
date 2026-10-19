#!/usr/bin/env python3
# Shared helpers for fetch generators.
#
# Contract: a fetch generator reads one package (JSON) on stdin and prints one
# JSON result on stdout. Everything else (progress bars, messages, errors) goes
# to stderr, so stdout always stays parseable.
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import yaml
from tqdm import tqdm

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


def readinput():
    return sys.stdin.buffer.read()


def log(message):
    tqdm.write(message, file=sys.stderr)


def warn(message):
    log(f"\x1b[33mWarning: {message}\x1b[0m")


def secrets_path():
    if os.name == "nt":
        base_dir = os.getenv("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
    else:
        base_dir = os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base_dir, "pkggen", "secrets.yaml")


def load_secrets():
    path = secrets_path()
    if not os.path.exists(path):
        warn(f"{path} was requested but not found. Creating it automatically.")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    try:
        with open(path, "r") as stream:
            result = yaml.safe_load(stream)
    except yaml.YAMLError as e:
        raise TinyError(f"couldn't parse {path}: {e}")
    if result is not None and not isinstance(result, dict):
        raise TinyError(f"{path} must contain a mapping of secret names to values")
    return result or {}


def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


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
    try:
        response = session.get(url, headers=request_headers, timeout=TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise TinyError(f"couldn't download {url}: {e}")

    with response:
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
