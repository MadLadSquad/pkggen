import json
import queue
import textwrap

import pytest

from pkggen import dependencies, generate, utils
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
    from tests.support.example import PACKAGES
    result = generate.load_packages(example_config, None)
    # In pkggen.yaml order: the libraries, the framework, the applications, then the overlay's own packages
    assert [p["name"] for p, _ in result] == list(PACKAGES) + ["btrustbiss"]
    packages = {p["name"]: p for p, _ in result}
    assert packages["untitled-cli-parser"]["templates"] == "cmake-library"
    assert packages["untitled-game-system-manager"]["templates"] == "untitled-game-system-manager"
    assert packages["abcccid"]["distributions"] == ["gentoo"]
    assert packages["untitled-open"]["gentoo"] == {"category": "ude-base", "bdepend": ["dev-util/wayland-scanner"]}
    # The group's dependencies with the package's own
    pkgconf = {"name": "pkgconf", "gentoo": "virtual/pkgconfig", "rpm-based": "pkgconfig"}
    assert packages["untitled-open"]["dependencies"] == {
        "build": ["cmake", pkgconf], "libraries": ["dbus", "wayland"], "runtime": ["xdg-desktop-portal"]}
    assert packages["untitled-game-system-manager"]["dependencies"]["build"] == [
        {"name": "cmake", "gentoo": False}, {**pkgconf, "gentoo": False},
        {"name": "go", "version": ">= 1.25", "debian": "golang-go (>= 2:1.25~)", "rpm-based": "go >= 1.25"}]


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


# --- load_distributions --------------------------------------------------------


def test_load_distributions(stub_generators, project):
    root = project({}, repos=("alpha",))
    failures = []
    distributions = generate.load_distributions({"repositories": REPOSITORIES}, None, failures)
    assert [(d.name, d.repo_path, d.repository) for d in distributions] == [
        ("alpha", str(root / "pkggen-build" / "alpha"), {"key": "value"})]
    assert distributions[0].script == str(stub_generators / "packaging" / "alpha.py")
    # beta has a block but no repository yet; nopackaging has no packaging generator
    assert [(f.distribution, f.package) for f in failures] == [("beta", "*")]
    assert "Run `pkggen gen-repo beta` first" in failures[0].message


def test_load_distributions_skips_dists_without_block(stub_generators, project):
    project({})
    failures = []
    assert generate.load_distributions({"repositories": {"beta": {}}}, None, failures)[0].name == "beta"
    assert failures == []


def test_load_distributions_filter(stub_generators, project):
    project({})
    failures = []
    distributions = generate.load_distributions({"repositories": {"alpha": {}}}, ["alpha", "beta"], failures)
    assert [d.name for d in distributions] == ["alpha"]
    # A requested distribution without a block is reported
    assert "no 'repositories.beta' block" in failures[0].message


def test_load_distributions_unknown_filter(stub_generators, project):
    project({})
    with pytest.raises(utils.GenericError, match=r"No packaging generator for: nopackaging, zzz \(available: alpha, beta\)"):
        generate.load_distributions({"repositories": REPOSITORIES}, ["zzz", "nopackaging"], [])


def test_load_distributions_real_generators(example_project, example_config):
    (example_project / "pkggen-build" / "arch").mkdir(parents=True)
    failures = []
    distributions = generate.load_distributions(example_config, None, failures)
    assert [d.name for d in distributions] == ["arch"]
    assert sorted(f.distribution for f in failures) == ["alpine", "debian", "gentoo", "rpm-based", "void"]


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
    dist = generate.Distribution("alpha", str(stub_generators / "packaging" / "alpha.py"),
                                 str(root / "pkggen-build" / "alpha"), {"key": "value"})
    result = generate.run_packaging(dist, {"name": "p", "version": "1"})
    job = json.loads((root / "pkggen-build" / "alpha" / "p.json").read_text())
    assert result["warnings"] == ["stub warning"]
    assert job == {
        "distribution": "alpha",
        "package": {"name": "p", "version": "1"},
        "repository": {"key": "value"},
        "repository_path": str(root / "pkggen-build" / "alpha"),
        "templates_path": str(root / "packages" / "alpha" / "p"),
        "templates_root": str(root / "packages" / "alpha"),
        "default_templates_path": str(stub_generators / "templates" / "alpha"),
        "run_path": str(root),
        "releases": [],
        "dependencies": {"build": [], "libraries": [], "runtime": [], "development": []},
    }


@pytest.mark.parametrize("key, message", [("pack_fail", "packaging broke"), ("pack_invalid", "printed invalid JSON")])
def test_run_packaging_errors(stub_generators, project, key, message):
    root = project({})
    dist = generate.Distribution("alpha", str(stub_generators / "packaging" / "alpha.py"),
                                 str(root / "pkggen-build" / "alpha"), {})
    with pytest.raises(RuntimeError, match=message):
        generate.run_packaging(dist, {"name": "p", "version": "1", key: "alpha"})


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


# --- templates / distributions ----------------------------------------------------

KNOWN = {"alpha": ["a", "alpha_2"], "beta": ["b"]}


@pytest.mark.parametrize("package", [
    {"name": "p", "templates": "cmake-library"},
    {"name": "p", "distributions": ["alpha"]},
    {"name": "p", "distributions": {"alpha": {}, "beta": None}},
    {"name": "p", "distributions": {"alpha": {"versions": ["a", "alpha-2"]}}},
])
def test_check_package_accepts_templates_and_distributions(package):
    assert generate.check_package(package, KNOWN) == []


@pytest.mark.parametrize("package, fragment", [
    ({"name": "p", "templates": "a/b"}, "'templates' of package 'p'"),
    ({"name": "p", "templates": ".."}, "'templates' of package 'p'"),
    ({"name": "p", "templates": ""}, "'templates' of package 'p'"),
    ({"name": "p", "distributions": []}, "must be a list of distributions"),
    ({"name": "p", "distributions": "alpha"}, "must be a list of distributions"),
    ({"name": "p", "distributions": {}}, "must be a list of distributions"),
    ({"name": "p", "distributions": ["alpha", "gamma"]}, "unknown distribution(s): gamma"),
    ({"name": "p", "distributions": {"gamma": {}}}, "unknown distribution(s): gamma"),
    ({"name": "p", "distributions": {"alpha": {"other": 1}}}, "'alpha' must be {} or { versions: [...] }"),
    ({"name": "p", "distributions": {"alpha": {"versions": []}}}, "'alpha.versions' must be a non-empty list"),
    ({"name": "p", "distributions": {"alpha": {"versions": ["z"]}}}, "unknown release(s): z (known: a, alpha_2)"),
])
def test_check_package_rejects_templates_and_distributions(package, fragment):
    errors = generate.check_package(package, KNOWN)
    assert len(errors) == 1 and fragment in errors[0], errors


def test_package_distributions_and_tested_on():
    assert generate.package_distributions({"name": "p"}) is None
    assert generate.package_distributions({"distributions": ["alpha"]}) == {"alpha": None}
    limited = {"distributions": {"alpha": {"versions": ["alpha-2"]}, "beta": {}}}
    assert generate.package_distributions(limited) == {"alpha": ["alpha-2"], "beta": None}
    assert generate.builds_for(limited, "beta") and not generate.builds_for(limited, "gamma")
    # Target names and rp-names match each other
    assert generate.tested_on(limited, "alpha", "alpha-2", "alpha_2")
    assert generate.tested_on(limited, "alpha", "whatever", "alpha_2")
    assert not generate.tested_on(limited, "alpha", "alpha-1", "a")
    assert generate.tested_on(limited, "beta", "b", "b")
    assert not generate.tested_on(limited, "gamma", "g", "g")
    assert generate.tested_on({"name": "p"}, "alpha", "x", "y")


def test_load_packages_checks_distributions_against_the_list(stub_generators):
    config = {"g": packages({"name": "p", "distributions": ["gamma"]}, {"name": "q"}, distributions=["alpha"])}
    with pytest.raises(utils.GenericError, match="unknown distribution\\(s\\): gamma"):
        generate.load_packages(config, None)
    # Group defaults apply, and the package's own value replaces the list
    config = {"g": packages({"name": "p", "distributions": ["beta"]}, {"name": "q"}, distributions=["alpha"])}
    loaded = {p["name"]: p["distributions"] for p, _ in generate.load_packages(config, None)}
    assert loaded == {"p": ["beta"], "q": ["alpha"]}


def test_generate_respects_distributions(stub_generators, project, capsys):
    root = project({"repositories": REPOSITORIES,
                    "group": packages({"name": "everywhere"}, {"name": "only-beta", "distributions": ["beta"]},
                                      {"name": "only-nopackaging", "distributions": ["nopackaging"]})})
    assert generate.generate() == 0
    built = sorted(str(p.relative_to(root / "pkggen-build")) for p in (root / "pkggen-build").rglob("*.json"))
    assert built == ["alpha/everywhere.json", "beta/everywhere.json", "beta/only-beta.json"]
    err = strip_ansi(capsys.readouterr().err)
    assert "only-nopackaging is only packaged for nopackaging (see its 'distributions')" in err
    assert "3/3 distribution packages generated" in err


def test_generate_shared_templates_folder(stub_generators, project):
    root = project({"repositories": REPOSITORIES,
                    "group": packages({"name": "one"}, {"name": "two", "templates": "own"}, templates="shared")})
    assert generate.generate(None, ["alpha"]) == 0
    jobs = {name: json.loads((root / "pkggen-build" / "alpha" / f"{name}.json").read_text())
            for name in ("one", "two")}
    assert jobs["one"]["templates_path"] == str(root / "packages" / "alpha" / "shared")
    assert jobs["two"]["templates_path"] == str(root / "packages" / "alpha" / "own")
    assert jobs["two"]["templates_root"] == str(root / "packages" / "alpha")


# --- dependencies ----------------------------------------------------------------

STUB_DEPENDENCIES = textwrap.dedent("""\
    import json, os, sys
    job = json.loads(sys.stdin.read())
    with open(os.environ["STUB_DEPENDENCY_LOG"], "a") as f:
        f.write(json.dumps(job) + "\\n")
    if "fail-lookup" in job["projects"]:
        print("\\x1b[31mError [dependencies]: repology is down\\x1b[0m", file=sys.stderr)
        sys.exit(1)
    print("\\x1b[33mWarning: an old answer\\x1b[0m", file=sys.stderr)
    print(json.dumps({"projects": {name: {rp: [{"srcname": name, "version": "1"}] for rp in job["repositories"]}
                                   for name in job["projects"]}, "missing": [], "warnings": []}))
""")


@pytest.fixture
def stub_dependencies(stub_generators, tmp_path, monkeypatch):
    (stub_generators / "dependencies").mkdir()
    (stub_generators / "dependencies" / "repology.py").write_text(STUB_DEPENDENCIES)
    log = tmp_path / "dependency-jobs.jsonl"
    monkeypatch.setenv("STUB_DEPENDENCY_LOG", str(log))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("value", [
    {"build": ["cmake"], "libraries": ["glfw >= 3.4", "python:requests"], "runtime": None, "development": []},
    {"libraries": [{"name": "glfw", "version": ">= 3.4", "alpha": "glfw3-dev", "beta": False}]},
    {"runtime": [{"name": "go", "version": "= 1.25.1"}, "xhost<2"]},
])
def test_check_package_accepts_dependencies(value):
    assert generate.check_package({"name": "p", "dependencies": value}, {"alpha": [], "beta": []}) == []


@pytest.mark.parametrize("value, fragment", [
    (["cmake"], "'dependencies' of package 'p' must be a mapping with the lists build, libraries"),
    ({"tools": ["cmake"]}, "has an unknown list 'tools'"),
    ({"build": "cmake"}, "'dependencies.build' of package 'p' must be a list"),
    ({"build": ["cmake >="]}, "'cmake >=' isn't \"<project>\" or \"<project> <op> <version>\""),
    ({"build": ["glfw ~> 3"]}, "isn't \"<project>\""),
    ({"build": [3]}, "must be a string like \"glfw >= 3.4\" or a mapping"),
    ({"build": [{"version": ">= 1"}]}, "needs a 'name'"),
    ({"build": [{"name": "glfw", "version": "3.4"}]}, "the 'version' of 'glfw' must be \"<op> <version>\""),
    ({"build": [{"name": "glfw", "alpha": True}]}, "'glfw.alpha' must be the dependency's name there"),
    ({"build": [{"name": "glfw", "alpha": ""}]}, "'glfw.alpha' must be the dependency's name there"),
    ({"build": [{"name": "glfw", "gamma": "x"}]}, "'glfw' has unknown key(s) gamma"),
])
def test_check_package_rejects_dependencies(value, fragment):
    errors = generate.check_package({"name": "p", "dependencies": value}, {"alpha": [], "beta": []})
    assert any(fragment in e for e in errors), errors


def test_load_packages_checks_dependency_overrides(stub_generators):
    config = {"g": packages({"name": "p", "dependencies": {"build": [{"name": "x", "gamma": "y"}]}})}
    with pytest.raises(utils.GenericError, match="'x' has unknown key\\(s\\) gamma"):
        generate.load_packages(config, None)


def test_generate_looks_dependencies_up_once(stub_generators, stub_dependencies, project, capsys):
    root = project({"repositories": REPOSITORIES, "group": packages(
        {"name": "lib"},
        {"name": "app", "dependencies": {
            "build": ["cmake"],
            "libraries": ["glfw >= 3.4", "lib", {"name": "zlib", "alpha": "zlib1g-dev", "beta": False}],
            "runtime": [{"name": "xhost", "alpha": "x11-xserver-utils"}]}},
        {"name": "other", "dependencies": {"build": ["cmake"]}})})
    assert generate.generate() == 0
    jobs = stub_dependencies()
    # Once, without the local package and the dependency every distribution names itself
    assert len(jobs) == 1
    assert jobs[0]["projects"] == ["cmake", "glfw", "xhost"]
    assert jobs[0]["repositories"] == ["a", "b"]
    assert jobs[0]["user_agent"].startswith("pkggen/")

    alpha = json.loads((root / "pkggen-build" / "alpha" / "app.json").read_text())
    assert alpha["releases"] == ["a"]
    deps = alpha["dependencies"]
    assert deps["build"] == [{"name": "cmake", "op": None, "version": None, "local": None,
                              "releases": {"a": [{"srcname": "cmake", "version": "1"}]}}]
    glfw, local, zlib = deps["libraries"]
    assert (glfw["op"], glfw["version"]) == (">=", "3.4")
    assert local == {"name": "lib", "op": None, "version": None, "releases": {},
                     "local": {"name": "lib", "packaged": True, "package": local["local"]["package"]}}
    assert local["local"]["package"]["name"] == "lib"  # its pkggen.yaml settings, for the packaging generators
    assert zlib["override"] == "zlib1g-dev" and zlib["releases"] == {}
    assert deps["runtime"][0]["override"] == "x11-xserver-utils"
    assert deps["development"] == []
    beta = json.loads((root / "pkggen-build" / "beta" / "app.json").read_text())["dependencies"]
    assert beta["libraries"][2]["override"] is False
    assert "override" not in beta["runtime"][0] and beta["runtime"][0]["releases"]

    err = strip_ansi(capsys.readouterr().err)
    assert err.count("Warning: an old answer") == 1
    assert "looked up 3 dependencies on repology" in err


def test_generate_without_dependencies_doesnt_look_anything_up(stub_generators, stub_dependencies, project):
    project({"repositories": REPOSITORIES, "group": packages({"name": "one"})})
    assert generate.generate() == 0
    assert stub_dependencies() == []


def test_a_failed_lookup_only_fails_packages_with_dependencies(stub_generators, stub_dependencies, project, capsys):
    root = project({"repositories": REPOSITORIES, "group": packages(
        {"name": "plain"}, {"name": "needy", "dependencies": {"build": ["fail-lookup"]}})})
    assert generate.generate() == 1
    assert (root / "pkggen-build" / "alpha" / "plain.json").exists()
    assert not (root / "pkggen-build" / "alpha" / "needy.json").exists()
    err = strip_ansi(capsys.readouterr().err)
    assert "couldn't look its dependencies up: repology is down" in err


def test_releases_come_from_the_package_then_the_test(stub_generators, stub_dependencies, project):
    (stub_generators / "distributions" / "distributions.yaml").write_text(textwrap.dedent("""\
        distributions:
          - name: alpha
            rp-names: [alpha_1, alpha_2, alpha_3]
          - name: beta
            rp-names: [beta_1]
    """))
    root = project({"repositories": REPOSITORIES,
                    "test": {"distributions": {"alpha": {"versions": ["alpha-2", "alpha-3"]}}},
                    "group": packages(
                        {"name": "own", "distributions": {"alpha": {"versions": ["alpha-1"]}}},
                        {"name": "tested", "dependencies": {"build": ["cmake"]}})})
    assert generate.generate() == 0
    job = lambda name: json.loads((root / "pkggen-build" / "alpha" / f"{name}.json").read_text())
    assert job("own")["releases"] == ["alpha_1"]
    assert job("tested")["releases"] == ["alpha_2", "alpha_3"]
    assert set(job("tested")["dependencies"]["build"][0]["releases"]) == {"alpha_2", "alpha_3"}


def test_target_names_of_shared_rp_names(stub_generators, stub_dependencies, project):
    """distributions.yaml's `targets`: one rp-name tested as several targets (Void's glibc and musl)."""
    (stub_generators / "distributions" / "distributions.yaml").write_text(textwrap.dedent("""\
        distributions:
          - name: alpha
            rp-names: [alpha_1, alpha_2]
            targets: { alpha_1: [alpha-glibc, alpha-musl] }
          - name: beta
            rp-names: [beta_1]
    """))
    root = project({"repositories": REPOSITORIES, "group": packages(
        {"name": "musl-only", "distributions": {"alpha": {"versions": ["alpha-musl"]}},
         "dependencies": {"build": ["cmake"]}})})
    assert generate.generate() == 0
    job = json.loads((root / "pkggen-build" / "alpha" / "musl-only.json").read_text())
    assert job["releases"] == ["alpha_1"]
    package = {"distributions": {"alpha": {"versions": ["alpha-musl"]}}}
    assert generate.tested_on(package, "alpha", "alpha-musl", "alpha_1")
    assert not generate.tested_on(package, "alpha", "alpha-glibc", "alpha_1")
    errors = generate.check_package({"name": "p", "distributions": {"alpha": {"versions": ["alpha-x"]}}},
                                    {"alpha": ["alpha_1", "alpha-glibc", "alpha-musl"]})
    assert errors and "known: alpha_1, alpha-glibc, alpha-musl" in errors[0]


def test_package_filters_still_know_every_local_package(stub_generators, stub_dependencies, project):
    root = project({"repositories": REPOSITORIES, "group": packages(
        {"name": "lib", "gentoo": {"category": "dev-libs"}},
        {"name": "app", "dependencies": {"libraries": ["lib"]}})})
    assert generate.generate(["app"]) == 0
    assert stub_dependencies() == []  # only a local package: nothing to look up
    local = json.loads((root / "pkggen-build" / "alpha" / "app.json").read_text())["dependencies"]["libraries"][0]
    assert local["local"]["name"] == "lib" and local["local"]["packaged"] is True
    assert local["local"]["package"]["gentoo"] == {"category": "dev-libs"}


def test_package_dependencies_add_to_the_groups(stub_generators):
    config = {"g": packages(
        {"name": "p", "dependencies": {"build": [{"name": "cmake", "alpha": False}, "go >= 1.25"],
                                       "runtime": ["xhost"]}},
        {"name": "q"},
        dependencies={"build": ["cmake", "pkgconf"], "libraries": ["glfw"]})}
    loaded = {p["name"]: p["dependencies"] for p, _ in generate.load_packages(config, None)}
    assert loaded["p"] == {"build": ["pkgconf", {"name": "cmake", "alpha": False}, "go >= 1.25"],
                           "libraries": ["glfw"], "runtime": ["xhost"]}
    assert loaded["q"] == {"build": ["cmake", "pkgconf"], "libraries": ["glfw"]}


def test_packages_that_need_nothing_from_repology_dont_wait_for_it(stub_generators, stub_dependencies, project):
    root = project({"repositories": REPOSITORIES, "group": packages(
        {"name": "lib"},
        {"name": "local-only", "dependencies": {"libraries": ["lib", {"name": "x", "alpha": "x", "beta": False}]}},
        {"name": "needy", "dependencies": {"build": ["fail-lookup"]}})})
    assert generate.generate() == 1
    assert (root / "pkggen-build" / "alpha" / "local-only.json").exists()
    assert not (root / "pkggen-build" / "alpha" / "needy.json").exists()


@pytest.mark.parametrize("test_block, fragment", [
    ({"distributions": {"alpha": {"versions": ["typo"]}}},
     "'test.distributions.alpha.versions' names unknown release(s): typo (known: a)"),
    ({"distributions": {"gamma": {"versions": ["g"]}}},
     "'test.distributions' names unknown distribution(s): gamma"),
    ({"distributions": {"alpha": {"versions": []}}},
     "'test.distributions.alpha.versions' must be a list of release names"),
])
def test_generate_validates_test_distribution_versions(stub_generators, project, test_block, fragment):
    project({"repositories": REPOSITORIES, "test": test_block, "group": packages({"name": "one"})})
    with pytest.raises(utils.GenericError) as info:
        generate.generate()
    assert fragment in info.value.args[0]


def test_dependencies_releases_does_not_fall_back_on_unknown_version():
    assert dependencies.releases(["typo"], ["a_1", "a_2"], None, generate.release_key) == []
