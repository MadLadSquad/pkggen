#!/usr/bin/env python3
# Uploads the repositories `pkggen test` built (pkggen-build/test/<target>/) to
# an S3-compatible bucket: Cloudflare R2, AWS S3, MinIO, Backblaze B2, ...
#
# Every target goes to its own folder (`path`, default `{target}`), or several
# share one (their files are merged, e.g. one repository with a section per
# release). `exclude` leaves files of the test output out, `gpg-key` or
# `rsa-key` sign the repositories. Only changed files are uploaded: packages
# first, then the indexes, then the metadata clients read first (each target's
# `publish`, from its testing generator, says which files those are), so
# clients never see an index that points at a missing file. With `prune`,
# objects no target has any more are deleted last, except `preserve` matches.
#
# Requests are signed with AWS Signature Version 4, using requests (no SDK).
import base64
import hashlib
import hmac
import mimetypes
import os
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import requests

import lib


KEYS = {"generator", "bucket", "endpoint", "region", "credentials", "addressing", "path", "targets", "prune",
        "preserve", "exclude", "gpg-key", "gpg-passphrase", "rsa-key", "rsa-passphrase", "key-name", "signed-by",
        "public-key"}
DEFAULT_PATH = "{target}"
TIMEOUT = 120
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Media types mimetypes doesn't know; the targets' `publish` adds their formats' own
CONTENT_TYPES = {".asc": "text/plain", ".gpg": "application/pgp-signature", ".zst": "application/zstd"}
PACKAGE, INDEX, METADATA = 0, 1, 2  # upload order


class Layout:
    """Which files of the bucket are indexes and metadata, by the `publish` of the targets they belong to."""

    def __init__(self, job, targets, paths):
        self.rules = []
        undescribed = []
        for t in targets:
            publish = t.publish or {}
            if not publish:
                undescribed.append(t.name)
            try:
                compiled = {key: [re.compile(p) for p in publish.get(key) or ()] for key in ("indexes", "metadata")}
            except re.error as e:
                raise lib.DeployError(f"the test results of {t.name} have an invalid 'publish' pattern ({e}): run "
                                      f"`pkggen test -t {t.name}` again")
            self.rules.append((paths[t.name], compiled, publish.get("content_types") or {}))
        if undescribed:
            job.warn(f"the test results of {', '.join(undescribed)} don't say which files are the repository's "
                     "indexes, so their files are uploaded in name order: run `pkggen test` again to record it")

    def matching(self, relative):
        """(path inside the target's folder, patterns, content types) of every target the path belongs to."""
        for folder, patterns, types in self.rules:
            if lib.under(relative, folder):
                yield (relative[len(folder) + 1:] if folder else relative), patterns, types

    def tier(self, relative):
        result = PACKAGE
        for inner, patterns, _ in self.matching(relative):
            if any(p.search(inner) for p in patterns["metadata"]):
                return METADATA
            if any(p.search(inner) for p in patterns["indexes"]):
                result = INDEX
        return result

    def content_type(self, relative):
        for _, _, types in self.matching(relative):
            for suffix in sorted(types, key=len, reverse=True):
                if relative.endswith(suffix):
                    return types[suffix]
        return (CONTENT_TYPES.get(os.path.splitext(relative)[1]) or mimetypes.guess_type(relative)[0]
                or "application/octet-stream")


# --- Credentials ---------------------------------------------------------------------

def credentials(job):
    """(access key id, secret access key, endpoint or None) for the deployment.

    `credentials` names an entry of `s3_credentials` in secrets.yaml, or
    PKGGEN_S3_ACCESS_KEY_<NICK>/PKGGEN_S3_SECRET_KEY_<NICK>/PKGGEN_S3_ENDPOINT_<NICK>.
    Without it, the usual AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are used.
    """
    nickname = job.config.get("credentials")
    if not nickname:
        access, secret = os.getenv("AWS_ACCESS_KEY_ID"), os.getenv("AWS_SECRET_ACCESS_KEY")
        if not access or not secret:
            raise lib.DeployError(f"set {job.key('credentials')} to a nickname of 's3_credentials' in "
                                  f"{lib.secrets_path()}, or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
        return access, secret, None
    entry = {}
    entries = lib.load_secrets().get("s3_credentials")
    if entries is not None and not isinstance(entries, dict):
        raise lib.DeployError(f"'s3_credentials' in {lib.secrets_path()} must be a mapping of nicknames to "
                              "{access-key-id, secret-access-key, endpoint}")
    if isinstance((entries or {}).get(nickname), dict):
        entry = entries[nickname]
    access = os.getenv(lib.env_name("PKGGEN_S3_ACCESS_KEY_", nickname)) or entry.get("access-key-id")
    secret = os.getenv(lib.env_name("PKGGEN_S3_SECRET_KEY_", nickname)) or entry.get("secret-access-key")
    endpoint = os.getenv(lib.env_name("PKGGEN_S3_ENDPOINT_", nickname)) or entry.get("endpoint")
    if not access or not secret:
        raise lib.DeployError(
            f"{job.key('credentials')}: there are no S3 credentials called '{nickname}': add `{nickname}: {{access-key-id: ..., "
            f"secret-access-key: ...}}` under 's3_credentials' in {lib.secrets_path()}, or set "
            f"{lib.env_name('PKGGEN_S3_ACCESS_KEY_', nickname)} and {lib.env_name('PKGGEN_S3_SECRET_KEY_', nickname)}")
    return str(access), str(secret), str(endpoint) if endpoint else None


# --- Signature Version 4 -------------------------------------------------------------

def uri_encode(value, slash=True):
    return quote(value, safe="-_.~" + ("/" if slash else ""))


def sign(method, url, headers, payload_hash, access_key, secret_key, region, now):
    """The headers of a request signed with AWS Signature Version 4 (service s3).

    url must already be percent-encoded. Every given header is signed, plus
    host, x-amz-content-sha256 and x-amz-date.
    """
    parts = urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    signed = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed.update({"host": parts.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
    names = sorted(signed)

    query = []
    for pair in parts.query.split("&") if parts.query else []:
        key, _, value = pair.partition("=")
        query.append((uri_encode(requests.utils.unquote(key), False), uri_encode(requests.utils.unquote(value), False)))
    canonical = "\n".join([
        method,
        parts.path or "/",
        "&".join(f"{k}={v}" for k, v in sorted(query)),
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_hash,
    ])
    scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = f"AWS4{secret_key}".encode("utf-8")
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = {**headers, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    result["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                               f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return result


class Bucket:
    def __init__(self, job):
        config = job.config
        self.job = job
        self.access, self.secret, endpoint = credentials(job)
        self.name = config["bucket"]
        endpoint = config.get("endpoint") or endpoint
        self.region = config.get("region") or ("auto" if endpoint else "us-east-1")
        if endpoint:
            self.base = endpoint.rstrip("/")
            self.virtual = config.get("addressing") == "virtual"
        else:
            self.base = f"https://s3.{self.region}.amazonaws.com"
            self.virtual = config.get("addressing", "virtual") == "virtual"
        self.session = requests.Session()

    def url(self, key="", query=""):
        parts = urlsplit(self.base)
        if self.virtual:
            base = f"{parts.scheme}://{self.name}.{parts.netloc}{parts.path}"
            path = "/" + uri_encode(key)
        else:
            base = self.base
            path = f"/{uri_encode(self.name)}/{uri_encode(key)}" if key else f"/{uri_encode(self.name)}"
        return base + path + (f"?{query}" if query else "")

    def request(self, method, key="", query="", data=b"", headers=None):
        url = self.url(key, query)
        payload_hash = hashlib.sha256(data).hexdigest() if data else EMPTY_SHA256
        signed = sign(method, url, headers or {}, payload_hash, self.access, self.secret, self.region,
                      datetime.now(timezone.utc))
        try:
            response = self.session.request(method, url, data=data or None, headers=signed, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise lib.DeployError(f"couldn't reach {self.base}: {e}")
        if response.status_code >= 300 and not (method == "GET" and response.status_code == 404 and key):
            raise lib.DeployError(f"{method} {key or self.name} failed: {error_message(response)}")
        return response

    def list(self, prefix):
        """{key: etag} of every object under prefix."""
        objects = {}
        token = None
        while True:
            query = "list-type=2&prefix=" + uri_encode(prefix, False)
            if token:
                query = "continuation-token=" + uri_encode(token, False) + "&" + query
            root = ElementTree.fromstring(self.request("GET", query=query).content)
            for item in root.iter(f"{S3_NS}Contents"):
                objects[item.findtext(f"{S3_NS}Key")] = (item.findtext(f"{S3_NS}ETag") or "").strip('"')
            if root.findtext(f"{S3_NS}IsTruncated") != "true":
                return objects
            token = root.findtext(f"{S3_NS}NextContinuationToken")

    def get(self, key):
        response = self.request("GET", key)
        return None if response.status_code == 404 else response.content

    def put(self, key, data, metadata, content_type):
        headers = {"Content-Type": content_type,
                   "Content-MD5": base64.b64encode(hashlib.md5(data).digest()).decode("ascii")}
        if metadata:
            # Indexes change in place: caches in front of the bucket must check them every time
            headers["Cache-Control"] = "no-cache"
        self.request("PUT", key, data=data, headers=headers)

    def delete(self, key):
        self.request("DELETE", key)


def error_message(response):
    try:
        root = ElementTree.fromstring(response.content)
        code, message = root.findtext("Code"), root.findtext("Message")
        if code:
            return f"HTTP {response.status_code} {code}: {message or ''}".strip()
    except ElementTree.ParseError:
        pass
    return f"HTTP {response.status_code} {response.text.strip()[:200]}"


# --- Deploying -----------------------------------------------------------------------

def target_paths(job, names):
    template = job.config.get("path", DEFAULT_PATH)
    return {name: lib.normalise(template.replace("{target}", name)) for name in names}


def prefix(folder):
    return f"{folder}/" if folder else ""


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(job):
    config = job.config
    if job.packages:
        job.warn("-p doesn't apply to S3 deployments: they upload whole repositories")
    targets = job.usable_targets()
    if not targets:
        if not job.skipped:
            raise lib.DeployError("nothing to deploy: no target has test output")
        return
    skipped = job.skipped_targets + [s["name"] for s in job.skipped]
    paths = target_paths(job, [t.name for t in targets] + skipped)
    staged = lib.stage_targets([(t.name, t.path, paths[t.name]) for t in targets], config.get("exclude") or (),
                               [paths[name] for name in skipped])
    if staged.hold:
        job.warn(f"nothing is pruned in {', '.join(f or 'the root' for f in staged.hold)} this time: a target "
                 "that shares it isn't deployed")

    bucket = Bucket(job)
    names = ", ".join(t.name for t in targets)
    job.say(f"deploying {names} to {bucket.url()}")
    remote = {}
    for folder in staged.folders:
        remote.update(bucket.list(prefix(folder)))
    if config.get("public-key"):
        # Relative to the bucket's root, which may be outside every target's folder
        remote.update(bucket.list(lib.normalise(config["public-key"])))

    def published(relative):
        return bucket.get(relative) if relative in remote else None

    layout = Layout(job, targets, paths)
    folder = lib.write_staged(staged, os.path.join(job.work_path, "stage"))
    lib.sign_repository(job, folder, published)

    files = {}
    for root, _, names_in_root in os.walk(folder):
        for name in names_in_root:
            path = os.path.join(root, name)
            files[lib.normalise(os.path.relpath(path, folder))] = path
    changed = sorted((relative for relative, path in files.items() if remote.get(relative) != md5_file(path)),
                     key=lambda relative: (layout.tier(relative), relative))
    patterns = [re.compile(p) for p in config.get("preserve") or ()]
    protected = [paths[name] for name in skipped]
    stale = []
    if config.get("prune"):
        staged.files = files  # with the signatures, which are published too
        stale = sorted(key for key in remote if staged.prunable(key, patterns, protected))

    if not changed and not stale:
        job.say("already up to date")
        job.unchanged.append(job.id)
        return
    summary = f"{len(changed)} file{'' if len(changed) == 1 else 's'} to upload"
    if stale:
        summary += f", {len(stale)} to delete"
    if job.dry_run:
        job.say(f"would upload/delete in {bucket.name}: {summary} (dry run)")
        for relative in changed:
            job.say(f"  + {relative}")
        for relative in stale:
            job.say(f"  - {relative}")
        job.pushed.append(job.id)
        return

    job.say(f"{summary}")
    for relative in changed:
        with open(files[relative], "rb") as f:
            bucket.put(relative, f.read(), layout.tier(relative) > PACKAGE, layout.content_type(relative))
    for relative in stale:
        bucket.delete(relative)
    job.say(f"uploaded {len(changed)}, deleted {len(stale)}")
    job.pushed.append(job.id)


# --- Configuration -------------------------------------------------------------------

def check(job):
    errors = []
    config = job.config
    lib.check_keys(job, KEYS, errors)
    lib.check_string(job, "bucket", errors, required=True)
    endpoint = lib.check_string(job, "endpoint", errors)
    if endpoint and not re.match(r"^https?://[^/]+", endpoint):
        errors.append(f"{job.key('endpoint')} must be a URL like https://<account>.r2.cloudflarestorage.com")
    lib.check_string(job, "region", errors)
    lib.check_string(job, "credentials", errors)
    addressing = config.get("addressing")
    if addressing is not None and addressing not in ("path", "virtual"):
        errors.append(f"{job.key('addressing')} must be 'path' or 'virtual'")
    if "prune" in config and not isinstance(config["prune"], bool):
        errors.append(f"{job.key('prune')} must be true or false")
    lib.check_patterns(job, "preserve", errors)
    lib.check_patterns(job, "exclude", errors)
    if config.get("preserve") is not None and not config.get("prune"):
        job.warn(f"{job.key('preserve')} only matters with 'prune: true': without it nothing is deleted")
    lib.check_signing(job, errors)
    try:
        _, _, secret_endpoint = credentials(job)
        if not endpoint and not secret_endpoint and config.get("credentials"):
            job.warn(f"{job.key('endpoint')} isn't set, so {job.id} uploads to AWS S3 ({config.get('region') or 'us-east-1'})")
    except lib.DeployError as e:
        errors.append(str(e))

    path = config.get("path", DEFAULT_PATH)
    if not isinstance(path, str):
        errors.append(f"{job.key('path')} must be a string like \"{{target}}\"")
    elif ".." in lib.normalise(path).split("/") or path.startswith("/"):
        errors.append(f"{job.key('path')} must be a folder inside the bucket, got {path!r}")
    else:
        folders = sorted(set(target_paths(job, [t.name for t in job.targets] + job.skipped_targets).values()))
        nested = [(a, b) for a in folders for b in folders if a != b and lib.under(b, a)]
        if nested:
            errors.append(f"{job.key('path')} puts one target's folder ({nested[0][1] or 'the root'}) inside "
                          f"another one's ({nested[0][0] or 'the root'}): add {{target}} to it, or use the same "
                          "folder for all of them")
    return errors


if __name__ == "__main__":
    lib.run(main, check)
