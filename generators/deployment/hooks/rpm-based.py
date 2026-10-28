# Deployment hook for Fedora, openSUSE and OpenMandriva: rpm-md repositories
# signed with GPG (`gpg-key`).
#
# dnf and zypper check repodata/repomd.xml.asc, and import the key from
# repodata/repomd.xml.key.
import os

import lib

SIGNING = "gpg"
OPTIONS = ()


def check(job, errors):
    pass


def metadata(folder):
    """[(repodata/repomd.xml, [(signature file, kind)])] in folder, as relative POSIX paths."""
    result = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        relative_root = lib.normalise(os.path.relpath(root, folder))
        if "repomd.xml" in files and relative_root.split("/")[-1] == "repodata":
            base = f"{relative_root}/repomd.xml"
            result.append((base, [(f"{base}.asc", "detach"), (f"{base}.key", "public-key")]))
    return sorted(result)


def sign(job, folder, published):
    return lib.sign_gpg(job, folder, published, metadata(folder), "repodata/repomd.xml")
