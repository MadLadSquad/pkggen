import json
import os
import textwrap

import pytest

import containers
import generate
import testing
import utils
from tests.core.test_generate import STUB_FETCH, STUB_PACKAGING, packages  # noqa: F401
from tests.support.helpers import run_cli, strip_ansi


def native(path):
    """pkggen prints paths with the host's separator."""
    return path.replace("/", os.sep)


STUB_TESTING = textwrap.dedent("""\
    import json, sys
    job = json.loads(sys.stdin.read())
    def step(name, run):
        return {"name": name, "run": run, "user": None, "workdir": None}
    targets, skipped, warnings = [], [], ["packages: %d" % len(job["packages"])]
    for rp in job["versions"] or job["rp_names"]:
        if rp == "bad":
            print("\\x1b[31mError: bad version\\x1b[0m", file=sys.stderr)
            sys.exit(1)
        if rp == "a2" and job["host_arch"] == "arm64":
            skipped.append({"name": job["distribution"] + "-" + rp, "rp_name": rp})
            warnings.append(job["distribution"] + "-" + rp + " has no arm64 container image, so it's skipped")
            continue
        built = []
        for entry in job["packages"]:
            c = entry["context"]
            fail = "FAKE_FAIL " if c.get("fail_build") == rp else ""
            built.append({"name": c["name"],
                          "build": [step("build " + c["name"], fail + "build " + c["name"] + " from " + entry["output"])],
                          "install": [step("install " + c["name"], "install " + c["name"])]})
        targets.append({
            "name": job["distribution"] + "-" + rp, "rp_name": rp, "image": "example.org/" + rp + ":1",
            "env": {"TARGET": rp, "MAKEFLAGS": "-j99"}, "repo": "/pkggen/repo",
            "setup": [step("tools", "install tools on " + job["host_arch"])] + job["test"]["setup"],
            "prepare": [step("prepare", "prepare")], "packages": built,
            "finalize": [step("collect", "cp -rL /pkggen/repo /pkggen/out")],
            "post_setup": job["test"]["post-setup"], "collect": "/pkggen/out",
        })
    print(json.dumps({"targets": targets, "skipped": skipped, "warnings": warnings}))
""")


@pytest.fixture
def generators(tmp_path, monkeypatch):
    root = tmp_path / "generators"
    (root / "distributions").mkdir(parents=True)
    (root / "distributions" / "distributions.yaml").write_text(textwrap.dedent("""\
        distributions:
          - name: alpha
            rp-names: [a1, a2]
          - name: beta
            rp-names: [b1]
          - name: notesting
            rp-names: [c1]
    """))
    for layer, script, dists in (("generation", STUB_FETCH, ["stub"]), ("packaging", STUB_PACKAGING, ["alpha", "beta", "notesting"]),
                                 ("testing", STUB_TESTING, ["alpha", "beta"])):
        (root / layer).mkdir()
        (root / layer / "lib.py").write_text("")
        for name in dists:
            (root / layer / f"{name}.py").write_text(script)
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(root))
    return root


@pytest.fixture
def project(tmp_path, monkeypatch, fake_engine):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(root))
    monkeypatch.setattr(containers, "host_platform", lambda: "linux")

    def make(test=None, repos=("alpha", "beta"), extra=None, **groups):
        import yaml
        config = {"repositories": {"alpha": {"key": "value"}, "beta": None},
                  "group": packages({"name": "one"}, {"name": "two"})}
        config.update(groups)
        if test is not None:
            config["test"] = test
        config.update(extra or {})
        (root / "pkggen.yaml").write_text(yaml.safe_dump(config))
        for dist in repos:
            (root / "pkggen-build" / dist).mkdir(parents=True, exist_ok=True)
        return root
    return make


def container_scripts(fake_engine, target):
    return [script for name, script in fake_engine.scripts() if name.endswith("-" + target)]


# --- load_test_config ----------------------------------------------------------------


def test_config_defaults():
    settings = testing.load_test_config({}, ["alpha", "beta"])
    assert settings == {"engine": None, "connection": None, "cpus": None, "memory": None, "jobs": None,
                        "disk": None, "pull": None,
                        "distributions": {d: {"versions": None, "setup": [], "post-setup": []} for d in ("alpha", "beta")}}
    assert testing.load_test_config({"test": None}, ["alpha"])["distributions"]["alpha"]["setup"] == []


def test_config_values():
    settings = testing.load_test_config({"test": {
        "engine": "docker", "connection": "box", "cpus": 6, "memory": "12G", "jobs": 2, "vm": {"disk": "80G"},
        "pull": "always",
        "setup": ["apt-get update\napt-get install -y foo", {"name": "as user", "run": "id", "user": "me", "workdir": "/w"}],
        "post-setup": ["foo --version"],
        "distributions": {"alpha": {"versions": ["a2"], "setup": ["alpha only"], "post-setup": [{"run": "check"}]}},
    }}, ["alpha", "beta"])
    assert (settings["engine"], settings["connection"], settings["cpus"], settings["memory"], settings["jobs"],
            settings["disk"], settings["pull"]) == ("docker", "box", 6, 12288, 2, 80, "always")
    alpha = settings["distributions"]["alpha"]
    assert alpha["versions"] == ["a2"]
    assert alpha["setup"] == [
        {"name": "apt-get update", "run": "apt-get update\napt-get install -y foo", "user": None, "workdir": None},
        {"name": "as user", "run": "id", "user": "me", "workdir": "/w"},
        {"name": "alpha only", "run": "alpha only", "user": None, "workdir": None},
    ]
    assert [s["name"] for s in alpha["post-setup"]] == ["foo --version", "check"]
    assert [s["name"] for s in settings["distributions"]["beta"]["setup"]] == ["apt-get update", "as user"]


def test_config_errors():
    with pytest.raises(utils.GenericError) as info:
        testing.load_test_config({"test": {
            "engine": "lxc", "connection": "", "cpus": -1, "memory": "lots", "jobs": 0, "vm": {"cpus": 2, "disk": "x"},
            "pull": "sometimes", "setup": "not a list", "post-setup": [3, {"run": "x", "shell": "bash", "user": 1}],
            "typo": True,
            "distributions": {"gamma": {}, "alpha": {"versions": [], "extra": 1}, "beta": "text"},
        }}, ["alpha", "beta"])
    message = info.value.args[0]
    for fragment in [
        "'test' has unknown keys: typo",
        "'test.engine' must be one of podman, docker",
        "'test.connection' must be the name of a podman connection or docker context",
        "'test.cpus' must be a positive number",
        "'test.memory' must be a size like 16G or 512M, got 'lots'",
        "'test.vm.disk' must be a size like 16G or 512M, got 'x'",
        "'test.jobs' must be a whole number of at least 1",
        "'test.vm' has unknown keys: cpus",
        "'test.pull' must be one of always, missing, never, newer",
        "'test.setup' must be a list of commands",
        "'test.post-setup[0]' must be a command, or a mapping with a 'run' command",
        "'test.post-setup[1]' has unknown keys: shell",
        "'test.post-setup[1].user' must be a string",
        "'test.distributions.gamma' isn't a distribution pkggen can test (available: alpha, beta)",
        "'test.distributions.alpha' has unknown keys: extra",
        "'test.distributions.alpha.versions' must be a list of release names",
        "'test.distributions.beta' must be a mapping",
    ]:
        assert fragment in message
    assert message.count("\n  - ") == 17


def test_config_must_be_a_mapping():
    with pytest.raises(utils.GenericError, match="'test' must be a mapping"):
        testing.load_test_config({"test": ["x"]}, ["alpha"])


# --- pkggen test -------------------------------------------------------------------------


def test_success(generators, project, fake_engine, capsys):
    root = project(test={"setup": ["user setup"], "post-setup": ["user check"]})
    assert testing.test() == 0

    # One container per target, all removed afterwards
    started = fake_engine.calls("run")
    # Named pkggen-<session>-<target>
    assert sorted(c[c.index("--name") + 1].split("-", 2)[2] for c in started) == ["alpha-a1", "alpha-a2", "beta-b1"]
    assert fake_engine.containers() == []

    # The 8 CPUs and half of the 16 GiB of the fake host are split between 3 containers
    run = next(c for c in started if c[-2] == "example.org/a1:1")
    assert run[run.index("--cpus") + 1] == "2.67" and run[run.index("--memory") + 1] == "2730m"
    env = [run[i + 1] for i, arg in enumerate(run) if arg == "-e"]
    assert "TARGET=a1" in env and "PKGGEN_JOBS=2" in env and "MAKEFLAGS=-j99" in env  # targets override the defaults
    assert "DEB_BUILD_OPTIONS=parallel=2" in env

    assert container_scripts(fake_engine, "a1") == [
        "install tools on amd64", "user setup", "prepare",
        "build one from one.json", "install one", "build two from two.json", "install two",
        "cp -rL /pkggen/repo /pkggen/out", "user check"]

    # The finished repository and the log of every target
    output = root / "pkggen-build" / "test"
    assert json.loads((output / "alpha-a1" / "one.json").read_text())["package"]["version"] == "1.0"
    assert sorted(p.name for p in output.iterdir()) == ["alpha-a1", "alpha-a1.log", "alpha-a2", "alpha-a2.log",
                                                        "beta-b1", "beta-b1.log", "results.json"]
    results = json.loads((output / "results.json").read_text())["targets"]
    assert sorted(results) == ["alpha-a1", "alpha-a2", "beta-b1"]
    assert results["alpha-a1"]["ok"] is True and results["alpha-a1"]["distribution"] == "alpha"
    assert results["alpha-a1"]["output"] == "alpha-a1"
    assert results["alpha-a1"]["packages"] == {"one": {"version": "1.0", "ok": True}, "two": {"version": "1.0", "ok": True}}
    log =(output / "alpha-a1.log").read_text()
    assert "==> setup: tools\n+ install tools on amd64" in log
    assert "==> build: build one\n+ build one from one.json" in log

    err = strip_ansi(capsys.readouterr().err)
    assert "Testing 3 targets with podman, 3 at a time (2.67 CPUs and 2730 MiB RAM each): alpha-a1, alpha-a2, beta-b1" in err
    assert "Warning: alpha: packages: 0" in err and "Warning: alpha: packages: 2" in err
    assert "✓ [alpha-a1] one built and installed" in err
    assert f"✓ [beta-b1] passed: 2 packages built and installed → {native('pkggen-build/test/beta-b1')}" in err
    assert "Done: 2/2 packages fetched, 4/4 distribution packages generated (alpha, beta)." in err
    assert err.rstrip().endswith("Done: 3/3 targets passed, 6/6 package builds succeeded.")


def test_package_failure_doesnt_stop_the_others(generators, project, fake_engine, capsys):
    root = project(test={"post-setup": ["user check"]},
                   group=packages({"name": "one", "fail_build": "a2"}, {"name": "two"}))
    assert testing.test() == 1
    # a2 still builds two, indexes the repository and is collected, but skips post-setup
    assert container_scripts(fake_engine, "a2") == [
        "install tools on amd64", "prepare", "FAKE_FAIL build one from one.json", "build two from two.json",
        "install two", "cp -rL /pkggen/repo /pkggen/out"]
    assert (root / "pkggen-build" / "test" / "alpha-a2" / "two.json").exists()
    assert container_scripts(fake_engine, "a1")[-1] == "user check"

    err = strip_ansi(capsys.readouterr().err)
    assert "✗ [alpha-a2] one failed to build" in err
    assert "Finished with 1 problem: 2/3 targets passed, 5/6 package builds succeeded." in err
    assert "✗ [alpha-a2] one: build failed: 'build one' exited with code 1" in err
    assert "fake failure" in err
    assert f"log: {native('pkggen-build/test/alpha-a2.log')}" in err


def read_results(root):
    return json.loads((root / "pkggen-build" / "test" / "results.json").read_text())["targets"]


def test_results_record_failed_packages_and_merge_runs(generators, project, fake_engine):
    root = project(group=packages({"name": "one", "fail_build": "a2"}, {"name": "two"}))
    assert testing.test() == 1
    a2 = read_results(root)["alpha-a2"]
    assert a2["ok"] is False and a2["output"] == "alpha-a2"
    assert a2["packages"]["one"]["ok"] is False and a2["packages"]["two"]["ok"] is True
    assert a2["failed"][0].startswith("build failed: 'build one' exited with code 1")

    # A later run of one target only replaces that target's entry
    (root / "pkggen.yaml").write_text((root / "pkggen.yaml").read_text().replace("fail_build: a2", "fail_build: none"))
    assert testing.test(target_filter=["a2"]) == 0
    results = read_results(root)
    assert sorted(results) == ["alpha-a1", "alpha-a2", "beta-b1"]
    assert results["alpha-a2"]["ok"] is True and results["alpha-a2"]["packages"]["one"]["ok"] is True


def test_results_record_releases_this_host_cant_run(generators, project, fake_engine, capsys):
    root = project()
    assert testing.test() == 0
    assert read_results(root)["alpha-a2"]["ok"] is True
    fake_engine.configure(info={"host": {"arch": "arm64", "cpus": 8, "memTotal": 16 * 1024 ** 3,
                                         "cgroupControllers": ["cpu", "memory"], "os": "linux"}})
    # The older result for a2 must not stay deployable
    with pytest.raises(utils.GenericError, match="no target can run on this machine"):
        testing.test(target_filter=["a2"])
    a2 = read_results(root)["alpha-a2"]
    assert a2["ok"] is False and a2["skipped"] == "it has no arm64 container image" and a2["output"] is None
    assert testing.test() == 0
    assert read_results(root)["alpha-a1"]["ok"] is True


def test_setup_failure(generators, project, fake_engine, capsys):
    project()
    fake_engine.configure(fail_images=["example.org/b1:1"])
    assert testing.test() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert f"✗ [beta-b1] failed, see {native('pkggen-build/test/beta-b1.log')}" in err
    assert "[beta-b1]: `podman run -d` failed with exit code 125: Error: pulling example.org/b1:1" in err
    assert "2/3 targets passed, 4/4 package builds succeeded" in err
    assert fake_engine.containers() == []


def test_filters(generators, project, fake_engine, capsys):
    root = project(test={"distributions": {"alpha": {"versions": ["a2"]}}})
    assert testing.test(["two"], ["alpha"], None) == 0
    assert [c[-2] for c in fake_engine.calls("run")] == ["example.org/a2:1"]
    assert container_scripts(fake_engine, "a2")[2:4] == ["build two from two.json", "install two"]
    # Only alpha was packaged
    assert not (root / "pkggen-build" / "beta" / "two.json").exists()


def test_target_filter(generators, project, fake_engine, capsys):
    project()
    assert testing.test(target_filter=["b1", "alpha-a1"]) == 0
    assert sorted(c[-2] for c in fake_engine.calls("run")) == ["example.org/a1:1", "example.org/b1:1"]
    assert "Testing 2 targets" in strip_ansi(capsys.readouterr().err)
    assert testing.test(target_filter=["b1"]) == 0
    # Only beta is packaged and tested
    assert "packaging for beta\n" in strip_ansi(capsys.readouterr().err)
    with pytest.raises(utils.GenericError, match=r"Unknown test target\(s\): zz \(available: alpha-a1, alpha-a2, beta-b1\)"):
        testing.test(target_filter=["zz"])


def test_target_filter_with_targets_this_host_cant_run(generators, project, fake_engine, capsys):
    project()
    fake_engine.configure(info={"host": {"arch": "arm64", "cpus": 8, "memTotal": 16 * 1024 ** 3,
                                         "cgroupControllers": ["cpu", "memory"], "os": "linux"}})
    # A release without an image for this host is known, so it's skipped with a warning, not an error
    assert testing.test(target_filter=["alpha-a2", "b1"]) == 0
    err = strip_ansi(capsys.readouterr().err)
    assert "Warning: alpha: alpha-a2 has no arm64 container image, so it's skipped" in err
    assert "Testing 1 target" in err
    assert [c[-2] for c in fake_engine.calls("run")] == ["example.org/b1:1"]
    with pytest.raises(utils.GenericError, match="Nothing to test: no target can run on this machine"):
        testing.test(target_filter=["a2"])
    assert "alpha-a2 has no arm64 container image" in strip_ansi(capsys.readouterr().err)
    with pytest.raises(utils.GenericError, match=r"Unknown test target\(s\): zz \(available: alpha-a1, beta-b1\)"):
        testing.test(target_filter=["zz", "a2"])


@pytest.mark.parametrize("dists, message", [
    (["gamma"], r"No testing generator for: gamma \(available: alpha, beta\)"),
    (["notesting"], "No testing generator for: notesting"),
])
def test_unknown_distribution(generators, project, dists, message):
    project()
    with pytest.raises(utils.GenericError, match=message):
        testing.test(distribution_filter=dists)


def test_distribution_without_repository_block(generators, project):
    project(extra={"repositories": {"alpha": {}}})
    with pytest.raises(utils.GenericError, match="no 'repositories.beta' block"):
        testing.test(distribution_filter=["beta"])


def test_nothing_to_test(generators, project):
    project(extra={"repositories": {"notesting": {}}})
    with pytest.raises(utils.GenericError, match="Nothing to test: pkggen.yaml has no 'repositories' block"):
        testing.test()


def test_testing_generator_errors(generators, project):
    project(test={"distributions": {"beta": {"versions": ["bad"]}}})
    with pytest.raises(utils.GenericError, match="The beta testing generator failed: Error: bad version"):
        testing.test()


def test_invalid_packages_are_reported_before_any_container_starts(generators, project, fake_engine):
    project(group={"generator": "stub", "packages": [{"nameless": True}]})
    with pytest.raises(utils.GenericError, match=r"'group.packages\[0\]' needs a 'name'"):
        testing.test()
    assert fake_engine.raw_calls() == []


def test_missing_repository_skips_its_targets(generators, project, fake_engine, capsys):
    project(repos=("alpha",))
    assert testing.test() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "- [beta-b1] skipped: the beta repository wasn't generated" in err
    assert "Run `pkggen gen-repo beta` first" in err
    assert "2/3 targets passed" in err
    assert fake_engine.containers() == []


def test_keep(generators, project, fake_engine, capsys):
    project()
    assert testing.test(target_filter=["a1"], keep=True) == 0
    assert len(fake_engine.containers()) == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "Containers were kept: remove them with `podman rm -f -v $(podman ps -aq --filter label=pkggen.session=" in err


def test_jobs(generators, project, fake_engine, capsys):
    project(test={"jobs": 2, "cpus": 4, "memory": "4G"})
    assert testing.test(jobs=1) == 0
    assert "1 at a time (4 CPUs and 4096 MiB RAM each)" in strip_ansi(capsys.readouterr().err)


def test_interrupt_removes_every_container(generators, project, fake_engine, monkeypatch):
    project()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(generate, "run_pipeline", interrupted)
    with pytest.raises(KeyboardInterrupt):
        testing.test()
    assert fake_engine.containers() == []
    assert fake_engine.calls("ps")


def test_package_entries_paths_are_relative_to_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    repo = tmp_path / "custom" / "gentoo"
    pipeline = generate.PipelineResult(
        packages=[({"name": "a"}, "x"), ({"name": "b"}, "x"), ({"name": "c"}, "x")],
        targets=[generate.Target("gentoo", "x", str(repo), {})],
        contexts={"a": {"name": "a"}, "b": {"name": "b"}, "c": {"name": "c"}},
        packaged={("gentoo", "a"): {"output": "custom/gentoo/dev-libs/a"},
                  ("gentoo", "c"): {"output": str(repo / "app-misc" / "c")}},
        failures=[], stats={})
    assert testing.package_entries("gentoo", pipeline) == [
        {"context": {"name": "a"}, "output": "dev-libs/a"}, {"context": {"name": "c"}, "output": "app-misc/c"}]
    assert testing.package_entries("arch", pipeline) is None


def test_cli(generators, project, fake_engine):
    project()
    result = run_cli("test", "-t", "a1", "-j", "1", env={"CONTAINER_HOST": "unix:///fake.sock"})
    assert result.code == 0, result.err
    assert "Done: 1/1 targets passed, 2/2 package builds succeeded." in result.err_plain


def test_cli_rejects_bad_jobs():
    result = run_cli("test", "-j", "0")
    assert result.code == 2
    assert "must be a whole number of at least 1" in result.err


def test_malformed_target_only_fails_itself(generators, project, fake_engine, capsys, monkeypatch):
    project()
    real = testing.describe

    def broken(dist, data):
        result = real(dist, data)
        if dist == "beta" and data["packages"]:
            del result["targets"][0]["packages"][0]["name"]
        return result

    monkeypatch.setattr(testing, "describe", broken)
    assert testing.test() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "[beta-b1]: unexpected KeyError: 'name'" in err
    assert "2/3 targets passed" in err
    assert fake_engine.containers() == []
