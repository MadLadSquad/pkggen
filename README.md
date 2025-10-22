# pkggen
[![MIT license](https://img.shields.io/badge/License-MIT-blue.svg)](https://lbesson.mit-license.org/)
[![Discord](https://img.shields.io/discord/717037253292982315.svg?label=&logo=discord&logoColor=ffffff&color=7389D8&labelColor=6A7EC2)](https://discord.gg/4wgH8ZE)

<img src="https://github.com/user-attachments/assets/585a52fa-3c75-40ff-a846-291899b1734c" width="225px" height="225px">

*<sub>Logo designed and created by <a href="https://www.instagram.com/_.insekhta._/">insekhta</a>.</sub>*

A distribution-agnostic application suite to automatically generate new package versions for your desktop applications.

## Who should use these applications
Developers that deal with the following should consider using pkggen in their development process:

1. Developers of cross-platform applications that have to ship on Unix-based systems
1. Developers of Unix-based systems and distributions that want to implement automatic generation of packages
1. QA and DevOps engineers that work with cross-platform applications, that want to implement robust testing and updates in their CI/CD pipelines

## How does pkggen work?
pkggen is simple:

1. Create folders for each packaging format you are planning to support. Each folder is then filled with jinja templates in your package manager's language
1. Create a `pkggen.yaml` file that describes how your packages will be generated
1. In a terminal, run `pkggen`. This command generates all packages automatically
1. Once successful, run `pkggen test`. This command spins up containers and virtual machines to test your packages
1. Finally, when everything runs successfully, run `pkggen deploy`. This command deploys each package to its corresponding distribution's repository

A `pkggen.yaml` file looks like this:
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
    - untitled-game-system-manager:
      github:
        user: "MadLadSquad"
        repo: "UntitledGameSystemManager"
        query: "releases"
        tarballs: [ "{{ pkgname }}.tar.gz" ]
        subrepos:
          - untitled-imgui-framework:
            github:
              user: "MadLadSquad"
              repo: "UntitledImGuiFramework"
              query: "releases"
              tarballs: [ "{{ pkgname }}.tar.gz" ]
    - ibus-anthy:
      github:
        user: ibus
        repo: ibus-anthy
        query: tags
        select: "^\d+\.\d+\.\d+$"
```
When `pkggen` is run, a script called a template generator uses package metadata from the `pkggen.yaml` file and fetches all required data. It then
prints out a JSON object containing all the needed data to fill the template. The pkggen application then proceeds to fill the template and exits successfully.

When `pkggen test` is run, pkggen launches containers and virtual machines for each distribution. It then copies over the current environment and runs
a testing generator, which runs the required tests for each packaging format.

Finally, when `pkggen deploy` is run, all the generated and tested files are given to a deployment generator, which deploys your packages to a repository using
your credentials.

Since each type of generator is a simple application that communicates with the pkggen utility through STDIN and STDOUT, it's easy to write custom generators
for any type of distribution, application, testing, deployment or query method.

## Features
### Distribution and format support
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

### Easy dependency resolution
With the `pkggen repology` command and its subcommands you can query the [repology database](https://repology.org) to get information about a dependency across all the distributions you are targeting.

This is also integrated directly into the `pkggen.yaml` file through the dependencies field, which allows you to easily declare and set dependencies.

### Battle-tested
All [MadLadSquad](https://madladsquad.com) and [Heapforge](https://heapforge.com) applications are already distributed using the pkggen system. In fact, pkggen bootstraps its own
packages when a new release is created.

### Extensive documentation
Though making packages with pkggen is easy and we provide documentation on anything related to the tools that pkggen provides, we also offer many pages on tips for making packages
for every distribution that is officially supported by pkggen. This allows for faster and smoother development without the loss of time that results from trying to find the 
exact thing you're looking for in each distribution's documentation.

We also provide traditional packaging examples in the form of our current and legacy package repositories so that packaging is as fast and as clear a process as possible.

## Getting started and learning
Get started by navigating [to our documentation](https://github.com/MadLadSquad/pkggen/wiki/Home).
