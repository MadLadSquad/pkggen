import json
import queue
import textwrap

import pytest

import generate
import utils
from tests.support.helpers import strip_ansi

# --- Stub generators ---------------------------------------------------------

STUB_FETCH = textwrap.dedent("""\
    import json, os, sys, time
    package = json.loads(sys.stdin.read())
    mode = package.get("stub")
    if mode == "fail":
        print("noise line", file=sys.stderr)
        print("\\x1b[31mError [" + package["name"] + "]: boom\\x1b[0m", file=sys.stderr)
        sys.exit(1)
    if mode == "crash":
        sys.exit(3)
    if mode == "invalid-json":
        print("this is not json")
        sys.exit(0)
    if mode == "no-version":
        print(json.dumps({"artifacts": []}))
        sys.exit(0)
    if mode == "wait-for":
        # Only finishes once another package has been packaged: proves packaging
        # starts while fetches are still running
        deadline = time.time() + 30
        while not os.path.exists(package["wait_for"]):
            if time.time() > deadline:
                print("Error [" + package["name"] + "]: timed out waiting", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.05)
    print(json.dumps({
        "version": package.get("stub_version", "1.0"),
        "date": "2026-01-02T03:04:05+00:00",
        "tag": "v1.0",
        "artifacts": [{"url": "https://example.com/a.tar.gz", "alias": "a.tar.gz"}],
        "exports": {"description": "exported description", "license": "MIT"},
        "position_seen": package["position"],
    }))
""")

STUB_PACKAGING = textwrap.dedent("""\
    import json, os, sys
    job = json.loads(sys.stdin.read())
    package = job["package"]
    if package.get("pack_fail") == job["distribution"]:
        print("\\x1b[31mError: packaging broke\\x1b[0m", file=sys.stderr)
        sys.exit(1)
    if package.get("pack_invalid") == job["distribution"]:
        print("nope")
        sys.exit(0)
    out = os.path.join(job["repository_path"], package["name"] + ".json")
    with open(out, "w") as f:
        json.dump(job, f)
    print(json.dumps({"output": out, "files_written": [out], "files_unchanged": [],
                      "warnings": ["stub warning"]}))
""")


@pytest.fixture
def stub_generators(tmp_path, monkeypatch):
    root = tmp_path / "generators"
    (root / "distributions").mkdir(parents=True)
    (root / "distributions" / "distributions.yaml").write_text(textwrap.dedent("""\
        distributions:
          - name: alpha
            rp-names: [a]
          - name: beta
            rp-names: [b]
          - name: nopackaging
            rp-names: [c]
    """))
    (root / "generation").mkdir()
    (root / "generation" / "stub.py").write_text(STUB_FETCH)
    (root / "generation" / "lib.py").write_text("")
    (root / "packaging").mkdir()
    for dist in ("alpha", "beta"):
        (root / "packaging" / f"{dist}.py").write_text(STUB_PACKAGING)
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(root))
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Write pkggen.yaml (a dict) into a fresh project and create its repositories."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(root))

    def make(config, repos=("alpha", "beta")):
        import yaml
        (root / "pkggen.yaml").write_text(yaml.safe_dump(config))
        for dist in repos:
            (root / "pkggen-build" / dist).mkdir(parents=True, exist_ok=True)
        return root
    return make


def packages(*entries, generator="stub", **defaults):
    return {"generator": generator, "packages": list(entries), **defaults}


REPOSITORIES = {"alpha": {"key": "value"}, "beta": None}

# --- deep_merge / check_package ------------------------------------------------


def test_deep_merge():
    base = {"a": 1, "nested": {"x": 1, "y": {"z": 1}}, "list": [1]}
    override = {"b": 2, "nested": {"y": {"w": 2}}, "list": [2]}
    assert generate.deep_merge(base, override) == {
        "a": 1, "b": 2, "nested": {"x": 1, "y": {"z": 1, "w": 2}}, "list": [2]}
    assert base == {"a": 1, "nested": {"x": 1, "y": {"z": 1}}, "list": [1]}


def test_deep_merge_replaces_mapping_with_scalar():
    assert generate.deep_merge({"a": {"x": 1}}, {"a": None}) == {"a": None}
    assert generate.deep_merge({"a": 1}, {"a": {"x": 1}}) == {"a": {"x": 1}}


@pytest.mark.parametrize("package", [
    {"name": "p"},
    {"name": "p", "maintainer": "Jane <j@x.org>", "author": "Upstream"},
    {"name": "p", "architectures": "any"},
    {"name": "p", "architectures": ["amd64", "ppc64"]},
])
def test_check_package_accepts(package):
    assert generate.check_package(package) == []


@pytest.mark.parametrize("package, fragment", [
    ({"name": "p", "maintainer": ""}, "'maintainer' of package 'p'"),
    ({"name": "p", "maintainer": "  "}, "'maintainer' of package 'p'"),
    ({"name": "p", "author": ["Jane"]}, "'author' of package 'p'"),
    ({"name": "p", "architectures": "amd64"}, "'architectures' of package 'p'"),
    ({"name": "p", "architectures": []}, "'architectures' of package 'p'"),
    ({"name": "p", "architectures": ["amd64", ""]}, "'architectures' of package 'p'"),
    ({"name": "p", "architectures": [1]}, "'architectures' of package 'p'"),
])
def test_check_package_rejects(package, fragment):
    errors = generate.check_package(package)
    assert len(errors) == 1 and fragment in errors[0]


# --- load_packages ---------------------------------------------------------------


def test_load_packages_applies_group_defaults():
    config = {
        "repositories": {"arch": {}}, "test": {}, "deployment": {},
        "group": packages({"name": "a", "github": {"repo": "A"}}, {"name": "b", "github": {"user": "Other"}},
                          generator="github", github={"user": "Me", "query": "releases"}),
    }
    result = generate.load_packages(config, None)
    assert [(p["name"], p["github"]) for p, _ in result] == [
        ("a", {"user": "Me", "query": "releases", "repo": "A"}),
        ("b", {"user": "Other", "query": "releases"}),
    ]
    assert all(script.endswith("github.py") for _, script in result)
    assert "generator" not in result[0][0] and "packages" not in result[0][0]


def test_load_packages_example(example_config):
    result = generate.load_packages(example_config, None)
    assert [p["name"] for p, _ in result] == ["untitled-cli-parser", "untitled-imgui-framework"]


def test_load_packages_filter(example_config):
    result = generate.load_packages(example_config, ["untitled-imgui-framework"])
    assert [p["name"] for p, _ in result] == ["untitled-imgui-framework"]


def test_load_packages_collects_every_error():
    config = {
        "not-a-group": "text",
        "no-generator": {"packages": [{"name": "x"}]},
        "unknown": packages({"name": "y"}, generator="sourceforge"),
        "empty": packages(),
        "group-maintainer": packages({"name": "z"}, generator="url", maintainer="Jane <j@x.org>"),
        "bad": packages({"name": "dup"}, {"name": "dup"}, {"nameless": True}, "string", {"name": "m", "maintainer": 1},
                        generator="url"),
    }
    with pytest.raises(utils.GenericError) as info:
        generate.load_packages(config, ["missing"])
    message = info.value.args[0]
    assert message.startswith("Problems found in pkggen.yaml:\n  - ")
    for fragment in [
        "'not-a-group' must be a package group",
        "group 'no-generator' has no 'generator'",
        "uses the fetch generator 'sourceforge', which doesn't exist",
        "(available: github, gitlab, url)",
        "group 'empty' needs a non-empty 'packages' list",
        "'maintainer' is set on group 'group-maintainer'",
        "package 'dup' is defined in both 'bad' and 'bad'",
        "'bad.packages[2]' needs a 'name'",
        "'bad.packages[3]' needs a 'name'",
        "'maintainer' of package 'm'",
        "unknown package(s) passed to --packages: missing",
    ]:
        assert fragment in message
    assert message.count("\n  - ") == 11


def test_load_packages_duplicate_across_groups():
    config = {"one": packages({"name": "p"}, generator="url"), "two": packages({"name": "p"}, generator="url")}
    with pytest.raises(utils.GenericError, match="package 'p' is defined in both 'one' and 'two'"):
        generate.load_packages(config, None)


def test_load_packages_without_generation_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    with pytest.raises(utils.GenericError, match="available: none"):
        generate.load_packages({"g": packages({"name": "p"}, generator="url")}, None)


# --- load_targets ----------------------------------------------------------------


def test_load_targets(stub_generators, project):
    root = project({}, repos=("alpha",))
    failures = []
    targets = generate.load_targets({"repositories": REPOSITORIES}, None, failures)
    assert [(t.name, t.repo_path, t.repository) for t in targets] == [
        ("alpha", str(root / "pkggen-build" / "alpha"), {"key": "value"})]
    assert targets[0].script == str(stub_generators / "packaging" / "alpha.py")
    # beta has a block but no repository yet; nopackaging has no packaging generator
    assert [(f.distribution, f.package) for f in failures] == [("beta", "*")]
    assert "Run `pkggen gen-repo beta` first" in failures[0].message


def test_load_targets_skips_dists_without_block(stub_generators, project):
    project({})
    failures = []
    assert generate.load_targets({"repositories": {"beta": {}}}, None, failures)[0].name == "beta"
    assert failures == []


def test_load_targets_filter(stub_generators, project):
    project({})
    failures = []
    targets = generate.load_targets({"repositories": {"alpha": {}}}, ["alpha", "beta"], failures)
    assert [t.name for t in targets] == ["alpha"]
    # A requested distribution without a block is reported
    assert "no 'repositories.beta' block" in failures[0].message


def test_load_targets_unknown_filter(stub_generators, project):
    project({})
    with pytest.raises(utils.GenericError, match=r"No packaging generator for: nopackaging, zzz \(available: alpha, beta\)"):
        generate.load_targets({"repositories": REPOSITORIES}, ["zzz", "nopackaging"], [])


def test_load_targets_real_generators(example_project, example_config):
    (example_project / "pkggen-build" / "arch").mkdir(parents=True)
    failures = []
    targets = generate.load_targets(example_config, None, failures)
    assert [t.name for t in targets] == ["arch"]
    assert sorted(f.distribution for f in failures) == ["debian", "gentoo", "rpm-based"]


# --- Small helpers -----------------------------------------------------------------


def test_build_context():
    package = {"name": "p", "description": "mine", "license": "", "github": {"user": "u"}}
    result = {"version": "1.0", "tag": "v1.0", "artifacts": [{"alias": "a"}, {"alias": "b"}],
              "exports": {"description": "theirs", "license": "MIT", "homepage": "https://h"}, "extra": 1}
    context = generate.build_context(package, result)
    assert context == {"name": "p", "description": "mine", "license": "MIT", "homepage": "https://h",
                       "github": {"user": "u"}, "version": "1.0", "tag": "v1.0",
                       "artifacts": [{"alias": "a"}, {"alias": "b"}], "source": {"alias": "a"}}
    assert package["license"] == ""


def test_build_context_without_artifacts():
    context = generate.build_context({"name": "p"}, {"version": "1", "artifacts": []})
    assert context["source"] == {}


def test_failure_cleans_generator_messages():
    failure = generate.Failure("fetch", "pkg", "\x1b[31mError [pkg]: it broke\x1b[0m\nError: second\n")
    assert failure.message == "it broke\nsecond"
    assert failure.label() == "fetch pkg"
    assert generate.Failure("package", "pkg", "x", "arch").label() == "[arch] pkg"


def test_error_lines():
    text = "progress\n" + "".join(f"Error {i}\n" for i in range(7)) + "done\n"
    assert generate.error_lines(text) == "\n".join(f"Error {i}" for i in range(2, 7))
    assert generate.error_lines("no problems") == ""


def test_plural():
    assert generate.plural(1, "package") == "1 package"
    assert generate.plural(0, "package") == "0 packages"


# --- run_fetch / run_packaging -------------------------------------------------------


def fetch(stub_generators, package):
    positions = queue.Queue()
    positions.put(7)
    try:
        return generate.run_fetch(package, str(stub_generators / "generation" / "stub.py"), positions)
    finally:
        assert positions.get_nowait() == 7  # the bar position is always handed back


def test_run_fetch(stub_generators):
    result = fetch(stub_generators, {"name": "p"})
    assert result["version"] == "1.0"
    assert result["position_seen"] == 7


@pytest.mark.parametrize("stub, message", [
    ("fail", "Error [p]: boom"),
    ("crash", "the fetch generator exited with code 3"),
    ("invalid-json", "stub.py printed invalid JSON"),
    ("no-version", "returned a result without a 'version' or 'artifacts' list"),
])
def test_run_fetch_errors(stub_generators, stub, message):
    with pytest.raises(RuntimeError) as info:
        fetch(stub_generators, {"name": "p", "stub": stub})
    assert message in strip_ansi(str(info.value))
    assert "noise line" not in str(info.value)


def test_run_packaging(stub_generators, project):
    root = project({})
    target = generate.Target("alpha", str(stub_generators / "packaging" / "alpha.py"),
                             str(root / "pkggen-build" / "alpha"), {"key": "value"})
    result = generate.run_packaging(target, {"name": "p", "version": "1"})
    job = json.loads((root / "pkggen-build" / "alpha" / "p.json").read_text())
    assert result["warnings"] == ["stub warning"]
    assert job == {
        "distribution": "alpha",
        "package": {"name": "p", "version": "1"},
        "repository": {"key": "value"},
        "repository_path": str(root / "pkggen-build" / "alpha"),
        "templates_path": str(root / "packages" / "alpha" / "p"),
        "default_templates_path": str(stub_generators / "templates" / "alpha"),
        "run_path": str(root),
    }


@pytest.mark.parametrize("key, message", [("pack_fail", "packaging broke"), ("pack_invalid", "printed invalid JSON")])
def test_run_packaging_errors(stub_generators, project, key, message):
    root = project({})
    target = generate.Target("alpha", str(stub_generators / "packaging" / "alpha.py"),
                             str(root / "pkggen-build" / "alpha"), {})
    with pytest.raises(RuntimeError, match=message):
        generate.run_packaging(target, {"name": "p", "version": "1", key: "alpha"})


# --- generate() ------------------------------------------------------------------------


def test_generate_success(stub_generators, project, capsys, isolated_env):
    root = project({"repositories": REPOSITORIES,
                    "group": packages({"name": "one"}, {"name": "two", "description": "own"})})
    assert generate.generate() == 0
    for dist in ("alpha", "beta"):
        for name in ("one", "two"):
            job = json.loads((root / "pkggen-build" / dist / f"{name}.json").read_text())
            assert job["package"]["version"] == "1.0"
    two = json.loads((root / "pkggen-build" / "alpha" / "two.json").read_text())["package"]
    assert two["description"] == "own" and two["license"] == "MIT"
    assert two["source"]["alias"] == "a.tar.gz"
    assert "position" not in two

    err = strip_ansi(capsys.readouterr().err)
    assert "Fetching 2 packages, packaging for alpha, beta" in err
    assert "Warning [alpha/one]: stub warning" in err
    assert "Done: 2/2 packages fetched, 4/4 distribution packages generated (alpha, beta)." in err
    # The secrets file is created up front
    assert (isolated_env / "pkggen" / "secrets.yaml").is_file()


def test_generate_filters(stub_generators, project):
    root = project({"repositories": REPOSITORIES, "group": packages({"name": "one"}, {"name": "two"})})
    assert generate.generate(["two"], ["beta"]) == 0
    assert sorted(p.name for p in (root / "pkggen-build").rglob("*.json")) == ["two.json"]
    assert (root / "pkggen-build" / "beta" / "two.json").exists()


def test_generate_packaging_overlaps_fetching(stub_generators, project, tmp_path):
    # "slow" only finishes after "fast" has been packaged, so this deadlocks
    # (and the stub times out) if packaging waited for every fetch
    marker = tmp_path / "project" / "pkggen-build" / "alpha" / "fast.json"
    project({"repositories": {"alpha": {}},
             "group": packages({"name": "slow", "stub": "wait-for", "wait_for": str(marker)}, {"name": "fast"})},
            repos=("alpha",))
    assert generate.generate() == 0


def test_generate_reports_failures_without_stopping(stub_generators, project, capsys):
    project({"repositories": REPOSITORIES,
             "group": packages({"name": "good"}, {"name": "broken", "stub": "fail"},
                               {"name": "half", "pack_fail": "beta"})})
    assert generate.generate() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "Finished with 2 problems: 2/3 packages fetched, 3/4 distribution packages generated (alpha, beta)." in err
    assert "✗ fetch broken: boom" in err
    assert "✗ [beta] half: packaging broke" in err


def test_generate_missing_repository_is_a_failure(stub_generators, project, capsys):
    project({"repositories": REPOSITORIES, "group": packages({"name": "one"})}, repos=("alpha",))
    assert generate.generate() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "✗ [beta] *: the repository pkggen-build" in err
    assert "Run `pkggen gen-repo beta` first" in err


def test_generate_without_targets_only_fetches(stub_generators, project, capsys):
    project({"group": packages({"name": "one"})}, repos=())
    assert generate.generate() == 0
    err = strip_ansi(capsys.readouterr().err)
    assert "Warning: no distribution to package for" in err
    assert "Done: 1/1 packages fetched, 0/0 distribution packages generated." in err


@pytest.mark.parametrize("content, message", [
    ("", "pkggen.yaml is empty or isn't a mapping!"),
    ("- a\n", "pkggen.yaml is empty or isn't a mapping!"),
    ("repositories: {}\n", "pkggen.yaml defines no packages!"),
])
def test_generate_rejects_configs(stub_generators, tmp_path, monkeypatch, content, message):
    (tmp_path / "pkggen.yaml").write_text(content)
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    with pytest.raises(utils.GenericError) as info:
        generate.generate()
    assert info.value.args[0] == message
