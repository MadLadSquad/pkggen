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

# Manifest hash name -> checksum key from the fetch generator
MANIFEST_HASHES = {
    "BLAKE2B": "blake2b", "BLAKE2S": "blake2s",
    "SHA256": "sha2_256", "SHA512": "sha2_512",
    "SHA3_256": "sha3_256", "SHA3_512": "sha3_512",
    "SHA1": "sha1", "MD5": "md5",
}
DEFAULT_MANIFEST_HASHES = ["BLAKE2B", "SHA512"]


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(v) for v in value]


def get_category(job):
    categories = as_list(job.repository.get("categories"))
    gentoo = job.context.get("gentoo") if isinstance(job.context.get("gentoo"), dict) else {}
    category = gentoo.get("category") or job.context.get("category")

    if not category:
        if not categories:
            raise lib.PackagingError(f"{job.name} has no 'category' and 'repositories.gentoo.categories' is empty. "
                                     "Set 'category' for the package in pkggen.yaml")
        category = categories[0]
        job.warn(f"{job.name} has no 'category', so it went into '{category}'. Set 'category' in pkggen.yaml to choose one")
    elif categories and category not in categories:
        job.warn(f"category '{category}' isn't listed in 'repositories.gentoo.categories', "
                 "so Portage won't see it until it's added to profiles/categories")

    if not CATEGORY_RE.match(category):
        raise lib.PackagingError(f"'{category}' isn't a valid Gentoo category name")
    return category


def write_manifest(job, out):
    names = [name.upper() for name in as_list(job.repository.get("manifest-hashes"))] or DEFAULT_MANIFEST_HASHES
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


def main(job):
    if not VERSION_RE.match(job.version):
        raise lib.PackagingError(f"version '{job.version}' of {job.name} isn't a valid Gentoo version. "
                                 "Adjust the fetch generator's 'transforms' so it produces one (e.g. 1.2.3, 1.2.3_rc1)")

    lib.ensure_templates(job, REQUIRED)
    out = os.path.join(job.repository_path, get_category(job), job.name)

    def place(rel):
        if rel == "ebuild":
            return os.path.join(out, f"{job.name}-{job.version}.ebuild")
        return os.path.join(out, rel)

    job.output = out
    lib.render_tree(job, place)
    write_manifest(job, out)


if __name__ == "__main__":
    lib.run(main)
