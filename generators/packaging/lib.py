#!/usr/bin/env python3
# Shared helpers for packaging generators.
#
# A packaging generator reads one job (JSON) on stdin:
#   {
#     "distribution": "arch",
#     "package": { ...template context... },
#     "repository": { ...repositories.<dist> from pkggen.yaml... },
#     "repository_path": "/abs/path/pkggen-build/arch",
#     "templates_path": "/abs/path/packages/arch/<folder>",   // the package's `templates`, else its name
#     "templates_root": "/abs/path/packages/arch",            // also searched by {% extends %}/{% include %}
#     "default_templates_path": "/abs/path/generators/templates/arch",
#     "run_path": "/abs/path",           // the pkggen.yaml directory, for messages
#     "releases": ["debian_13", ...],    // the rp-names the package is built for
#     "dependencies": {                  // the package's `dependencies`, see dependencies() below
#       "build" | "libraries" | "runtime" | "development": [
#         { "name": "glfw", "op": ">=", "version": "3.4",
#           "override": "<literal>" | false,          // only when pkggen.yaml sets one for this distribution
#           "local": { "name", "category", "packaged" } | null,   // one of pkggen.yaml's own packages
#           "releases": { "<rp-name>": [ <repology package> ] } } ] }
#   }
# It renders the templates into the repository and prints
#   { "output": "...", "files_written": [...], "files_unchanged": [...], "warnings": [...] }
# as JSON on stdout. Errors go to stderr, with exit code 1.
import json
import os
import re
import shutil
import sys
import traceback
from datetime import datetime, timezone
from email.utils import format_datetime

import jinja2

DEBUG = os.getenv("PKGGEN_DEBUG") == "1"
TEMPLATE_SUFFIX = ".tmpl"
IGNORED_FILES = {".gitkeep", ".DS_Store", "Thumbs.db"}
# Used when a package has no `architectures`
DEFAULT_ARCHITECTURES = ["amd64", "arm64"]


class PackagingError(Exception):
    """An expected error. Reported as a single message, without a traceback."""


class Job:
    def __init__(self, data):
        self.distribution = data["distribution"]
        self.context = dict(data["package"])
        self.repository = data.get("repository") or {}
        self.repository_path = data["repository_path"]
        self.templates_path = data["templates_path"]
        # Templates can extend or include templates of other folders through this root
        self.templates_root = data.get("templates_root") or os.path.dirname(self.templates_path)
        self.defaults_path = data["default_templates_path"]
        self.run_path = data.get("run_path") or os.getcwd()

        self.name = self.context["name"]
        self.version = str(self.context["version"])
        self.releases = list(data.get("releases") or [])
        self.dependencies = data.get("dependencies") or {}

        self.output = None  # set by the generator: where the package files went
        self.written = []
        self.unchanged = []
        self.warnings = []

        self.context.setdefault("distribution", self.distribution)
        self.context.setdefault("repository", self.repository)
        self.context.setdefault("architectures", list(DEFAULT_ARCHITECTURES))
        # Where the templates live, for "edit ... instead" comments in the output
        self.context["template_dir"] = self.display(self.templates_path).replace(os.sep, "/")
        # The package's own `<distribution>:` block, e.g. `arch: { depends: [...] }`
        dist = self.context.get(self.distribution)
        self.context["dist"] = dist if isinstance(dist, dict) else {}
        now = source_date()
        self.context["now"] = now
        self.context["date"] = parse_date(self.context.get("date")) or now

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def display(self, path):
        """Path relative to the pkggen.yaml directory, for messages."""
        try:
            rel = os.path.relpath(path, self.run_path)
        except ValueError:  # different drive on Windows
            return path
        return path if rel.startswith("..") else rel


def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


# --- Dates ---------------------------------------------------------------

def source_date():
    # Honour SOURCE_DATE_EPOCH for reproducible output
    epoch = os.getenv("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return datetime.now(timezone.utc)


def parse_date(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def filter_rfc2822(value):
    """Debian changelog date: 'Mon, 19 Oct 2026 12:00:00 +0000'."""
    return format_datetime(parse_date(value) or source_date())


def filter_rpmdate(value):
    """RPM %changelog date: 'Mon Oct 19 2026' (always English, unlike strftime)."""
    date = parse_date(value) or source_date()
    return f"{DAYS[date.weekday()]} {MONTHS[date.month - 1]} {date.day:02d} {date.year}"


# --- People and architectures --------------------------------------------

PERSON_RE = re.compile(r"^(.*?)\s*<([^<>]*)>$")


def split_person(value):
    """'Jane Doe <jane@example.com>' -> ('Jane Doe', 'jane@example.com').

    A bare address gives ('', address) and a bare name (name, '').
    """
    text = str(value).strip()
    match = PERSON_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    if "@" in text and not any(c.isspace() for c in text):
        return "", text
    return text, ""


def filter_person_name(value):
    return split_person(value)[0]


def filter_person_email(value):
    return split_person(value)[1]


def architectures(job, names, any_value, other=None):
    """The package's `architectures`, translated to this format's names.

    `architectures` is "any" or a list of pkggen names. "any" returns any_value.
    Names in `names` (e.g. {"amd64": "x86_64"}) are translated. Other names are
    passed to other(name), which returns the format's name or None to drop
    them; without `other` they're dropped. Fails if no architecture is left.
    """
    value = job.context["architectures"]
    if value == "any":
        return any_value
    if not isinstance(value, list) or not value or not all(isinstance(a, str) and a for a in value):
        raise PackagingError(f"'architectures' of {job.name} must be \"any\" or a list of names, got {value!r}")

    result = []
    for arch in value:
        native = names.get(arch) or (other(arch) if other else None)
        if native and native not in result:
            result.append(native)
    if not result:
        raise PackagingError(f"none of the architectures of {job.name} ({', '.join(value)}) can be packaged for "
                             f"{job.distribution}, which supports {', '.join(names)} or \"any\"")
    return result


# --- Dependencies ----------------------------------------------------------
#
# pkggen.yaml lists a package's dependencies once, by repology project name,
# in four roles (ROLES). pkggen looks them up on repology and passes every
# targeted release's packages of each project in the job. A generator calls
# dependencies(job, fields, pick, render), which:
#   - maps each role to the template fields it fills and the kind of package
#     it needs there: "main" (the program or library) or "dev" (its
#     development files), e.g. Debian's {"libraries": [("build_depends", "dev")]}
#   - picks one repology package per release (choose_package()), and the name
#     to use from it with pick(job, dep, package, kind) -> name or None
#   - renders the names of every release with render(job, dep, names) ->
#     a value (or a list of them) for the field, e.g. "libglfw3-dev (>= 3.4)"
# and sets job.context["deps"] = {field: [values]}. Overrides are used as they
# are, `false` drops the dependency, local packages use local_name(job, local).

ROLES = ("build", "libraries", "runtime", "development")
# Repology statuses of a release's current package (not an old branch, a live ebuild, ...)
CURRENT_STATUSES = {"newest", "devel", "unique", "outdated"}
# Packages for other targets: cross compilers, 32-bit multilib
OTHER_TARGET_RE = re.compile(r"^(mingw(32|64)?-|ucrt64-|lib32-|cross-)|-cross-")
# Binary packages that are never what a dependency means
NOISE_RE = re.compile(r"-(doc|docs|dbg|dbgsym|debuginfo|debugsource|static|tests?|src|devel-static|devel-doc)$")
DEV_SUFFIXES = ("-dev", "-devel")
VERSION_PREFIX_RE = re.compile(r"\d+(\.\d+)*")


def version_key(version):
    """A distribution version as a comparable tuple: the epoch is dropped, then the leading numbers count."""
    text = re.sub(r"^\d+:", "", str(version or ""))
    match = VERSION_PREFIX_RE.match(text)
    return tuple(int(part) for part in match.group(0).split(".")) if match else ()


def satisfies(version, op, wanted):
    """Whether `version` meets "<op> <wanted>" (by version_key; unknown versions do)."""
    have, want = version_key(version), version_key(wanted)
    if not have or not want:
        return True
    width = max(len(have), len(want))
    have, want = have + (0,) * (width - len(have)), want + (0,) * (width - len(want))
    return {">=": have >= want, "<=": have <= want, "=": have == want, ">": have > want, "<": have < want}[op]


def basename(srcname):
    """Gentoo's srcname is category/name."""
    return str(srcname or "").rsplit("/", 1)[-1]


def choose_package(project, packages):
    """The package a release means by a project: its own name, current, newest; None for none."""
    candidates = [p for p in packages
                  if not OTHER_TARGET_RE.search(basename(p.get("srcname")))
                  and not OTHER_TARGET_RE.search(p.get("binname") or "")
                  and not NOISE_RE.search(p.get("binname") or "")]
    if not candidates:
        return None

    def rank(p):
        source = basename(p.get("srcname")).lower()
        binary = (p.get("binname") or "").lower()
        return (source == project.lower(),
                not binary or binary == source,           # Arch lists each split package
                p.get("status") in CURRENT_STATUSES,
                version_key(p.get("version")),
                -len(source))                             # glfw rather than glfw-wayland
    return max(candidates, key=rank)


def binary_names(package):
    names = package.get("binnames") or ([package["binname"]] if package.get("binname") else [])
    return [n for n in names if not NOISE_RE.search(n) and not OTHER_TARGET_RE.search(n)]


def lists_binaries(package):
    """Whether repology knows the binary packages (openSUSE Leap lists each one as its own srcname)."""
    return bool(package.get("binnames") or package.get("binname"))


def main_package(package, project):
    """The program or library itself among a source package's binary packages."""
    source = basename(package.get("srcname"))
    if not lists_binaries(package):
        return source or None
    names = [n for n in binary_names(package) if not n.endswith(DEV_SUFFIXES)]
    if not names:
        return None
    for wanted in (source, project, f"lib{source}", f"lib{project}"):
        if wanted in names:
            return wanted
    return min(names, key=lambda n: (len(n), n))


def dev_package(package, project, suffix):
    """The development files' package (…-dev, …-devel, lib64…-devel), else the main package.

    None when repology doesn't list the binary packages (see guess_dev_package()).
    """
    if not lists_binaries(package):
        return None
    names = [n for n in binary_names(package) if n.endswith(suffix)]
    source = basename(package.get("srcname"))
    for stem in (source, project):
        for wanted in (f"{stem}{suffix}", f"lib{stem}{suffix}", f"lib64{stem}{suffix}"):
            if wanted in names:
                return wanted
    if names:
        return min(names, key=lambda n: (not n.startswith("lib"), len(n), n))
    return main_package(package, project)


def describe(dep):
    return dep["name"] + (f" {dep['op']} {dep['version']}" if dep.get("op") else "")


def local_name(job, local):
    """The name of one of pkggen.yaml's own packages here (Gentoo overrides it with the category)."""
    return local["name"]


def dependencies(job, fields, pick, render, local=local_name):
    """Fill job.context["deps"]: {field: [rendered dependencies]} (see the comment above)."""
    deps = {field: [] for kinds in fields.values() for field, _ in kinds}
    missing = []
    for role in ROLES:
        for dep in job.dependencies.get(role) or []:
            for field, kind in fields.get(role, ()):
                if "override" in dep:
                    values = [dep["override"]] if dep["override"] is not False else []
                elif dep.get("local"):
                    if not dep["local"].get("packaged"):
                        job.warn(f"the dependency '{dep['name']}' is one of pkggen.yaml's packages, but it "
                                 f"isn't packaged for {job.distribution}")
                    name = local(job, dep["local"])
                    values = render(job, dep, {rp: name for rp in job.releases} or {job.distribution: name})
                else:
                    names = {}
                    for rp in job.releases:
                        package = choose_package(dep["name"], (dep.get("releases") or {}).get(rp) or [])
                        name = pick(job, dep, package, kind) if package else None
                        if name:
                            names[rp] = name
                            if dep.get("op") and package.get("status") != "rolling" \
                                    and not satisfies(package.get("version"), dep["op"], dep["version"]):
                                job.warn(f"{rp} has {dep['name']} {package.get('version')}, but {job.name} "
                                         f"needs {describe(dep)}")
                    if not names:
                        missing.append(dep["name"])
                        continue
                    absent = [rp for rp in job.releases if rp not in names]
                    if absent:
                        job.warn(f"{dep['name']} isn't packaged in {', '.join(absent)} (repology)")
                    values = render(job, dep, names)
                for value in values if isinstance(values, list) else [values]:
                    if value not in deps[field]:
                        deps[field].append(value)
    if missing:
        names = ", ".join(dict.fromkeys(missing))
        raise PackagingError(
            f"repology has no package for {names} in {', '.join(job.releases) or job.distribution}. Check the "
            f"repology project name with `pkggen repology <name>`, or give the dependency its "
            f"{job.distribution} name in pkggen.yaml: {{ name: <project>, {job.distribution}: \"<name>\" }} "
            f"(or false to leave it out there)")
    job.context["deps"] = deps
    return deps


def releases_in_order(job, names):
    """The distinct names, in the order of the releases."""
    return list(dict.fromkeys(names[rp] for rp in job.releases if rp in names)) or list(dict.fromkeys(names.values()))


# --- Licenses and versions -------------------------------------------------

# SPDX id (including the deprecated ids GitHub still returns) -> Debian DEP-5 short name
DEBIAN_LICENSES = {
    "MIT": "Expat",
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
    "LGPL-2.0": "LGPL-2", "LGPL-2.0-only": "LGPL-2", "LGPL-2.0-or-later": "LGPL-2+",
    "LGPL-2.1": "LGPL-2.1", "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1+",
    "LGPL-3.0": "LGPL-3", "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3+",
    "AGPL-3.0": "AGPL-3", "AGPL-3.0-only": "AGPL-3", "AGPL-3.0-or-later": "AGPL-3+",
    "BSD-2-Clause": "BSD-2-clause", "BSD-3-Clause": "BSD-3-clause",
}

# SPDX id -> Gentoo license file name
GENTOO_LICENSES = {
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
    "LGPL-2.0": "LGPL-2", "LGPL-2.0-only": "LGPL-2", "LGPL-2.0-or-later": "LGPL-2+",
    "LGPL-2.1": "LGPL-2.1", "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1+",
    "LGPL-3.0": "LGPL-3", "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3+",
    "AGPL-3.0": "AGPL-3", "AGPL-3.0-only": "AGPL-3", "AGPL-3.0-or-later": "AGPL-3+",
    "BSD-2-Clause": "BSD-2", "BSD-3-Clause": "BSD",
    "Zlib": "ZLIB", "BSL-1.0": "Boost-1.0",
}

# GitHub reports deprecated SPDX ids; RPM and Arch want the current ones
SPDX_UPDATES = {
    "GPL-2.0": "GPL-2.0-only", "GPL-3.0": "GPL-3.0-only",
    "LGPL-2.1": "LGPL-2.1-only", "LGPL-3.0": "LGPL-3.0-only", "AGPL-3.0": "AGPL-3.0-only",
}


def filter_debian_license(value):
    return DEBIAN_LICENSES.get(str(value), str(value))


def filter_gentoo_license(value):
    return GENTOO_LICENSES.get(str(value), str(value))


def filter_spdx(value):
    return SPDX_UPDATES.get(str(value), str(value))


def filter_shquote(value):
    """Quote for bash with single quotes: it's -> 'it'\\''s'."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def filter_dquote(value):
    """Quote for bash with double quotes, escaping \\ " $ and `."""
    return '"' + re.sub(r'([\\"$`])', r"\\\1", str(value)) + '"'


def filter_pkgver(value):
    """Arch/RPM versions can't contain '-', ':', '/' or whitespace."""
    return re.sub(r"[-:/\s]", "_", str(value))


FILTERS = {
    "rfc2822": filter_rfc2822,
    "rpmdate": filter_rpmdate,
    "debian_license": filter_debian_license,
    "gentoo_license": filter_gentoo_license,
    "spdx": filter_spdx,
    "pkgver": filter_pkgver,
    "shquote": filter_shquote,
    "dquote": filter_dquote,
    "person_name": filter_person_name,
    "person_email": filter_person_email,
}


# --- Templates -------------------------------------------------------------

def iter_files(root):
    """Yield paths (relative, '/'-separated) of every file under root, sorted."""
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if name in IGNORED_FILES or name.endswith(("~", ".swp")) or name.startswith(".#"):
                continue
            yield os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/")


def copy_atomic(src, dst):
    """Copy src to dst through a temporary file, so a reader never sees half a file.

    Several packages can share a template folder, and their packaging
    generators run at the same time.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.pkggen-{os.getpid()}.tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def copy_default(job, rel):
    copy_atomic(os.path.join(job.defaults_path, rel), os.path.join(job.templates_path, rel))


def create_default_folder(job):
    """Create the missing template folder with every default template in one step.

    The defaults are copied into a temporary sibling folder that's renamed into
    place, so a generator running at the same time for another package of a
    shared folder either sees no folder or the full set. Returns False when
    another generator created the folder first.
    """
    tmp = f"{job.templates_path}.pkggen-{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(job.defaults_path, tmp)
    os.makedirs(os.path.dirname(job.templates_path), exist_ok=True)
    try:
        os.rename(tmp, job.templates_path)
        return True
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.isdir(job.templates_path):
            return False
        raise


def ensure_templates(job, required):
    """Make sure the package's template folder exists and has the required templates.

    A missing or template-less folder gets the full default set. A folder that
    lacks some required templates only gets those. Existing files are never
    overwritten. Both cases are reported as warnings.
    """
    if not os.path.isdir(job.defaults_path):
        raise PackagingError(f"default templates for {job.distribution} not found at {job.defaults_path}")

    shown = job.display(job.templates_path)
    if not os.path.exists(job.templates_path) and create_default_folder(job):
        job.warn(f"no template data found for {job.name}, so default templates were created in {shown}. "
                 "Edit them to customise the package")
        return

    has_templates = os.path.isdir(job.templates_path) and any(
        rel.endswith(TEMPLATE_SUFFIX) for rel in iter_files(job.templates_path))

    if not has_templates:
        for rel in iter_files(job.defaults_path):
            if not os.path.exists(os.path.join(job.templates_path, rel)):
                copy_default(job, rel)
        job.warn(f"no template data found for {job.name}, so default templates were created in {shown}. "
                 "Edit them to customise the package")
        return

    missing = [rel for rel in required if not os.path.isfile(os.path.join(job.templates_path, rel))]
    for rel in missing:
        if not os.path.isfile(os.path.join(job.defaults_path, rel)):
            raise PackagingError(f"{shown} is missing {rel}, and there's no default for it in {job.defaults_path}")
        copy_default(job, rel)
    if missing:
        job.warn(f"{shown} was missing {', '.join(missing)}, so default versions were created. "
                 "Edit them to customise the package")


def make_environment(job):
    env = jinja2.Environment(
        # The package's folder first, then the root, so `{% extends "shared/PKGBUILD.tmpl" %}` works
        loader=jinja2.FileSystemLoader([job.templates_path, job.templates_root]),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    env.filters.update(FILTERS)
    return env


def template_location(job, error, path):
    """'<template>:<line>' of the innermost template frame an exception was raised from.

    Uses Jinja's rewritten traceback, so it also finds lines in templates
    that `path` extends or includes. Falls back to path itself.
    """
    roots = [os.path.abspath(job.templates_path), os.path.abspath(job.templates_root)]
    found = None
    for frame in traceback.extract_tb(error.__traceback__):
        filename = os.path.abspath(frame.filename)
        if any(filename.startswith(root + os.sep) for root in roots):
            found = (filename, frame.lineno)
    if not found:
        return job.display(path)
    return f"{job.display(found[0])}:{found[1]}"


def render(job, env, rel):
    path = os.path.join(job.templates_path, rel)
    shown = job.display(path)
    try:
        return env.get_template(rel).render(job.context)
    except jinja2.TemplateSyntaxError as e:
        where = job.display(e.filename) if e.filename else shown
        raise PackagingError(f"{where}:{e.lineno}: syntax error: {e.message}")
    except jinja2.TemplateNotFound as e:
        raise PackagingError(f"{shown}: template {e.name!r} not found in {job.display(job.templates_path)} "
                             f"or {job.display(job.templates_root)}")
    except jinja2.UndefinedError as e:
        where = template_location(job, e, path)
        raise PackagingError(f"{where}: {e.message}. Define it for {job.name} in pkggen.yaml, "
                             "or guard it with '| default(...)' in the template")
    except Exception as e:
        where = template_location(job, e, path)
        message = e.message if isinstance(e, jinja2.TemplateError) and e.message else str(e)
        raise PackagingError(f"{where}: {type(e).__name__}: {message}")


def write_output(job, path, content, executable=False):
    """Write content (str or bytes) to path, only touching the file if it changed.

    Text is always written with LF line endings, regardless of the host OS.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    os.makedirs(os.path.dirname(path), exist_ok=True)

    old = None
    if os.path.isfile(path):
        with open(path, "rb") as f:
            old = f.read()

    if old == data:
        job.unchanged.append(job.display(path))
    else:
        with open(path, "wb") as f:
            f.write(data)
        job.written.append(job.display(path))

    if executable:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o111)


def render_tree(job, place, executable=()):
    """Render every template in the package's folder into the repository.

    *.tmpl files are rendered with Jinja2 and lose the suffix. Other files are
    copied unchanged. place(rel) maps a relative output name to its absolute
    destination. Outputs named in `executable` (or whose template is
    executable) get the executable bit.
    """
    env = make_environment(job)
    for rel in iter_files(job.templates_path):
        src = os.path.join(job.templates_path, rel)
        if rel.endswith(TEMPLATE_SUFFIX):
            out_rel = rel[:-len(TEMPLATE_SUFFIX)]
            content = render(job, env, rel)
        else:
            out_rel = rel
            with open(src, "rb") as f:
                content = f.read()

        exec_bit = out_rel in executable or bool(os.stat(src).st_mode & 0o111)
        write_output(job, place(out_rel), content, executable=exec_bit)


# --- Entry point -----------------------------------------------------------

def run(main):
    """Read the job from stdin, call main(job) and print the result as JSON."""
    try:
        data = json.loads(sys.stdin.buffer.read())
        job = Job(data)
        main(job)
    except Exception as e:
        if DEBUG:
            traceback.print_exc()
        if isinstance(e, (PackagingError, OSError)):
            message = str(e)
        elif isinstance(e, (json.JSONDecodeError, KeyError)):
            message = f"invalid packaging job: {type(e).__name__}: {e}"
        else:
            message = f"unexpected {type(e).__name__}: {e} (set PKGGEN_DEBUG=1 for a traceback)"
        print(f"\x1b[31mError: {message}\x1b[0m", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(json.dumps({
        "output": job.display(job.output) if job.output else None,
        "files_written": job.written,
        "files_unchanged": job.unchanged,
        "warnings": job.warnings,
    }) + "\n")
