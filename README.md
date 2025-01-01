# pkggen
A distribution-agnostic way to automatically generate new package versions for your desktop applications.

This project is inspired by [funtoo-metatools](https://www.funtoo.org/Funtoo:Metatools) and users of it will find using it really similar, though it's greatly simplitied in many cases.

## Who should use this package
Developers that deal with the following should consider using pkggen in their development process:

1. Developers of cross-platform applications that have to ship on Unix-based systems
1. Developers of Unix-based systems that want to implement automatic peneration of packages
1. QA and devops engineers that work with cross-platform applications, that want to implement robust testing and updates in their CI/CD pipelines

## How does pkggen work?
pkggen gets fed a description of your packages with their required sources, and templates for each package manager you wish for them to support.

You describe your packages in YAML format. Example:
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
Inside the current working director create a folder for each package manager. We currently support the following:

1. Arch Linux PKGBUILDs - `arch`
1. Gentoo & Funtoo EBUILDs - `ebuild`
1. DEBs - `debian`
1. RPMs - `rpm`
1. Void templates - `void`
1. Homebrew formulae - `brew`

Inside each folder, create a `templates` folder and place Jinja-like templates for each package with its name.

You can then set up a `distro.yaml` config with specific variables for each distribution folder. For example, you can set your AUR username there for Arch Linux

After all that setup is done, you can run `pkggen` in the root project directory. This will generate all the packages in parrallel.

With commands like `pkggen test`, you can spin up LXD/Incus containers that you can use to test, if all these packages compile and install correctly.

With commands like `pkggen upload`, you can upload your packages to an external repository. For example, the AUR, or a PPA.

### Generators - the core of pkggen
As seen in the above example, to get the latest version of an application, a generator for GitHub is used. But what is a generator? 

A generator is simply a shell command that accepts and outputs YAML! That's right, you can write a generator in any of your favourite programming languages! Fortunately, you don't even need to write your own generator in most cases, as we provide generators for popular platforms, such as GitHub.
