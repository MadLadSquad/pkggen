"""A stand-in for the podman/docker CLI, selected with PKGGEN_TEST_ENGINE.

It keeps its state in the folder named by PKGGEN_FAKE_ENGINE:
  calls.jsonl         every command line (without the program), one JSON list per line
  config.json         optional: {"info": {...}, "fail_images": [...], "machines": [...]}
  containers/<name>/  each container: meta.json and fs/, its file system

Steps (`exec ... sh -exc <script>`) aren't really run. A script that contains
FAKE_FAIL fails, and `cp -rL <src> <dst>` lines are copied inside fs/, so the
collected output looks like the real thing.
"""
import json
import os
import shutil
import sys

STATE = os.environ["PKGGEN_FAKE_ENGINE"]
CONTAINERS = os.path.join(STATE, "containers")
MACHINES = os.path.join(STATE, "machines.json")

DEFAULT_INFO = {"host": {"arch": "amd64", "cpus": 8, "memTotal": 16 * 1024 ** 3,
                         "cgroupControllers": ["cpu", "memory", "pids"], "os": "linux"}}
# Options that take a value, per command
VALUE_OPTIONS = {"--name", "--label", "--entrypoint", "--pull", "--platform", "--cpus", "--memory", "-e",
                 "--volumes-from", "-u", "-w", "--format", "--filter", "--disk-size"}


def config():
    path = os.path.join(STATE, "config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def split(args):
    """(options {name: [values]}, positional arguments)."""
    options, positional = {}, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in VALUE_OPTIONS and i + 1 < len(args):
            options.setdefault(arg, []).append(args[i + 1])
            i += 2
        elif arg.startswith("-") and not positional:
            options.setdefault(arg, []).append(True)
            i += 1
        else:
            positional.append(arg)
            i += 1
    return options, positional


def fs(name):
    return os.path.join(CONTAINERS, name, "fs")


def inside(name, path):
    return os.path.join(fs(name), path.lstrip("/"))


def create(args, start):
    options, positional = split(args)
    name, image = options["--name"][0], positional[0]
    if image in config().get("fail_images", []):
        print(f"Error: pulling {image}: manifest unknown", file=sys.stderr)
        return 125
    if os.path.isdir(os.path.join(CONTAINERS, name)):
        print(f"Error: the container name {name} is already in use", file=sys.stderr)
        return 125
    os.makedirs(fs(name))
    with open(os.path.join(CONTAINERS, name, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"image": image, "labels": options.get("--label", []), "running": start}, f)
    print(f"{name}-id")
    return 0


def start(args):
    name = args[-1]
    if not os.path.isdir(fs(name)):
        print(f"Error: no container with name or ID {name} found", file=sys.stderr)
        return 125
    print(name)
    return 0


def exec_(args):
    options, positional = split(args)
    name, command = positional[0], positional[1:]
    if not os.path.isdir(fs(name)):
        print(f"Error: no container with name or ID {name} found", file=sys.stderr)
        return 125
    if command[:2] == ["mkdir", "-p"]:
        os.makedirs(inside(name, command[2]), exist_ok=True)
        return 0
    script = command[-1]
    print(f"+ {script.strip().splitlines()[0] if script.strip() else ''}")
    if "FAKE_FAIL" in script:
        print("fake failure")
        return 1
    for line in script.splitlines():
        parts = line.split()
        if parts[:2] == ["cp", "-rL"] and len(parts) == 4 and os.path.isdir(inside(name, parts[2])):
            shutil.copytree(inside(name, parts[2]), inside(name, parts[3]), dirs_exist_ok=True)
    return 0


def cp(args):
    src, dst = args[-2], args[-1]
    if ":" in src:  # container -> host
        name, path = src.split(":", 1)
        source = inside(name, path)
        if not os.path.exists(source.rstrip("/.")):
            print(f"Error: {path}: no such file or directory", file=sys.stderr)
            return 125
        shutil.copytree(source, dst, dirs_exist_ok=True)
    else:
        name, path = dst.split(":", 1)
        if not os.path.isdir(fs(name)):
            print(f"Error: no container with name or ID {name} found", file=sys.stderr)
            return 125
        shutil.copytree(src, inside(name, path), dirs_exist_ok=True)
    return 0


def rm(args):
    _, names = split(args)
    for name in names:
        shutil.rmtree(os.path.join(CONTAINERS, name), ignore_errors=True)
    return 0


def ps(args):
    options, _ = split(args)
    wanted = [f.split("=", 1)[1] for f in options.get("--filter", []) if f.startswith("label=")]
    if os.path.isdir(CONTAINERS):
        for name in sorted(os.listdir(CONTAINERS)):
            with open(os.path.join(CONTAINERS, name, "meta.json"), encoding="utf-8") as f:
                labels = json.load(f)["labels"]
            if all(label in labels for label in wanted):
                print(name)
    return 0


def load_machines():
    if os.path.isfile(MACHINES):
        with open(MACHINES, encoding="utf-8") as f:
            return json.load(f)
    return config().get("machines", [])


def save_machines(machines):
    with open(MACHINES, "w", encoding="utf-8") as f:
        json.dump(machines, f)


def machine(args):
    command, rest = args[0], args[1:]
    options, positional = split(rest)
    machines = load_machines()
    by_name = {m["Name"]: m for m in machines}
    if command == "list":
        print(json.dumps([{"Name": m["Name"], "Running": m["State"] == "running", "VMType": m.get("VMType", "applehv")}
                          for m in machines]))
        return 0
    name = positional[-1]
    if command == "init":
        machines.append({"Name": name, "State": "stopped", "Rootful": "--rootful" in options,
                         "VMType": "applehv",
                         "Resources": {"CPUs": int(options["--cpus"][0]), "Memory": int(options["--memory"][0])}})
        save_machines(machines)
        return 0
    if name not in by_name:
        print(f"Error: {name}: VM does not exist", file=sys.stderr)
        return 125
    entry = by_name[name]
    if command == "inspect":
        print(json.dumps([entry]))
    elif command == "set":
        if entry["State"] == "running":
            print("Error: the machine must be stopped", file=sys.stderr)
            return 125
        entry["Resources"] = {"CPUs": int(options["--cpus"][0]), "Memory": int(options["--memory"][0])}
    elif command == "start":
        entry["State"] = "running"
    elif command == "stop":
        entry["State"] = "stopped"
    save_machines(machines)
    return 0


def main():
    args = sys.argv[1:]
    with open(os.path.join(STATE, "calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
    while args and args[0] in ("--connection", "--context"):
        args = args[2:]
    command, rest = args[0], args[1:]
    if command == "info":
        print(json.dumps(config().get("info", DEFAULT_INFO)))
        return 0
    handlers = {"run": lambda a: create(a, True), "create": lambda a: create(a, False), "start": start, "exec": exec_,
                "cp": cp, "rm": rm, "ps": ps, "machine": machine}
    return handlers[command](rest)


if __name__ == "__main__":
    sys.exit(main())
