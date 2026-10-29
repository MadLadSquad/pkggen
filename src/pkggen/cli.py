"""pkggen's command line: `pkggen <command>`.

It's both the console script of an installed pkggen (pkggen.cli:main) and what
the src/pkggen/pkggen launcher runs from a checkout.
"""
import argparse
import sys
from datetime import datetime

from . import deploy, gen_repo, generate, testing, utils, version
from .utilities.repology import query_repology


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, got {value!r}")
    return number


def run():
    parser = argparse.ArgumentParser(
        prog="pkggen",
        description="Automatically generate desktop packages for multiple distributions",
        epilog=f"Copyright (c) MadLadSquad 2024 - {datetime.now().year}. Licensed under the terms of the MIT license."
    )
    parser.add_argument("-v", "--version", action="version", version=f"pkggen version {version.PKGGEN_VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate desktop packages")
    generate_parser.add_argument("-p", "--packages", help="Only generate these packages", nargs='+')
    generate_parser.add_argument("-d", "--distributions", help="Only package for these distributions", nargs='+')

    dist_error = None
    if "gen-repo" in sys.argv[1:]:
        try:
            distributions = gen_repo.get_distributions()
        except utils.GenericError as e:
            dist_error = e
            distributions = None
        if dist_error is not None:
            # Takes any arguments (see parse_known_args() below), so the error is the missing list
            subparsers.add_parser("gen-repo", help="Generate repository folder structures", add_help=False)
        else:
            gen_repo_parser = subparsers.add_parser("gen-repo", help="Generate repository folder structures")
            gen_repo_subparsers = gen_repo_parser.add_subparsers(dest="distribution", required=True)
            gen_repo_options = argparse.ArgumentParser(add_help=False)
            gen_repo_options.add_argument("-o", "--output", help="Set the output directory for the generator")
            gen_repo_options.add_argument("-f", "--force", help="Overwrite files that already exist",
                                          action="store_true")
            for dist in distributions:
                gen_repo_subparsers.add_parser(dist, help=f"Generate repository for {dist}",
                                               parents=[gen_repo_options])
    else:
        subparsers.add_parser("gen-repo", help="Generate repository folder structures")

    test_parser = subparsers.add_parser("test", help="Build and install the packages in a container per distribution release")
    test_parser.add_argument("-p", "--packages", help="Only test these packages", nargs='+')
    test_parser.add_argument("-d", "--distributions", help="Only test these distributions", nargs='+')
    test_parser.add_argument("-t", "--targets", help="Only test these releases (e.g. fedora-44 debian_13)", nargs='+')
    test_parser.add_argument("-j", "--jobs", help="How many containers run at once", type=positive_int)
    test_parser.add_argument("--engine", help="The container engine to use", choices=["podman", "docker"])
    test_parser.add_argument("--keep", help="Don't remove the containers afterwards, for debugging", action="store_true")

    deploy_parser = subparsers.add_parser("deploy", help="Push the tested packages to their production repositories")
    deploy_parser.add_argument("-p", "--packages", help="Only deploy these packages (for deployment generators that publish packages one by one)", nargs='+')
    deploy_parser.add_argument("-d", "--distributions", help="Only deploy these distributions", nargs='+')
    deploy_parser.add_argument("-t", "--targets", help="Only deploy these test targets (e.g. fedora-44)", nargs='+')
    deploy_parser.add_argument("--skip-tests", help="Also deploy targets and packages whose last test failed",
                               action="store_true")
    deploy_parser.add_argument("--dry-run", help="Commit the changes locally, but don't push them",
                               action="store_true")

    repology_parser = subparsers.add_parser("repology", help="Show what a dependency (a repology project) is called "
                                                             "in every release, as `dependencies` in pkggen.yaml uses it")
    repology_parser.add_argument("package", help="The repology project name, e.g. glfw")
    repology_parser.add_argument("-j", "--json", help="Print the result of the query as a JSON object", action="store_true")
    repology_parser.add_argument("-i", "--include-outdated", help="Include versions in outdated distribution releases", action="store_true")

    subparsers.add_parser("version")

    if dist_error is not None:
        # `gen-repo --help` or `gen-repo -o dir arch` must report the missing distribution list, not
        # argparse's "unrecognized arguments". "gen-repo" may also only be a value (`generate -p gen-repo`)
        args, _ = parser.parse_known_args()
        if args.command == "gen-repo":
            raise dist_error
    args = parser.parse_args()
    if args.command == "generate":
        sys.exit(generate.generate(args.packages, args.distributions))
    elif args.command == "gen-repo":
        gen_repo.gen_repo(args.distribution, args.output, args.force)
    elif args.command == "test":
        sys.exit(testing.test(args.packages, args.distributions, args.targets, args.jobs, args.engine, args.keep))
    elif args.command == "deploy":
        sys.exit(deploy.deploy(args.packages, args.distributions, args.targets, args.skip_tests, args.dry_run))
    elif args.command == "repology":
        sys.exit(query_repology(args.package, args.json, args.include_outdated))
    elif args.command == "version":
        print(f"pkggen version {version.PKGGEN_VERSION}")

def main():
    # Expected errors are shown as one line, without a traceback
    try:
        run()
    except utils.GenericError as e:
        print(f"\x1b[31mError: {e.args[0]}\x1b[0m", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

if __name__ == "__main__":
    main()
