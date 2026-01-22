# pkggen
[![MIT license](https://img.shields.io/badge/License-MIT-blue.svg)](https://lbesson.mit-license.org/)
[![Discord](https://img.shields.io/discord/717037253292982315.svg?label=&logo=discord&logoColor=ffffff&color=7389D8&labelColor=6A7EC2)](https://discord.gg/4wgH8ZE)

<img src="https://github.com/user-attachments/assets/585a52fa-3c75-40ff-a846-291899b1734c" width="225px" height="225px">

*<sub>Logo designed and created by <a href="https://www.instagram.com/_.insekhta._/">insekhta</a>.</sub>*

> [!CAUTION]
> pkggen is still not ready for use with many core features currently missing.

A drop-in replacement for your entire desktop packaging and deployment pipeline

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
1. Dependency helpers - pkggen can help you add the correct dependencies for your package by cross-referencing with APIs like repology
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
    - untitled-cli-parser:
      github:
        user: "MadLadSquad"
        repo: "UntitledCLIParser"
        query: "releases"
        tarballs: [ "{{ pkgname }}.tar.gz" ]
    - untitled-exec:
      description: "C/C++ cross-platform wrapper for launching applications as separate processes"
      homepage: "https://github.com/MadLadSquad/UntitledExec"
      license: "MIT"
      github:
        user: "MadLadSquad"
        repo: "UntitledExec"
        query: "releases"
        tarballs: [ "{{ pkgname }}.tar.gz" ]
```
When declaring a package, you specify a fetch generator, which is the script that will fetch the sources for it. Each generator has its own additional configuration that is parsed in its own code.
We include pre-installed generators for integrating with popular platforms like GitHub, however if a generator does not exist for your use case, you can write one in python.

Then, in a folder for every distribution, you write a templated version for your packages, where each variable is defined by the given generator that you are using.

Finally, to generate your repositories, run `pkggen`. This will generate all repositories in the `pkggen-build` directory. Just like the fetch generator, each distribution has its own repository
generator in the form of a shell script, which allows you to easily introduce support for platforms we may not currently support.

Once successful, you can run `pkggen test`, which will spin up containers and virtual machines to test out your package automatically. After the default testing round, you can also run your own
testing generator as a shell script, which will be executed inside every container and virtual machine.

And if all tests pass, run `pkggen deploy` to automatically deploy your packages to all repositories. Deployment generators are also provided for popular platforms like the AUR, and you can
also write your own in python.

## Distribution/package support
We currently support the following distributions and packaging formats:

1. RedHat, Fedora and other RPM-based(RPMs) 🚧
   - Generating packages 🚧
   - Testing packages 🚧
   - Deploying packages 🚧
1. Arch Linux(PKGBUILDs) 🚧
   - Generating packages 🚧
   - Testing packages 🚧
   - Deploying packages 🚧
1. Gentoo Linux(Ebuilds) 🚧
   - Generating packages 🚧
   - Testing packages 🚧
   - Deploying packages 🚧

Planned future support:

1. macOS:
   - Homebrew
   - MacPorts
1. Linux:
   - Debian/Ubuntu(debs)
   - Void(templates)
   - Alpine(apks)
1. BSD:
   - FreeBSD(pkgs)
   - OpenBSD(ports)

## Getting started and learning
Get started by navigating [to our documentation](https://github.com/MadLadSquad/pkggen/wiki/Home).
