import json

import pytest
import yaml

import deploy
import utils
from tests.deployment.conftest import git, upstream, write_tree  # noqa: F401
from tests.support.helpers import run_cli, strip_ansi

IDENTITY = {"username": "Packager", "email": "packager@example.org"}
PACKAGES = {"generator": "url", "packages": [{"name": "foo"}, {"name": "bar"}]}


def result(dist, ok=True, packages=("foo",), output=True, **extra):
    entry = {"distribution": dist, "ok": ok, "finished": "2026-10-19T12:00:00+00:00", "output": None,
             "packages": {p: {"version": "1.0", "ok": ok} for p in packages}}
    entry.update(extra)
    return entry, output


@pytest.fixture
def project(tmp_path, monkeypatch):
    """project(deployment, results={target: result(...)}) -> the project folder."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(root))

    def make(deployment, results=None, **extra):
        config = {"repositories": {"rpm-based": {}, "arch": {}}, "group": PACKAGES, **extra}
        if deployment is not None:
            config["deployment"] = deployment
        (root / "pkggen.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        if results is not None:
            test_dir = root / "pkggen-build" / "test"
            test_dir.mkdir(parents=True, exist_ok=True)
            targets = {}
            for name, (entry, output) in results.items():
                if output:
                    entry["output"] = name
                    write_tree(test_dir / name, {f"RPMS/{name}.rpm": name})
                targets[name] = entry
            (test_dir / "results.json").write_text(json.dumps({"targets": targets}))
        return root
    return make


def errors_of(info):
    return info.value.args[0].split("\n  - ")[1:]


# --- Configuration -----------------------------------------------------------------------------


def test_no_deployment_block(project):
    project(None)
    with pytest.raises(utils.GenericError, match="Nothing to deploy: pkggen.yaml has no 'deployment' block"):
        deploy.deploy()


def test_config_errors_are_listed_together(project):
    project({"slackware": {}, "rpm-based": [{"generator": "nope"}, "text"], "arch": {"generator": "aur", "targets": "x"}})
    with pytest.raises(utils.GenericError) as info:
        deploy.deploy()
    errors = errors_of(info)
    assert errors[0].startswith("'deployment.slackware' isn't a distribution pkggen knows")
    assert errors[1].startswith("'deployment.rpm-based[0].generator' must name a deployment generator in ")
    assert errors[1].endswith("(available: aur, git)")
    assert errors[2] == "'deployment.rpm-based[1]' must be a mapping with a 'generator' key"
    assert errors[3] == "'deployment.arch.targets' must be a list of test target names like fedora-44"
    assert len(errors) == 4


def test_generator_checks_are_listed_before_anything_is_pushed(project, upstream):
    up = upstream()
    project({"rpm-based": [{"generator": "git", "url": up.url, "email": "x@y"}, {"generator": "git", **IDENTITY}],
             "arch": {"generator": "aur", **IDENTITY, "password": "p"}},
            results={"fedora-44": result("rpm-based"), "arch": result("arch")})
    with pytest.raises(utils.GenericError) as info:
        deploy.deploy()
    assert errors_of(info) == [
        "'deployment.rpm-based[0].username' is required",
        "'deployment.rpm-based[1].url' is required",
        "'deployment.arch.password' can't be set for AUR deployments: the AUR only accepts SSH keys",
    ]
    assert up.files() is None


def test_missing_results(project, upstream):
    project({"rpm-based": {"generator": "git", "url": upstream().url, **IDENTITY}})
    with pytest.raises(utils.GenericError, match=r"There's nothing tested to deploy yet .* run `pkggen test` first"):
        deploy.deploy()


def test_unknown_filters(project, upstream):
    project({"rpm-based": {"generator": "git", "url": upstream().url, **IDENTITY}},
            results={"fedora-44": result("rpm-based")})
    with pytest.raises(utils.GenericError, match=r"pkggen.yaml has no deployment for: gentoo \(deployed: rpm-based\)"):
        deploy.deploy(distribution_filter=["gentoo"])
    with pytest.raises(utils.GenericError, match=r"never tested: fedora-1 \(tested: fedora-44\)"):
        deploy.deploy(target_filter=["fedora-1"])
    with pytest.raises(utils.GenericError, match=r"unknown package\(s\) passed to --packages: baz"):
        deploy.deploy(package_filter=["baz"])


def test_targets_of_another_distribution(project, upstream):
    project({"rpm-based": {"generator": "git", "url": upstream().url, "targets": ["arch"], **IDENTITY}},
            results={"arch": result("arch")})
    with pytest.raises(utils.GenericError, match="'deployment.rpm-based.targets' has arch, which is a arch target"):
        deploy.deploy()


def test_missing_git(project, upstream, monkeypatch):
    project({"rpm-based": {"generator": "git", "url": upstream().url, **IDENTITY}},
            results={"fedora-44": result("rpm-based")})
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    with pytest.raises(utils.GenericError, match="pkggen deploy needs git"):
        deploy.deploy()


# --- Deploying -----------------------------------------------------------------------------


def test_success(project, upstream, capsys):
    up = upstream()
    root = project({"rpm-based": {"generator": "git", "url": up.url, **IDENTITY}},
                   results={"fedora-44": result("rpm-based"), "fedora-43": result("rpm-based"), "arch": result("arch")})
    assert deploy.deploy() == 0
    assert up.files() == {"fedora-43/RPMS/fedora-43.rpm": "fedora-43", "fedora-44/RPMS/fedora-44.rpm": "fedora-44"}
    assert (root / "pkggen-build" / "deploy" / "rpm-based-git" / ".git").is_dir()
    err = strip_ansi(capsys.readouterr().err)
    assert "Deploying rpm-based-git (fedora-43, fedora-44)" in err
    assert err.rstrip().endswith("Done: 1 pushed, 0 already up to date.")

    assert deploy.deploy() == 0
    assert strip_ansi(capsys.readouterr().err).rstrip().endswith("Done: 0 pushed, 1 already up to date.")


def test_the_test_gate(project, upstream, capsys):
    up = upstream()
    project({"rpm-based": {"generator": "git", "url": up.url, **IDENTITY}},
            results={"fedora-44": result("rpm-based"),
                     "fedora-43": result("rpm-based", ok=False, failed=["build failed: 'build foo' exited with code 1"]),
                     "fedora-rawhide": result("rpm-based", ok=False, output=False,
                                              skipped="it has no arm64 container image")})
    assert deploy.deploy() == 1
    assert up.files() == {"fedora-44/RPMS/fedora-44.rpm": "fedora-44"}
    err = strip_ansi(capsys.readouterr().err)
    assert "Finished with 2 problems: 1 pushed, 0 already up to date." in err
    assert ("- [rpm-based-git] fedora-rawhide: skipped: its last test was skipped: it has no arm64 container image. "
            "Test it on a machine with that architecture, or with a remote 'test.connection'") in err
    assert ("- [rpm-based-git] fedora-43: skipped: its last test failed (build failed: 'build foo' exited with "
            "code 1) (--skip-tests deploys it anyway)") in err

    # --skip-tests deploys the failed target; there's still nothing to deploy for rawhide
    assert deploy.deploy(skip_tests=True) == 1
    assert "fedora-43/RPMS/fedora-43.rpm" in up.files()


def test_configured_targets(project, upstream, capsys):
    up = upstream()
    project({"rpm-based": {"generator": "git", "url": up.url, "targets": ["fedora-44", "opensuse-tumbleweed"],
                           **IDENTITY}},
            results={"fedora-44": result("rpm-based"), "fedora-43": result("rpm-based")})
    assert deploy.deploy() == 1
    assert up.files() == {"fedora-44/RPMS/fedora-44.rpm": "fedora-44"}
    assert ("- [rpm-based-git] opensuse-tumbleweed: skipped: it hasn't been tested yet: run "
            "`pkggen test -t opensuse-tumbleweed`") in strip_ansi(capsys.readouterr().err)


def test_filters(project, upstream, capsys):
    rpms, mirror = upstream("rpms.git"), upstream("mirror.git")
    project({"rpm-based": [{"generator": "git", "url": rpms.url, **IDENTITY},
                           {"generator": "git", "url": mirror.url, "path": "", **IDENTITY}]},
            results={"fedora-44": result("rpm-based")})
    # -t keeps only that target; both deployments use it
    assert deploy.deploy(target_filter=["fedora-44"]) == 0
    assert rpms.files() == {"fedora-44/RPMS/fedora-44.rpm": "fedora-44"}
    assert mirror.files() == {"RPMS/fedora-44.rpm": "fedora-44"}
    err = strip_ansi(capsys.readouterr().err)
    assert "Deploying rpm-based-git (fedora-44)" in err and "Deploying rpm-based-git-2 (fedora-44)" in err


def test_executables_from_the_results_reach_the_upstream(project, upstream, monkeypatch):
    # Git for Windows can't see executable bits; the list pkggen test recorded can
    for key, value in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.filemode", "GIT_CONFIG_VALUE_0": "false"}.items():
        monkeypatch.setenv(key, value)
    up = upstream()
    project({"rpm-based": {"generator": "git", "url": up.url, **IDENTITY}},
            results={"fedora-44": result("rpm-based", executables=["RPMS/fedora-44.rpm"])})
    assert deploy.deploy() == 0
    assert git("--git-dir", up.url, "ls-tree", "main", "fedora-44/RPMS/fedora-44.rpm").split()[0] == "100755"


def test_dry_run(project, upstream, capsys):
    up = upstream()
    project({"rpm-based": {"generator": "git", "url": up.url, **IDENTITY}}, results={"fedora-44": result("rpm-based")})
    assert deploy.deploy(dry_run=True) == 0
    assert up.files() is None
    err = strip_ansi(capsys.readouterr().err)
    assert "Deploying rpm-based-git (fedora-44) as a dry run" in err
    assert "Done: 1 would be pushed (dry run), 0 already up to date." in err


def test_a_failing_deployment_doesnt_stop_the_others(project, upstream, tmp_path, monkeypatch, capsys):
    up = upstream()
    monkeypatch.setenv("PKGGEN_TEST_AUR_URL", str(tmp_path / "nowhere" / "{pkgbase}.git"))
    root = project({"arch": {"generator": "aur", **IDENTITY},
                    "rpm-based": {"generator": "git", "url": str(tmp_path / "missing.git"), **IDENTITY}},
                   results={"fedora-44": result("rpm-based"), "arch": result("arch")})
    write_tree(root / "pkggen-build" / "test" / "arch", {"foo/PKGBUILD": "x", "foo/.SRCINFO": "pkgbase = foo\npkgver = 1"})
    assert deploy.deploy() == 1
    err = strip_ansi(capsys.readouterr().err)
    assert "Finished with 2 problems: 0 pushed, 0 already up to date." in err
    assert "✗ [arch-aur] foo: git fetch failed: " in err
    assert "✗ [rpm-based-git]: git fetch failed: " in err
    assert up.files() is None


def test_cli(project, upstream):
    up = upstream()
    project({"rpm-based": {"generator": "git", "url": up.url, **IDENTITY}}, results={"fedora-44": result("rpm-based")})
    result_ = run_cli("deploy", "-d", "rpm-based", "-t", "fedora-44", "--dry-run")
    assert result_.code == 0, result_.err
    assert "Done: 1 would be pushed (dry run)" in result_.err_plain
    result_ = run_cli("deploy")
    assert result_.code == 0, result_.err
    assert up.files() == {"fedora-44/RPMS/fedora-44.rpm": "fedora-44"}
