import io
import json

import pytest

from tests.support.helpers import load_generator


@pytest.fixture
def lib(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return load_generator("repositories", "lib")


def feed(lib, monkeypatch, data):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    monkeypatch.setattr(lib, "readinput", lambda: raw)


def test_load_repo_config(lib, monkeypatch):
    feed(lib, monkeypatch, {"repositories": {"arch": {"x": 1}}})
    assert lib.load_repo_config("arch") == {"x": 1}


@pytest.mark.parametrize("data", [b"", b"  \n", {}, {"repositories": None}, {"repositories": {"debian": {}}}])
def test_load_repo_config_missing_block_is_empty(lib, monkeypatch, data):
    feed(lib, monkeypatch, data)
    assert lib.load_repo_config("arch") == {}


@pytest.mark.parametrize("data, required, message", [
    (b"{not json", (), "couldn't parse the pkggen configuration"),
    ({}, ("name",), "pkggen.yaml has no 'repositories.gentoo' block"),
    ({"repositories": {"gentoo": ["list"]}}, (), "'repositories.gentoo' must be a mapping"),
    ({"repositories": {"gentoo": {"name": ""}}}, ("name", "x"), "'repositories.gentoo' is missing: name, x"),
])
def test_load_repo_config_errors(lib, monkeypatch, capsys, data, required, message):
    feed(lib, monkeypatch, data)
    with pytest.raises(SystemExit) as info:
        lib.load_repo_config("gentoo", required=required)
    assert info.value.code == 1
    assert message in capsys.readouterr().err


def test_write_file_creates_parents_with_lf(lib, tmp_path, capsys):
    lib.write_file("conf/a.txt", "one\ntwo\n")
    assert (tmp_path / "conf" / "a.txt").read_bytes() == b"one\ntwo\n"
    assert "created conf/a.txt" in capsys.readouterr().err


def test_write_file_skips_existing(lib, tmp_path, capsys):
    (tmp_path / "a.txt").write_text("mine")
    lib.write_file("a.txt", "theirs")
    assert (tmp_path / "a.txt").read_text() == "mine"
    assert "skipped a.txt (already exists, use --force to overwrite)" in capsys.readouterr().err


def test_write_file_force(lib, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(lib, "FORCE", True)
    (tmp_path / "a.txt").write_text("mine")
    lib.write_file("a.txt", "theirs")
    assert (tmp_path / "a.txt").read_text() == "theirs"
    assert "overwrote a.txt" in capsys.readouterr().err


def test_force_comes_from_env(monkeypatch):
    monkeypatch.setenv("PKGGEN_FORCE", "1")
    assert load_generator("repositories", "lib").FORCE is True
    monkeypatch.setenv("PKGGEN_FORCE", "0")
    assert load_generator("repositories", "lib").FORCE is False


def test_make_dirs(lib, tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / ".gitkeep").write_text("keep me")
    lib.make_dirs(["a/x", "b"])
    assert (tmp_path / "a" / "x" / ".gitkeep").read_text() == ""
    assert (tmp_path / "b" / ".gitkeep").read_text() == "keep me"
    lib.make_dirs(["c"], gitkeep=False)
    assert list((tmp_path / "c").iterdir()) == []


@pytest.mark.parametrize("value, expected", [(None, []), ("a", ["a"]), (["a", "b"], ["a", "b"]), (("a",), ["a"]),
                                             (3, [3])])
def test_as_list(lib, value, expected):
    assert lib.as_list(value) == expected


@pytest.mark.parametrize("value, default, expected", [
    (None, True, True), (None, False, False), ("yes", False, True), (" ON ", False, True), ("1", False, True),
    ("false", True, False), ("no", True, False), (0, True, False), (1, False, True),
])
def test_to_bool(lib, value, default, expected):
    assert lib.to_bool(value, default) is expected


def test_lines(lib):
    assert lib.lines("a", "b") == "a\nb\n"
    assert lib.lines() == "\n"
