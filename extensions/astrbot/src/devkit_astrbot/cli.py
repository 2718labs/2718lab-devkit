"""Command-line entry point for scaffolding and validating plugins."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __release__
from .scaffold import ScaffoldError, scaffold_plugin
from .validator import validate_plugin


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone ``devkit-astrbot`` command."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "scaffold":
        try:
            target = scaffold_plugin(
                args.plugin_name,
                args.destination,
                author=args.author,
                repo=args.repo,
                display_name=args.display_name,
            )
        except ScaffoldError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"created {target}")
        return 0

    report = validate_plugin(args.plugin_directory)
    print(report.render())
    return 0 if report.is_valid else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devkit-astrbot",
        description="Scaffold and statically validate AstrBot plugins.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__release__}"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    scaffold = subcommands.add_parser(
        "scaffold", help="create a minimal plugin skeleton"
    )
    scaffold.add_argument("plugin_name", help="name beginning with astrbot_plugin_")
    scaffold.add_argument("destination", help="directory that will contain the plugin")
    scaffold.add_argument("--author", default="2718lab", help="metadata author")
    scaffold.add_argument("--repo", help="metadata repository URL")
    scaffold.add_argument("--display-name", help="metadata display name")

    validate = subcommands.add_parser(
        "validate", help="validate a plugin without loading it"
    )
    validate.add_argument("plugin_directory", help="plugin directory to inspect")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
