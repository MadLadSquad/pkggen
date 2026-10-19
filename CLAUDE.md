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

Available commands: `generate [-p <packages>] [-d <distributions>]`, `gen-repo <distribution> [-o <dir>] [-f]`, `test` (stub), `deploy` (stub), `repology <package>`, `version`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PKGGEN_GENERATORS_PATH` | `cwd` | Path to the `generators/` directory |
| `PKGGEN_RUN_PATH` | `cwd` | Directory containing `pkggen.yaml` |
| `PKGGEN_DEBUG` | unset | `1` makes generators print a traceback with their error |
| `SOURCE_DATE_EPOCH` | unset | Fallback date for templates when a fetch generator reports no release date |

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
Secrets are stored in `$XDG_CONFIG_HOME/pkggen/secrets.yaml` (defaults to `~/.config/pkggen/secrets.yaml`), or `%APPDATA%\pkggen\secrets.yaml` on Windows. Loaded by `load_secrets()` in `generators/generation/lib.py`. Today `github_key` (GitHub), `gitlab_key` (gitlab.com) and `gitlab_keys` (a `{<domain>: <token>}` map for self-hosted GitLab instances) are used. Deployment generators should also accept secrets from environment variables.

### Current state vs. intended design
- `pkggen test` and `pkggen deploy` are stubs; `generators/testing/` and `generators/deployment/` only hold a `.gitkeep`.

## Architecture (current implementation)

pkggen has three generator layers. All of them are invoked as **subprocesses** (`sys.executable <script>`) that communicate via **JSON over stdin/stdout**; progress and errors go to stderr. Every layer has a `lib.py` with its shared helpers.

### 1. Repository generators (`generators/repositories/`)
Create distribution-specific repository skeletons: `gentoo.py` (overlay: `profiles/`, `metadata/layout.conf`, `repositories.xml`), `rpm-based.py` (rpmbuild tree + optional dnf/zypper `.repo` file), `debian.py` (reprepro APT layout: `conf/distributions`, `conf/options`, `packages/`, optional deb822 `.sources` file) and `arch.py` (just a `.gitignore`). Each reads the full `pkggen.yaml` as JSON from stdin and writes into its working directory.

The working directory is `-o` if given, otherwise the repository path: `<pkggen.yaml dir>/pkggen-build/<dist>`, or `repositories.<dist>.path` (relative to `pkggen.yaml`) when set. `utils.get_repo_path()` resolves it; `generate` uses the same function to find the repositories.

Naming convention: generator filename == `name` in `distributions.yaml` == key under `repositories:` in `pkggen.yaml` == packaging generator filename == default template folder (e.g. `rpm-based.py` ↔ `name: rpm-based` ↔ `repositories.rpm-based` ↔ `packaging/rpm-based.py` ↔ `templates/rpm-based/`).

`lib.py` holds the shared helpers: `load_repo_config(dist, required=...)`, `write_file()` (skips existing files unless `PKGGEN_FORCE=1`, which `gen-repo --force` sets; always writes LF), `make_dirs()`, `lines()`, `as_list()`, `to_bool()`, `warn()`/`fail()`. Generators report progress on stderr; stdout is unused.

File contents style: when a file's content is fixed (optionally with f-string values filled in), write it as a `"""\` string literal starting at column 0 and ending with a newline. Use `lib.lines(*entries)` only when the lines are built up in code (conditional appends, loops).

`gen_repo.py` discovers available distributions by cross-referencing `utils.load_distributions()` with the presence of a matching `.py` file in `repositories/`.

### 2. Fetch generators (`generators/generation/`)
`github.py`, `gitlab.py` and `url.py` read one package (JSON) from stdin and print one JSON result on stdout:
```json
{ "version": "1.2.0", "date": "<ISO 8601 UTC, optional>", "tag": "<releases/tags>", "commit": "<commits>",
  "artifacts": [ { "url": "...", "alias": "pkg-1.2.0.tar.gz", "type": "source|asset",
                   "directory": "pkg-1.2.0", "size": 1234, "checksums": { "sha2_256": "...", ... } } ],
  "exports": { "description": "...", "license": "MIT", "homepage": "..." } }
```
`alias` is the unique distfile name, `directory` a best guess of the folder the archive extracts to, `checksums` holds md5, sha1, sha2_224…sha2_512, sha3_224…sha3_512, blake2b and blake2s, `exports` only contains keys the package doesn't set itself. The input also carries `position`, the tqdm bar line to use.

`lib.py`: `run(generate)` (reads stdin, prints the JSON result, turns `TinyError`/network errors into one `Error [pkg]: ...` line on stderr and exit code 1), `download_and_hash()` (streams a URL and hashes it in one pass, without decompressing), `http_error()` (messages with URL, status and rate-limit hints), `apply_transforms()`, `to_bool()`, `load_secrets()`, `archive_stem()`.

GitHub release queries with `artifacts` download only the listed assets (a missing one is an error); `include-source: true` also adds GitHub's auto-generated tarball. Auto-generated tarballs use `https://<domain>/<user>/<repo>/archive/<ref>.tar.gz`.

`gitlab.py` mirrors `github.py` (same keys and queries, a `gitlab:` block, placeholders `{gitlab_user}`/`{gitlab_repo}`). Differences: `user` may contain subgroups; `domain` (default `gitlab.com`, may include a path prefix) replaces `domain`/`api-domain`; GitLab has no drafts or pre-releases, so `include-upcoming` (releases dated in the future) replaces `include-drafts`/`include-pre-releases`; release `artifacts` are matched against release asset links and use their `direct_asset_url`; auto-generated tarballs use `https://<domain>/<user>/<repo>/-/archive/<ref>/<repo>-<ref>.tar.gz` and extract to `<repo>-<ref>` (leading `v` kept, `/` → `-`); Licensee license keys are mapped to the SPDX ids GitHub reports. The token is only sent to the instance's own host.

### 3. Packaging generators (`generators/packaging/`)
`arch.py`, `debian.py`, `gentoo.py`, `rpm-based.py`. Each reads a job from stdin: `{distribution, package (template context), repository (repositories.<dist>), repository_path, templates_path, default_templates_path, run_path}`, and prints `{output, files_written, files_unchanged, warnings}` on stdout.

Template folders live next to `pkggen.yaml` in `packages/<dist>/<package>/`. If a folder is missing or has no `.tmpl` files, the generator copies `generators/templates/<dist>/` into it and warns; if only some required templates are missing, just those are copied (existing files are never overwritten). Then every `*.tmpl` file is rendered with Jinja2 and every other file is copied:

| Generator | Required templates | Output |
|---|---|---|
| `arch.py` | `PKGBUILD.tmpl` (default also has `.SRCINFO.tmpl`) | `<repo>/<name>/` |
| `debian.py` | `debian/control.tmpl`, `debian/rules.tmpl`, `debian/changelog.tmpl` | `<repo>/packages/<name>/debian/…` (`rules` made executable) |
| `gentoo.py` | `ebuild.tmpl` | `<repo>/<category>/<name>/<name>-<version>.ebuild` + other files, plus a `Manifest` with DIST entries built from `artifacts` (existing entries for other distfiles are kept). `category` comes from the package (or its `gentoo:` block), falling back to the first `repositories.gentoo.categories` entry with a warning. The version must be a valid Gentoo version |
| `rpm-based.py` | `spec.tmpl` | `spec.tmpl` → `<repo>/SPECS/<name>.spec`, other files → `<repo>/SOURCES/` |

`lib.py`: `Job`, `ensure_templates()`, `render_tree(job, place, executable=())`, `write_output()` (LF endings, only rewrites changed files), `run(main)`. The Jinja environment uses `StrictUndefined` (errors are reported as `<template>:<line>: ...`), `trim_blocks`, `lstrip_blocks` and `keep_trailing_newline`. Avoid `{#` in shell code inside templates: Jinja reads it as a comment.

### Orchestration (`src/pkggen/generate.py`)
`pkggen generate` loads every package group (every top-level key except `repositories`, `test`, `deployment`), validates them all up front, then runs the fetch generators in a thread pool (up to 10). As each fetch finishes, it builds the template context and submits one packaging job per target distribution to a second pool (`os.cpu_count()` workers), so packaging overlaps with downloads. Targets are distributions that have a packaging generator **and** a `repositories.<dist>` block; a missing repository directory is reported with a hint to run `gen-repo`. Failures never cancel other jobs; a summary lists them at the end and the exit code is 1.

### Config files
- **`pkggen.yaml`** (project root) — defines package groups and repository metadata. Top-level keys other than `repositories`, `test` and `deployment` are package groups, each requiring a `generator` name and a `packages` list. Other keys of a group are defaults for its packages, deep-merged under each package's own keys, except `maintainer` and `author`, which can only be set per package (a group that sets them is an error). Optional package fields every packaging generator reads (checked up front by `check_package()` in `generate.py`):
  - `maintainer`: string, `"Name <email>"`. The packager: Debian `Maintainer:` and changelog trailer, PKGBUILD `# Maintainer:` header, RPM `%changelog`, Gentoo `metadata.xml` `<maintainer>` (needs the email, else the overlay `owners` are used).
  - `author`: string, `"Name <email>"` or just a name. Upstream author: Debian `copyright` `Copyright:` field, Gentoo `metadata.xml` `<upstream><maintainer>`.
  - `architectures`: `"any"` (builds on every architecture) or a list of names, default `[amd64, arm64]`. Each packaging generator translates it with `lib.architectures(job, names, any_value, other=None)` into the template variable `arches`: arch → `x86_64`/`aarch64`, `any` → `['any']`; debian → `amd64`/`arm64`, `any` → `any`; rpm-based → `ExclusiveArch: x86_64 aarch64`, `any` → no `ExclusiveArch`; gentoo → `~<arch>` keywords, other names passed through, `any` → every main Gentoo arch. Names a generator doesn't know are dropped; if none is left it fails. Packages without compiled code override it per distribution (`debian: { architecture: all }`, `arch: { arch: [any] }`, `BuildArch: noarch` in the spec).
- **`$XDG_CONFIG_HOME/pkggen/secrets.yaml`** (default `~/.config/pkggen/secrets.yaml`; `%APPDATA%\pkggen\secrets.yaml` on Windows) — API keys. Currently used keys: `github_key` for authenticated GitHub API requests, `gitlab_key` for gitlab.com and `gitlab_keys: { <domain>: <token> }` for other GitLab instances.

### Template context
Templates see the package's own `pkggen.yaml` keys at the top level (`name`, `github.user`, `maintainer`, …), plus:
- `version`, `date` (datetime), `tag`, `commit`, `artifacts` (fetch output), `source` (= `artifacts[0]`)
- `description`, `license`, `homepage` (package value, else fetch `exports`)
- `distribution`, `repository` (its `repositories.<dist>` block), `now`
- `dist`: the package's own `<distribution>:` block, e.g. `arch: { depends: [glibc] }` → `dist.depends`
- `architectures` (default `[amd64, arm64]`) and `arches`, its translation for this distribution
- `maintainer` and `author` are undefined when not set: use `maintainer | default("Your Name <you@example.com>")`

Filters: `pkgver` (`-`, `:`, `/`, whitespace → `_`), `spdx` (deprecated GitHub SPDX ids → current ones), `debian_license`, `gentoo_license`, `rfc2822`, `rpmdate`, `shquote`, `dquote`, `person_name`/`person_email` (`"Jane <j@x.org>"` → `Jane` / `j@x.org`).

### Adding a new fetch generator
1. Create `generators/generation/<name>.py`: define `generate(package) -> dict` returning the output contract above and end with `lib.run(generate)`. Log only to stderr.
2. Reference it as `generator: "<name>"` in `pkggen.yaml`.

### Adding a new repository generator
1. Create `generators/repositories/<name>.py` — get its config with `lib.load_repo_config("<name>")`, write files into `cwd` with `lib.write_file()`.
2. Add `- name: <name>` to `generators/distributions/distributions.yaml` with its repology `rp-names` (and optional `rp-names-wildcards`).

### Adding a new packaging generator
1. Create `generators/packaging/<name>.py`: set `job.context["arches"] = lib.architectures(job, {"amd64": ..., "arm64": ...}, <any value>)`, call `lib.ensure_templates(job, REQUIRED)`, set `job.output`, call `lib.render_tree(job, place)` with a `place(rel) -> absolute path` mapping, and end with `lib.run(main)`.
2. Add the default templates in `generators/templates/<name>/`. Keep them language-neutral: no build-system-specific build/install steps or build dependencies (pkggen packages projects in any language), keep the build functions as empty, commented-out stubs (e.g. `#build() {` / `#}`).

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- When graphify-out/graph.json exists, run `graphify query "<question>"` before your first grep, search or source-file read for a codebase question, and put this rule in every subagent prompt that explores code. Read raw files only for lines the graph or an earlier read already located. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.
