# Deployment hook for Debian and Ubuntu: apt repositories signed with GPG (`gpg-key`).
#
# apt checks dists/<suite>/InRelease, or Release + Release.gpg. Only the
# suite's Release is signed, not the per-component ones further down.
import os

import lib

SIGNING = "gpg"
OPTIONS = ()


def check(job, errors):
    pass


def metadata(folder):
    """[(dists/<suite>/Release, [(signature file, kind)])] in folder, as relative POSIX paths."""
    result = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        relative_root = lib.normalise(os.path.relpath(root, folder))
        parts = relative_root.split("/") if relative_root else []
        if "Release" in files and len(parts) >= 2 and parts[-2] == "dists":
            base = f"{relative_root}/Release"
            result.append((base, [(f"{relative_root}/InRelease", "clearsign"), (f"{base}.gpg", "detach")]))
    return sorted(result)


def sign(job, folder, published):
    return lib.sign_gpg(job, folder, published, metadata(folder), "dists/<suite>/Release")
