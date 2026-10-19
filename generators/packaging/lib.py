#!/usr/bin/env python3
# Shared helpers for packaging generators.
#
# A packaging generator reads one job (JSON) on stdin:
#   {
#     "distribution": "arch",
#     "package": { ...template context... },
#     "repository": { ...repositories.<dist> from pkggen.yaml... },
#     "repository_path": "/abs/path/pkggen-build/arch",
#     "templates_path": "/abs/path/packages/arch/<name>",
#     "default_templates_path": "/abs/path/generators/templates/arch",
#     "run_path": "/abs/path"            // the pkggen.yaml directory, for messages
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
        self.defaults_path = data["default_templates_path"]
        self.run_path = data.get("run_path") or os.getcwd()

        self.name = self.context["name"]
        self.version = str(self.context["version"])

        self.output = None  # set by the generator: where the package files went
        self.written = []
        self.unchanged = []
        self.warnings = []

        self.context.setdefault("distribution", self.distribution)
        self.context.setdefault("repository", self.repository)
        self.context.setdefault("architectures", list(DEFAULT_ARCHITECTURES))
        # The package's own `<distribution>:` block, e.g. `arch: { depends: [...] }`
        dist = self.context.get(self.distribution)
        self.context["dist"] = dist if isinstance(dist, dict) else {}
        now = source_date()
        self.context["now"] = now
        self.context["date"] = parse_date(self.context.get("date")) or now

    def warn(self, message):
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


# --- Licenses and versions -------------------------------------------------

# SPDX id (including the deprecated ids GitHub still returns) -> Debian DEP-5 short name
DEBIAN_LICENSES = {
    "MIT": "Expat",
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
    "LGPL-2.1": "LGPL-2.1", "LGPL-2.1-only": "LGPL-2.1", "LGPL-2.1-or-later": "LGPL-2.1+",
    "LGPL-3.0": "LGPL-3", "LGPL-3.0-only": "LGPL-3", "LGPL-3.0-or-later": "LGPL-3+",
    "AGPL-3.0": "AGPL-3", "AGPL-3.0-only": "AGPL-3", "AGPL-3.0-or-later": "AGPL-3+",
    "BSD-2-Clause": "BSD-2-clause", "BSD-3-Clause": "BSD-3-clause",
}

# SPDX id -> Gentoo license file name
GENTOO_LICENSES = {
    "GPL-2.0": "GPL-2", "GPL-2.0-only": "GPL-2", "GPL-2.0-or-later": "GPL-2+",
    "GPL-3.0": "GPL-3", "GPL-3.0-only": "GPL-3", "GPL-3.0-or-later": "GPL-3+",
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


def copy_default(job, rel):
    src = os.path.join(job.defaults_path, rel)
    dst = os.path.join(job.templates_path, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def ensure_templates(job, required):
    """Make sure the package's template folder exists and has the required templates.

    A missing or template-less folder gets the full default set. A folder that
    lacks some required templates only gets those. Existing files are never
    overwritten. Both cases are reported as warnings.
    """
    if not os.path.isdir(job.defaults_path):
        raise PackagingError(f"default templates for {job.distribution} not found at {job.defaults_path}")

    shown = job.display(job.templates_path)
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
        loader=jinja2.FileSystemLoader(job.templates_path),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    env.filters.update(FILTERS)
    return env


def template_line(error, filename):
    """Find the template line an exception was raised from, using Jinja's rewritten traceback."""
    line = None
    for frame in traceback.extract_tb(error.__traceback__):
        if frame.filename == filename:
            line = frame.lineno
    return line


def render(job, env, rel):
    path = os.path.join(job.templates_path, rel)
    shown = job.display(path)
    try:
        return env.get_template(rel).render(job.context)
    except jinja2.TemplateSyntaxError as e:
        raise PackagingError(f"{shown}:{e.lineno}: syntax error: {e.message}")
    except jinja2.UndefinedError as e:
        line = template_line(e, path)
        where = f"{shown}:{line}" if line else shown
        raise PackagingError(f"{where}: {e.message}. Define it for {job.name} in pkggen.yaml, "
                             "or guard it with '| default(...)' in the template")
    except Exception as e:
        line = template_line(e, path)
        where = f"{shown}:{line}" if line else shown
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
