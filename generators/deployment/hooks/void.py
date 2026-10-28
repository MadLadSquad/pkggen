# Deployment hook for Void: xbps repositories signed with an RSA key (`rsa-key`).
#
# <package>.xbps.sig2 is the signature of the package file, and the public key
# goes into <arch>-repodata's index-meta.plist (as xbps-rindex --sign writes
# it), where xbps finds it when a user first adds the repository. Signed on the
# host with the Python package cryptography, so the key never enters the test
# containers.
import base64
import io
import os
import re
import tarfile

import lib

SIGNING = "rsa"
OPTIONS = ("signed-by",)

REPODATA_RE = re.compile(r"-repodata$")
REPODATA_META = "index-meta.plist"
# Compressions Python's tarfile reads and writes
REPODATA_COMPRESSIONS = {b"\xfd7zXZ\x00": "xz", b"\x1f\x8b": "gz", b"BZh": "bz2"}


def check(job, errors):
    if job.config.get("rsa-key") and not job.config.get("signed-by"):
        errors.append(f"{job.key('signed-by')} is required with {job.key('rsa-key')} for Void repositories: "
                      "xbps shows it when users first trust the key (e.g. \"Jane Doe <jane@example.com>\")")


def xbps_files(folder):
    """[(relative repodata path, [relative .xbps paths])] of the xbps repositories in folder."""
    result = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        relative_root = lib.normalise(os.path.relpath(root, folder))
        prefix = f"{relative_root}/" if relative_root else ""
        for name in sorted(files):
            if REPODATA_RE.search(name):
                arch = name[:-len("-repodata")]
                packages = [prefix + p for p in sorted(files) if p.endswith(f".{arch}.xbps") or p.endswith(".noarch.xbps")]
                result.append((prefix + name, packages))
    return result


def xbps_meta(signer, signed_by):
    """index-meta.plist as xbps-rindex --sign writes it."""
    key = base64.b64encode(signer.public_key().encode("ascii")).decode("ascii")
    by = signed_by.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>public-key</key>
	<data>{key}</data>
	<key>public-key-size</key>
	<integer>{signer.bits}</integer>
	<key>signature-by</key>
	<string>{by}</string>
	<key>signature-type</key>
	<string>rsa</string>
</dict>
</plist>
""".encode("utf-8")


def repodata_compression(data, path):
    for magic, compression in REPODATA_COMPRESSIONS.items():
        if data.startswith(magic):
            return compression
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        raise lib.DeployError(f"{path} is compressed with zstd, which pkggen can't rewrite: index the repository "
                              "with `xbps-rindex --compression xz` (pkggen's Void testing generator does)")
    if len(data) >= 262 and data[257:262] == b"ustar":
        return ""
    raise lib.DeployError(f"{path} isn't an xbps repository index")


def repodata_members(data, path):
    """[(TarInfo, bytes)] of a repodata archive."""
    compression = repodata_compression(data, path)
    members = []
    with tarfile.open(fileobj=io.BytesIO(data), mode=f"r:{compression}") as tar:
        for info in tar.getmembers():
            content = tar.extractfile(info).read() if info.isfile() else b""
            members.append((info, content))
    return compression, members


def sign_repodata(data, path, meta):
    """The repodata archive with index-meta.plist replaced by meta, compressed as it was."""
    compression, members = repodata_members(data, path)
    output = io.BytesIO()
    found = False
    with tarfile.open(fileobj=output, mode=f"w:{compression}", format=tarfile.USTAR_FORMAT) as tar:
        for info, content in members:
            if info.name == REPODATA_META:
                content, found = meta, True
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        if not found:
            info = tarfile.TarInfo(REPODATA_META)
            info.size, info.mode, info.uname, info.gname = len(meta), 0o644, "root", "root"
            tar.addfile(info, io.BytesIO(meta))
    return output.getvalue()


def same_repodata(a, b, path):
    """Whether two repodata archives hold the same files."""
    try:
        return ({i.name: c for i, c in repodata_members(a, path)[1]}
                == {i.name: c for i, c in repodata_members(b, path)[1]})
    except (lib.DeployError, tarfile.TarError, OSError, EOFError):
        return False


def sign(job, folder, published):
    signer = lib.RsaSigner(job, job.config["rsa-key"])
    signed_by = job.config.get("signed-by") or ""
    repositories = xbps_files(folder)
    if repositories and not signed_by:
        raise lib.DeployError(f"{job.key('signed-by')} is required to sign xbps repositories")
    added = []
    for repodata, packages in repositories:
        for package in packages:
            signature = f"{package}.sig2"
            data = lib.read_relative(folder, package)
            old_signature = published(signature)
            if old_signature is not None and published(package) == data:
                lib.write_relative(folder, signature, old_signature)
            else:
                lib.write_relative(folder, signature, signer.sign(data, "sha256"))
            added.append(signature)
        data = sign_repodata(lib.read_relative(folder, repodata), repodata, xbps_meta(signer, signed_by))
        old = published(repodata)
        lib.write_relative(folder, repodata, old if old is not None and same_repodata(old, data, repodata) else data)
        added.append(repodata)
    return lib.finish_signing(job, folder, added, "an <arch>-repodata", signer.public_key)
