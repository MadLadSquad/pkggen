"""Signing Alpine and Void repositories with an RSA key (`rsa-key`), and publishing them to S3."""
import base64
import gzip
import io
import lzma
import tarfile

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.deployment.conftest import target
from tests.deployment.test_s3 import config, s3, s3_generator  # noqa: F401 (fixtures)


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key, passphrase=None):
    encryption = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
                  else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             encryption).decode()


@pytest.fixture
def rsa_key(monkeypatch):
    """The RSA key nicknamed 'test' (PKGGEN_RSA_KEY_TEST holds the PEM key itself)."""
    key = new_key()
    monkeypatch.setenv("PKGGEN_RSA_KEY_TEST", pem(key))
    return key


def verify(key, signature, data, digest=hashes.SHA256):
    key.public_key().verify(signature, data, padding.PKCS1v15(), digest())
    return True


# --- Alpine ------------------------------------------------------------------------------

def tar_member(files, cut=True):
    """A gzip'd tar member of {name: bytes}; cut drops the end-of-archive blocks, as abuild does."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    if cut:
        data = data.rstrip(b"\0")
        data += b"\0" * ((-len(data)) % 512)
    return gzip.compress(data, mtime=0)


def old_signature(name="builder-1234.rsa.pub"):
    return tar_member({f".SIGN.RSA.{name}": b"x" * 256})


CONTROL = tar_member({".PKGINFO": b"pkgname = foo\npkgver = 1.0-r0\n"})
DATA = tar_member({"usr/bin/foo": b"\x7fELF"}, cut=False)
INDEX = tar_member({"DESCRIPTION": b"pkggen\n", "APKINDEX": b"P:foo\nV:1.0-r0\n"}, cut=False)


def members(data, count):
    """Split data into its gzip members with the Alpine hook's own helper."""
    from tests.support.helpers import load_generator
    alpine = load_generator("deployment", "lib").load_hook("alpine")
    found, rest = alpine.gzip_members(data, count)
    return found + ([rest] if rest else [])


def signature_entry(member):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(member) + b"\0" * 1024), mode="r:") as tar:
        info = tar.next()
        return info.name, tar.extractfile(info).read(), info


def alpine_output(tmp_path, name="alpine-3.24", apk=None, index=None):
    folder = tmp_path / "test" / name
    (folder / "x86_64").mkdir(parents=True)
    (folder / "x86_64" / "foo-1.0-r0.apk").write_bytes(apk or old_signature() + CONTROL + DATA)
    (folder / "x86_64" / "APKINDEX.tar.gz").write_bytes(index or old_signature() + INDEX)
    (folder / "foo").mkdir()
    (folder / "foo" / "APKBUILD").write_text("pkgname=foo\n")
    return folder


def test_apk_signature_replaces_the_throwaway_one(deploy_lib, make_job, rsa_key, tmp_path):
    folder = alpine_output(tmp_path)
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test", "key-name": "repo-1.rsa.pub"},
                   distribution="alpine")
    added = deploy_lib.sign_repository(job, str(folder), lambda relative: None)
    assert added == ["x86_64/foo-1.0-r0.apk", "x86_64/APKINDEX.tar.gz"]

    signature, control, data = members((folder / "x86_64" / "foo-1.0-r0.apk").read_bytes(), 2)
    name, value, info = signature_entry(signature)
    assert name == ".SIGN.RSA256.repo-1.rsa.pub"
    assert (info.uid, info.gid, info.mode) == (0, 0, 0o644)
    # The control member (whose checksum the index has) and the data are untouched
    assert (control, data) == (CONTROL, DATA)
    assert verify(rsa_key, value, CONTROL)
    # No end-of-archive blocks, like abuild-tar --cut
    assert len(gzip.decompress(signature)) == 1024

    signature, rest = members((folder / "x86_64" / "APKINDEX.tar.gz").read_bytes(), 1)
    name, value, _ = signature_entry(signature)
    assert name == ".SIGN.RSA256.repo-1.rsa.pub"
    assert rest == INDEX and verify(rsa_key, value, INDEX)
    # The APKBUILD folders aren't touched
    assert (folder / "foo" / "APKBUILD").read_text() == "pkgname=foo\n"


def test_unsigned_apk_files_get_a_signature(deploy_lib, make_job, rsa_key, tmp_path):
    folder = alpine_output(tmp_path, apk=CONTROL + DATA, index=INDEX)
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test"}, distribution="alpine")
    deploy_lib.sign_repository(job, str(folder), lambda relative: None)
    signature, control, data = members((folder / "x86_64" / "foo-1.0-r0.apk").read_bytes(), 2)
    # Without key-name, the key is named after its nickname
    assert signature_entry(signature)[0] == ".SIGN.RSA256.test.rsa.pub"
    assert (control, data) == (CONTROL, DATA)


def test_unchanged_apk_files_keep_their_published_signature(deploy_lib, make_job, rsa_key, tmp_path):
    folder = alpine_output(tmp_path)
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test"}, distribution="alpine")
    deploy_lib.sign_repository(job, str(folder), lambda relative: None)
    published = {p: (folder / p).read_bytes() for p in ("x86_64/foo-1.0-r0.apk", "x86_64/APKINDEX.tar.gz")}

    # The test signed them with a new throwaway key, but the content is the same
    again = alpine_output(tmp_path, "again")
    deploy_lib.sign_repository(job, str(again), published.get)
    assert {p: (again / p).read_bytes() for p in published} == published

    # Another key name: signed again
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test", "key-name": "other.rsa.pub"},
                   distribution="alpine")
    third = alpine_output(tmp_path, "third")
    deploy_lib.sign_repository(job, str(third), published.get)
    assert signature_entry(members((third / "x86_64" / "APKINDEX.tar.gz").read_bytes(), 1)[0])[0] == \
        ".SIGN.RSA256.other.rsa.pub"


# --- Void --------------------------------------------------------------------------------

def repodata(files=None, compression="xz"):
    files = files or {"index.plist": b"<plist>foo</plist>\n", "index-meta.plist": b"", "stage.plist": b""}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode=f"w:{compression}", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uname, info.gname = len(data), 0o644, "root", "root"
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def repodata_files(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        return {i.name: tar.extractfile(i).read() for i in tar.getmembers()}


def void_output(tmp_path, name="void-glibc", index=None):
    folder = tmp_path / "test" / name / "binpkgs"
    folder.mkdir(parents=True)
    (folder / "foo-1.0_1.x86_64.xbps").write_bytes(b"glibc package")
    (folder / "foo-1.0_1.x86_64-musl.xbps").write_bytes(b"musl package")
    (folder / "x86_64-repodata").write_bytes(index or repodata())
    return folder.parent


VOID = {"rsa-key": "test", "signed-by": "Jane <jane@example.com>"}


def test_xbps_packages_and_index(deploy_lib, make_job, rsa_key, tmp_path):
    output = void_output(tmp_path)
    job = make_job(deploy_lib_generator(deploy_lib), VOID, distribution="void")
    added = deploy_lib.sign_repository(job, str(output), lambda relative: None)
    # Only the index's own architecture: the musl package belongs to x86_64-musl-repodata
    assert added == ["binpkgs/foo-1.0_1.x86_64.xbps.sig2", "binpkgs/x86_64-repodata"]
    signature = (output / "binpkgs" / "foo-1.0_1.x86_64.xbps.sig2").read_bytes()
    assert len(signature) == 256 and verify(rsa_key, signature, b"glibc package")

    index = (output / "binpkgs" / "x86_64-repodata").read_bytes()
    assert index.startswith(b"\xfd7zXZ")  # still xz
    files = repodata_files(index)
    assert list(files) == ["index.plist", "index-meta.plist", "stage.plist"]
    assert files["index.plist"] == b"<plist>foo</plist>\n"
    meta = files["index-meta.plist"].decode()
    public = rsa_key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    assert f"<data>{base64.b64encode(public).decode()}</data>" in meta
    assert "<integer>2048</integer>" in meta
    assert "<string>Jane &lt;jane@example.com&gt;</string>" in meta
    assert "<string>rsa</string>" in meta


def test_unchanged_xbps_files_keep_their_published_signatures(deploy_lib, make_job, rsa_key, tmp_path):
    output = void_output(tmp_path)
    job = make_job(deploy_lib_generator(deploy_lib), VOID, distribution="void")
    deploy_lib.sign_repository(job, str(output), lambda relative: None)
    published = {str(p.relative_to(output)).replace("\\", "/"): p.read_bytes() for p in output.rglob("*") if p.is_file()}

    again = void_output(tmp_path, "again")
    # xz output can differ between runs of the test: the published index is kept when its files are the same
    deploy_lib.sign_repository(job, str(again), published.get)
    assert {p: (again / p).read_bytes() for p in published} == published

    (again / "binpkgs" / "foo-1.0_1.x86_64.xbps").write_bytes(b"rebuilt")
    deploy_lib.sign_repository(job, str(again), published.get)
    assert verify(rsa_key, (again / "binpkgs" / "foo-1.0_1.x86_64.xbps.sig2").read_bytes(), b"rebuilt")


def test_gzip_and_uncompressed_repodata(deploy_lib, make_job, rsa_key, tmp_path):
    for compression, magic in (("gz", b"\x1f\x8b"), ("", None)):
        output = void_output(tmp_path, f"plain-{compression}", repodata(compression=compression))
        job = make_job(deploy_lib_generator(deploy_lib), VOID, distribution="void")
        deploy_lib.sign_repository(job, str(output), lambda relative: None)
        index = (output / "binpkgs" / "x86_64-repodata").read_bytes()
        if magic:
            assert index.startswith(magic)
        assert b"signature-by" in repodata_files(index)["index-meta.plist"]


def test_repodata_without_meta_gets_one(deploy_lib, make_job, rsa_key, tmp_path):
    output = void_output(tmp_path, index=repodata({"index.plist": b"x"}))
    deploy_lib.sign_repository(make_job(deploy_lib_generator(deploy_lib), VOID, distribution="void"),
                               str(output), lambda relative: None)
    assert list(repodata_files((output / "binpkgs" / "x86_64-repodata").read_bytes())) == \
        ["index.plist", "index-meta.plist"]


def test_zstd_repodata_is_an_error(deploy_lib, make_job, rsa_key, tmp_path):
    output = void_output(tmp_path, index=b"\x28\xb5\x2f\xfd" + lzma.compress(b"x"))
    job = make_job(deploy_lib_generator(deploy_lib), VOID, distribution="void")
    with pytest.raises(deploy_lib.DeployError, match=r"compressed with zstd.*xbps-rindex --compression xz"):
        deploy_lib.sign_repository(job, str(output), lambda relative: None)


def test_public_key_and_nothing_to_sign(deploy_lib, make_job, rsa_key, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test", "public-key": "keys/repo.rsa.pub"},
                   distribution="alpine")
    assert deploy_lib.sign_repository(job, str(folder), lambda relative: None) == ["keys/repo.rsa.pub"]
    assert (folder / "keys" / "repo.rsa.pub").read_text().startswith("-----BEGIN PUBLIC KEY-----\n")
    assert any("there's no repository to sign" in w for w in job.warnings)


# --- Keys and checks ---------------------------------------------------------------------

def test_key_from_a_file_with_a_passphrase(deploy_lib, make_job, secrets, tmp_path):
    key = new_key()
    path = tmp_path / "key.pem"
    path.write_text(pem(key, "hunter2"))
    secrets({"rsa_keys": {"repo": str(path)}, "rsa_passphrases": {"pass": "hunter2"}})
    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "repo", "rsa-passphrase": "pass"})
    signer = deploy_lib.RsaSigner(job, "repo")
    assert verify(key, signer.sign(b"data"), b"data")
    assert signer.bits == 2048

    job = make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "repo"})
    with pytest.raises(deploy_lib.DeployError, match=r"couldn't load the RSA key 'repo'.*set 'deployment.dist.rsa-passphrase'"):
        deploy_lib.RsaSigner(job, "repo")


def test_not_an_rsa_key(deploy_lib, make_job, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    monkeypatch.setenv("PKGGEN_RSA_KEY_ED", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()).decode())
    with pytest.raises(deploy_lib.DeployError, match="isn't an RSA key"):
        deploy_lib.RsaSigner(make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "ed"}), "ed")


@pytest.mark.parametrize("deployment, distribution, message", [
    ({"rsa-key": "nope"}, "alpine", "there's no RSA key called 'nope'"),
    ({"rsa-key": "test", "gpg-key": "test"}, "alpine",
     "'deployment.dist.gpg-key' doesn't apply to alpine repositories: they're signed with 'deployment.dist.rsa-key'"),
    ({"rsa-key": "test"}, "void", "'deployment.dist.signed-by' is required"),
    ({"rsa-key": "test", "key-name": "repo.pub"}, "alpine", "must be a file name ending in .rsa.pub"),
    ({"signed-by": "Me"}, "void", "'deployment.dist.signed-by' needs 'deployment.dist.rsa-key'"),
    ({"key-name": "a.rsa.pub"}, "alpine", "'deployment.dist.key-name' needs 'deployment.dist.rsa-key'"),
    ({"public-key": "k.pub"}, "alpine", "'deployment.dist.public-key' needs 'deployment.dist.rsa-key'"),
])
def test_check_errors(deploy_lib, make_job, rsa_key, deployment, distribution, message):
    errors = []
    deploy_lib.check_signing(make_job(deploy_lib_generator(deploy_lib), deployment, distribution=distribution), errors)
    assert any(message in e for e in errors), errors


def test_rsa_key_must_be_pem(deploy_lib, monkeypatch):
    monkeypatch.setenv("PKGGEN_RSA_KEY_BAD", "not a key")
    assert "must be the path of a PEM private key" in deploy_lib.secret_problem("rsa-key", "bad")


def test_missing_cryptography(deploy_lib, make_job, rsa_key, monkeypatch):
    monkeypatch.setattr(deploy_lib, "have_cryptography", lambda: False)
    errors = []
    deploy_lib.check_signing(make_job(deploy_lib_generator(deploy_lib), {"rsa-key": "test"}, distribution="alpine"),
                             errors)
    assert any("needs the Python package 'cryptography'" in e for e in errors)


# --- Publishing ----------------------------------------------------------------------------

def test_signed_apk_repositories_on_s3(s3_generator, make_job, s3, rsa_key, tmp_path):  # noqa: F811
    deployment = config(path="{target}", exclude=["^(?!x86_64/)"], prune=True, **{
        "rsa-key": "test", "key-name": "repo.rsa.pub", "public-key": "repo.rsa.pub"})
    output = alpine_output(tmp_path)
    job = make_job(s3_generator, deployment, targets=[target(output, "alpine-3.24")], distribution="alpine")
    assert s3_generator.check(job) == []
    s3_generator.main(job)
    # Packages first, the index last; the APKBUILDs are left out
    assert s3.uploads() == ["alpine-3.24/x86_64/foo-1.0-r0.apk", "repo.rsa.pub", "alpine-3.24/x86_64/APKINDEX.tar.gz"]
    assert s3.headers["alpine-3.24/x86_64/foo-1.0-r0.apk"]["Content-Type"] == "application/octet-stream"
    assert s3.headers["alpine-3.24/x86_64/APKINDEX.tar.gz"]["Cache-Control"] == "no-cache"
    signature = members(s3.objects["alpine-3.24/x86_64/APKINDEX.tar.gz"], 1)[0]
    assert verify(rsa_key, signature_entry(signature)[1], INDEX)

    # Deployed again after a new test run (new throwaway signatures): nothing changes
    s3.log.clear()
    job = make_job(s3_generator, deployment, targets=[target(alpine_output(tmp_path, "rerun"), "alpine-3.24")],
                   distribution="alpine")
    s3_generator.main(job)
    assert job.unchanged == ["dist-gen"] and s3.uploads() == []


def test_signed_xbps_repository_on_s3(s3_generator, make_job, s3, rsa_key, tmp_path):  # noqa: F811
    deployment = config(path="", **VOID)
    targets = [target(void_output(tmp_path, "void-glibc"), "void-glibc")]
    job = make_job(s3_generator, deployment, targets=targets, distribution="void")
    assert s3_generator.check(job) == []
    s3_generator.main(job)
    assert s3.uploads()[-1] == "binpkgs/x86_64-repodata"
    assert "binpkgs/foo-1.0_1.x86_64.xbps.sig2" in s3.objects
    assert s3.headers["binpkgs/x86_64-repodata"]["Cache-Control"] == "no-cache"


def test_git_signs_with_rsa(make_job, rsa_key, tmp_path):
    from tests.support.helpers import load_generator
    git = load_generator("deployment", "git")
    output = alpine_output(tmp_path)
    job = make_job(git, {"rsa-key": "test"}, distribution="alpine")
    staged = git.lib.stage_targets([("alpine-3.24", str(output), "")])
    work = tmp_path / "work" / "checkout"
    work.mkdir(parents=True)
    signed = git.signed(job, str(work), staged)
    signature = members(open(signed.files["x86_64/APKINDEX.tar.gz"], "rb").read(), 1)[0]
    assert signature_entry(signature)[0] == ".SIGN.RSA256.test.rsa.pub"


def deploy_lib_generator(deploy_lib):
    """make_job() takes a generator module and uses its `lib`: wrap the lib itself."""
    class Generator:
        lib = deploy_lib
    return Generator
