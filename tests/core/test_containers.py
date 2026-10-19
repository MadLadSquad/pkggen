import io
import json

import pytest

import containers
import utils


def settings(**values):
    base = {"engine": None, "connection": None, "cpus": None, "memory": None, "jobs": None, "disk": None, "pull": None}
    base.update(values)
    return base


INFO = {"arch": "amd64", "cpus": 8, "memory": 16384, "os": "linux", "cpu_limits": True, "memory_limits": True}

# --- Sizes and platforms ---------------------------------------------------------


@pytest.mark.parametrize("value, mib", [
    ("16G", 16384), ("16GiB", 16384), ("512M", 512), ("512 mb", 512), ("1.5g", 1536), ("2T", 2 * 1024 * 1024),
    (4, 4096), (0.5, 512), ("8", 8192), ("2048K", 2),
])
def test_parse_size(value, mib):
    assert containers.parse_size(value) == mib


@pytest.mark.parametrize("value", ["", "lots", "-1G", "0", "1X", True, "1K"])
def test_parse_size_rejects(value):
    with pytest.raises(ValueError):
        containers.parse_size(value)


@pytest.mark.parametrize("platform, name", [("linux", "linux"), ("darwin", "macos"), ("win32", "windows"),
                                            ("freebsd14", "bsd"), ("openbsd7", "bsd"), ("sunos5", "other")])
def test_host_platform(monkeypatch, platform, name):
    monkeypatch.setattr(containers.sys, "platform", platform)
    assert containers.host_platform() == name


def test_host_memory():
    memory = containers.host_memory()
    assert memory is None or memory > 0


# --- Engine detection ------------------------------------------------------------


def test_detect_prefers_podman(monkeypatch):
    monkeypatch.setattr(containers.shutil, "which", lambda name: f"/usr/bin/{name}")
    engine = containers.Engine.detect()
    assert (engine.kind, engine.argv) == ("podman", ["/usr/bin/podman"])


def test_detect_falls_back_to_docker(monkeypatch):
    monkeypatch.setattr(containers.shutil, "which", lambda name: "/bin/docker" if name == "docker" else None)
    engine = containers.Engine.detect(connection="remote")
    assert (engine.kind, engine.argv, engine.connection) == ("docker", ["/bin/docker"], "remote")


def test_detect_chosen_engine(monkeypatch):
    monkeypatch.setattr(containers.shutil, "which", lambda name: "/bin/docker" if name == "docker" else None)
    assert containers.Engine.detect("docker").kind == "docker"
    with pytest.raises(utils.GenericError, match="'podman' was chosen as the container engine, but it isn't installed"):
        containers.Engine.detect("podman")


def test_detect_without_engine(monkeypatch):
    monkeypatch.setattr(containers.shutil, "which", lambda name: None)
    with pytest.raises(utils.GenericError, match="neither podman nor docker was found"):
        containers.Engine.detect()


def test_detect_test_engine(fake_engine):
    engine = containers.Engine.detect("docker")
    assert engine.kind == "docker" and engine.argv[-1].endswith("fake_engine.py")


def test_connection_options():
    assert containers.Engine("podman", ["podman"], "vm").command("ps") == ["podman", "--connection", "vm", "ps"]
    assert containers.Engine("docker", ["docker"], "ctx").command("ps") == ["docker", "--context", "ctx", "ps"]
    assert containers.Engine("podman", ["podman"], "vm").command("machine", "list", remote=False) == [
        "podman", "machine", "list"]


# --- Engine commands ---------------------------------------------------------------


def test_info_podman(fake_engine):
    fake_engine.configure(info={"host": {"arch": "arm64", "cpus": 4, "memTotal": 8 * 1024 ** 3,
                                         "cgroupControllers": ["memory", "pids"], "os": "linux"}})
    assert containers.Engine.detect().info() == {"arch": "arm64", "cpus": 4, "memory": 8192, "os": "linux",
                                                 "cpu_limits": False, "memory_limits": True}


def test_info_docker(fake_engine):
    fake_engine.configure(info={"Architecture": "x86_64", "NCPU": 24, "MemTotal": 32 * 1024 ** 3, "OSType": "linux",
                                "CpuCfsQuota": True, "MemoryLimit": False})
    assert containers.Engine.detect("docker").info() == {"arch": "amd64", "cpus": 24, "memory": 32768, "os": "linux",
                                                         "cpu_limits": True, "memory_limits": False}


def test_container_lifecycle(fake_engine, tmp_path):
    engine = containers.Engine.detect()
    engine.create_data("data", "example.org/data:1", session="s1", pull="always")
    engine.start("box", "example.org/img:1", session="s1", env={"A": "1"}, cpus=1.5, memory=2048,
                 platform="linux/amd64", volumes_from=["data"])
    assert fake_engine.calls("create") == [["create", "--name", "data", "--label", "pkggen.session=s1",
                                            "--entrypoint", "true", "--pull", "always", "example.org/data:1"]]
    # Podman only fills the volume when the container first starts
    assert fake_engine.calls("start") == [["start", "data"]]
    assert fake_engine.calls("run") == [["run", "-d", "--name", "box", "--label", "pkggen.session=s1",
                                         "--entrypoint", "sleep", "--platform", "linux/amd64", "--cpus", "1.5",
                                         "--memory", "2048m", "-e", "A=1", "--volumes-from", "data",
                                         "example.org/img:1", "infinity"]]

    repo = tmp_path / "pkggen-build" / "arch"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "PKGBUILD").write_text("x")
    engine.copy_in("box", str(repo), "/pkggen/repo")
    assert fake_engine.calls("cp")[-1] == ["cp", "./arch/.", "box:/pkggen/repo"]

    log = io.BytesIO()
    code, tail = engine.exec("box", {"name": "s", "run": "cp -rL /pkggen/repo /pkggen/out\n", "user": "b",
                                     "workdir": "/w"}, log)
    assert code == 0 and tail == ["+ cp -rL /pkggen/repo /pkggen/out"]
    assert fake_engine.calls("exec")[-1] == ["exec", "-u", "b", "-w", "/w", "box", "sh", "-exc",
                                             "cp -rL /pkggen/repo /pkggen/out\n"]
    code, tail = engine.exec("box", {"run": "FAKE_FAIL"}, log)
    assert code == 1 and tail[-1] == "fake failure"
    assert b"fake failure" in log.getvalue()

    out = tmp_path / "test" / "arch"
    (out / "stale").mkdir(parents=True)
    engine.copy_out("box", "/pkggen/out/", str(out))
    assert (out / "pkg" / "PKGBUILD").read_text() == "x"
    assert not (out / "stale").exists()
    assert fake_engine.calls("cp")[-1] == ["cp", "box:/pkggen/out/.", "./arch"]

    engine.remove_session("s1")
    assert fake_engine.containers() == []
    assert fake_engine.calls("rm") == [["rm", "-f", "-v", "box", "data"]]


def test_engine_errors(fake_engine):
    fake_engine.configure(fail_images=["example.org/missing:1"])
    engine = containers.Engine.detect()
    with pytest.raises(containers.EngineError, match=r"`podman run -d` failed with exit code 125: "
                                                     r"Error: pulling example.org/missing:1: manifest unknown"):
        engine.start("box", "example.org/missing:1", session="s")


def test_engine_that_cant_run():
    with pytest.raises(containers.EngineError, match="couldn't run podman"):
        containers.Engine("podman", ["/nonexistent/podman"]).run("info")


# --- Resources -----------------------------------------------------------------------


def test_plan_resources_defaults():
    plan = containers.plan_resources(settings(), 10, INFO, whole_machine=False)
    # 8 CPUs and half of 16 GiB: 4 containers with at least 2 CPUs and 2 GiB each
    assert plan == {"jobs": 4, "cpus": 2.0, "memory": 2048, "threads": 2, "warnings": []}


def test_plan_resources_whole_machine():
    plan = containers.plan_resources(settings(), 10, INFO, whole_machine=True)
    assert (plan["jobs"], plan["cpus"], plan["memory"]) == (4, 2.0, 4096)


def test_plan_resources_budget():
    plan = containers.plan_resources(settings(cpus=6, memory=12288), 2, INFO, whole_machine=False)
    assert (plan["jobs"], plan["cpus"], plan["memory"], plan["threads"]) == (2, 3.0, 6144, 3)


def test_plan_resources_jobs():
    plan = containers.plan_resources(settings(jobs=3), 10, INFO, whole_machine=False)
    assert (plan["jobs"], plan["cpus"], plan["memory"]) == (3, 2.67, 2730)
    # The command line wins over pkggen.yaml, and there are never more jobs than targets
    assert containers.plan_resources(settings(jobs=3), 10, INFO, False, jobs=1)["jobs"] == 1
    assert containers.plan_resources(settings(jobs=8), 2, INFO, False)["jobs"] == 2


def test_plan_resources_small_machine():
    plan = containers.plan_resources(settings(), 5, dict(INFO, cpus=1, memory=1024), whole_machine=False)
    assert (plan["jobs"], plan["cpus"], plan["memory"]) == (1, 1.0, 512)
    assert plan["warnings"] == ["each container only gets 512 MiB of RAM: lower 'test.jobs' or raise 'test.memory'"]


def test_plan_resources_more_than_available():
    plan = containers.plan_resources(settings(cpus=64, memory=65536), 1, INFO, whole_machine=False)
    assert (plan["cpus"], plan["memory"]) == (8.0, 16384)
    assert plan["warnings"] == ["'test.cpus' is 64, but the container host only has 8 CPUs",
                                "'test.memory' is 65536 MiB, but the container host only has 16384 MiB"]


def test_plan_resources_without_cgroup_controllers():
    plan = containers.plan_resources(settings(), 1, dict(INFO, cpu_limits=False, memory_limits=False), False)
    assert plan["cpus"] is None and plan["memory"] is None and plan["threads"] == 8
    assert len(plan["warnings"]) == 2 and "Podman" in plan["warnings"][0]


# --- Hosts and VMs -------------------------------------------------------------------


def messages():
    said = []
    return said, said.append


@pytest.fixture
def on(monkeypatch):
    def set_platform(name):
        monkeypatch.setattr(containers, "host_platform", lambda: name)
    return set_platform


def test_linux_needs_no_vm(fake_engine, on):
    on("linux")
    assert containers.prepare_host(containers.Engine.detect(), settings(), print) == ([], False)
    assert fake_engine.raw_calls() == []


@pytest.mark.parametrize("platform", ["macos", "windows", "bsd"])
def test_remote_hosts_need_no_vm(fake_engine, on, monkeypatch, platform):
    on(platform)
    monkeypatch.setenv("CONTAINER_HOST", "ssh://builder@example.org/run/podman/podman.sock")
    assert containers.prepare_host(containers.Engine.detect(), settings(), print) == ([], False)
    monkeypatch.delenv("CONTAINER_HOST")
    assert containers.prepare_host(containers.Engine.detect(), settings(connection="box"), print) == ([], False)


def test_bsd_without_remote_host(fake_engine, on):
    on("bsd")
    with pytest.raises(utils.GenericError, match="can't start a Linux VM on this system .* set 'test.connection'"):
        containers.prepare_host(containers.Engine.detect(), settings(), print)


def test_docker_desktop(fake_engine, on):
    on("macos")
    assert containers.prepare_host(containers.Engine.detect("docker"), settings(), print) == ([], True)
    assert fake_engine.calls() == [["info", "--format", "{{json .}}"]]


def test_docker_desktop_not_running(on):
    on("windows")
    engine = containers.Engine("docker", ["/nonexistent/docker"])
    with pytest.raises(utils.GenericError, match="Docker isn't reachable .* Start Docker Desktop or Colima"):
        containers.prepare_host(engine, settings(), print)


def test_podman_machine_is_created(fake_engine, on):
    on("macos")
    said, say = messages()
    engine = containers.Engine.detect()
    warnings, whole = containers.prepare_host(engine, settings(cpus=6, memory=8192, disk=80), say)
    assert (warnings, whole) == ([], True)
    assert ["machine", "init", "--rootful", "--cpus", "6", "--memory", "8192", "--disk-size", "80",
            "pkggen"] in fake_engine.calls("machine")
    assert ["machine", "start", "pkggen"] in fake_engine.calls("machine")
    assert engine.connection == "pkggen-root"
    assert said[0].startswith("Creating the 'pkggen' Podman machine (6 CPUs, 8192 MiB RAM)")
    # Engine commands now go to the machine
    engine.info()
    assert fake_engine.raw_calls()[-1][:2] == ["--connection", "pkggen-root"]


def test_podman_machine_defaults(fake_engine, on, monkeypatch):
    on("windows")
    monkeypatch.setattr(containers.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(containers, "host_memory", lambda: 32768)
    containers.prepare_host(containers.Engine.detect(), settings(), print)
    assert ["machine", "init", "--rootful", "--cpus", "12", "--memory", "16384", "pkggen"] in fake_engine.calls("machine")


def test_podman_machine_is_resized(fake_engine, on):
    on("macos")
    fake_engine.configure(machines=[{"Name": "pkggen", "State": "running", "Rootful": False,
                                     "Resources": {"CPUs": 2, "Memory": 2048}}])
    engine = containers.Engine.detect()
    containers.prepare_host(engine, settings(cpus=4, memory=4096), print)
    calls = [c for c in fake_engine.calls("machine") if c[1] not in ("list", "inspect")]
    assert calls == [["machine", "stop", "pkggen"], ["machine", "set", "--cpus", "4", "--memory", "4096", "pkggen"],
                     ["machine", "start", "pkggen"]]
    assert engine.connection == "pkggen"


def test_podman_machine_already_right(fake_engine, on):
    on("macos")
    fake_engine.configure(machines=[{"Name": "pkggen", "State": "running", "Rootful": True,
                                     "Resources": {"CPUs": 4, "Memory": 4096}}])
    containers.prepare_host(containers.Engine.detect(), settings(cpus=4, memory=4096), print)
    assert [c[1] for c in fake_engine.calls("machine")] == ["list", "inspect"]


def test_other_running_machine_on_macos_is_used(fake_engine, on):
    on("macos")
    fake_engine.configure(machines=[{"Name": "podman-machine-default", "State": "running", "Rootful": True,
                                     "Resources": {"CPUs": 2, "Memory": 2048}}])
    engine = containers.Engine.detect()
    warnings, whole = containers.prepare_host(engine, settings(cpus=4), print)
    assert whole and engine.connection == "podman-machine-default-root"
    assert "'podman-machine-default' is already running" in warnings[0]
    assert not any(c[1] == "init" for c in fake_engine.calls("machine"))


def test_wsl_machines_warn(fake_engine, on):
    on("windows")
    fake_engine.configure(machines=[{"Name": "pkggen", "State": "stopped", "Rootful": True, "VMType": "wsl",
                                     "Resources": {"CPUs": 4, "Memory": 4096}}])
    warnings, _ = containers.prepare_host(containers.Engine.detect(), settings(cpus=4, memory=4096), print)
    assert ["machine", "start", "pkggen"] in fake_engine.calls("machine")
    assert warnings == ["Podman machines that use WSL share the resources set in .wslconfig, so 'test.cpus' and "
                        "'test.memory' only limit the containers, not the VM"]
