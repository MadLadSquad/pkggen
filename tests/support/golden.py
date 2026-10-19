"""Golden snapshots: a whole output tree as one text file, compared exactly."""
import os

from tests.support import paths


def snapshot(root):
    """Every file under root, sorted, as '==> path (N bytes)' followed by its content."""
    parts = []
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as f:
                data = f.read()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = f"<binary, {len(data)} bytes>\n"
            if text and not text.endswith("\n"):
                text += "\n\\ No newline at end of file\n"
            parts.append(f"==> {rel} ({len(data)} bytes)\n{text}")
    return "\n".join(parts)


def assert_golden(request, name, actual):
    """Compare actual with tests/golden/<name>; `pytest --update-golden` rewrites it."""
    path = paths.GOLDEN / name
    if request.config.getoption("--update-golden"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(actual)
        return
    assert path.exists(), f"{path} is missing. Run `pytest --update-golden` to create it"
    with open(path, encoding="utf-8", newline="") as f:
        expected = f.read()
    assert actual == expected, f"output differs from {path}. If the change is intended, run `pytest --update-golden`"
