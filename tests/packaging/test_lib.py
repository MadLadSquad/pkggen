import json
import os
import sys
from datetime import datetime, timezone

import pytest

from tests.conftest import SOURCE_DATE_EPOCH
from tests.support.helpers import run_generator

# --- Job -----------------------------------------------------------------------------------


def test_job_defaults(plib, make_job, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", SOURCE_DATE_EPOCH)
    data = make_job("arch", package={"name": "p", "version": 1.5, "arch": {"depends": ["x"]}})
    job = plib.Job(data)
    assert (job.name, job.version) == ("p", "1.5")
    assert job.context["distribution"] == "arch"
    assert job.context["repository"] == data["repository"]
    assert job.context["architectures"] == ["amd64", "arm64"]
    assert job.context["dist"] == {"depends": ["x"]}
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert job.context["now"] == now
    assert job.context["date"] == now  # no date from the fetch generator
    assert job.output is None and job.written == [] and job.warnings == []


def test_job_parses_date_and_keeps_package_values(plib, make_job):
    data = make_job("arch", package={"name": "p", "version": "1", "date": "2026-01-02T03:04:05Z",
                                     "distribution": "custom", "architectures": "any", "arch": "not a mapping"})
    job = plib.Job(data)
    assert job.context["date"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert job.context["distribution"] == "custom"
    assert job.context["architectures"] == "any"
    assert job.context["dist"] == {}


def test_job_display(plib, make_job, tmp_path):
    job = plib.Job(make_job("arch", package={"name": "p", "version": "1"}))
    assert job.display(str(tmp_path / "a" / "b")) == os.path.join("a", "b")
    assert job.display(str(tmp_path.parent / "x")) == str(tmp_path.parent / "x")


# --- Dates and filters -----------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("2026-01-02T03:04:05+00:00", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
    ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
    ("2026-01-02", datetime(2026, 1, 2, tzinfo=timezone.utc)),
    ("garbage", None), ("", None), (None, None),
])
def test_parse_date(plib, value, expected):
    assert plib.parse_date(value) == expected


def test_source_date(plib, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert plib.source_date() == datetime(1970, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not a number")
    assert abs((plib.source_date() - datetime.now(timezone.utc)).total_seconds()) < 60


def test_date_filters(plib, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", SOURCE_DATE_EPOCH)
    assert plib.filter_rfc2822("2026-01-02T03:04:05Z") == "Fri, 02 Jan 2026 03:04:05 +0000"
    assert plib.filter_rfc2822(None) == "Mon, 19 Oct 2026 12:00:00 +0000"
    assert plib.filter_rpmdate("2026-01-02T03:04:05Z") == "Fri Jan 02 2026"
    assert plib.filter_rpmdate(datetime(2026, 12, 6, tzinfo=timezone.utc)) == "Sun Dec 06 2026"
    assert plib.filter_rpmdate("nope") == "Mon Oct 19 2026"


def test_rpmdate_ignores_locale(plib, monkeypatch):
    import locale
    for name in ("de_DE.UTF-8", "fr_FR.UTF-8", "ru_RU.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no non-English locale installed")
    try:
        assert plib.filter_rpmdate("2026-01-02T00:00:00Z") == "Fri Jan 02 2026"
    finally:
        locale.setlocale(locale.LC_TIME, "C")


@pytest.mark.parametrize("value, name, email", [
    ("Jane Doe <jane@example.com>", "Jane Doe", "jane@example.com"),
    ("  Jane   <jane@example.com>  ", "Jane", "jane@example.com"),
    ("<jane@example.com>", "", "jane@example.com"),
    ("jane@example.com", "", "jane@example.com"),
    ("Jane Doe", "Jane Doe", ""),
    ("Jane at jane@example.com", "Jane at jane@example.com", ""),
])
def test_person_filters(plib, value, name, email):
    assert plib.split_person(value) == (name, email)
    assert plib.filter_person_name(value) == name
    assert plib.filter_person_email(value) == email


@pytest.mark.parametrize("filter_name, value, expected", [
    ("pkgver", "1.0-rc1:2/x y", "1.0_rc1_2_x_y"),
    ("pkgver", 3, "3"),
    ("spdx", "GPL-3.0", "GPL-3.0-only"),
    ("spdx", "MIT", "MIT"),
    ("debian_license", "MIT", "Expat"),
    ("debian_license", "GPL-2.0-or-later", "GPL-2+"),
    ("debian_license", "Zlib", "Zlib"),
    ("gentoo_license", "BSD-3-Clause", "BSD"),
    ("gentoo_license", "BSL-1.0", "Boost-1.0"),
    ("gentoo_license", "MIT", "MIT"),
    ("shquote", "it's", "'it'\\''s'"),
    ("shquote", "$HOME", "'$HOME'"),
    ("dquote", 'a "b" $c `d` \\e', '"a \\"b\\" \\$c \\`d\\` \\\\e"'),
])
def test_filters(plib, filter_name, value, expected):
    assert plib.FILTERS[filter_name](value) == expected


def test_every_filter_is_registered(plib):
    assert set(plib.FILTERS) == {"rfc2822", "rpmdate", "debian_license", "gentoo_license", "spdx", "pkgver",
                                 "shquote", "dquote", "person_name", "person_email"}


@pytest.mark.parametrize("value", ["it's a \"test\" with $vars and `cmds`", "\\", "", "multi\nline"])
def test_quote_filters_round_trip_through_bash(plib, value):
    if sys.platform == "win32" or not os.path.exists("/bin/bash"):
        pytest.skip("needs bash")
    import subprocess
    for quoted in (plib.filter_shquote(value), plib.filter_dquote(value)):
        out = subprocess.run(["/bin/bash", "-c", f"printf %s {quoted}"], capture_output=True, text=True).stdout
        assert out == value


# --- architectures() --------------------------------------------------------------------------

def job_with(plib, make_job, architectures, dist="arch"):
    return plib.Job(make_job(dist, package={"name": "p", "version": "1", "architectures": architectures}))


NAMES = {"amd64": "x86_64", "arm64": "aarch64"}


def test_architectures_translates_and_dedupes(plib, make_job):
    job = job_with(plib, make_job, ["arm64", "amd64", "arm64", "ppc64"])
    assert plib.architectures(job, NAMES, ["any"]) == ["aarch64", "x86_64"]


def test_architectures_any(plib, make_job):
    assert plib.architectures(job_with(plib, make_job, "any"), NAMES, "ANY") == "ANY"


def test_architectures_other(plib, make_job):
    job = job_with(plib, make_job, ["amd64", "riscv", "bad"])
    assert plib.architectures(job, NAMES, [], other=lambda a: None if a == "bad" else a.upper()) == ["x86_64", "RISCV"]


@pytest.mark.parametrize("value, message", [
    (["ppc64"], "none of the architectures of p (ppc64) can be packaged for arch, which supports amd64, arm64"),
    ([], "'architectures' of p must be \"any\" or a list of names"),
    ("amd64", "'architectures' of p must be \"any\" or a list of names"),
])
def test_architectures_errors(plib, make_job, value, message):
    with pytest.raises(plib.PackagingError) as info:
        plib.architectures(job_with(plib, make_job, value), NAMES, [])
    assert message in str(info.value)


# --- Templates ----------------------------------------------------------------------------------

def make_tree(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))  # exact bytes, even on Windows


def test_iter_files(plib, tmp_path):
    make_tree(tmp_path, {"b.tmpl": "", "a/z.txt": "", "a/b/c": "", ".gitkeep": "", "x~": "", "y.swp": "",
                         ".#lock": "", ".DS_Store": "", "Thumbs.db": "", ".SRCINFO.tmpl": ""})
    assert list(plib.iter_files(tmp_path)) == [".SRCINFO.tmpl", "b.tmpl", "a/z.txt", "a/b/c"]


def defaults_job(plib, make_job, tmp_path, required=("main.tmpl",)):
    defaults = tmp_path / "defaults"
    make_tree(defaults, {"main.tmpl": "default main", "extra.tmpl": "default extra", "data/file": "default data"})
    data = make_job("arch", package={"name": "p", "version": "1"})
    data["default_templates_path"] = str(defaults)
    return plib.Job(data)


def test_ensure_templates_copies_defaults_into_missing_folder(plib, make_job, tmp_path):
    job = defaults_job(plib, make_job, tmp_path)
    plib.ensure_templates(job, ["main.tmpl"])
    assert sorted(plib.iter_files(job.templates_path)) == ["data/file", "extra.tmpl", "main.tmpl"]
    assert job.warnings == [f"no template data found for p, so default templates were created in "
                            f"{os.path.join('packages', 'arch', 'p')}. Edit them to customise the package"]


def test_ensure_templates_fills_a_folder_without_templates(plib, make_job, tmp_path):
    job = defaults_job(plib, make_job, tmp_path)
    make_tree(tmp_path / "packages" / "arch" / "p", {"data/file": "mine", "notes.txt": "keep"})
    plib.ensure_templates(job, ["main.tmpl"])
    assert (tmp_path / "packages" / "arch" / "p" / "data" / "file").read_text() == "mine"
    assert (tmp_path / "packages" / "arch" / "p" / "main.tmpl").read_text() == "default main"


def test_ensure_templates_only_adds_missing_required(plib, make_job, tmp_path):
    job = defaults_job(plib, make_job, tmp_path)
    make_tree(tmp_path / "packages" / "arch" / "p", {"other.tmpl": "mine"})
    plib.ensure_templates(job, ["main.tmpl"])
    assert sorted(plib.iter_files(job.templates_path)) == ["main.tmpl", "other.tmpl"]
    assert "was missing main.tmpl, so default versions were created" in job.warnings[0]


def test_ensure_templates_complete_folder_is_untouched(plib, make_job, tmp_path):
    job = defaults_job(plib, make_job, tmp_path)
    make_tree(tmp_path / "packages" / "arch" / "p", {"main.tmpl": "mine"})
    plib.ensure_templates(job, ["main.tmpl"])
    assert job.warnings == []
    assert list(plib.iter_files(job.templates_path)) == ["main.tmpl"]


def test_ensure_templates_errors(plib, make_job, tmp_path):
    job = defaults_job(plib, make_job, tmp_path)
    make_tree(tmp_path / "packages" / "arch" / "p", {"other.tmpl": "mine"})
    with pytest.raises(plib.PackagingError, match="is missing nope.tmpl, and there's no default for it"):
        plib.ensure_templates(job, ["nope.tmpl"])
    job.defaults_path = str(tmp_path / "missing")
    with pytest.raises(plib.PackagingError, match="default templates for arch not found"):
        plib.ensure_templates(job, ["main.tmpl"])


def render_job(plib, make_job, tmp_path, templates, **package):
    make_tree(tmp_path / "packages" / "arch" / "p", templates)
    return plib.Job(make_job("arch", package={"name": "p", "version": "1", **package}))


def test_render_tree(plib, make_job, tmp_path):
    job = render_job(plib, make_job, tmp_path, {
        "a.tmpl": "{% if true %}\n  {{ name }}-{{ version | pkgver }} {{ dist.x | default('none') }}\n{% endif %}\n",
        "sub/raw.txt": "{{ not rendered }}\r\n",
    })
    out = tmp_path / "out"
    plib.render_tree(job, lambda rel: str(out / rel))
    # trim_blocks + lstrip_blocks + keep_trailing_newline
    assert (out / "a").read_bytes() == b"  p-1 none\n"
    # Other files are copied byte for byte
    assert (out / "sub" / "raw.txt").read_bytes() == b"{{ not rendered }}\r\n"
    assert sorted(job.written) == [os.path.join("out", "a"), os.path.join("out", "sub", "raw.txt")]


def test_render_tree_writes_lf(plib, make_job, tmp_path):
    job = render_job(plib, make_job, tmp_path, {"placeholder.txt": ""})
    (tmp_path / "packages" / "arch" / "p" / "crlf.tmpl").write_bytes(b"line1\r\nline2\r\n")
    plib.render_tree(job, lambda rel: str(tmp_path / "out" / rel))
    assert (tmp_path / "out" / "crlf").read_bytes() == b"line1\nline2\n"


@pytest.mark.parametrize("template, message", [
    ("ok\n{{ missing_var }}\n", "packages/arch/p/t.tmpl:2: 'missing_var' is undefined. Define it for p in pkggen.yaml"),
    ("{% for %}\n", "packages/arch/p/t.tmpl:1: syntax error:"),
    ("\n\n{{ 1 / 0 }}", "packages/arch/p/t.tmpl:3: ZeroDivisionError: division by zero"),
    ("{{ dist.depends.first }}", "packages/arch/p/t.tmpl:1: 'dict object' has no attribute 'depends'"),
])
def test_render_errors_point_at_the_template_line(plib, make_job, tmp_path, template, message):
    job = render_job(plib, make_job, tmp_path, {"t.tmpl": template})
    with pytest.raises(plib.PackagingError) as info:
        plib.render_tree(job, lambda rel: str(tmp_path / "out" / rel))
    assert str(info.value).replace(os.sep, "/").startswith(message)


def test_write_output_reports_unchanged(plib, make_job, tmp_path):
    job = render_job(plib, make_job, tmp_path, {})
    path = tmp_path / "out" / "f"
    plib.write_output(job, str(path), "same\n")
    mtime = path.stat().st_mtime_ns
    plib.write_output(job, str(path), "same\n")
    plib.write_output(job, str(path), b"changed")
    assert job.written == [os.path.join("out", "f")] * 2
    assert job.unchanged == [os.path.join("out", "f")]
    assert path.read_bytes() == b"changed"
    assert mtime <= path.stat().st_mtime_ns


@pytest.mark.skipif(sys.platform == "win32", reason="no executable bit on Windows")
def test_executable_bits(plib, make_job, tmp_path):
    job = render_job(plib, make_job, tmp_path, {"script.tmpl": "#!/bin/sh\n", "named.tmpl": "x", "plain.tmpl": "x"})
    os.chmod(tmp_path / "packages" / "arch" / "p" / "script.tmpl", 0o755)
    plib.render_tree(job, lambda rel: str(tmp_path / "out" / rel), executable={"named"})
    assert os.access(tmp_path / "out" / "script", os.X_OK)
    assert os.access(tmp_path / "out" / "named", os.X_OK)
    assert not os.access(tmp_path / "out" / "plain", os.X_OK)


# --- Shared template folders -------------------------------------------------------------------

def test_templates_extend_and_include_other_folders(plib, make_job, tmp_path):
    root = tmp_path / "packages" / "arch"
    make_tree(root, {
        "base/PKGBUILD.tmpl": "name={{ name }}\n{% block build %}default build\n{% endblock %}end\n",
        "base/.SRCINFO.tmpl": "pkgbase = {{ name }}\n",
        "_snippets/libdir.j2": "libdir=lib\n",
        "p/PKGBUILD.tmpl": '{% extends "base/PKGBUILD.tmpl" %}{% block build %}{% include "_snippets/libdir.j2" %}\n'
                           "{% endblock %}",
    })
    job = plib.Job(make_job("arch", package={"name": "p", "version": "1"}))
    assert job.templates_root == str(root)
    assert job.context["template_dir"] == "packages/arch/p"
    out = tmp_path / "out"
    plib.render_tree(job, lambda rel: str(out / rel))
    assert (out / "PKGBUILD").read_text() == "name=p\nlibdir=lib\nend\n"
    # Only the package's own folder is rendered
    assert sorted(os.listdir(out)) == ["PKGBUILD"]


def test_errors_in_an_extended_template_point_at_it(plib, make_job, tmp_path):
    make_tree(tmp_path / "packages" / "arch", {
        "base/t.tmpl": "line1\n{{ nope }}\n",
        "p/t.tmpl": '{% extends "base/t.tmpl" %}',
    })
    job = plib.Job(make_job("arch", package={"name": "p", "version": "1"}))
    with pytest.raises(plib.PackagingError) as info:
        plib.render_tree(job, lambda rel: str(tmp_path / "out" / rel))
    assert str(info.value).replace(os.sep, "/").startswith("packages/arch/base/t.tmpl:2: 'nope' is undefined")


def test_missing_extended_template(plib, make_job, tmp_path):
    make_tree(tmp_path / "packages" / "arch", {"p/t.tmpl": '{% extends "base/missing.tmpl" %}'})
    job = plib.Job(make_job("arch", package={"name": "p", "version": "1"}))
    with pytest.raises(plib.PackagingError, match="template 'base/missing.tmpl' not found in"):
        plib.render_tree(job, lambda rel: str(tmp_path / "out" / rel))


def test_shared_folder_gets_defaults_once(plib, make_job, tmp_path):
    """Two packages of one shared folder: the second one finds the full set the first one created."""
    first = defaults_job(plib, make_job, tmp_path)
    second = defaults_job(plib, make_job, tmp_path)
    for job in (first, second):
        job.templates_path = str(tmp_path / "packages" / "arch" / "shared")
    plib.ensure_templates(first, ["main.tmpl"])
    plib.ensure_templates(second, ["main.tmpl"])
    assert sorted(plib.iter_files(first.templates_path)) == ["data/file", "extra.tmpl", "main.tmpl"]
    assert len(first.warnings) == 1 and second.warnings == []
    # No temporary folders are left behind
    assert os.listdir(tmp_path / "packages" / "arch") == ["shared"]


def test_create_default_folder_loses_the_race_quietly(plib, make_job, tmp_path, monkeypatch):
    job = defaults_job(plib, make_job, tmp_path)
    real_rename = os.rename

    def racing_rename(src, dst):
        make_tree(tmp_path / "packages" / "arch" / "p", {"main.tmpl": "from the other generator"})
        return real_rename(src, dst)

    monkeypatch.setattr(plib.os, "rename", racing_rename)
    assert plib.create_default_folder(job) is False
    assert (tmp_path / "packages" / "arch" / "p" / "main.tmpl").read_text() == "from the other generator"
    assert os.listdir(tmp_path / "packages" / "arch") == ["p"]


# --- run() --------------------------------------------------------------------------------------

@pytest.mark.parametrize("stdin, message", [
    ("{nope", "Error: invalid packaging job: JSONDecodeError"),
    ('{"package": {}}', "Error: invalid packaging job: KeyError: 'distribution'"),
])
def test_run_rejects_invalid_jobs(stdin, message):
    result = run_generator("packaging", "arch", stdin)
    assert result.code == 1
    assert result.out == ""
    assert result.err_plain.startswith(message)


def test_run_output(make_job, tmp_path):
    result = run_generator("packaging", "arch", make_job("arch"))
    assert result.code == 0, result.err
    assert result.out.count("\n") == 1
    assert json.loads(result.out)["output"] == os.path.join("pkggen-build", "arch", "untitled-cli-parser")


def test_run_debug_traceback(make_job):
    job = make_job("arch", architectures=["sparc"])
    plain = run_generator("packaging", "arch", job)
    debug = run_generator("packaging", "arch", job, env={"PKGGEN_DEBUG": "1"})
    assert "Traceback" not in plain.err and "Traceback" in debug.err
    assert plain.code == debug.code == 1
