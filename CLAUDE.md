# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running pkggen

The virtualenv lives in `src/venv/`. Set it up once:
```sh
cd src && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
```

Run directly (no install required):
```sh
cd src && PKGGEN_RUN_PATH=../example python3 pkggen/pkggen <command>
```

Or install as a CLI tool (pulls in the dependencies; works on every OS):
```sh
pip install ./src      # copies generators/ into the package, as pkggen/generators
pip install -e ./src   # editable: uses the checkout's generators/, so edits apply right away
# then from any directory with a pkggen.yaml:
pkggen <command>
```

The CLI is `src/pkggen/cli.py` (the `pkggen.cli:main` console script); `src/pkggen/pkggen` is a launcher that runs it from a checkout. The modules import each other by plain name (`import utils`), so `cli.py` puts its folder on `sys.path` first. `src/setup.py` bundles `generators/` in `build_py` (skipped for editable installs) and adds it to sdists, so wheels built from an sdist have it too.

Available commands: `generate [-p <packages>] [-d <distributions>]`, `gen-repo <distribution> [-o <dir>] [-f]`, `test [-p <packages>] [-d <distributions>] [-t <targets>] [-j <n>] [--engine podman|docker] [--keep]`, `deploy [-p <packages>] [-d <distributions>] [-t <targets>] [--skip-tests] [--dry-run]`, `repology <package>`, `version`.

## Running tests

```sh
pip install -r src/requirements-dev.txt   # pytest + responses, on top of requirements.txt
python -m pytest                           # from the repository root; offline, ~20 s
python -m pytest --live                    # also hit the real GitHub/repology APIs (or PKGGEN_TEST_LIVE=1; GITHUB_TOKEN avoids rate limits)
python -m pytest --containers              # also build in real podman/docker containers (or PKGGEN_TEST_CONTAINERS=1; needs the network)
python -m pytest tests/core/test_install.py # pip-installs pkggen (normal + editable) into a venv, offline, and runs it
python -m pytest --update-golden           # rewrite tests/golden/ after an intended output change
```

`tests/` mirrors the code: `core/` (CLI, `utils`, `gen_repo`, `generate`, `testing`, `containers`, `deploy`, repology), `repositories/`, `generation/`, `packaging/`, `testing/`, `deployment/` (real `git` and `ssh-keygen`, with local bare repositories as the upstreams), `e2e/` (the full `gen-repo` + `generate` pipeline on a copy of `example/`, compared against `tests/golden/example.snapshot`), `live/` and `containers/` (`pkggen test` with a real engine, which sees the user's `CONTAINER_HOST`/`DOCKER_HOST` and Podman configuration through the `real_engine` fixture, so on macOS/Windows it reuses the `pkggen` machine; `PKGGEN_TEST_CONTAINERS_TARGETS` picks the releases or `all`, `PKGGEN_TEST_CONTAINERS_ENGINE` the engine, `PKGGEN_TEST_CONTAINERS_LOGS` a folder to copy the logs to). `.github/workflows/tests.yaml` runs the offline tests on Linux, macOS and Windows on every push and pull request. Run it by hand (workflow_dispatch) to also run the live tests, or the container tests on `ubuntu-latest` with Podman and Docker (the `targets` input picks the releases; the logs are uploaded as artifacts). Hosted macOS/Windows runners can't run a Linux VM, so the container tests only run on Linux.

- Tests never touch the user's config or `example/`: an autouse fixture points `XDG_CONFIG_HOME`/`APPDATA` at a temp dir (so managed SSH keys land there too), points `GIT_CONFIG_GLOBAL` at a gitconfig in it that only sets `init.defaultBranch = main` (with `GIT_CONFIG_NOSYSTEM=1`), hides `CONTAINER_HOST`/`DOCKER_HOST` and the deployment variables below, and `example_project` works on a copy. The only exception is the real container test: its `real_engine` fixture links the user's `<config dir>/containers` into the temp dir and restores those two variables, so the real engine uses the user's machines and connections (pkggen's own secrets stay isolated).
- Generators run either in-process via `tests.support.helpers.load_generator(layer, name)` (swaps in that layer's `lib`), or as real subprocesses via `run_generator()` / `run_cli()`.
- HTTP is mocked with `responses`: the `http` fixture in-process, `http_subprocess(routes)` for subprocesses (via `tests/support/http_shim/sitecustomize.py`, enabled by `PKGGEN_TEST_HTTP`). Unmatched requests fail, so nothing reaches the network. Canned GitHub data for the example packages is in `tests/support/example.py`.
- Container engines are faked by `tests/support/fake_engine.py` (the `fake_engine` fixture sets `PKGGEN_TEST_ENGINE`): it records every command, keeps a fake file system per container for `cp`, fails steps containing `FAKE_FAIL` and emulates `podman machine`. Patch `containers.host_platform` to test the VM paths.
- Tests that expose known bugs are marked `xfail(strict=True)` with the reason; fixing the bug makes them fail until the mark is removed.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PKGGEN_GENERATORS_PATH` | bundled | Path to the `generators/` directory. Default: the copy an installed pkggen ships with, else the checkout's `generators/` (editable installs, running from `src/`), else `cwd` |
| `PKGGEN_RUN_PATH` | `cwd` | Directory containing `pkggen.yaml` |
| `PKGGEN_DEBUG` | unset | `1` makes generators print a traceback with their error |
| `SOURCE_DATE_EPOCH` | unset | Fallback date for templates when a fetch generator reports no release date |
| `CONTAINER_HOST` / `DOCKER_HOST` | unset | A remote container host for `pkggen test`; when set, pkggen doesn't start a VM |
| `PKGGEN_TEST_ENGINE` | unset | Tests only: a JSON argv list that replaces the podman/docker command |
| `PKGGEN_PASSWORD_<NICK>` / `PKGGEN_SSH_KEY_<NICK>` | unset | A deployment password / SSH key path for nickname `<NICK>` (upper-cased, non-alphanumerics → `_`); wins over `secrets.yaml` (for CI) |
| `PKGGEN_TEST_NOW` | unset | Tests only: an ISO 8601 date deployment generators use as "now", to test SSH key expiry |
| `PKGGEN_TEST_AUR_URL` | unset | Tests only: replaces the AUR's fetch and push URLs, with `{pkgbase}` for the package |

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

5. **Testing generators** — `pkggen test`. Each distribution's testing generator gives pkggen a container (or VM) configuration per release it supports; pkggen starts them itself (in a Linux VM when the host isn't Linux), runs the generator's and the user's setup steps from the `test` key in `pkggen.yaml`, builds every package with the distribution's build system, installs it, then runs the post-setup steps from the same `test` key. The `test` key also sets the CPU/RAM budget.

6. **Deployment generators** — `pkggen deploy`. They use the `deployment` key in `pkggen.yaml`. A per-repository, per-distribution-type deployment generator updates the upstream repository with the new packages. Many distributions may not need this step. Deployment usually needs secrets, read from environment variables or the standard secrets file (see below).

### Secrets
Secrets are stored in `$XDG_CONFIG_HOME/pkggen/secrets.yaml` (defaults to `~/.config/pkggen/secrets.yaml`), or `%APPDATA%\pkggen\secrets.yaml` on Windows. Loaded by `load_secrets()` in `generators/generation/lib.py`. Today `github_key` (GitHub), `gitlab_key` (gitlab.com) and `gitlab_keys` (a `{<domain>: <token>}` map for self-hosted GitLab instances) are used by fetch generators, and `git_passwords` / `ssh_keys` (`{<nickname>: <password>}` / `{<nickname>: <private key path>}`) by deployment generators, which load them with their own `load_secrets()` in `generators/deployment/lib.py` and also accept them from environment variables (`PKGGEN_PASSWORD_<NICK>`, `PKGGEN_SSH_KEY_<NICK>`). pkggen-managed SSH keys live next to it in `keys/`.

### Current state vs. intended design
- Deployment generators exist for git remotes (`git`) and the AUR (`aur`) only.
- `pkggen test` only uses containers: VMs are only used to host them on macOS/Windows.

## Architecture (current implementation)

pkggen has five generator layers. All of them are invoked as **subprocesses** (`sys.executable <script>`) that communicate via **JSON over stdin/stdout**; progress and errors go to stderr. Every layer has a `lib.py` with its shared helpers.

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

### 4. Testing generators (`generators/testing/`)
`arch.py`, `debian.py`, `gentoo.py`, `rpm-based.py`. They only *describe* containers; `src/pkggen/testing.py` runs them. Each has a `TARGETS` table `{rp-name: {name, image, arches?, ...}}` that must cover exactly the distribution's `rp-names` in `distributions.yaml` (a test checks this). Input on stdin: `{distribution, repository, test: {setup, post-setup} (the user's steps, global then per distribution), rp_names, versions (null or test.distributions.<dist>.versions), host_arch, packages: [{context, output}]}` (`output` relative to the repository; `packages` is empty when pkggen only asks for the targets). Output: `{targets: [...], skipped: [{name, rp_name}], warnings}` (`skipped`: the releases with no image for `host_arch`, so `-t` can name them and get a warning instead of an "unknown target" error), each target being `{name, rp_name, image, platform, data_images: [{image, path}], env, repo, setup, prepare, packages: [{name, build, install}], finalize, post_setup, collect}`; a step is `{name, run (sh script), user, workdir}` and runs in its own `sh -exc`. `data_images` are created and run once with `--entrypoint true` (Podman only fills their volumes on first start), so they must contain `true`.

| Generator | Releases (target names) | Build → install → finalize |
|---|---|---|
| `arch.py` | `arch` (`archlinux:base-devel`, amd64 only) | `makepkg -s` as user `builder` → `pacman -U` → packages moved to `<repo>/<arch>/` + `repo-add <repositories.arch.name or pkggen>.db.tar.zst` |
| `debian.py` | `debian-unstable/14/13/12`, `ubuntu-26.10/26.04/24.04/22.04` | source downloaded and checked, `<name>_<version>.orig.tar.*`, `debian/` copied in, `apt-get build-dep`, `dpkg-buildpackage -b` → `apt-get install` → `reprepro includedeb` into a rewritten, unsigned `conf/distributions` with only the release's suite (from `repositories.debian.suites` when its codename/suite matches) |
| `gentoo.py` | `gentoo` (`gentoo/stage3` + `gentoo/portage` tree via `--volumes-from`) | overlay registered in `repos.conf`, `~<arch>` accepted, category from the packaging output (added to `profiles/categories` if missing); deps with `emerge --onlydeps` (binhost), `ebuild … install` → `ebuild … qmerge` → `quickpkg` into `binpkgs/` |
| `rpm-based.py` | `fedora-rawhide/44/43`, `opensuse-tumbleweed`, `opensuse-leap-15.6`, `openmandriva-rolling/6.0` | sources downloaded into `SOURCES/` and checked (no `spectool`), deps via `dnf builddep` or `rpmspec --buildrequires \| zypper in`, `rpmbuild -ba` with `_topdir` = the repository → `dnf`/`zypper install` → `createrepo_c RPMS` |

Inside the containers the repository is at `/pkggen/repo`, scratch space at `/pkggen/work` and `/pkggen/state`, and the finished repository is collected from `/pkggen/out`. `lib.py`: `Job`, `Package`, `select_targets(job, TARGETS)`, `target()`, `step()`, `user_steps()`, `download()` (curl + `sha256sum -c`), `heredoc()`, `tarball_extension()`, `collect_step()`, `shquote()`, `run(main)`.

### Running the tests (`src/pkggen/testing.py`, `src/pkggen/containers.py`)
`pkggen test` checks `pkggen.yaml` (`load_test_config()`, one error listing every problem) and the packages, picks the engine (`Engine.detect()`: `--engine`, `test.engine`, else podman then docker, all through their CLIs), and calls `prepare_host()`: on Linux or with a remote host (`test.connection`, `CONTAINER_HOST`, `DOCKER_HOST`) nothing happens. On macOS/Windows with Podman, `ensure_machine()` creates, resizes or starts the rootful `pkggen` Podman machine with the budget, capped by `machine_budget()` to the host's CPUs and to its RAM minus a quarter (at least 2 GiB), with a warning (or uses another machine that's already running on macOS). When pkggen sized its own machine, that machine is the budget: `prepare_host()` clears `cpus`/`memory` so the containers share all of it. WSL machines (the Windows default) are never resized (`podman machine set` can't, they get `.wslconfig`'s resources), so there the budget still limits the containers, with a warning; the VM type comes from `machine list`, which `machine inspect` doesn't report. With Docker, Docker Desktop/Colima must be running. BSDs need a remote host. It then asks every testing generator for its targets, and `plan_resources()` splits the budget: `jobs` containers at once (default `min(targets, cpus // 2, GiB // 2)`), each with `--cpus total/jobs` and `--memory total/jobs` (dropped with a warning when the engine's cgroups lack the controller), and `MAKEFLAGS`/`MAKEOPTS`/`RPM_BUILD_NCPUS`/`DEB_BUILD_OPTIONS`/`PKGGEN_JOBS` set to match.

A thread pool of `jobs` workers starts the containers (`run -d --entrypoint sleep … infinity`, labelled `pkggen.session=<id>`) and runs their setup while the main thread runs `generate.run_pipeline()` on the host. Once a distribution's packages are generated, its testing generator is run again with them. Each worker then copies the repository in (`cp`, which also works with remote engines), runs prepare, then build + install per package (a failure skips that package only), finalize (always) and post-setup (only if nothing failed), and copies `collect` out to `pkggen-build/test/<target>/`. Step output goes to `pkggen-build/test/<target>.log`; the terminal only gets one line per event. Containers are removed at the end, on errors and on Ctrl-C (by label), unless `--keep` is given.

`test:` in `pkggen.yaml` (all optional): `engine`, `connection`, `cpus`, `memory` (`16G`, `512M`, or a number of GiB), `jobs`, `vm: {disk}`, `pull` (`always`/`missing`/`never`/`newer`), `setup`/`post-setup` (lists of commands, or `{name, run, user, workdir}`), and `distributions: {<dist>: {versions, setup, post-setup}}`, where `versions` holds rp-names or target names.

When the run ends (not on Ctrl-C), `write_results()` merges every target of the run into `pkggen-build/test/results.json` (`load_results()` reads it): `{"targets": {<target>: {distribution, ok, finished, output (the folder in pkggen-build/test/, or null), packages: {<name>: {version, ok}}, failed?: [first lines], skipped?: reason}}}`. Entries of targets that weren't run are kept; releases a testing generator reports as `skipped` (no image for the host's architecture) are written with `ok: false`, also when nothing could run, so an older result can't be deployed.

### 5. Deployment generators (`generators/deployment/`)
`git.py` and `aur.py` push what `pkggen test` collected. Input on stdin: `{check, id, where (e.g. deployment.rpm-based[1]), distribution, deployment (its pkggen.yaml block), repository, run_path, work_path (pkggen-build/deploy/<id>/), targets: [{name, path (its test output), ok, reason, packages: {<name>: {version, ok}}}], skipped_targets (selected, but with no output), packages (-p, or null), skip_tests, interactive, dry_run}`. With `check: true` a generator only validates its block and prints `{errors, warnings}`; otherwise it prints `{pushed, unchanged, failed: [{name, message}], skipped: [{name, reason}], warnings}`. Progress, warnings and prompts go to stderr; prompts read the terminal itself (`/dev/tty`, `CONIN$`), since stdin carries the job.

- `git.py` (`deployment.<dist>: {generator: git, url, username, email, ssh-key | password | managed-key, branch, targets, path, prune, preserve, message, sign}`): skips targets that failed their tests (`job.usable_targets()`, unless `skip_tests`), fetches the upstream into `work_path` (a pkggen-owned clone that is reset and cleaned every run; an empty remote gets an orphan branch; `branch` defaults to the remote's HEAD, else `main`), copies every target's output into `path` (default `{target}`; `""` = the root, only with one target) overwriting changed files and keeping executable bits, then commits as `username`/`email` and pushes (never forced; if the upstream moved meanwhile, everything is redone once on top of it). Files that only exist upstream are kept unless `prune: true`, which deletes them except paths where the file or a parent folder matches a `preserve` regex (`re.search` on the POSIX path relative to the root), and the folders of selected targets that aren't deployed this time. Commits aren't signed unless `sign: true` (`-c commit.gpgsign=false`), because signing keys that need a passphrase or a touch can't prompt there. Every git command gets `-c core.autocrlf=false -c core.longpaths=true` (`GIT_OPTIONS`), so files go upstream byte for byte even with Git for Windows' system-wide `autocrlf=true`, and paths over 260 characters (createrepo_c's `repodata/<sha256>-…`) can be added. `--dry-run` commits in `work_path` without pushing. `-p` doesn't apply (warning).
- Auth (`auth_env()`): `https` URLs with `password` get a `credential.helper` set through `GIT_CONFIG_COUNT/KEY_n/VALUE_n` that prints `$PKGGEN_GIT_USERNAME`/`$PKGGEN_GIT_PASSWORD` (the URL's user, else `username`), after an empty helper that clears the user's own; the secret is never on a command line or on disk. SSH URLs (`git@host:path`, `ssh://`) use `GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"` (+ `BatchMode=yes` without a terminal) with the `ssh-key` nickname's key, else a pkggen-managed key named `managed-key` (default `git-<host>`). Local paths and `file://` need nothing.
- `aur.py` (`deployment.arch: {generator: aur, username, email, ssh-key | managed-key, targets, prune, preserve, message, sign}`; `url`, `branch`, `password`, `path` are errors) loads `git.py` by path (`import git` could find GitPython) and reuses `auth_env()`/`deploy_tree()`. It deploys every PKGBUILD folder of the first target's output (`pkggen-build/test/arch/<name>/`, not the `x86_64/` pacman repository), one after another, each to `ssh://aur@aur.archlinux.org/<pkgbase>.git` on `master` (fetched anonymously from `https://aur.archlinux.org/<pkgbase>.git`, work clone `work_path/<pkgbase>`). Packages that failed or weren't tested are skipped unless `skip_tests`; a folder without `.SRCINFO` or with subfolders (the AUR rejects both) fails only itself, as does a failed push. `pkgbase` and the version (`[epoch:]pkgver-pkgrel`) come from `.SRCINFO`; `message` defaults to `Update to {version}` (`{name}` and `{version}` are replaced). Without `ssh-key` it uses the managed key `aur` and tells the user to add it at `https://aur.archlinux.org/account/<username>/edit`.
- Managed keys (`lib.managed_key(job, name, where_to_add)`): `<config dir>/pkggen/keys/<name>` (+ `.pub`, + `.json` with `created`/`expires`), made with `ssh-keygen -t ed25519 -N ""` and valid for 90 days (`KEY_LIFETIME`). Less than a week left (`KEY_WARNING`) → a warning; expired → a new key, and the old one's fingerprint to remove. After making a key pkggen prints the public key and where to add it, then waits for Enter when `interactive`, or fails that deployment with the instructions. A key without its `.json` counts from the file's modification time.

`lib.py`: `Job` (`usable_targets()`, `warn()`, `say()`, `key()`), `Target`, `DeployError`, `load_secrets()`, `secret_password()`, `secret_ssh_key()`, `secret_problem()`, `managed_key()`, `wait_for_user()`/`open_console()`, `check_keys()`, `check_string()`, `now()`, `run(main, check)`. `git.py` also exposes `url_kind()`, `prepare()`, `sync()`, `commit()`, `deploy_tree()` and `check_common()` (identity, auth, `prune`/`preserve`/`sign` checks shared with `aur.py`).

### Deploying (`src/pkggen/deploy.py`)
`pkggen deploy` loads `deployment` (`load_deployments()`: every `deployment.<dist>` is a mapping or a list of them, each with a `generator` in `generators/deployment/` and optional `targets`; ids are `<dist>-<generator>`, then `-2`, `-3`…; one error lists every problem), checks `-p` names with `generate.load_packages()`, needs `git` and `results.json`. `select_targets()` picks each deployment's targets (`targets`, else every target of its distribution in the results, narrowed by `-t`); targets without output are reported as skipped and passed as `skipped_targets`, the others are passed with their `ok`/`reason` (the generators apply the gate, per target for git and per package for the AUR). Then every generator runs in check mode (one error for all), and the deployments run one after another with stderr shown live (and kept for the summary). A failure doesn't stop the other deployments. The summary counts pushed (or "would be pushed" with `--dry-run`) and up-to-date repositories and lists every failure and skip; the exit code is 1 if there's any.

### Orchestration (`src/pkggen/generate.py`)
`pkggen generate` loads every package group (every top-level key except `repositories`, `test`, `deployment`), validates them all up front, then runs the fetch generators in a thread pool (up to 10). As each fetch finishes, it builds the template context and submits one packaging job per target distribution to a second pool (`os.cpu_count()` workers), so packaging overlaps with downloads. Targets are distributions that have a packaging generator **and** a `repositories.<dist>` block; a missing repository directory is reported with a hint to run `gen-repo`. Failures never cancel other jobs; a summary lists them at the end and the exit code is 1. `run_pipeline(config, package_filter, distribution_filter)` does the work and returns a `PipelineResult` (`packages`, `targets`, `contexts`, `packaged`, `failures`, `stats`), so `pkggen test` can reuse it.

### Config files
- **`pkggen.yaml`** (project root) — defines package groups and repository metadata. Top-level keys other than `repositories`, `test` and `deployment` are package groups, each requiring a `generator` name and a `packages` list. Other keys of a group are defaults for its packages, deep-merged under each package's own keys, except `maintainer` and `author`, which can only be set per package (a group that sets them is an error). Optional package fields every packaging generator reads (checked up front by `check_package()` in `generate.py`):
  - `maintainer`: string, `"Name <email>"`. The packager: Debian `Maintainer:` and changelog trailer, PKGBUILD `# Maintainer:` header, RPM `%changelog`, Gentoo `metadata.xml` `<maintainer>` (needs the email, else the overlay `owners` are used).
  - `author`: string, `"Name <email>"` or just a name. Upstream author: Debian `copyright` `Copyright:` field, Gentoo `metadata.xml` `<upstream><maintainer>`.
  - `architectures`: `"any"` (builds on every architecture) or a list of names, default `[amd64, arm64]`. Each packaging generator translates it with `lib.architectures(job, names, any_value, other=None)` into the template variable `arches`: arch → `x86_64`/`aarch64`, `any` → `['any']`; debian → `amd64`/`arm64`, `any` → `any`; rpm-based → `ExclusiveArch: %{?x86_64}%{!?x86_64:x86_64} aarch64` (the `%{x86_64}` macro where it's defined, because OpenMandriva builds x86_64 as `znver1`), `any` → no `ExclusiveArch`; gentoo → `~<arch>` keywords, other names passed through, `any` → every main Gentoo arch. Names a generator doesn't know are dropped; if none is left it fails. Packages without compiled code override it per distribution (`debian: { architecture: all }`, `arch: { arch: [any] }`, `BuildArch: noarch` in the spec).
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

### Adding a new testing generator
1. Create `generators/testing/<name>.py` (same name as the packaging generator) with a `TARGETS` table covering every rp-name of the distribution, and a `main(job)` that returns `[lib.target(job, rp, spec, setup=..., prepare=..., packages=[{name, build, install}], finalize=[..., lib.collect_step()])` for `lib.select_targets(job, TARGETS)`; end with `lib.run(main)`.
2. Use fully qualified image names (`docker.io/library/…`), so podman never asks which registry to use.

### Adding a new deployment generator
1. Create `generators/deployment/<name>.py` with `check(job) -> [errors]` (start with `lib.check_keys()`; messages name keys with `job.key("...")`) and `main(job)`, which fills `job.pushed` / `job.unchanged` / `job.failed` / `job.skipped` (or raises `lib.DeployError` when nothing can be deployed); end with `lib.run(main, check)`. Respect `job.dry_run`, apply the test gate (`job.usable_targets()` or the per-package `ok`) unless `job.skip_tests`, and keep scratch files in `job.work_path`.
2. Reference it as `deployment.<dist>.generator: "<name>"` in `pkggen.yaml`. A generator that pushes with git can reuse `git.py` by loading it by path, like `aur.py` does.

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
