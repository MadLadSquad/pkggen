# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running pkggen

The virtualenv lives in `src/venv/`. Set it up once:
```sh
cd src && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

Run directly (no install required):
```sh
cd src && PKGGEN_GENERATORS_PATH=../generators PKGGEN_RUN_PATH=../example python3 pkggen/pkggen <command>
```

Or install as a CLI tool:
```sh
cd src && pip install -e .
# then from any directory with a pkggen.yaml:
PKGGEN_GENERATORS_PATH=/path/to/generators pkggen <command>
```

Available commands: `generate`, `gen-repo <distribution> [-o <dir>] [-f]`, `test` (stub), `deploy` (stub), `repology <package>`, `version`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PKGGEN_GENERATORS_PATH` | `cwd` | Path to the `generators/` directory |
| `PKGGEN_RUN_PATH` | `cwd` | Directory containing `pkggen.yaml` |

## Intended design (target, not yet implemented)

This section is the author's plan for pkggen. The code does **not** match it yet — see "Current state vs. intended design" below and the "Architecture" section for what exists today.

pkggen takes inspiration from funtoo-metatools but extends the idea to the entire desktop application packaging lifecycle: repository creation → fetching → packaging → testing → deployment.

**Cross-platform requirement:** every stage except testing and deployment (i.e. repository creation, fetching and packaging) must run on any OS, including non-Linux ones (Windows, macOS, BSDs). Generators must not depend on the host being the target distribution or on distro tools (`rpmbuild`, `reprepro`, `createrepo_c`, …) or shell-specific behaviour; they only write text files. Target-side placeholders such as dnf/zypper's `$releasever`/`$basearch` are written out literally and expanded later by the client's package manager, so they are fine.

### Generators in general
- Generators are scripts (Python today, but any language should work) that augment packages at a specific step of the pipeline.
- Every generator reads the contents of `pkggen.yaml` and uses the data relevant to its step.
- Shared functionality for a generator type lives in a `lib.py` file in that generator type's directory.

### Pipeline stages

1. **Distribution list** — `generators/distributions/distributions.yaml` lists every supported distribution with its repology names (`rp-names`) for dependency lookups. Other generators use this list to generate repositories and query dependencies.

2. **Repository generators** (`generators/repositories/`) — build the skeleton for a package manager's repository layout (PPA, Gentoo overlay, etc.). Meant to run once. They read the `repositories` key in `pkggen.yaml`. Command: `pkggen gen-repo <distribution>`.

3. **Fetch generators** (`generators/generation/`) — often just called "generators" (a naming leftover from funtoo-metatools; "fetch generator" is the correct term). pkggen scans the top-level keys of `pkggen.yaml`; every key other than `repositories`, `test` and `deployment` is a package group handled by a fetch generator. Each group must have a `generator` key naming a valid fetch generator. The fetch generator queries the user's source control and package repositories for new versions, including binary/source tarballs, and returns the final URLs, alias names and hashes per artifact to pkggen.

4. **Packaging generators** (`generators/packaging/`) — per-distribution toolkit scripts that take everything pkggen now knows about the package's files and produce package files consumable by users and package managers (ebuilds, spec files, PKGBUILDs, …). Output goes into the repository created in stage 2.
   `pkggen generate` = stage 3 + stage 4, and exits once the package files are in the repository.

5. **Testing generators** — `pkggen test`. They share a `lib.py` with startup functions that launch a container or VM. Each repository's testing generator calls one of these to start a VM/container, runs the setup steps from the `test` key in `pkggen.yaml`, builds the package with the distribution's build system, installs it, then runs the post-setup steps from the same `test` key.

6. **Deployment generators** — `pkggen deploy`. They use the `deployment` key in `pkggen.yaml`. A per-repository, per-distribution-type deployment generator updates the upstream repository with the new packages. Many distributions may not need this step. Deployment usually needs secrets, read from environment variables or the standard secrets file (see below).

### Secrets
Secrets are stored in `$XDG_CONFIG_HOME/pkggen/secrets.yaml` (defaults to `~/.config/pkggen/secrets.yaml`), or `%APPDATA%\pkggen\secrets.yaml` on Windows. Loaded by `load_secrets()` in `generators/generation/lib.py`. Today only `github_key` is used. Deployment generators should also accept secrets from environment variables.

### Current state vs. intended design
- `generators/packaging/` exists but is empty — no packaging generators yet.
- `pkggen test` and `pkggen deploy` are stubs; there are no testing or deployment generator directories or `lib.py` files.
- `generate.py` only skips the `repositories` key, not `test`/`deployment`, and returns after the first package group instead of processing all of them.

## Architecture (current implementation)

pkggen has two distinct generator layers, both invoked as **subprocesses** that communicate via **JSON over stdin/stdout**:

### 1. Fetch generators (`generators/generation/`)
Resolve package metadata and download artifacts for a given package. Each generator (`github.py`, `url.py`) reads a JSON package descriptor from stdin and prints a JSON result (version string + tarball URLs with checksums) to stdout. `lib.py` is a shared utility for hashing, downloading, and secrets loading.

`generate.py` reads `pkggen.yaml`, finds the matching generator file, then runs it concurrently (up to 10 threads via `ThreadPoolExecutor`) for all packages in that group.

### 2. Repository generators (`generators/repositories/`)
Create distribution-specific repository skeletons: `gentoo.py` (overlay: `profiles/`, `metadata/layout.conf`, `repositories.xml`), `rpm-based.py` (rpmbuild tree + optional dnf/zypper `.repo` file), `debian.py` (reprepro APT layout: `conf/distributions`, `conf/options`, `packages/`, optional deb822 `.sources` file) and `arch.py` (just a `.gitignore`). Each reads the full `pkggen.yaml` as JSON from stdin and writes into its working directory (the `-o` output dir).

Naming convention: generator filename == `name` in `distributions.yaml` == key under `repositories:` in `pkggen.yaml` (e.g. `rpm-based.py` ↔ `name: rpm-based` ↔ `repositories.rpm-based`).

`lib.py` holds the shared helpers: `load_repo_config(dist, required=...)`, `write_file()` (skips existing files unless `PKGGEN_FORCE=1`, which `gen-repo --force` sets), `make_dirs()`, `lines()`, `as_list()`, `to_bool()`, `warn()`/`fail()`. Generators report progress on stderr; stdout is unused.

File contents style: when a file's content is fixed (optionally with f-string values filled in), write it as a `"""\` string literal starting at column 0 and ending with a newline. Use `lib.lines(*entries)` only when the lines are built up in code (conditional appends, loops).

`gen_repo.py` discovers available distributions by cross-referencing `utils.load_distributions()` with the presence of a matching `.py` file in `repositories/`.

### Config files
- **`pkggen.yaml`** (project root) — defines package groups and repository metadata. Top-level keys other than `repositories` are treated as package group definitions, each requiring a `generator` name and a `packages` list.
- **`$XDG_CONFIG_HOME/pkggen/secrets.yaml`** (default `~/.config/pkggen/secrets.yaml`; `%APPDATA%\pkggen\secrets.yaml` on Windows) — API keys. Currently used key: `github_key` for authenticated GitHub API requests.

### Package templates
Templates (e.g., `example/packages/untitled-cli-parser/PKGBUILD.tmpl`) use Jinja2 `{{ variable }}` syntax. Template variables are populated from the generator output merged with package-level metadata from `pkggen.yaml`. Available variables include `name`, `version`, `description`, `license`, `github.user`, `github.repo`, and `artifacts[i].checksums.*`.

### Adding a new fetch generator
1. Create `generators/generation/<name>.py` — read input via `lib.readinput()`, parse JSON, print result JSON to stdout.
2. Reference it as `generator: "<name>"` in `pkggen.yaml`.

### Adding a new repository generator
1. Create `generators/repositories/<name>.py` — get its config with `lib.load_repo_config("<name>")`, write files into `cwd` with `lib.write_file()`.
2. Add `- name: <name>` to `generators/distributions/distributions.yaml` with its repology `rp-names` (and optional `rp-names-wildcards`).

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- When graphify-out/graph.json exists, run `graphify query "<question>"` before your first grep, search or source-file read for a codebase question, and put this rule in every subagent prompt that explores code. Read raw files only for lines the graph or an earlier read already located. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.
