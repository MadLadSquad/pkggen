import os

import pytest

import utils
from tests.support import paths


def fake_generators(path):
    (path / "distributions").mkdir(parents=True)
    (path / "distributions" / "distributions.yaml").write_text("distributions: []\n")
    return str(path)


def test_generators_path_defaults_to_the_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    monkeypatch.chdir(tmp_path)
    assert os.path.normcase(utils.get_generators_path()) == os.path.normcase(str(paths.GENERATORS))


def test_generators_path_prefers_the_bundled_copy(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    bundled, checkout = fake_generators(tmp_path / "bundled"), fake_generators(tmp_path / "checkout")
    monkeypatch.setattr(utils, "BUNDLED_GENERATORS", (bundled, checkout))
    assert utils.get_generators_path() == bundled
    monkeypatch.setattr(utils, "BUNDLED_GENERATORS", (str(tmp_path / "missing"), checkout))
    assert utils.get_generators_path() == checkout


def test_generators_path_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    monkeypatch.setattr(utils, "BUNDLED_GENERATORS", (str(tmp_path / "missing"),))
    monkeypatch.chdir(tmp_path)
    assert utils.get_generators_path() == str(tmp_path)


def test_generators_path_is_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", "gens")
    assert utils.get_generators_path() == str(tmp_path / "gens")


def test_run_path_defaults_to_cwd_and_honours_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert utils.get_run_path() == str(tmp_path)
    monkeypatch.setenv("PKGGEN_RUN_PATH", "sub")
    assert utils.get_run_path() == str(tmp_path / "sub")


def test_load_distributions_reads_the_real_list():
    names = [dist["name"] for dist in utils.load_distributions()]
    assert set(names) >= {"arch", "debian", "gentoo", "rpm-based"}
    for dist in utils.load_distributions():
        assert dist["rp-names"], f"{dist['name']} has no repology names"


def write_distributions(root, content):
    (root / "distributions").mkdir(parents=True, exist_ok=True)
    (root / "distributions" / "distributions.yaml").write_text(content, encoding="utf-8")


def test_load_distributions_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    with pytest.raises(utils.GenericError, match="Is PKGGEN_GENERATORS_PATH set"):
        utils.load_distributions()


def test_load_distributions_invalid_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, "distributions: [\n")
    with pytest.raises(utils.GenericError, match="Couldn't parse"):
        utils.load_distributions()


def test_load_distributions_entry_without_name(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, "distributions:\n  - rp-names: [x]\n")
    with pytest.raises(utils.GenericError, match="needs a 'name' key"):
        utils.load_distributions()


def test_load_distributions_empty_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, "")
    assert utils.load_distributions() == []


def test_get_pkggen_config(example_project, example_config):
    assert utils.get_pkggen_config() == example_config


def test_get_pkggen_config_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    with pytest.raises(utils.GenericError, match="set PKGGEN_RUN_PATH"):
        utils.get_pkggen_config()


def test_get_pkggen_config_invalid_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    (tmp_path / "pkggen.yaml").write_text("a: [\n", encoding="utf-8")
    with pytest.raises(utils.GenericError, match="Couldn't parse"):
        utils.get_pkggen_config()


@pytest.mark.parametrize("config", [None, {}, {"repositories": None}, {"repositories": {"arch": None}},
                                    {"repositories": {"arch": {}}}])
def test_get_repo_path_default(monkeypatch, tmp_path, config):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    assert utils.get_repo_path("arch", config) == str(tmp_path / "pkggen-build" / "arch")


def test_get_repo_path_relative_to_pkggen_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    config = {"repositories": {"gentoo": {"path": "../overlay"}}}
    assert utils.get_repo_path("gentoo", config) == str(tmp_path.parent / "overlay")


def test_get_repo_path_absolute_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    absolute = tmp_path / "elsewhere"
    assert utils.get_repo_path("arch", {"repositories": {"arch": {"path": str(absolute)}}}) == str(absolute)
    home = utils.get_repo_path("arch", {"repositories": {"arch": {"path": "~/repo"}}})
    assert home == os.path.abspath(os.path.expanduser("~/repo"))


def test_display_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path / "project"))
    inside = tmp_path / "project" / "pkggen-build" / "arch"
    outside = tmp_path / "other"
    assert utils.display_path(str(inside)) == os.path.join("pkggen-build", "arch")
    assert utils.display_path(str(outside)) == str(outside)


def test_create_secrets_file(isolated_env):
    path = utils.create_secrets_file()
    assert path == str(isolated_env / "pkggen" / "secrets.yaml")
    assert open(path).read() == ""


def test_create_secrets_file_keeps_existing_content(secrets):
    path = secrets({"github_key": "abc"})
    assert utils.create_secrets_file() == str(path)
    assert "abc" in path.read_text()


def test_create_secrets_file_uses_appdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(utils.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert utils.create_secrets_file() == str(tmp_path / "AppData" / "pkggen" / "secrets.yaml")


def test_generic_error_keeps_plain_message():
    error = utils.GenericError("something broke")
    assert error.args == ("something broke",)
    assert "something broke" in str(error)


def test_default_paths_are_this_repository():
    assert utils.get_generators_path() == str(paths.GENERATORS)
