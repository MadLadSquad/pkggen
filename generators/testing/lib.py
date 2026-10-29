#!/usr/bin/env python3
# Shared helpers for testing generators.
#
# A testing generator describes the containers that test one distribution's
# packages; pkggen starts them and runs the steps. It reads one job (JSON) on stdin:
#   {
#     "distribution": "debian",
#     "repository": { ...repositories.<dist> from pkggen.yaml... },
#     "test": { "setup": [steps], "post-setup": [steps] },   // the user's steps, global then per distribution
#     "rp_names": [ ...rp-names of the distribution in distributions.yaml... ],
#     "versions": null,                // or the rp-names/target names to test (test.distributions.<dist>.versions)
#     "host_arch": "amd64",            // architecture of the container host
#     "packages": [ { "context": { ...template context... }, "output": "packages/foo" } ]
#   }
# `output` is where the packaging generator put the package, relative to the
# repository. `packages` is empty when pkggen only asks for the targets.
#
# It prints { "targets": [target, ...], "skipped": [...], "warnings": [...] } on stdout,
# `skipped` being the { "name", "rp_name" } of the releases that can't run on
# this host (no image for its architecture). A target is:
#   {
#     "name": "debian-13", "rp_name": "debian_13", "image": "docker.io/library/debian:trixie",
#     "platform": null,                          // e.g. "linux/amd64" to force one
#     "data_images": [ { "image": "...", "path": "/var/db/repos/gentoo" } ],  // mounted with --volumes-from
#     "env": { "DEBIAN_FRONTEND": "noninteractive",   // ${PKGGEN_JOBS} becomes the job count pkggen picked
#              "DEB_BUILD_OPTIONS": "parallel=${PKGGEN_JOBS}" },   // (also set as PKGGEN_JOBS and MAKEFLAGS)
#     "repo": "/pkggen/repo",                    // where pkggen copies the repository in
#     "setup":    [steps],                       // before the repository is copied in
#     "prepare":  [steps],                       // after it's copied in
#     "packages": [ { "name": "foo", "build": [steps], "install": [steps] } ],
#     "finalize": [steps],                       // index the finished repository into `collect`
#     "post_setup": [steps],
#     "collect": "/pkggen/out",                  // copied out to pkggen-build/test/<name>/
#     "publish": {                               // how deployment generators upload what's collected:
#       "indexes": [regex],                      //   files that list the packages (uploaded after them)
#       "metadata": [regex],                     //   files clients read first, e.g. signed ones (uploaded last)
#       "content_types": { ".deb": "..." }       //   media types by file name suffix
#     }
#   }
# The `publish` regexes are searched in paths relative to `collect`; pkggen
# records them in results.json for `pkggen deploy`.
# A step is { "name": "...", "run": "<sh script>", "user": null, "workdir": null }.
# Each step runs in its own `sh -exc`, so `cd` and variables don't carry over.
import importlib.util
import json
import os
import re
import sys
import traceback

# generators/common.py: the helpers every layer shares (shquote, to_bool, secrets_path, ...), as lib.<name>.
# Loaded by path once per process, under a name no other module has
if "pkggen_common" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "pkggen_common", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "common.py"))
    sys.modules["pkggen_common"] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(sys.modules["pkggen_common"])
from pkggen_common import *  # noqa: E402,F401,F403

DEBUG = os.getenv("PKGGEN_DEBUG") == "1"

# Paths inside the containers
# In a target's `env`, replaced by the job count pkggen picked for each container
JOBS = "${PKGGEN_JOBS}"
REPO = "/pkggen/repo"
OUT = "/pkggen/out"
WORK = "/pkggen/work"
STATE = "/pkggen/state"


class TestingError(Exception):
    """An expected error. Reported as a single message, without a traceback."""


class Package:
    def __init__(self, entry):
        self.context = dict(entry["context"])
        self.output = (entry.get("output") or "").replace("\\", "/").strip("/")
        self.name = self.context["name"]
        self.version = str(self.context["version"])
        self.artifacts = self.context.get("artifacts") or []
        self.source = self.context.get("source") or (self.artifacts[0] if self.artifacts else {})

    def block(self, distribution):
        """The package's own `<distribution>:` block."""
        value = self.context.get(distribution)
        return value if isinstance(value, dict) else {}

    def source_dir(self):
        return self.context.get("source_dir") or self.source.get("directory") or ""


class Job:
    def __init__(self, data):
        self.distribution = data["distribution"]
        self.repository = data.get("repository") or {}
        self.test = data.get("test") or {}
        self.rp_names = list(data.get("rp_names") or [])
        self.versions = data.get("versions")
        self.host_arch = data.get("host_arch") or "amd64"
        self.warnings = []
        self.skipped = []
        self.packages = []
        for entry in data.get("packages") or []:
            package = Package(entry)
            if builds_on(package, self.host_arch):
                self.packages.append(package)
            else:
                self.warn(f"{package.name} isn't built for {self.host_arch} (see its 'architectures'), "
                          f"so it's not tested on {self.distribution}")

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)


def builds_on(package, arch):
    value = package.context.get("architectures", ["amd64", "arm64"])
    return value == "any" or not isinstance(value, list) or arch in value


# --- Steps -------------------------------------------------------------------

def step(name, run, user=None, workdir=None):
    return {"name": name, "run": run.strip("\n") + "\n", "user": user, "workdir": workdir}


def user_steps(job, key):
    """The user's `setup` or `post-setup` steps, already normalised by pkggen."""
    return [step(s.get("name") or "user step", s["run"], s.get("user"), s.get("workdir"))
            for s in job.test.get(key) or []]


def heredoc(path, content):
    """Shell code that writes content to path, without expanding anything in it."""
    marker = "PKGGEN_EOF"
    while marker in content:
        marker += "_"
    return f"cat > {shquote(path)} <<'{marker}'\n{content.rstrip(chr(10))}\n{marker}\n"


def download(artifact, directory, name=None):
    """Shell code that downloads an artifact into directory and checks its SHA-256."""
    name = name or artifact["alias"]
    path = f"{directory}/{name}"
    code = f"curl -fsSL --retry 3 -o {shquote(path)} {shquote(artifact['url'])}\n"
    sha256 = (artifact.get("checksums") or {}).get("sha2_256")
    if sha256:
        code += f"echo {shquote(sha256 + '  ' + path)} | sha256sum -c -\n"
    return code


TARBALL_RE = re.compile(r"\.(tar\.(gz|xz|bz2|zst|lz|lzma)|tgz|tbz2|txz)$")


def tarball_extension(name):
    """'foo-1.0.tar.xz' -> 'tar.xz', None for files that aren't tarballs."""
    match = TARBALL_RE.search(name)
    if not match:
        return None
    ext = match.group(1)
    return {"tgz": "tar.gz", "tbz2": "tar.bz2", "txz": "tar.xz"}.get(ext, ext)


def collect_step():
    """The last finalize step: copy the finished repository to OUT, following symlinks."""
    return step("collect the repository", f"rm -rf {OUT}\ncp -rL {REPO} {OUT}\n")


# --- Targets -----------------------------------------------------------------

def table_specs(table):
    """[(rp-name, spec)] of a {rp-name: spec or [specs]} table, in its order.

    A list gives one rp-name several targets (Void's glibc and musl builds are
    both repology's void_x86_64; distributions.yaml's `targets` names them).
    """
    return [(rp, spec) for rp, value in table.items() for spec in (value if isinstance(value, list) else [value])]


def select_targets(job, table):
    """The (rp-name, spec) pairs to test, from a {rp-name: spec or [specs]} table.

    Every spec has a `name` (the target name), an `image` and the host
    architectures it runs on (`arches`). Without `versions`, every rp-name
    of the distribution is tested. An rp-name in `versions` selects all of
    its targets, a target name only that one.
    """
    specs = table_specs(table)
    if job.versions is None:
        wanted = []
        for rp in job.rp_names:
            if rp in table:
                wanted += [entry for entry in specs if entry[0] == rp]
            else:
                job.warn(f"{job.distribution} has no container image for {rp}, so it's not tested")
    else:
        wanted, unknown = [], []
        for version in job.versions:
            found = [entry for entry in specs if version in (entry[0], entry[1]["name"])]
            if not found:
                unknown.append(str(version))
            wanted += [entry for entry in found if entry not in wanted]
        if unknown:
            # The rp-names, and the target names of those with several targets
            available = ", ".join(list(table) + [spec["name"] for rp, value in table.items()
                                                 if isinstance(value, list) for spec in value])
            raise TestingError(f"unknown {job.distribution} version(s) in 'test.distributions.{job.distribution}"
                               f".versions': {', '.join(unknown)}. Available: {available}")

    result = []
    for rp, spec in wanted:
        if job.host_arch not in spec.get("arches", ["amd64", "arm64"]):
            job.warn(f"{spec['name']} has no {job.host_arch} container image, so it's skipped")
            job.skipped.append({"name": spec["name"], "rp_name": rp})
            continue
        result.append((rp, spec))
    return result


def target(job, rp_name, spec, *, setup, prepare, packages, finalize, env=None, data_images=None, publish=None):
    return {
        "name": spec["name"],
        "rp_name": rp_name,
        "image": spec["image"],
        "platform": spec.get("platform"),
        "data_images": data_images or [],
        "env": env or {},
        "repo": REPO,
        "setup": setup + user_steps(job, "setup"),
        "prepare": [step("create the work folders", f"mkdir -p {WORK} {STATE}\nchmod 777 {WORK} {STATE}\n")] + prepare,
        "packages": packages,
        "finalize": finalize,
        "post_setup": user_steps(job, "post-setup"),
        "collect": OUT,
        "publish": publish or {},
    }


# --- Entry point -------------------------------------------------------------

def run(main):
    """Read the job from stdin, call main(job), which returns the targets, and print them as JSON."""
    try:
        data = json.loads(sys.stdin.buffer.read())
        job = Job(data)
        targets = main(job)
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        if isinstance(e, (TestingError, OSError)):
            message = str(e)
        elif isinstance(e, (json.JSONDecodeError, KeyError)):
            message = f"invalid testing job: {type(e).__name__}: {e}"
        else:
            message = f"unexpected {type(e).__name__}: {e} (set PKGGEN_DEBUG=1 for a traceback)"
        print(f"\x1b[31mError: {message}\x1b[0m", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(json.dumps({"targets": targets, "skipped": job.skipped, "warnings": job.warnings}) + "\n")
