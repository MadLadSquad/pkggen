import pytest

from tests.support.helpers import load_generator, run_generator
from tests.testing.conftest import SHA256, artifact, context, entry


@pytest.fixture
def lib():
    return load_generator("testing", "lib")


def job(lib, **fields):
    data = {"distribution": "demo", "rp_names": ["a_1", "a_2"], "packages": []}
    data.update(fields)
    return lib.Job(data)


TABLE = {
    "a_1": {"name": "a-1", "image": "example.org/a:1"},
    "a_2": {"name": "a-2", "image": "example.org/a:2", "arches": ["amd64"]},
    "a_3": {"name": "a-3", "image": "example.org/a:3"},
}


def test_select_targets_defaults_to_every_rp_name(lib):
    j = job(lib)
    assert [rp for rp, _ in lib.select_targets(j, TABLE)] == ["a_1", "a_2"]
    assert j.warnings == []


def test_select_targets_warns_about_rp_names_without_image(lib):
    j = job(lib, rp_names=["a_1", "a_9"])
    assert [rp for rp, _ in lib.select_targets(j, TABLE)] == ["a_1"]
    assert j.warnings == ["demo has no container image for a_9, so it's not tested"]


def test_select_targets_versions_by_rp_name_or_target_name(lib):
    j = job(lib, versions=["a-3", "a_1", "a_3"])
    assert [rp for rp, _ in lib.select_targets(j, TABLE)] == ["a_3", "a_1"]


def test_select_targets_unknown_versions(lib):
    with pytest.raises(lib.TestingError, match=r"unknown demo version\(s\) in 'test.distributions.demo.versions': "
                                               r"zz, a-9. Available: a_1, a_2, a_3"):
        lib.select_targets(job(lib, versions=["zz", "a-9"]), TABLE)


def test_select_targets_skips_other_host_architectures(lib):
    j = job(lib, host_arch="arm64")
    assert [rp for rp, _ in lib.select_targets(j, TABLE)] == ["a_1"]
    assert j.warnings == ["a-2 has no arm64 container image, so it's skipped"]


@pytest.mark.parametrize("architectures, kept", [(["amd64"], False), (["arm64", "riscv"], True), ("any", True)])
def test_job_skips_packages_not_built_for_the_host(lib, architectures, kept):
    j = job(lib, host_arch="arm64", packages=[entry(architectures=architectures)])
    assert [p.name for p in j.packages] == (["foo"] if kept else [])
    assert bool(j.warnings) != kept


def test_package(lib):
    package = lib.Package({"context": context(source_dir="custom", version=1.5), "output": "cat\\foo\\"})
    assert (package.name, package.version, package.output, package.source_dir()) == ("foo", "1.5", "cat/foo", "custom")
    assert lib.Package({"context": context()}).source_dir() == "foo-1.2.0"
    bare = lib.Package({"context": {"name": "x", "version": "1", "artifacts": []}})
    assert bare.source == {} and bare.source_dir() == ""


def test_target_adds_the_user_steps(lib):
    j = job(lib, test={"setup": [{"name": "deps", "run": "apt-get install foo", "user": None, "workdir": None}],
                       "post-setup": [{"name": "run it", "run": "foo --version", "user": "me", "workdir": "/tmp"}]})
    own = lib.step("tools", "install tools")
    result = lib.target(j, "a_1", TABLE["a_1"], setup=[own], prepare=[], packages=[], finalize=[])
    assert [s["name"] for s in result["setup"]] == ["tools", "deps"]
    assert result["post_setup"] == [{"name": "run it", "run": "foo --version\n", "user": "me", "workdir": "/tmp"}]
    assert result["prepare"][0]["name"] == "create the work folders"
    assert (result["name"], result["rp_name"], result["image"], result["repo"], result["collect"]) == (
        "a-1", "a_1", "example.org/a:1", "/pkggen/repo", "/pkggen/out")


def test_download(lib):
    code = lib.download(artifact("foo.tar.xz", url="https://x.org/a b's.tar.xz"), "/w")
    assert code == ("curl -fsSL --retry 3 -o '/w/foo.tar.xz' 'https://x.org/a b'\\''s.tar.xz'\n"
                    f"echo '{SHA256}  /w/foo.tar.xz' | sha256sum -c -\n")
    unchecked = dict(artifact("foo.zip"), checksums={})
    assert "sha256sum" not in lib.download(unchecked, "/w", name="other.zip")
    assert "/w/other.zip" in lib.download(unchecked, "/w", name="other.zip")


def test_heredoc_never_ends_early(lib):
    code = lib.heredoc("/etc/x", "a\nPKGGEN_EOF\n$HOME\n")
    assert code == "cat > '/etc/x' <<'PKGGEN_EOF_'\na\nPKGGEN_EOF\n$HOME\nPKGGEN_EOF_\n"


@pytest.mark.parametrize("name, ext", [("a-1.tar.gz", "tar.gz"), ("a.tgz", "tar.gz"), ("a.tar.zst", "tar.zst"),
                                       ("a.txz", "tar.xz"), ("a.zip", None), ("a.tar", None)])
def test_tarball_extension(lib, name, ext):
    assert lib.tarball_extension(name) == ext


def test_run_reports_invalid_jobs():
    result = run_generator("testing", "arch", "not json")
    assert result.code == 1
    assert result.err_plain.startswith("Error: invalid testing job: JSONDecodeError")


def test_run_reports_testing_errors():
    result = run_generator("testing", "debian", {"distribution": "debian", "rp_names": ["debian_13"],
                                                  "versions": ["debian_99"]})
    assert result.code == 1
    assert "Traceback" not in result.err
    assert result.err_plain.strip() == ("Error: unknown debian version(s) in 'test.distributions.debian.versions': "
                                        "debian_99. Available: " + ", ".join(load_generator("testing", "debian").TARGETS))


def test_run_prints_targets_and_warnings():
    result = run_generator("testing", "arch", {"distribution": "arch", "rp_names": ["arch"], "host_arch": "arm64"})
    assert result.code == 0, result.err
    assert result.json == {"targets": [], "warnings": ["arch has no arm64 container image, so it's skipped"]}
