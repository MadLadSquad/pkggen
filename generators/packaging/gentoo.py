#!/usr/bin/env python3
# Renders packages/gentoo/<name>/ into <repo>/<category>/<name>/:
#   ebuild.tmpl      -> <name>-<version>.ebuild
#   everything else  -> same name (metadata.xml, files/...)
# and writes the Manifest DIST entries from the fetched artifacts.
import os
import re
import lib

REQUIRED = ["ebuild.tmpl"]

# Gentoo version syntax (PMS 3.2)
VERSION_RE = re.compile(r"^\d+(\.\d+)*[a-z]?((_alpha|_beta|_pre|_rc|_p)\d*)*(-r\d+)?$")
CATEGORY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9+_.-]*$")
KEYWORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Gentoo uses pkggen's names, plus many niche ones (ppc64, riscv, ...) that are
# passed through as they are. "any" means every main Gentoo architecture.
ARCHITECTURES = {"amd64": "amd64", "arm64": "arm64"}
ALL_ARCHITECTURES = ["alpha", "amd64", "arm", "arm64", "hppa", "loong", "m68k", "mips",
                     "ppc", "ppc64", "riscv", "s390", "sparc", "x86"]

# Manifest hash name -> checksum key from the fetch generator
MANIFEST_HASHES = {
    "BLAKE2B": "blake2b", "BLAKE2S": "blake2s",
    "SHA256": "sha2_256", "SHA512": "sha2_512",
    "SHA3_256": "sha3_256", "SHA3_512": "sha3_512",
    "SHA1": "sha1", "MD5": "md5",
}
DEFAULT_MANIFEST_HASHES = ["BLAKE2B", "SHA512"]


def package_category(package):
    """The category a package sets in pkggen.yaml (`gentoo.category`, else `category`), or None."""
    gentoo = package.get("gentoo") if isinstance(package.get("gentoo"), dict) else {}
    return gentoo.get("category") or package.get("category")


def get_category(job):
    categories = lib.words(job.repository.get("categories"))
    category = package_category(job.context)

    if not category:
        if not categories:
            raise lib.PackagingError(f"{job.name} has no 'category' and 'repositories.gentoo.categories' is empty. "
                                     "Set 'category' for the package in pkggen.yaml")
        category = categories[0]
        job.warn(f"{job.name} has no 'category', so it went into '{category}'. Set 'category' in pkggen.yaml to choose one")
    elif categories and category not in categories and not lib.words(job.repository.get("masters")):
        # With masters (e.g. gentoo), the overlay also has the master repositories' categories
        job.warn(f"category '{category}' isn't listed in 'repositories.gentoo.categories', "
                 "so Portage won't see it until it's added to profiles/categories")

    if not CATEGORY_RE.match(category):
        raise lib.PackagingError(f"'{category}' isn't a valid Gentoo category name")
    return category


def keyword(job, arch):
    if KEYWORD_RE.match(arch):
        return arch
    job.warn(f"'{arch}' in 'architectures' of {job.name} isn't a valid Gentoo keyword, so it was left out. "
             "Write the arch name without a leading '~'")
    return None


def write_manifest(job, out):
    names = [name.upper() for name in lib.words(job.repository.get("manifest-hashes"))] or DEFAULT_MANIFEST_HASHES
    unknown = [name for name in names if name not in MANIFEST_HASHES]
    if unknown:
        raise lib.PackagingError(f"unsupported manifest hash {', '.join(unknown)} in 'repositories.gentoo.manifest-hashes'. "
                                 f"Supported: {', '.join(MANIFEST_HASHES)}")
    if not lib.to_bool(job.repository.get("thin-manifests"), True):
        job.warn("'thin-manifests' is off, but pkggen only writes DIST entries. "
                 "Run 'pkgdev manifest' before publishing")

    path = os.path.join(out, "Manifest")

    # Keep DIST entries of other distfiles, so older ebuilds stay valid
    entries = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "DIST":
                    entries[parts[1]] = line.strip()

    for artifact in job.context.get("artifacts") or []:
        checksums = artifact["checksums"]
        hashes = " ".join(f"{name} {checksums[MANIFEST_HASHES[name]]}" for name in names)
        entries[artifact["alias"]] = f"DIST {artifact['alias']} {artifact['size']} {hashes}"

    if entries:
        lib.write_output(job, path, "\n".join(entries[name] for name in sorted(entries)) + "\n")


# Gentoo has no separate development packages
DEPENDENCY_FIELDS = {
    "build": [("bdepend", "main")],
    "libraries": [("depend", "main")],
    "runtime": [("rdepend", "main")],
    "development": [("depend", "main")],
}
# "=" means any revision of the version
OPERATORS = {">=": ">=", "<=": "<=", "=": "~", ">": ">", "<": "<"}


def source_name(package):
    """Repology's srcname is the atom, category/name: the name is what's compared with the project."""
    return lib.srcname(package).rsplit("/", 1)[-1]
# Packages for other targets: the MinGW toolchain (dev-util/mingw64-toolchain). Cross compilers live in
# cross-* categories, which crossdev creates locally, so repology doesn't list them
OTHER_TARGETS = re.compile(r"^mingw(32|64)?-")


def pick(job, dep, package, kind):
    srcname = package.get("srcname") or ""
    return srcname if "/" in srcname else None


def render(job, dep, names):
    """>=media-libs/glfw-3.4"""
    atom = lib.releases_in_order(job, names)[0]
    if not dep.get("op"):
        return atom
    version = str(dep["version"])
    if not VERSION_RE.match(version):
        raise lib.PackagingError(f"the version of the dependency '{lib.describe(dep)}' isn't a Gentoo version. "
                                 f"Give it its Gentoo atom in pkggen.yaml: {{ name: {dep['name']}, gentoo: \"...\" }}")
    return f"{OPERATORS[dep['op']]}{atom}-{version}"


def local_atom(job, local):
    """category/name of one of pkggen.yaml's own packages."""
    category = (package_category(local.get("package") or {})
                or next(iter(lib.words(job.repository.get("categories"))), None))
    if not category:
        raise lib.PackagingError(f"the dependency '{local['name']}' has no 'category' and "
                                 "'repositories.gentoo.categories' is empty. Set 'category' for it in pkggen.yaml")
    return f"{category}/{local['name']}"


# SPDX id -> Gentoo license file name
LICENSES = {
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
    "LGPL-2.0": "LGPL-2", "LGPL-2.0-only": "LGPL-2", "LGPL-2.0-or-later": "LGPL-2+",
    "LGPL-2.1": "LGPL-2.1", "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1+",
    "LGPL-3.0": "LGPL-3", "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3+",
    "AGPL-3.0": "AGPL-3", "AGPL-3.0-only": "AGPL-3", "AGPL-3.0-or-later": "AGPL-3+",
    "BSD-2-Clause": "BSD-2", "BSD-3-Clause": "BSD",
    "Zlib": "ZLIB", "BSL-1.0": "Boost-1.0",
}


def gentoo_license(value):
    return LICENSES.get(str(value), str(value))


FILTERS = {"gentoo_license": gentoo_license}


def main(job):
    if not VERSION_RE.match(job.version):
        raise lib.PackagingError(f"version '{job.version}' of {job.name} isn't a valid Gentoo version. "
                                 "Adjust the fetch generator's 'transforms' so it produces one (e.g. 1.2.3, 1.2.3_rc1)")

    job.context["arches"] = lib.architectures(job, ARCHITECTURES, ALL_ARCHITECTURES,
                                              other=lambda arch: keyword(job, arch))
    lib.dependencies(job, DEPENDENCY_FIELDS, pick, render, local=local_atom, other_targets=OTHER_TARGETS,
                     source_name=source_name)
    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, get_category(job), job.name)

    def place(rel):
        if rel == "ebuild":
            return os.path.join(out, f"{job.name}-{job.version}.ebuild")
        return os.path.join(out, rel)

    job.output = out
    lib.render_tree(job, place, filters=FILTERS)
    write_manifest(job, out)


if __name__ == "__main__":
    lib.run(main)
