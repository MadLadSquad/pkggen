import pytest

from tests.support.helpers import run_generator


@pytest.fixture
def gen_repo(tmp_path):
    """gen_repo(dist, block) runs repositories/<dist>.py on {"repositories": {dist: block}} in tmp_path/repo."""
    out = tmp_path / "repo"
    out.mkdir()

    def run(dist, block, force=False, config=None):
        payload = config if config is not None else {"repositories": {dist: block}}
        return run_generator("repositories", dist, payload, cwd=out, env={"PKGGEN_FORCE": "1" if force else "0"})
    run.out = out
    return run


def files(root):
    return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())
