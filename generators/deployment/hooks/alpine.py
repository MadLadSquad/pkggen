# Deployment hook for Alpine: apk repositories signed with an RSA key (`rsa-key`).
#
# An .apk and APKINDEX.tar.gz start with a gzip'd tar member holding
# .SIGN.RSA256.<key name>, the signature of the rest (an .apk's control member,
# the whole index). The test's throwaway signature is replaced, so the index's
# checksums of the packages don't change. Signed on the host with the Python
# package cryptography, so the key never enters the test containers.
import gzip
import io
import os
import re
import tarfile
import zlib

import lib

SIGNING = "rsa"
OPTIONS = ("key-name",)

KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*\.rsa\.pub$")
SIGNATURE_PREFIX = ".SIGN."
SIGNATURE_TYPE = "RSA256"  # SHA-256; abuild-sign's default "RSA" is SHA-1
TAR_BLOCK = 512
INDEX = "APKINDEX.tar.gz"


def check(job, errors):
    key_name = job.config.get("key-name")
    if isinstance(key_name, str) and key_name.strip() and not KEY_NAME_RE.match(key_name):
        errors.append(f"{job.key('key-name')} must be a file name ending in .rsa.pub (the name of the public key in "
                      f"/etc/apk/keys), got {key_name!r}")


def gzip_members(data, count):
    """The first `count` gzip members of data, then the rest (possibly empty), as bytes."""
    members = []
    rest = data
    for _ in range(count):
        if not rest:
            break
        decompressor = zlib.decompressobj(wbits=31)
        try:
            decompressor.decompress(rest)
        except zlib.error as e:
            raise lib.DeployError(f"not a gzip stream: {e}")
        if not decompressor.eof:
            raise lib.DeployError("a gzip member is cut short")
        size = len(rest) - len(decompressor.unused_data)
        members.append(rest[:size])
        rest = rest[size:]
    return members, rest


def signature_name(member):
    """The .SIGN.* file name in a gzip'd tar member, or None when it's another member."""
    try:
        data = gzip.decompress(member)
    except (OSError, EOFError, zlib.error):
        return None
    # abuild cuts the end-of-archive blocks: put them back for tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(data + b"\0" * (2 * TAR_BLOCK)), mode="r:") as tar:
            first = tar.next()
    except tarfile.TarError:
        return None
    if first is not None and first.name.startswith(SIGNATURE_PREFIX):
        return first.name
    return None


def unsigned(data):
    """An .apk or APKINDEX.tar.gz without its leading signature member: (signature file name or None, rest)."""
    members, rest = gzip_members(data, 1)
    if not members:
        return None, data
    name = signature_name(members[0])
    return (name, rest) if name else (None, data)


def signature_member(name, signature):
    """A gzip'd tar member holding the signature, without end-of-archive blocks (like abuild-tar --cut)."""
    info = tarfile.TarInfo(name)
    info.size = len(signature)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    header = info.tobuf(format=tarfile.USTAR_FORMAT)
    padding = (-len(signature)) % TAR_BLOCK
    return gzip.compress(header + signature + b"\0" * padding, compresslevel=9, mtime=0)


def sign_apk(signer, data, key_name, index):
    """data (an .apk, or APKINDEX.tar.gz when index) signed with the key. The old signature is replaced."""
    _, rest = unsigned(data)
    if index:
        signed_part = rest
    else:
        members, _ = gzip_members(rest, 1)
        if not members:
            raise lib.DeployError("the package has no control member")
        signed_part = members[0]
    name = f"{SIGNATURE_PREFIX}{SIGNATURE_TYPE}.{key_name}"
    return signature_member(name, signer.sign(signed_part, "sha256")) + rest


def apk_files(folder):
    """[(relative path, is the index)] of the apk repositories in folder: every folder with an APKINDEX.tar.gz."""
    result = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        if INDEX not in files:
            continue
        relative_root = lib.normalise(os.path.relpath(root, folder))
        prefix = f"{relative_root}/" if relative_root else ""
        result += [(prefix + name, False) for name in sorted(files) if name.endswith(".apk")]
        result.append((prefix + INDEX, True))
    return result


def sign(job, folder, published):
    nickname = job.config["rsa-key"]
    signer = lib.RsaSigner(job, nickname)
    key_name = job.config.get("key-name") or f"{nickname}.rsa.pub"
    expected = f"{SIGNATURE_PREFIX}{SIGNATURE_TYPE}.{key_name}"
    added = []
    for relative, index in apk_files(folder):
        data = lib.read_relative(folder, relative)
        old = published(relative)
        # Unchanged: the published file has the same content, signed with this key
        if old is not None and unsigned(old) == (expected, unsigned(data)[1]):
            lib.write_relative(folder, relative, old)
        else:
            lib.write_relative(folder, relative, sign_apk(signer, data, key_name, index))
        added.append(relative)
    return lib.finish_signing(job, folder, added, INDEX, signer.public_key)
