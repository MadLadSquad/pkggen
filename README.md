# pkggen
[![MIT license](https://img.shields.io/badge/License-MIT-blue.svg)](https://lbesson.mit-license.org/)
[![Discord](https://img.shields.io/discord/717037253292982315.svg?label=&logo=discord&logoColor=ffffff&color=7389D8&labelColor=6A7EC2)](https://discord.gg/4wgH8ZE)

<img src="https://github.com/user-attachments/assets/585a52fa-3c75-40ff-a846-291899b1734c" width="225px" height="225px">

*<sub>Logo designed and created by <a href="https://www.instagram.com/_.insekhta._/">insekhta</a>.</sub>*

> [!CAUTION]
> pkggen is still not ready for use with many core features currently missing.

A drop-in replacement for your entire desktop packaging and deployment pipeline.

## Why should I use pkggen?
Currently, there is no universal way to automate package updating, dependency resolution, testing and deployment across multiple Unix distributions
and multiple versions of the same OS distribution at the same time. And due to distributions having completely different package managers, packaging formats
and package versions for your dependencies, a successful attempt at automating this process while completely covering all popular Unix platforms is simply
impossible without you also getting a desktop packaging PhD along the way.

Here is how pkggen solves all your package deployment pain points:

1. Designed for both small and large repositories - pkggen is so robust, it can be used to deploy everything from a single-app repository to an entire bleeding edge distribution
1. Completely automates away version bumps - it automatically fetches the latest release of your applications, downloads all sources, fetches checksums and verifies source integrity
1. Templatize everything - pkggen pre-processes all your packages with Jinja 2, ships with many default variables and allows you to plug your own too
1. Skip per-distribution boilerplate - pkggen automatically handles all distribution-specific setup logic so that you never have to worry about your development environment
1. Write dependencies once - list them by their [repology](https://repology.org) name, and pkggen looks up what every distribution release calls them
1. Develop once, test everywhere - pkggen allows you to automatically test all your packages and repositories on multiple operating systems in parallel
1. Zero-click deployments - pkggen allows you to go through the entire package update, testing and deployment phases without any user interaction
1. Modular design - whether you want to use your own scripts instead of the built-in ones, or you want to add a completely new platform, our modular design allows you to extend pkggen to fit any of your deployment needs
1. Fully documented including additional packaging tips and platform-specific resources

## The workflow
You write a `pkggen.yaml` file that details all common metadata for the packages in your repository:
```yaml
packages:
  generator: github
  packages:
    - name: untitled-cli-parser
      maintainer: "MadLadSquad <contact@madladsquad.com>"   # who packages it (control file, PKGBUILD header, changelogs)
      author: "MadLadSquad <contact@madladsquad.com>"       # who wrote it (Debian copyright, Gentoo upstream metadata)
      github:
        user: "MadLadSquad"
        repo: "UntitledCLIParser"
        query: "releases"
        artifacts: [ "{pkgname}-{version}.tar.xz" ]
        transforms: [ [ "^v", "" ] ]
    - name: untitled-exec
      description: "C/C++ cross-platform wrapper for launching applications as separate processes"
      homepage: "https://github.com/MadLadSquad/UntitledExec"
      license: "MIT"
      architectures: any               # or a list, by default [ amd64, arm64 ]
      dependencies:                    # by repology project name, see below
        build: [ cmake ]
        libraries: [ "glfw >= 3.4" ]
      arch: { depends: [ "glibc" ] }   # distribution-specific values, available as `dist` in templates
      github:
        user: "MadLadSquad"
        repo: "UntitledExec"
        query: "releases"
        transforms: [ [ "^v", "" ] ]
```
When declaring a package, you specify a fetch generator, which is the script that will fetch the sources for it. Each generator has its own additional configuration that is parsed in its own code.
We include pre-installed generators for integrating with popular platforms like GitHub and GitLab, however if a generator does not exist for your use case, you can write one in python.

Next, create the repository skeleton for every distribution you target with `pkggen gen-repo <distribution>`. Repositories are created in the `pkggen-build` directory
next to your `pkggen.yaml`. Each distribution has its own repository generator, which allows you to easily introduce support for platforms we may not currently support.

Finally, run `pkggen generate`. It fetches every package in parallel and renders its Jinja 2 templates from `packages/<distribution>/<package>/` into the repositories.
If a package has no templates yet, pkggen creates sensible defaults for you to edit and warns you about it.

Dependencies are written once, by their [repology](https://repology.org) project name, in four lists: `build` (tools needed to build), `libraries`
(linked against: their development files are needed to build), `runtime` (needed to run) and `development` (development files needed to build and to
run, for libraries other packages compile against). `pkggen generate` looks every dependency up and gives each distribution's templates its own names,
e.g. `glfw >= 3.4` in `libraries` becomes `libglfw3-dev (>= 3.4)` in Debian's `Build-Depends`, `glfw-devel >= 3.4` in an RPM spec, `glfw>=3.4` in a
PKGBUILD, `glfw-dev>=3.4` in an APKBUILD, `glfw-devel` in a Void template and `>=media-libs/glfw-3.4` in an ebuild. It warns when a release you build for has an older version than you need, and an entry can name
the package itself where repology's name doesn't fit:
```yaml
dependencies:
  build: [ cmake, { name: pkgconf, gentoo: "virtual/pkgconfig" } ]
  runtime: [ { name: xhost, debian: "x11-xserver-utils" }, { name: incus, arch: false } ]   # false: not on Arch
```
`pkggen repology <name>` shows what a project is called, and which version it has, in every release. Your own packages are dependencies by their name.

Once successful, you can run `pkggen test`. It starts a container for every release of every distribution you target (Fedora 43/44/rawhide, openSUSE, OpenMandriva, Debian, Ubuntu,
Arch, Gentoo, Alpine edge/3.24/3.23 and Void glibc/musl) and builds, installs and indexes every package in it, in parallel. The finished repositories land in `pkggen-build/test/<release>/`. It uses Podman (or Docker),
and on macOS and Windows it starts a Linux VM for the containers itself. The `test` block of your `pkggen.yaml` sets how many CPUs and how much RAM it may use, and the shell
commands to run in every container before the packages are built and after they're installed:
```yaml
test:
  cpus: 8
  memory: 16G
  post-setup: [ "my-app --version" ]
  distributions:
    debian:
      setup: [ "apt-get install -y libfoo-dev" ]
```

And if all tests pass, run `pkggen deploy` to automatically deploy your packages to all repositories. Deployment generators are also provided for popular platforms like the AUR, git remotes and S3-compatible
buckets (Cloudflare R2, AWS, MinIO, ...), and you can also write your own in python. They sign apt and dnf repositories with your GPG key, and Alpine
and Void repositories with your RSA key, on your machine: the keys never enter the test containers.

## Distribution/package support
We currently support the following distributions and packaging formats:

1. RedHat, Fedora and other RPM-based(RPMs)
1. Debian, Ubuntu and other deb-based
1. Arch Linux(PKGBUILDs)
1. Gentoo Linux(Ebuilds)
1. Alpine Linux(APKBUILDs)
1. Void Linux(xbps-src templates, glibc and musl)

Planned future support:

1. macOS:
   - Homebrew
   - MacPorts
1. BSD:
   - FreeBSD(pkgs)
   - OpenBSD(ports)

## Installing
From a clone of this repository:
```sh
pip install ./src
```
This installs the `pkggen` command, its dependencies and the bundled generators. Use `pip install -e ./src` instead to work on pkggen itself,
and set `PKGGEN_GENERATORS_PATH` to use your own copy of the `generators` folder.

## Getting started and learning
Get started by navigating [to our documentation](https://github.com/MadLadSquad/pkggen/wiki/Home).

## Running the tests
From the repository root:
```sh
pip install -r src/requirements-dev.txt
python -m pytest
```
The tests run offline. Add `--live` to also test against the real GitHub and repology APIs, and `--containers` to build packages
in real containers (needs Podman or Docker). On GitHub Actions, run the Tests workflow by hand to also run the container tests with Podman and Docker.
