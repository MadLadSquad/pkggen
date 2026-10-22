"""Canned GitHub answers for the packages in example/pkggen.yaml."""
import hashlib

from tests.support import repology
from tests.support.http_shim.pkggen_test_http import Routes

API = "https://api.github.com/repos/MadLadSquad"
DOWNLOADS = "https://github.com/MadLadSquad"
RELEASES_QUERY = {"page": "1", "per_page": "100"}


def archive_bytes(name):
    """Deterministic stand-in for a release tarball. Nothing extracts it."""
    return (f"pkggen test archive {name}\n" * 64).encode("utf-8")


def checksums(data):
    return {
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha2_224": hashlib.sha224(data).hexdigest(),
        "sha3_224": hashlib.sha3_224(data).hexdigest(),
        "sha2_256": hashlib.sha256(data).hexdigest(),
        "sha3_256": hashlib.sha3_256(data).hexdigest(),
        "sha2_384": hashlib.sha384(data).hexdigest(),
        "sha3_384": hashlib.sha3_384(data).hexdigest(),
        "sha2_512": hashlib.sha512(data).hexdigest(),
        "sha3_512": hashlib.sha3_512(data).hexdigest(),
        "blake2b": hashlib.blake2b(data).hexdigest(),
        "blake2s": hashlib.blake2s(data).hexdigest(),
    }


def release(repo, tag, asset_names, published="2026-09-01T10:00:00Z", draft=False, prerelease=False,
            downloads=DOWNLOADS):
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published,
        "created_at": published,
        "assets": [{"name": name, "browser_download_url": f"{downloads}/{repo}/releases/download/{tag}/{name}"}
                   for name in asset_names],
    }


# package name -> (GitHub user, repository, newest accepted tag), in pkggen.yaml order
PACKAGES = {
    "untitled-cli-parser": ("MadLadSquad", "UntitledCLIParser", "v6.1.0.0"),
    "untitled-xdg-basedir": ("MadLadSquad", "UntitledXDGBasedir", "v6.0.0.0"),
    "untitled-exec": ("MadLadSquad", "UntitledExec", "v5.0.0.1"),
    "untitled-i18n": ("MadLadSquad", "UntitledI18N", "v1.6.2.0"),
    "untitled-dbus-utils": ("MadLadSquad", "UntitledDBusUtils", "v2.2.0.0"),
    "untitled-open": ("MadLadSquad", "UntitledOpen", "v3.0.1.0"),
    "untitled-imgui-framework": ("MadLadSquad", "UntitledImGuiFramework", "v2.5.0.1"),
    "ude-session-logout": ("MadLadSquad", "UntitledDESessionLogout", "v2.3.0.0"),
    "untitled-game-system-manager": ("MadLadSquad", "UntitledGameSystemManager", "v2.9.1.0"),
    "untitled-ibus-handwriting": ("MadLadSquad", "UntitledIBusHandwriting", "v1.5.1.0"),
    "abcccid": ("invakid404", "abcccid", "2.1.8"),
}
# Packaged for every distribution; the others are Gentoo only
EVERYWHERE = [name for name in PACKAGES if name != "abcccid"]
# The repology projects the example's `dependencies` name (in the order pkggen looks them up)
EXAMPLE_DEPENDENCIES = ["cmake", "pkgconf", "dbus", "wayland", "xdg-desktop-portal", "utfcpp", "vulkan-headers",
                        "vulkan-loader", "glfw", "freetype", "fontconfig", "libglvnd", "libx11", "libxrandr",
                        "go", "incus", "xhost", "rust", "ibus"]

# btrustbiss comes from a fixed URL, locked to the download's hash
BTRUSTBISS_URL = "https://www.b-trust.bg/attachments/BtrustPrivateFile/146/docs/B-TrustBISS.tar"
BTRUSTBISS_HASH = "0afec10974e6910b2e422458888558dd4dee53b158125770ae3d0acbf8250ea7"


def asset_name(name, tag):
    version = tag.lstrip("v")
    return f"Circle_Linux_Mac_Driver_v{version}.zip" if name == "abcccid" else f"{name}-{version}.tar.xz"


def lock_btrustbiss(project):
    """Point the example's hash lock at the canned btrustbiss download, in a copy of the example."""
    path = project / "pkggen.yaml"
    path.write_text(path.read_text().replace(BTRUSTBISS_HASH, checksums(archive_bytes("btrustbiss"))["sha2_256"]))


def example_routes(failing=()):
    """Routes for every example package. Packages in `failing` get a 404 on their releases."""
    routes = Routes()
    for name, (user, repo, tag) in PACKAGES.items():
        api = f"https://api.github.com/repos/{user}/{repo}"
        downloads = f"https://github.com/{user}"
        if name in failing:
            routes.json(f"{api}/releases", {"message": "Not Found"}, query=RELEASES_QUERY, status=404)
            continue
        asset = asset_name(name, tag)
        releases = [
            # Newer, but skipped: drafts and pre-releases aren't accepted by default
            release(repo, "v9.0.0", [asset_name(name, "v9.0.0")], published="2026-10-01T00:00:00Z", draft=True,
                    downloads=downloads),
            release(repo, f"{tag}-rc1", [asset_name(name, tag + "-rc1")], published="2026-09-15T00:00:00Z",
                    prerelease=True, downloads=downloads),
            release(repo, tag, [asset, "checksums.txt"], downloads=downloads),
            release(repo, "v0.1.0", [asset_name(name, "v0.1.0")], published="2025-01-01T00:00:00Z",
                    downloads=downloads),
        ]
        routes.json(f"{api}/releases", releases, query=RELEASES_QUERY)
        routes.body(f"{downloads}/{repo}/releases/download/{tag}/{asset}", archive_bytes(asset))
    if "btrustbiss" not in failing:
        routes.body(BTRUSTBISS_URL, archive_bytes("btrustbiss"))
    # The packages' dependencies, as repology answered when they were recorded
    repology.routes(routes, EXAMPLE_DEPENDENCIES)
    return routes
