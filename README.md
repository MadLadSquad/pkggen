# Metatools
A distribution-agnostic way to automatically generate new package versions for your desktop applications.

This project is inspired by [funtoo-metatools](https://www.funtoo.org/Funtoo:Metatools) and users of it will find using it really similar, though it's greatly simplitied in many cases.
We removed the `funtoo` name, because this project is developed from scratch, and it is not dependent on your distribution or package manager.

## Who should use this package
This package is obviously designed to be used by developers that want to ship to multiple platforms, but that's not really the case. Developers of distributions that want to implement
automatic package updates, as does [Funtoo Linux](https://www.funtoo.org/) will greatly benefit from a more standardised solution that they can just create a plugin for.

## How does metatools work?
Metatools gets fed a description of your packages with their required sources, and templates for each package manager you wish for them to support.

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

Inside each folder, create a `templates` folder and place Jinja templates for each package with its name.

You can then set up a `distro.yaml` config with specific variables for each distribution folder. For example, you can set your AUR username there for Arch Linux

After all that setup is done, you can then run `metatools` in the root project directory. This will generate all the packages in parrallel.

With commands like `metatools test`, you can spin up LXD/Incus containers that you can use to test, if all these packages compile and install correctly.

With commands like `metatools upload`, you can upload your packages to an external repository. For example, the AUR, or a PPA.
