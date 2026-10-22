"""The S3 deployment generator, against an in-memory S3 served through `responses`."""
import base64
import hashlib
import re
import shutil
import subprocess
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest

from tests.deployment.conftest import target, write_tree
from tests.support.helpers import load_generator

ENDPOINT = "https://account.r2.example.com"
BUCKET = "debs"
needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs GnuPG")


class FakeS3:
    """Path-style S3 with ListObjectsV2, GetObject, PutObject and DeleteObject."""

    def __init__(self, mock, endpoint=ENDPOINT, bucket=BUCKET):
        self.objects = {}
        self.headers = {}
        self.log = []
        self.prefix = f"{endpoint}/{bucket}"
        pattern = re.compile(re.escape(self.prefix) + r"(/[^?]*)?(\?.*)?$")
        for method in ("GET", "PUT", "DELETE"):
            mock.add_callback(method, pattern, callback=self.handle)

    def handle(self, request):
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=KEYID/")
        parts = urlsplit(request.url)
        key = unquote(parts.path[len(urlsplit(self.prefix).path) + 1:])
        if request.method == "GET" and not key:
            return self.list(parse_qs(parts.query).get("prefix", [""])[0])
        self.log.append((request.method, key))
        if request.method == "GET":
            return (200, {}, self.objects[key]) if key in self.objects else (404, {}, "<Error><Code>NoSuchKey</Code></Error>")
        if request.method == "PUT":
            body = request.body if isinstance(request.body, bytes) else (request.body or b"")
            assert request.headers["Content-MD5"] == base64.b64encode(hashlib.md5(body).digest()).decode()
            assert request.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
            self.objects[key] = body
            self.headers[key] = dict(request.headers)
            return 200, {"ETag": f'"{hashlib.md5(body).hexdigest()}"'}, ""
        self.objects.pop(key, None)
        return 204, {}, ""

    def list(self, prefix):
        items = "".join(f"<Contents><Key>{escape(k)}</Key><ETag>&quot;{hashlib.md5(v).hexdigest()}&quot;</ETag>"
                        f"<Size>{len(v)}</Size></Contents>"
                        for k, v in sorted(self.objects.items()) if k.startswith(prefix))
        return 200, {}, ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
                         'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                         f"{items}<IsTruncated>false</IsTruncated></ListBucketResult>")

    def uploads(self):
        return [key for method, key in self.log if method == "PUT"]

    def deletes(self):
        return [key for method, key in self.log if method == "DELETE"]


@pytest.fixture
def s3_generator():
    return load_generator("deployment", "s3")


@pytest.fixture
def s3(http, monkeypatch):
    monkeypatch.setenv("PKGGEN_S3_ACCESS_KEY_R2", "KEYID")
    monkeypatch.setenv("PKGGEN_S3_SECRET_KEY_R2", "SECRET")
    return FakeS3(http)


def config(**extra):
    return {"generator": "s3", "bucket": BUCKET, "endpoint": ENDPOINT, "credentials": "r2", **extra}


def apt_output(tmp_path, name, codename, version):
    """A reprepro repository like the Debian testing generator collects."""
    return write_tree(tmp_path / "test" / name, {
        f"pool/main/f/foo/foo_{version}_amd64.deb": f"deb {version}",
        f"dists/{codename}/Release": f"Codename: {codename}\nfoo {version}\n",
        f"dists/{codename}/main/binary-amd64/Packages": f"Package: foo\nVersion: {version}\n",
        f"dists/{codename}/main/binary-amd64/Release": "Component: main\n",
        "conf/distributions": f"Codename: {codename}\n",
        "db/packages.db": f"{name} database",
        "index.html": "landing page",
    })


def deploy(s3_generator, make_job, deployment, targets, **fields):
    job = make_job(s3_generator, deployment, targets=targets, **fields)
    errors = s3_generator.check(job)
    assert errors == [], errors
    s3_generator.main(job)
    return job


DEBIAN = config(path="", exclude=["^(conf|db)/"], prune=True, preserve=["^keep/"])


def test_merged_releases_share_one_repository(s3_generator, make_job, s3, tmp_path):
    targets = [target(apt_output(tmp_path, "debian-13", "trixie", "1.0-1~deb13"), "debian-13"),
               target(apt_output(tmp_path, "ubuntu-24.04", "noble", "1.0-1~ubuntu24.04"), "ubuntu-24.04")]
    job = deploy(s3_generator, make_job, DEBIAN, targets)
    assert job.pushed == ["dist-gen"]
    assert sorted(s3.objects) == [
        "dists/noble/Release", "dists/noble/main/binary-amd64/Packages", "dists/noble/main/binary-amd64/Release",
        "dists/trixie/Release", "dists/trixie/main/binary-amd64/Packages", "dists/trixie/main/binary-amd64/Release",
        "index.html", "pool/main/f/foo/foo_1.0-1~deb13_amd64.deb", "pool/main/f/foo/foo_1.0-1~ubuntu24.04_amd64.deb"]
    uploads = s3.uploads()
    # Packages first, then the indexes, then the Release files clients read first
    order = [0 if k.startswith("pool/") or k == "index.html" else 2 if k.endswith("/Release") else 1 for k in uploads]
    assert order == sorted(order)
    assert s3.headers["dists/trixie/Release"]["Cache-Control"] == "no-cache"
    assert "Cache-Control" not in s3.headers["pool/main/f/foo/foo_1.0-1~deb13_amd64.deb"]
    assert s3.headers["pool/main/f/foo/foo_1.0-1~deb13_amd64.deb"]["Content-Type"] == \
        "application/vnd.debian.binary-package"

    # Nothing changed: nothing is uploaded
    s3.log.clear()
    job = deploy(s3_generator, make_job, DEBIAN, targets)
    assert job.unchanged == ["dist-gen"] and s3.uploads() == []


def test_prune_preserve_and_exclude(s3_generator, make_job, s3, tmp_path):
    output = apt_output(tmp_path, "debian-13", "trixie", "1.0-1")
    s3.objects.update({"pool/main/f/foo/foo_0.9-1_amd64.deb": b"old", "keep/notes.txt": b"mine",
                       "db/packages.db": b"excluded files are never pruned"})
    job = deploy(s3_generator, make_job, DEBIAN, [target(output, "debian-13")])
    assert s3.deletes() == ["pool/main/f/foo/foo_0.9-1_amd64.deb"]
    assert s3.objects["db/packages.db"] == b"excluded files are never pruned"
    assert "keep/notes.txt" in s3.objects and "conf/distributions" not in s3.objects
    # Deletions come after every upload
    assert s3.log.index(("DELETE", "pool/main/f/foo/foo_0.9-1_amd64.deb")) > max(
        i for i, (method, _) in enumerate(s3.log) if method == "PUT")
    assert job.pushed == ["dist-gen"]


def test_every_target_in_its_own_folder(s3_generator, make_job, s3, tmp_path):
    targets = [target(write_tree(tmp_path / "test" / name, {"RPMS/x86_64/foo.rpm": name, "SOURCES/foo.tar.gz": "s"}),
                      name) for name in ("fedora-44", "fedora-43")]
    deploy(s3_generator, make_job, config(exclude=["^SOURCES/"]), targets)
    assert sorted(s3.objects) == ["fedora-43/RPMS/x86_64/foo.rpm", "fedora-44/RPMS/x86_64/foo.rpm"]


def test_dry_run_changes_nothing(s3_generator, make_job, s3, tmp_path, capfd):
    s3.objects["pool/stale.deb"] = b"old"
    job = deploy(s3_generator, make_job, DEBIAN, [target(apt_output(tmp_path, "debian-13", "trixie", "1"), "debian-13")],
                 dry_run=True)
    assert s3.uploads() == [] and s3.deletes() == [] and job.pushed == ["dist-gen"]
    err = capfd.readouterr().err
    assert "would upload/delete in debs: 5 files to upload, 1 to delete (dry run)" in err
    assert "  + dists/trixie/Release" in err and "  - pool/stale.deb" in err


def test_files_that_differ_between_merged_targets(s3_generator, make_job, s3, tmp_path):
    a = write_tree(tmp_path / "test" / "a", {"index.html": "one"})
    b = write_tree(tmp_path / "test" / "b", {"index.html": "two"})
    job = make_job(s3_generator, config(path=""), targets=[target(a, "a"), target(b, "b")])
    with pytest.raises(s3_generator.lib.DeployError, match="a and b both have index.html, with different content"):
        s3_generator.main(job)
    assert s3.uploads() == []


def test_a_skipped_target_holds_back_pruning_in_its_folder(s3_generator, make_job, s3, tmp_path, capfd):
    s3.objects["dists/noble/Release"] = b"the last noble release"
    output = apt_output(tmp_path, "debian-13", "trixie", "1")
    deploy(s3_generator, make_job, DEBIAN, [target(output, "debian-13")], skipped_targets=["ubuntu-24.04"])
    assert s3.deletes() == [] and s3.objects["dists/noble/Release"] == b"the last noble release"
    assert "nothing is pruned in the root this time" in capfd.readouterr().err


def test_failed_targets_are_not_deployed(s3_generator, make_job, s3, tmp_path):
    job = make_job(s3_generator, DEBIAN, targets=[target(apt_output(tmp_path, "debian-13", "trixie", "1"), "debian-13",
                                                         ok=False, reason="its tests failed")])
    s3_generator.main(job)
    assert s3.log == [] and job.skipped[0]["name"] == "debian-13"


@pytest.mark.parametrize("deployment, message", [
    ({"generator": "s3"}, "'deployment.dist.bucket' is required"),
    (config(endpoint="r2.example.com"), "'deployment.dist.endpoint' must be a URL"),
    (config(typo=1), "has unknown keys: typo"),
    (config(addressing="sideways"), "'deployment.dist.addressing' must be 'path' or 'virtual'"),
    (config(credentials="missing"), "there are no S3 credentials called 'missing'"),
    (config(path="../out"), "'deployment.dist.path' must be a folder inside the bucket"),
    (config(exclude="^conf/"), "'deployment.dist.exclude' must be a list of regular expressions"),
    (config(**{"public-key": "keys/public.asc"}), "'deployment.dist.public-key' needs 'deployment.dist.gpg-key'"),
    (config(**{"gpg-key": "nope"}), "there's no GPG key called 'nope'"),
])
def test_check_errors(s3_generator, make_job, s3, deployment, message):
    errors = s3_generator.check(make_job(s3_generator, deployment))
    assert any(message in e for e in errors), errors


def test_nested_target_folders(s3_generator, make_job, s3):
    job = make_job(s3_generator, config(path="{target}"), targets=[target("/x", "a")], skipped_targets=["a/b"])
    assert any("puts one target's folder (a/b) inside another one's (a)" in e for e in s3_generator.check(job))


def test_credentials_from_secrets_and_aws_variables(s3_generator, make_job, secrets, monkeypatch):
    secrets({"s3_credentials": {"r2": {"access-key-id": "A", "secret-access-key": "S",
                                       "endpoint": "https://e.example.com"}}})
    job = make_job(s3_generator, {"generator": "s3", "bucket": "b", "credentials": "r2"})
    assert s3_generator.credentials(job) == ("A", "S", "https://e.example.com")
    assert s3_generator.Bucket(job).url("a b/c") == "https://e.example.com/b/a%20b/c"

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SK")
    job = make_job(s3_generator, {"generator": "s3", "bucket": "b", "region": "eu-west-1"})
    assert s3_generator.credentials(job) == ("AK", "SK", None)
    # AWS itself: virtual-hosted URLs
    assert s3_generator.Bucket(job).url("k") == "https://b.s3.eu-west-1.amazonaws.com/k"


def test_signature_version_4_matches_aws_examples(s3_generator):
    """The examples of https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html"""
    when = datetime(2013, 5, 24, tzinfo=timezone.utc)
    access, secret = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    headers = s3_generator.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                                s3_generator.EMPTY_SHA256, access, secret, "us-east-1", when)
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    headers = s3_generator.sign("GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", {},
                                s3_generator.EMPTY_SHA256, access, secret, "us-east-1", when)
    assert headers["Authorization"].endswith("Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


# --- Signing ---------------------------------------------------------------------------


@pytest.fixture
def gpg_key(tmp_path, monkeypatch):
    """A throwaway signing key, given to pkggen as armored text in PKGGEN_GPG_KEY_TEST."""
    home = tmp_path / "keymaker"
    home.mkdir(mode=0o700)
    env = {"GNUPGHOME": str(home)}

    def gpg(*args):
        return subprocess.run(["gpg", "--batch", "--yes", *args], env={**__import__("os").environ, **env},
                              check=True, capture_output=True, text=True).stdout
    gpg("--passphrase", "", "--quick-gen-key", "Repository <repo@example.org>", "ed25519", "sign", "never")
    monkeypatch.setenv("PKGGEN_GPG_KEY_TEST", gpg("--armor", "--export-secret-keys"))
    yield gpg
    subprocess.run(["gpgconf", "--kill", "gpg-agent"], env={**__import__("os").environ, **env}, capture_output=True)


def verify(gpg, tmp_path, files, signature, signed=None):
    """gpg --verify with only the published public key."""
    (tmp_path / "check").mkdir(exist_ok=True)
    for name, data in files.items():
        (tmp_path / "check" / name.replace("/", "_")).write_bytes(data)
    home = tmp_path / "verify"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {**__import__("os").environ, "GNUPGHOME": str(home)}
    subprocess.run(["gpg", "--batch", "--import", str(tmp_path / "check" / "keys_public.asc")], env=env,
                   check=True, capture_output=True)
    args = [str(tmp_path / "check" / signature.replace("/", "_"))]
    if signed:
        args.append(str(tmp_path / "check" / signed.replace("/", "_")))
    return subprocess.run(["gpg", "--batch", "--verify", *args], env=env, capture_output=True).returncode == 0


@needs_gpg
def test_signed_apt_repository(s3_generator, make_job, s3, tmp_path, gpg_key):
    deployment = {**DEBIAN, "gpg-key": "test", "public-key": "keys/public.asc"}
    output = apt_output(tmp_path, "debian-13", "trixie", "1.0-1")
    deploy(s3_generator, make_job, deployment, [target(output, "debian-13")])
    published = s3.objects
    assert {"dists/trixie/InRelease", "dists/trixie/Release.gpg", "keys/public.asc"} <= set(published)
    # Only the suite's Release is signed, not the components'
    assert "dists/trixie/main/binary-amd64/InRelease" not in published
    assert b"-----BEGIN PGP SIGNED MESSAGE-----" in published["dists/trixie/InRelease"]
    assert b"Codename: trixie" in published["dists/trixie/InRelease"]
    assert verify(gpg_key, tmp_path, published, "dists/trixie/Release.gpg", "dists/trixie/Release")
    assert verify(gpg_key, tmp_path, published, "dists/trixie/InRelease")
    # The key stays out of the work folder
    assert not list((tmp_path / "work").glob("gnupg-*"))

    # Unchanged: the published signatures are kept, so nothing is uploaded
    s3.log.clear()
    job = deploy(s3_generator, make_job, deployment, [target(output, "debian-13")])
    assert job.unchanged == ["dist-gen"] and s3.uploads() == []

    # A new release file is signed again
    (output / "dists" / "trixie" / "Release").write_text("Codename: trixie\nfoo 1.0-2\n")
    s3.log.clear()
    deploy(s3_generator, make_job, deployment, [target(output, "debian-13")])
    assert {"dists/trixie/Release", "dists/trixie/InRelease", "dists/trixie/Release.gpg"} <= set(s3.uploads())
    assert verify(gpg_key, tmp_path, s3.objects, "dists/trixie/Release.gpg", "dists/trixie/Release")


@needs_gpg
def test_signed_rpm_repositories(s3_generator, make_job, s3, tmp_path, gpg_key):
    output = write_tree(tmp_path / "test" / "fedora-44", {"RPMS/repodata/repomd.xml": "<repomd/>",
                                                          "RPMS/x86_64/foo.rpm": "rpm"})
    deploy(s3_generator, make_job, config(**{"gpg-key": "test", "public-key": "rpmkey.pub"}),
           [target(output, "fedora-44")])
    # The public key is at the root, where the .repo file's gpgkey points
    assert {"fedora-44/RPMS/repodata/repomd.xml.asc", "fedora-44/RPMS/repodata/repomd.xml.key",
            "rpmkey.pub"} <= set(s3.objects)
    assert s3.objects["fedora-44/RPMS/repodata/repomd.xml.asc"].startswith(b"-----BEGIN PGP SIGNATURE-----")
    assert s3.objects["fedora-44/RPMS/repodata/repomd.xml.key"] == s3.objects["rpmkey.pub"]
    s3.log.clear()
    job = deploy(s3_generator, make_job, config(**{"gpg-key": "test", "public-key": "rpmkey.pub"}),
                 [target(output, "fedora-44")])
    assert job.unchanged == ["dist-gen"] and s3.uploads() == []
