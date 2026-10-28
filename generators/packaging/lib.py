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
#           // one of pkggen.yaml's own packages, with its pkggen.yaml settings
#           "local": { "name", "packaged", "package": { ... } } | null,
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


def date_of(value):
    """A template value as a datetime: a date, an ISO 8601 string, else now (source_date()).

    For the generators' own date filters (Debian's rfc2822, RPM's rpmdate).
    """
    return parse_date(value) or source_date()


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
#   - prepares each release's repology packages with the generator's own
#     function, if any (`prepare`, e.g. merge_split_packages())
#   - leaves out the packages a dependency never means, by the generator's
#     rules (drop_unwanted(): `noise`, `other_targets`)
#   - picks one repology package per release (choose_package()), and the name
#     to use from it with pick(job, dep, package, kind) -> name or None
#   - renders the names of every release with render(job, dep, names) ->
#     a value (or a list of them) for the field, e.g. "libglfw3-dev (>= 3.4)"
# and sets job.context["deps"] = {field: [values]}. Overrides are used as they
# are, `false` drops the dependency, local packages use local_name(job, local).
# Repology's `srcname` is compared with the project's name as it is, unless
# the generator passes `source_name` (Gentoo's srcname is category/name).

ROLES = ("build", "libraries", "runtime", "development")
# Repology statuses of a release's current package (not an old branch, a live ebuild, ...)
CURRENT_STATUSES = {"newest", "devel", "unique", "outdated"}
# Library packages are often named lib<name>; generators add their own prefixes
LIBRARY_PREFIXES = ("lib",)
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


def srcname(package):
    """A repology package's source name, as repology lists it (the default `source_name`)."""
    return str(package.get("srcname") or "")


def choose_package(project, packages, source_name=srcname):
    """The package a release means by a project: its own name, current, newest; None for none.

    The packages should have been through drop_unwanted() first. source_name(package)
    is the name compared with the project's.
    """
    if not packages:
        return None

    def rank(p):
        source = source_name(p).lower()
        binary = (p.get("binname") or "").lower()
        return (source == project.lower(),
                not binary or binary == source,           # an entry per binary package: the source's own
                p.get("status") in CURRENT_STATUSES,
                version_key(p.get("version")),
                -len(source))                             # glfw rather than glfw-wayland
    return max(packages, key=rank)


def binary_names(package):
    return package.get("binnames") or ([package["binname"]] if package.get("binname") else [])


def drop_unwanted(packages, noise=(), other_targets=None, source_name=srcname):
    """The packages without the ones a dependency never means, by the generator's rules.

    `noise` is a tuple of binary package name suffixes (-doc, -dbgsym, ...).
    `other_targets` is a regex (searched) or a function name -> bool for the
    packages of other targets (cross compilers, 32-bit multilib, ...); it's
    checked against the source name (source_name(package)) too. A package whose source or binname is
    unwanted, or whose binnames all are, is dropped; the others lose the
    unwanted binnames.
    """
    if other_targets is not None and not callable(other_targets):
        other_targets = other_targets.search

    def unwanted(name, source=False):
        if not name:
            return False
        return bool((not source and noise and name.endswith(tuple(noise)))
                    or (other_targets and other_targets(name)))

    result = []
    for package in packages:
        if unwanted(source_name(package), source=True) or unwanted(package.get("binname")):
            continue
        if package.get("binnames"):
            kept = [n for n in package["binnames"] if not unwanted(n)]
            if not kept:
                continue
            package = {**package, "binnames": kept}
        result.append(package)
    return result


def lists_binaries(package):
    """Whether repology lists the package's binary packages (some repositories only have source names)."""
    return bool(package.get("binnames") or package.get("binname"))


def main_package(package, project, dev_suffix, prefixes=LIBRARY_PREFIXES):
    """The program or library itself among a source package's binary packages.

    `dev_suffix` is the format's suffix of development packages (-dev, -devel),
    which are never the main package; `prefixes` the prefixes of its library
    packages (lib<name>). Without binary packages, the source name.
    """
    source = srcname(package)
    if not lists_binaries(package):
        return source or None
    names = [n for n in binary_names(package) if not n.endswith(dev_suffix)]
    if not names:
        return None
    for prefix in ("",) + tuple(prefixes):
        for stem in (source, project):
            if f"{prefix}{stem}" in names:
                return f"{prefix}{stem}"
    return min(names, key=lambda n: (len(n), n))


def dev_package(package, project, dev_suffix, prefixes=LIBRARY_PREFIXES):
    """The development files' package (<name><dev_suffix>, <prefix><name><dev_suffix>), else the main package.

    None when repology doesn't list the binary packages: the generator decides
    then (rpm-based.py guesses from the source name).
    """
    if not lists_binaries(package):
        return None
    names = [n for n in binary_names(package) if n.endswith(dev_suffix)]
    source = srcname(package)
    for stem in (source, project):
        for prefix in ("",) + tuple(prefixes):
            if f"{prefix}{stem}{dev_suffix}" in names:
                return f"{prefix}{stem}{dev_suffix}"
    if names:
        return min(names, key=lambda n: (not n.startswith(tuple(prefixes)), len(n), n))
    return main_package(package, project, dev_suffix, prefixes)


def merge_split_packages(packages):
    """One entry per source package, with every binary package's name in `binnames`.

    For repositories that repology lists one binary package per entry, each
    with only a `binname` (glfw, glfw-dev, ...): the entries of one source
    package and version are merged into the first one, keeping their order.
    Generators pass it to dependencies() as `prepare`.
    """
    merged = {}
    for package in packages:
        key = (package.get("srcname"), package.get("version"), package.get("subrepo"))
        entry = merged.get(key)
        if entry is None:
            entry = merged[key] = dict(package)
            entry["binnames"] = list(package.get("binnames") or [])
            entry["binname"] = srcname(package) or package.get("binname")
        for name in [package.get("binname")] + list(package.get("binnames") or []):
            if name and name not in entry["binnames"]:
                entry["binnames"].append(name)
    return list(merged.values())


def describe(dep):
    return dep["name"] + (f" {dep['op']} {dep['version']}" if dep.get("op") else "")


def local_name(job, local):
    """The name of one of pkggen.yaml's own packages here: its name.

    `local` is {name, packaged, package (its pkggen.yaml settings)}; generators
    that name packages differently pass their own function.
    """
    return local["name"]


def dependencies(job, fields, pick, render, local=local_name, prepare=None, noise=(), other_targets=None,
                 source_name=srcname):
    """Fill job.context["deps"]: {field: [rendered dependencies]} (see the comment above).

    `render` is one function, or {field: function} when fields are written
    differently. prepare(packages) -> packages runs first on each release's
    repology packages (e.g. merge_split_packages()). `noise` and
    `other_targets` are the distribution's packages that are never what a
    dependency means (see drop_unwanted()); source_name(package) is the name
    compared with the project's (default: repology's srcname).
    """
    deps = {field: [] for kinds in fields.values() for field, _ in kinds}
    missing = []
    for role in ROLES:
        for dep in job.dependencies.get(role) or []:
            for field, kind in fields.get(role, ()):
                render_field = render[field] if isinstance(render, dict) else render
                if "override" in dep:
                    values = [dep["override"]] if dep["override"] is not False else []
                elif dep.get("local"):
                    if not dep["local"].get("packaged"):
                        job.warn(f"the dependency '{dep['name']}' is one of pkggen.yaml's packages, but it "
                                 f"isn't packaged for {job.distribution}")
                    name = local(job, dep["local"])
                    values = render_field(job, dep, {rp: name for rp in job.releases} or {job.distribution: name})
                else:
                    names = {}
                    for rp in job.releases:
                        packages = (dep.get("releases") or {}).get(rp) or []
                        if prepare:
                            packages = prepare(packages)
                        packages = drop_unwanted(packages, noise, other_targets, source_name)
                        package = choose_package(dep["name"], packages, source_name)
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
                    values = render_field(job, dep, names)
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

# GitHub reports deprecated SPDX ids; RPM and Arch want the current ones
SPDX_UPDATES = {
    "GPL-2.0": "GPL-2.0-only", "GPL-3.0": "GPL-3.0-only",
    "LGPL-2.1": "LGPL-2.1-only", "LGPL-3.0": "LGPL-3.0-only", "AGPL-3.0": "AGPL-3.0-only",
}


def filter_spdx(value):
    return SPDX_UPDATES.get(str(value), str(value))


def filter_shquote(value):
    """Quote for bash with single quotes: it's -> 'it'\\''s'."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def filter_dquote(value):
    """Quote for bash with double quotes, escaping \\ " $ and `."""
    return '"' + re.sub(r'([\\"$`])', r"\\\1", str(value)) + '"'


def substitute(pattern, replacement):
    """A template filter that rewrites its value: every match of `pattern` becomes `replacement`.

    `pattern` is a regex (text or compiled), `replacement` a string or a
    function of the match, as re.sub() takes them. The value is stripped first.
    Generators build their formats' version filters with it, e.g. Void's
    substitute(r"[-_:/\\s]", ".") or Alpine's with a function for _rc1 suffixes.
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern

    def apply(value):
        return compiled.sub(replacement, str(value).strip())
    return apply


def package_version(job, key, translate, valid, example):
    """The version a format writes: the package's `<distribution>.<key>`, else translate(version).

    `valid` is the format's version syntax: a regex (matched) or a function
    version -> bool. Fails when the version isn't valid, with a hint to fix it.
    """
    value = job.context["dist"].get(key)
    version = str(value) if value is not None else translate(job.version)
    if not (valid(version) if callable(valid) else valid.match(version)):
        raise PackagingError(
            f"version '{job.version}' of {job.name} gives '{version}', which isn't a valid {job.distribution} "
            f"version (e.g. {example}). Adjust the fetch generator's 'transforms', or set "
            f"'{job.distribution}: {{ {key}: ... }}' for the package")
    return version


FILTERS = {
    "spdx": filter_spdx,
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


def make_environment(job, filters=None):
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
    env.filters.update(filters or {})  # the generator's own, e.g. Alpine's apkver
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


def render_tree(job, place, executable=(), filters=None):
    """Render every template in the package's folder into the repository.

    *.tmpl files are rendered with Jinja2 and lose the suffix. Other files are
    copied unchanged. place(rel) maps a relative output name to its absolute
    destination. Outputs named in `executable` (or whose template is
    executable) get the executable bit. `filters` ({name: function}) adds the
    generator's own template filters to the shared ones (FILTERS).
    """
    env = make_environment(job, filters)
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
