import pytest

from tests.support.helpers import load_generator

SHA256 = "ab" * 32


def artifact(alias, url=None, directory=None):
    return {"url": url or f"https://downloads.example.com/{alias}", "alias": alias, "type": "asset",
            "directory": directory or alias.rsplit(".tar", 1)[0], "size": 1234, "checksums": {"sha2_256": SHA256}}


def context(name="foo", version="1.2.0", **extra):
    """A template context like the one `pkggen generate` builds."""
    artifacts = extra.pop("artifacts", [artifact(f"{name}-{version}.tar.xz")])
    result = {"name": name, "version": version, "architectures": ["amd64", "arm64"],
              "artifacts": artifacts, "source": artifacts[0] if artifacts else {}}
    result.update(extra)
    return result


def entry(output=None, **context_values):
    ctx = context(**context_values)
    return {"context": ctx, "output": output if output is not None else ctx["name"]}


@pytest.fixture
def describe(example_config):
    """describe(dist, packages=[entry, ...], **job fields) -> (targets, warnings), run in-process."""
    def run(dist, packages=(), **fields):
        generator = load_generator("testing", dist)
        data = {
            "distribution": dist,
            "repository": (example_config["repositories"].get(dist) or {}),
            "test": {"setup": [], "post-setup": []},
            "rp_names": [],
            "versions": None,
            "host_arch": "amd64",
            "packages": list(packages),
        }
        data.update(fields)
        if not data["rp_names"]:
            data["rp_names"] = list(generator.TARGETS)
        job = generator.lib.Job(data)
        return generator.main(job), job.warnings
    return run


def scripts(steps):
    return "\n".join(step["run"] for step in steps)
