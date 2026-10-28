"""The deployment hooks (generators/deployment/hooks/<distribution>.py) and how lib loads them."""
import pytest
import yaml

from tests.support import paths
from tests.support.helpers import run_generator

DISTRIBUTIONS = [d["name"] for d in yaml.safe_load(
    (paths.GENERATORS / "distributions" / "distributions.yaml").read_text())["distributions"]]
HOOKS = paths.GENERATORS / "deployment" / "hooks"


def test_every_distribution_has_a_hook():
    assert sorted(p.stem for p in HOOKS.glob("*.py")) == sorted(DISTRIBUTIONS)


@pytest.mark.parametrize("distribution", DISTRIBUTIONS)
def test_hooks_define_the_api(deploy_lib, distribution):
    hook = deploy_lib.load_hook(distribution)
    assert hook.SIGNING in (None, "gpg", "rsa")
    assert set(hook.OPTIONS) <= set(deploy_lib.HOOK_OPTIONS)
    assert callable(hook.check) and callable(hook.sign)
    # Loaded once
    assert deploy_lib.load_hook(distribution) is hook


@pytest.mark.parametrize("name", ["nope", "../lib", "hooks/debian", ""])
def test_unknown_hooks(deploy_lib, name):
    with pytest.raises(deploy_lib.DeployError, match="there's no deployment hook for the distribution"):
        deploy_lib.load_hook(name)


def test_hooks_use_the_lib_that_loaded_them(deploy_lib):
    # The tests load lib under another name than `lib`: the hook still gets this one
    assert deploy_lib.load_hook("alpine").lib is deploy_lib


def test_hook_without_the_api(deploy_lib, monkeypatch, tmp_path):
    (tmp_path / "broken.py").write_text("SIGNING = 'gpg'\n")
    monkeypatch.setattr(deploy_lib, "HOOKS_PATH", str(tmp_path))
    with pytest.raises(deploy_lib.DeployError, match="doesn't define OPTIONS, check, sign"):
        deploy_lib.load_hook("broken")


@pytest.mark.parametrize("distribution, deployment, message", [
    ("gentoo", {"gpg-key": "k"}, "'deployment.dist.gpg-key' doesn't apply to gentoo repositories: pkggen doesn't "
                                 "sign them"),
    ("arch", {"rsa-key": "k"}, "'deployment.dist.rsa-key' doesn't apply to arch repositories"),
    ("debian", {"rsa-key": "k"}, "they're signed with 'deployment.dist.gpg-key'"),
    ("rpm-based", {"gpg-key": "k", "key-name": "a.rsa.pub"},
     "'deployment.dist.key-name' doesn't apply to rpm-based repositories"),
    ("alpine", {"rsa-key": "k", "signed-by": "Me"}, "'deployment.dist.signed-by' doesn't apply to alpine"),
    ("debian", {"gpg-passphrase": "p"}, "'deployment.dist.gpg-passphrase' needs 'deployment.dist.gpg-key'"),
    ("gentoo", {"public-key": "k.pub"}, "'deployment.dist.public-key' doesn't apply to gentoo repositories"),
])
def test_checks_follow_the_hook(deploy_lib, make_job, distribution, deployment, message):
    errors = []
    deploy_lib.check_signing(make_job(_generator(deploy_lib), deployment, distribution=distribution), errors)
    assert any(message in e for e in errors), errors


def test_generators_load_the_hook_of_their_distribution(tmp_path):
    job = {"check": True, "id": "x", "distribution": "nope", "deployment": {"generator": "git"},
           "run_path": str(tmp_path), "targets": []}
    result = run_generator("deployment", "git", job)
    assert result.code == 1
    assert "there's no deployment hook for the distribution 'nope'" in result.err_plain


def _generator(deploy_lib):
    class Generator:
        lib = deploy_lib
    return Generator
