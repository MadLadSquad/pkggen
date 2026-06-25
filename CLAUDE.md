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

Available commands: `generate`, `gen-repo <distribution>`, `test` (stub), `deploy` (stub), `repology <package>`, `version`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PKGGEN_GENERATORS_PATH` | `cwd` | Path to the `generators/` directory |
| `PKGGEN_RUN_PATH` | `cwd` | Directory containing `pkggen.yaml` |

## Architecture

pkggen has two distinct generator layers, both invoked as **subprocesses** that communicate via **JSON over stdin/stdout**:

### 1. Fetch generators (`generators/generation/`)
Resolve package metadata and download artifacts for a given package. Each generator (`github.py`, `url.py`) reads a JSON package descriptor from stdin and prints a JSON result (version string + tarball URLs with checksums) to stdout. `lib.py` is a shared utility for hashing, downloading, and secrets loading.

`generate.py` reads `pkggen.yaml`, finds the matching generator file, then runs it concurrently (up to 10 threads via `ThreadPoolExecutor`) for all packages in that group.

### 2. Repository generators (`generators/repositories/`)
Create distribution-specific repository folder structures (e.g., Gentoo overlay layout, RPM build tree). Each generator (`gentoo.py`, `rpm-based.py`) reads the full `pkggen.yaml` JSON from stdin and creates files/dirs in the working directory. `lib.py` only provides `readinput()`.

`gen_repo.py` discovers available distributions by cross-referencing `generators/distributions/distributions.yaml` with the presence of a matching `.py` file in `repositories/`.

### Config files
- **`pkggen.yaml`** (project root) — defines package groups and repository metadata. Top-level keys other than `repositories` are treated as package group definitions, each requiring a `generator` name and a `packages` list.
- **`~/.config/pkggen/secrets.yaml`** (or `%APPDATA%\pkggen\secrets.yaml` on Windows) — API keys. Currently used key: `github_key` for authenticated GitHub API requests.

### Package templates
Templates (e.g., `example/packages/untitled-cli-parser/PKGBUILD.tmpl`) use Jinja2 `{{ variable }}` syntax. Template variables are populated from the generator output merged with package-level metadata from `pkggen.yaml`. Available variables include `name`, `version`, `description`, `license`, `github.user`, `github.repo`, and `artifacts[i].checksums.*`.

### Adding a new fetch generator
1. Create `generators/generation/<name>.py` — read input via `lib.readinput()`, parse JSON, print result JSON to stdout.
2. Reference it as `generator: "<name>"` in `pkggen.yaml`.

### Adding a new repository generator
1. Create `generators/repositories/<name>.py` — read YAML config via `lib.readinput()`, create files in `cwd`.
2. Add the distribution to `generators/distributions/distributions.yaml` with its repology `rp-names`.
