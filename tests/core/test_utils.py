import os

import pytest

from pkggen import utils
from tests.support import paths


def fake_generators(path):
    (path / "distributions").mkdir(parents=True)
    (path / "distributions" / "distributions.yaml").write_text("distributions: []\n")
    return str(path)


def same(a, b):
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def test_generators_path_defaults_to_the_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    monkeypatch.chdir(tmp_path)
    assert same(utils.builtin_generators_dir(), paths.GENERATORS)
    # The user's and system's folders of the test environment don't exist: only the built-in one is left
    assert [os.path.normcase(d) for d in utils.generator_dirs()] == [os.path.normcase(str(paths.GENERATORS))]


def test_generators_path_prefers_the_bundled_copy(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    bundled, checkout = fake_generators(tmp_path / "bundled"), fake_generators(tmp_path / "checkout")
    share = str(tmp_path / "share")
    monkeypatch.setattr(utils, "INSTALLED_GENERATORS", (bundled, checkout, share))
    assert utils.builtin_generators_dir() == bundled
    monkeypatch.setattr(utils, "INSTALLED_GENERATORS", (str(tmp_path / "missing"), checkout, share))
    assert utils.builtin_generators_dir() == checkout
    # Distribution packages may move them to <prefix>/share/pkggen/generators
    monkeypatch.setattr(utils, "INSTALLED_GENERATORS", (str(tmp_path / "missing"), str(tmp_path / "gone"),
                                                        fake_generators(tmp_path / "share")))
    assert utils.builtin_generators_dir() == share
    assert utils.generator_dirs() == [share]


def test_generators_path_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    monkeypatch.setattr(utils, "INSTALLED_GENERATORS", (str(tmp_path / "missing"),))
    monkeypatch.chdir(tmp_path)
    assert utils.builtin_generators_dir() is None
    assert utils.generator_dirs() == [str(tmp_path)]


def test_generators_path_is_a_list_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join(["mine", "", "gens", "mine"]))
    # Only these folders, in this order, without duplicates; the last one is the base
    assert utils.generator_dirs() == [str(tmp_path / "mine"), str(tmp_path / "gens")]
    assert utils.base_generators_dir() == str(tmp_path / "gens")


def user_and_system(monkeypatch, tmp_path):
    """Use the default folders: a user folder, two system folders and the checkout's generators."""
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "home"))
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "local"))
        system = [tmp_path / "local"]
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_DATA_DIRS", os.pathsep.join([str(tmp_path / "local"), "relative/ignored",
                                                             str(tmp_path / "usr")]))
        system = [tmp_path / "local", tmp_path / "usr"]
    folders = [p / "pkggen" / "generators" for p in [tmp_path / "home", *system]]
    for folder in folders:
        folder.mkdir(parents=True)
    return [str(f) for f in folders]


def test_default_search_order(monkeypatch, tmp_path):
    folders = user_and_system(monkeypatch, tmp_path)
    assert [os.path.normcase(d) for d in utils.generator_dirs()] == \
        [os.path.normcase(d) for d in folders + [str(paths.GENERATORS)]]
    # Folders that don't exist are left out
    os.rmdir(folders[0])
    assert len(utils.generator_dirs()) == len(folders)


def test_xdg_data_dirs_default(monkeypatch):
    if os.name == "nt":
        pytest.skip("XDG folders")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/u/.local/share")
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    assert utils.common.default_generator_dirs() == [
        "/home/u/.local/share/pkggen/generators", "/usr/local/share/pkggen/generators",
        "/usr/share/pkggen/generators"]


def test_builtin_folder_is_never_listed_twice(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("XDG folders")
    # A distribution package's /usr/share/pkggen/generators is both a system folder and the built-in one
    share = tmp_path / "usr"
    builtin = fake_generators(share / "pkggen" / "generators")
    monkeypatch.delenv("PKGGEN_GENERATORS_PATH")
    monkeypatch.setenv("XDG_DATA_DIRS", str(share))
    monkeypatch.setattr(utils, "INSTALLED_GENERATORS", (builtin,))
    assert utils.generator_dirs() == [builtin]


def generator(root, layer, name, text="print('hi')\n"):
    path = root / layer / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def test_find_and_list_generators_across_folders(monkeypatch, tmp_path, capsys):
    mine, base = tmp_path / "mine", tmp_path / "base"
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join([str(mine), str(base)]))
    own = generator(mine, "generation", "sourceforge")
    builtin = generator(base, "generation", "github")
    (base / "generation" / "_lib").mkdir()
    (base / "generation" / "notes.txt").write_text("")
    assert utils.list_generators("generation") == ["github", "sourceforge"]
    assert utils.find_generator("generation", "sourceforge") == own
    assert utils.find_generator("generation", "github") == builtin
    for name in ("missing", "lib", "../base/generation/github", "", None):
        assert utils.find_generator("generation", name) is None
    assert capsys.readouterr().err == ""


def test_a_higher_priority_generator_wins_with_a_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(utils, "_warned", set())
    mine, base = tmp_path / "mine", tmp_path / "base"
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join([str(mine), str(base)]))
    own, builtin = generator(mine, "packaging", "arch"), generator(base, "packaging", "arch")
    assert utils.list_generators("packaging") == ["arch"]
    assert utils.has_generator("packaging", "arch")
    assert capsys.readouterr().err == ""  # listing doesn't warn
    assert utils.find_generator("packaging", "arch") == own
    assert utils.find_generator("packaging", "arch") == own
    err = capsys.readouterr().err
    assert utils.ANSI_RE.sub("", err) == f"Warning: the packaging generator 'arch' in {own} replaces {builtin}\n"


def test_a_stray_lib_py_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(utils, "_warned", set())
    mine, base = tmp_path / "mine", tmp_path / "base"
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join([str(mine), str(base)]))
    generator(mine, "generation", "lib")
    own = generator(mine, "generation", "mine")
    assert utils.list_generators("generation") == ["mine"]
    assert utils.find_generator("generation", "mine") == own
    assert "lib.py is reserved" in capsys.readouterr().err


def test_generator_env(monkeypatch, tmp_path):
    mine, base = tmp_path / "mine", tmp_path / "base"
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join([str(mine), str(base)]))
    monkeypatch.setenv("PYTHONPATH", "existing")
    env = utils.generator_env("packaging", {"KEEP": "1", "PYTHONPATH": "given"})
    assert env["KEEP"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    assert env["PKGGEN_GENERATORS_DIRS"] == os.pathsep.join([str(mine), str(base)])
    # The base folder's lib comes first, in front of what was there
    assert env["PYTHONPATH"] == os.pathsep.join([str(base / "packaging" / "_lib"), "given"])
    assert utils.generator_env("testing")["PYTHONPATH"] == os.pathsep.join([str(base / "testing" / "_lib"),
                                                                            "existing"])


def test_distributions_are_merged_across_folders(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(utils, "_warned", set())
    mine, base = tmp_path / "mine", tmp_path / "base"
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", os.pathsep.join([str(mine), str(tmp_path / "none"), str(base)]))
    write_distributions(base, "distributions:\n  - {name: arch, rp-names: [arch]}\n"
                              "  - {name: debian, rp-names: [debian_13]}\n")
    write_distributions(mine, "distributions:\n  - {name: freebsd, rp-names: [freebsd]}\n"
                              "  - {name: arch, rp-names: [arch, artix]}\n")
    assert utils.load_distributions() == [{"name": "arch", "rp-names": ["arch", "artix"]},
                                          {"name": "debian", "rp-names": ["debian_13"]},
                                          {"name": "freebsd", "rp-names": ["freebsd"]}]
    assert "the distribution 'arch' in " in capsys.readouterr().err
    # A folder without the file is fine; none at all isn't
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path / "none"))
    with pytest.raises(utils.GenericError, match="Couldn't find the distributions list"):
        utils.load_distributions()


def test_distribution_names_are_not_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, "distributions:\n  - {name: ../arch, rp-names: [arch]}\n")
    with pytest.raises(utils.GenericError, match="needs a 'name' key"):
        utils.load_distributions()


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


def test_target_aliases(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, "distributions:\n  - name: v\n    rp-names: [v_1, v_2]\n"
                                  "    targets: { v_1: [v-glibc, v-musl] }\n")
    dist = utils.load_distributions()[0]
    assert utils.target_aliases(dist) == {"v-glibc": "v_1", "v-musl": "v_1"}
    assert utils.release_names(dist) == ["v_1", "v_2", "v-glibc", "v-musl"]
    assert utils.target_aliases({"name": "x"}) == {}


@pytest.mark.parametrize("targets", ["[a]", "{ nope: [a] }", "{ v_1: [] }", "{ v_1: a }"])
def test_invalid_targets(monkeypatch, tmp_path, targets):
    monkeypatch.setenv("PKGGEN_GENERATORS_PATH", str(tmp_path))
    write_distributions(tmp_path, f"distributions:\n  - name: v\n    rp-names: [v_1]\n    targets: {targets}\n")
    with pytest.raises(utils.GenericError, match="'targets' of v .* must map its rp-names to lists"):
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
    assert utils.generator_dirs() == [str(paths.GENERATORS)]


def test_get_pkggen_config_utf8_and_invalid_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("PKGGEN_RUN_PATH", str(tmp_path))
    (tmp_path / "pkggen.yaml").write_bytes("maintainer: \"Jürgen <j@example.org>\"\ndescription: \"Ümlauts — 日本語\"\n".encode("utf-8"))
    assert utils.get_pkggen_config() == {"maintainer": "Jürgen <j@example.org>", "description": "Ümlauts — 日本語"}

    (tmp_path / "pkggen.yaml").write_bytes(b"name: \xff\n")
    with pytest.raises(utils.GenericError, match="isn't UTF-8"):
        utils.get_pkggen_config()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_create_secrets_file_permissions(isolated_env, secrets):
    path = utils.create_secrets_file()
    assert os.stat(path).st_mode & 0o777 == 0o600
    os.chmod(path, 0o644)
    utils.create_secrets_file()
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_shared_common_imported_in_every_layer_and_no_bare_shadowing():
    from tests.support.helpers import load_generator
    shared = ("ANSI_RE", "ERROR_PREFIX_RE", "config_dir", "secrets_path",
              "ensure_secrets_file", "shquote", "to_bool", "as_list", "words")
    for layer in ("dependencies", "deployment", "generation", "packaging", "repositories", "testing"):
        lib = load_generator(layer, "lib")
        for attr in shared:
            assert hasattr(lib, attr), (layer, attr)
    with pytest.raises(ModuleNotFoundError):
        __import__("utils")
