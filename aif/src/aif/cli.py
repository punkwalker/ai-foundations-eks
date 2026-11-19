"""Command line entry point for aif.

Every command is a stub for now. Each prints "not implemented" and exits 2.
"""

import argparse
import sys
from collections.abc import Sequence

from aif import __version__

NOT_IMPLEMENTED_EXIT = 2


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"aif {args.command}: not implemented", file=sys.stderr)
    return NOT_IMPLEMENTED_EXIT


def _stage_id(value: str) -> str:
    if len(value) != 2 or not value.isdigit() or int(value) > 13:
        raise argparse.ArgumentTypeError(f"stage must be 00 to 13, got {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aif", description=__doc__)
    parser.add_argument("--version", action="version", version=f"aif {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("up", "build the lab"),
        ("down", "remove the lab"),
        ("status", "show the state of each stage"),
        ("doctor", "check local tools and access"),
        ("cost", "show the cost of running resources"),
    ):
        sub.add_parser(name, help=help_text)

    stage = sub.add_parser("stage", help="turn a stage on or off")
    stage.add_argument("state", choices=("on", "off"))
    stage.add_argument("stage_id", type=_stage_id, metavar="NN")

    cleanup = sub.add_parser("cleanup", help="find and remove leftover resources")
    cleanup.add_argument("--report", action="store_true", help="list only, delete nothing")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _not_implemented(args)
