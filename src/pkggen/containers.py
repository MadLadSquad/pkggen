#!/usr/bin/env python3
# The container engine behind `pkggen test`: Podman (default) or Docker,
# driven through their CLIs, which accept the same commands. On macOS and
# Windows the containers run in a Linux VM: a Podman machine that pkggen
# creates and sizes itself, or whatever VM Docker Desktop/Colima provide.
import collections
import json
import math
import os
import re
import shutil
import subprocess
import sys

import utils

MACHINE_NAME = "pkggen"
# The session label every container gets, so a run can clean up after itself
SESSION_LABEL = "pkggen.session"


class EngineError(RuntimeError):
    """A container engine command failed."""


def host_platform():
    """linux, macos, windows, bsd or other."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    if "bsd" in sys.platform or sys.platform.startswith("dragonfly"):
        return "bsd"
    return "other"


def host_memory():
    """Total RAM of this machine in MiB, or None when it can't be found."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
        elif sys.platform in ("win32", "cygwin"):
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys // (1024 * 1024)
        else:
            key = "hw.memsize" if sys.platform == "darwin" else "hw.physmem"
            out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=10).stdout
            return int(out.strip()) // (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return None


ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def _get(mapping, *keys):
    """Look a key up ignoring case: podman's JSON is camelCase, docker's is PascalCase."""
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


class Engine:
    """A container engine CLI: `kind` is podman or docker, `argv` the command that runs it."""

    def __init__(self, kind, argv, connection=None):
        self.kind = kind
        self.argv = list(argv)
        self.connection = connection

    @classmethod
    def detect(cls, preferred=None, connection=None):
        # Tests swap in a fake engine: a JSON list with the command to run
        fake = os.getenv("PKGGEN_TEST_ENGINE")
        if fake:
            return cls(preferred or "podman", json.loads(fake), connection)

        if preferred:
            path = shutil.which(preferred)
            if not path:
                raise utils.GenericError(f"'{preferred}' was chosen as the container engine, but it isn't installed "
                                         "or isn't on PATH")
            return cls(preferred, [path], connection)
        for kind in ("podman", "docker"):
            path = shutil.which(kind)
            if path:
                return cls(kind, [path], connection)
        raise utils.GenericError("`pkggen test` needs a container engine, but neither podman nor docker was found. "
                                 "Install Podman (https://podman.io/docs/installation) or Docker")

    def command(self, *args, remote=True):
        """The full command line. Machine commands run locally, so they skip the connection."""
        prefix = list(self.argv)
        if remote and self.connection:
            prefix += ["--connection", self.connection] if self.kind == "podman" else ["--context", self.connection]
        return prefix + [str(a) for a in args]

    def run(self, *args, check=True, cwd=None, remote=True, capture=True, timeout=None):
        cmd = self.command(*args, remote=remote)
        try:
            process = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, timeout=timeout,
                                     stdout=subprocess.PIPE if capture else None,
                                     stderr=subprocess.PIPE if capture else None)
        except OSError as e:
            raise EngineError(f"couldn't run {self.kind}: {e}")
        except subprocess.TimeoutExpired:
            raise EngineError(f"`{self.kind} {args[0]}` timed out")
        out = (process.stdout or b"").decode("utf-8", "replace")
        err = (process.stderr or b"").decode("utf-8", "replace")
        if check and process.returncode != 0:
            details = (err.strip() or out.strip()).splitlines()[-5:]
            raise EngineError(f"`{self.kind} {' '.join(str(a) for a in args[:2])}` failed with exit code "
                              f"{process.returncode}" + (": " + "\n".join(details) if details else ""))
        return subprocess.CompletedProcess(cmd, process.returncode, out, err)

    # --- Facts about the engine ------------------------------------------------

    def info(self):
        """{arch, cpus, memory (MiB), os, cpu_limits, memory_limits} of the machine the containers run on."""
        try:
            raw = json.loads(self.run("info", "--format", "{{json .}}", timeout=120).stdout)
        except json.JSONDecodeError as e:
            raise EngineError(f"`{self.kind} info` printed invalid JSON: {e}")

        host = _get(raw, "host")
        if isinstance(host, dict):  # podman
            controllers = _get(host, "cgroupControllers")
            arch = _get(host, "arch")
            cpus = _get(host, "cpus")
            memory = _get(host, "memTotal")
            system = _get(host, "os") or "linux"
            cpu_limits = controllers is None or "cpu" in controllers
            memory_limits = controllers is None or "memory" in controllers
        else:  # docker
            arch = _get(raw, "Architecture")
            cpus = _get(raw, "NCPU")
            memory = _get(raw, "MemTotal")
            system = _get(raw, "OSType") or "linux"
            cpu_limits = _get(raw, "CpuCfsQuota") is not False
            memory_limits = _get(raw, "MemoryLimit") is not False
        return {
            "arch": ARCHES.get(str(arch), str(arch or "amd64")),
            "cpus": int(cpus) if cpus else (os.cpu_count() or 2),
            "memory": int(memory) // (1024 * 1024) if memory else (host_memory() or 4096),
            "os": str(system),
            "cpu_limits": cpu_limits,
            "memory_limits": memory_limits,
        }

    # --- Containers ------------------------------------------------------------

    def start(self, name, image, *, session, env=None, cpus=None, memory=None, platform=None,
              volumes_from=(), pull=None):
        """Start a container that idles, so steps can be run in it with exec()."""
        args = ["run", "-d", "--name", name, "--label", f"{SESSION_LABEL}={session}", "--entrypoint", "sleep"]
        if pull:
            args += ["--pull", pull]
        if platform:
            args += ["--platform", platform]
        if cpus:
            args += ["--cpus", f"{cpus:g}"]
        if memory:
            args += ["--memory", f"{int(memory)}m"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        for source in volumes_from:
            args += ["--volumes-from", source]
        self.run(*args, image, "infinity")

    def create_data(self, name, image, *, session, platform=None, pull=None):
        """A container whose volumes other containers mount with --volumes-from.

        It runs `true` once: Podman only copies the image's files into a new
        volume when the container first starts (Docker does it on create).
        """
        args = ["create", "--name", name, "--label", f"{SESSION_LABEL}={session}", "--entrypoint", "true"]
        if pull:
            args += ["--pull", pull]
        if platform:
            args += ["--platform", platform]
        self.run(*args, image)
        self.run("start", name)

    def exec(self, name, step, log, tail_lines=15):
        """Run a step in a container, writing its output to the log file. Returns (exit code, last lines)."""
        args = ["exec"]
        if step.get("user"):
            args += ["-u", step["user"]]
        if step.get("workdir"):
            args += ["-w", step["workdir"]]
        # The script goes in as an argument: stdin stays free (and empty) for the commands it runs
        cmd = self.command(*args, name, "sh", "-exc", step["run"])
        tail = collections.deque(maxlen=tail_lines)
        try:
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            raise EngineError(f"couldn't run {self.kind}: {e}")
        for line in process.stdout:
            log.write(line)
            tail.append(line.decode("utf-8", "replace").rstrip())
        log.flush()
        return process.wait(), list(tail)

    def copy_in(self, name, host_dir, path):
        """Copy the contents of host_dir to path in the container."""
        parent, base = os.path.split(os.path.abspath(host_dir))
        self.run("exec", name, "mkdir", "-p", path)
        # Relative paths: a Windows drive letter would look like "container:path"
        self.run("cp", f"./{base}/.", f"{name}:{path}", cwd=parent)

    def copy_out(self, name, path, host_dir):
        """Copy the contents of path in the container to host_dir, which is replaced."""
        host_dir = os.path.abspath(host_dir)
        if os.path.isdir(host_dir):
            shutil.rmtree(host_dir)
        parent, base = os.path.split(host_dir)
        os.makedirs(parent, exist_ok=True)
        self.run("cp", f"{name}:{path.rstrip('/')}/.", f"./{base}", cwd=parent)

    def list_executables(self, name, path):
        """The files below path in the container with the owner's executable bit, as sorted POSIX paths
        relative to it, or None when they can't be listed.

        copy_out can't keep that bit on file systems without one (NTFS), so the
        deployment puts it back from this list.
        """
        result = self.run("exec", name, "find", path, "-type", "f", "-perm", "-u+x", "-print0", check=False)
        if result.returncode != 0:
            return None
        prefix = path.rstrip("/") + "/"
        return sorted(p[len(prefix):] for p in result.stdout.split("\0") if p.startswith(prefix))

    def remove(self, *names):
        if names:
            self.run("rm", "-f", "-v", *names, check=False)

    def remove_session(self, session):
        """Remove every container of a run, even ones whose thread didn't get to clean up."""
        result = self.run("ps", "-aq", "--filter", f"label={SESSION_LABEL}={session}", check=False)
        ids = result.stdout.split()
        if ids:
            self.remove(*ids)

    # --- Podman machines -------------------------------------------------------

    def machine_inspect(self, name):
        result = self.run("machine", "inspect", name, check=False, remote=False)
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return data[0] if isinstance(data, list) and data else None

    def machine_list(self):
        result = self.run("machine", "list", "--format", "json", check=False, remote=False)
        try:
            data = json.loads(result.stdout) if result.returncode == 0 else []
        except json.JSONDecodeError:
            data = []
        return data if isinstance(data, list) else []


def connection_name(name, rootful):
    # `podman machine init` adds a rootless and a rootful ("-root") connection
    return f"{name}-root" if rootful else name


def machine_budget(cpus, memory):
    """Cap a VM budget to what this machine can give. Returns (cpus, memory, warnings).

    The VM gets at most every CPU, and the host keeps a quarter of its RAM
    (at least 2 GiB), so it doesn't swap while the containers build.
    """
    warnings = []
    host_cpus = os.cpu_count()
    if host_cpus and cpus > host_cpus:
        warnings.append(f"'test.cpus' is {cpus}, but this machine only has {host_cpus} CPUs, so the "
                        f"'{MACHINE_NAME}' Podman machine gets {host_cpus}")
        cpus = host_cpus
    total = host_memory()
    if total:
        limit = max(2048, total - max(2048, total // 4))
        if memory > limit:
            warnings.append(f"'test.memory' is {memory} MiB, but this machine only has {total} MiB, so the "
                            f"'{MACHINE_NAME}' Podman machine gets {limit} MiB and the rest is left to the host")
            memory = limit
    return cpus, memory, warnings


def ensure_machine(engine, cpus, memory, disk, say):
    """Create, resize and start the pkggen Podman machine. Returns (warnings, sized).

    cpus and memory (MiB) are the total budget; the machine gets exactly that,
    capped by machine_budget(). sized is False when another machine that's
    already running is used as it is, and for WSL machines, which get their
    resources from .wslconfig.
    """
    warnings = []
    machines = engine.machine_list()
    running = [m for m in machines if _get(m, "Running") and _get(m, "Name") != MACHINE_NAME]
    vm_type = next((_get(m, "VMType") for m in machines if _get(m, "VMType")), None)

    # macOS runs one machine at a time: use the one that's already up rather than stopping it
    if running and host_platform() == "macos":
        other = running[0]
        name = str(_get(other, "Name")).rstrip("*")
        warnings.append(f"the Podman machine '{name}' is already running, so it's used as it is and "
                        f"'test.cpus'/'test.memory' don't resize it. Stop it (`podman machine stop {name}`) "
                        f"to let pkggen manage its own '{MACHINE_NAME}' machine")
        details = engine.machine_inspect(name) or {}
        engine.connection = connection_name(name, bool(_get(details, "Rootful")))
        return warnings, False

    cpus, memory, capped = machine_budget(cpus, memory)
    warnings += capped
    details = engine.machine_inspect(MACHINE_NAME)
    if details is None:
        say(f"Creating the '{MACHINE_NAME}' Podman machine ({cpus} CPUs, {memory} MiB RAM). "
            "The first start downloads its image, which takes a while")
        args = ["machine", "init", "--rootful", "--cpus", cpus, "--memory", memory]
        if disk:
            args += ["--disk-size", disk]
        engine.run(*args, MACHINE_NAME, remote=False, capture=False)
        details = engine.machine_inspect(MACHINE_NAME) or {}
        # Only `machine list` says which VM type the new machine got
        vm_type = vm_type or next((_get(m, "VMType") for m in engine.machine_list()
                                   if _get(m, "Name") == MACHINE_NAME), None)
    elif str(vm_type).lower() == "wsl":
        pass  # WSL machines get their resources from .wslconfig, `machine set` can't change them
    else:
        resources = _get(details, "Resources") or {}
        if _get(resources, "CPUs") != cpus or _get(resources, "Memory") != memory:
            say(f"Resizing the '{MACHINE_NAME}' Podman machine to {cpus} CPUs and {memory} MiB RAM")
            if str(_get(details, "State")).lower() == "running":
                engine.run("machine", "stop", MACHINE_NAME, remote=False)
            engine.run("machine", "set", "--cpus", cpus, "--memory", memory, MACHINE_NAME, remote=False)
            details = engine.machine_inspect(MACHINE_NAME) or details

    if str(_get(details, "State")).lower() != "running":
        say(f"Starting the '{MACHINE_NAME}' Podman machine")
        engine.run("machine", "start", MACHINE_NAME, remote=False, capture=False)

    engine.connection = connection_name(MACHINE_NAME, _get(details, "Rootful") is not False)
    if str(vm_type).lower() == "wsl":
        # The VM isn't the budget, so the containers keep it
        warnings.append("Podman machines that use WSL share the resources set in .wslconfig, so "
                        "'test.cpus' and 'test.memory' only limit the containers, not the VM")
        return warnings, False
    return warnings, True


def prepare_host(engine, settings, say):
    """Make sure the engine can run Linux containers. Returns (warnings, whole_machine).

    whole_machine is True when the containers get a machine of their own (a
    VM), so the default budget is all of it instead of half the host. When
    pkggen sizes its own Podman machine to the budget, that machine is the
    budget: settings' cpus and memory are cleared, so the containers share all
    of it (its kernel keeps some of the RAM, which isn't worth a warning).
    """
    remote = settings.get("connection") or os.getenv("CONTAINER_HOST") or os.getenv("DOCKER_HOST")
    platform = host_platform()
    if remote or platform == "linux":
        return [], False

    if platform in ("macos", "windows"):
        if engine.kind == "podman":
            cpus = int(math.ceil(settings.get("cpus") or os.cpu_count() or 2))
            memory = settings.get("memory") or max(2048, (host_memory() or 8192) // 2)
            warnings, sized = ensure_machine(engine, cpus, memory, settings.get("disk"), say)
            if sized:
                settings["cpus"] = settings["memory"] = None
            return warnings, True
        try:
            engine.info()
        except EngineError as e:
            raise utils.GenericError(f"Docker isn't reachable ({e}). Start Docker Desktop or Colima "
                                     "(`colima start --cpu N --memory GiB`), or use Podman, which pkggen "
                                     "can start a VM for")
        return [], True

    raise utils.GenericError(f"`pkggen test` can't start a Linux VM on this system ({sys.platform}). Point it at "
                             "a Linux machine that runs Podman or Docker instead: set 'test.connection' to a "
                             "`podman system connection` (or docker context) name, or set CONTAINER_HOST/DOCKER_HOST")


# --- Resources -------------------------------------------------------------------

MEMORY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
UNITS = {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}


def parse_size(value, default_unit="g"):
    """'16G', '512M', '1.5GiB' or a number (GiB) -> MiB. Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        amount, unit = float(value), default_unit
    else:
        match = MEMORY_RE.match(str(value))
        if not match:
            raise ValueError(value)
        amount, unit = float(match.group(1)), (match.group(2) or default_unit).lower()
    mib = int(amount * UNITS[unit])
    if mib <= 0:
        raise ValueError(value)
    return mib


def plan_resources(settings, n_targets, info, whole_machine, jobs=None):
    """Split the CPU/RAM budget between the containers that run at once.

    Returns {jobs, cpus, memory, threads, warnings}: cpus and memory (MiB) are
    per container, None when the engine can't limit them.
    """
    warnings = []
    cpus = settings.get("cpus") or info["cpus"]
    if cpus > info["cpus"]:
        warnings.append(f"'test.cpus' is {cpus:g}, but the container host only has {info['cpus']} CPUs")
        cpus = info["cpus"]
    memory = settings.get("memory") or (info["memory"] if whole_machine else info["memory"] // 2)
    if memory > info["memory"]:
        warnings.append(f"'test.memory' is {memory} MiB, but the container host only has {info['memory']} MiB")
        memory = info["memory"]

    jobs = jobs or settings.get("jobs") or max(1, min(n_targets, int(cpus // 2), int(memory // 2048)))
    jobs = max(1, min(jobs, n_targets))
    per_cpus = round(cpus / jobs, 2)
    per_memory = memory // jobs
    if per_memory < 1024:
        warnings.append(f"each container only gets {per_memory} MiB of RAM: lower 'test.jobs' or raise 'test.memory'")

    if not info["cpu_limits"]:
        warnings.append(f"the container engine can't limit CPUs (the cgroup 'cpu' controller isn't available), so "
                        "'test.cpus' isn't enforced. For rootless Podman, delegate it: "
                        "https://github.com/containers/podman/blob/main/troubleshooting.md#26-running-containers-with-resource-limits-fails-with-a-permissions-error")
    if not info["memory_limits"]:
        warnings.append("the container engine can't limit memory (the cgroup 'memory' controller isn't available), "
                        "so 'test.memory' isn't enforced")
    return {
        "jobs": jobs,
        "cpus": per_cpus if info["cpu_limits"] else None,
        "memory": per_memory if info["memory_limits"] else None,
        "threads": max(1, int(per_cpus)),
        "warnings": warnings,
    }
