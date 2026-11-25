"""A one-argument greeting CLI."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """The CLI's surface. The task adds ``--shout`` here."""
    parser = argparse.ArgumentParser(prog="greeter")
    parser.add_argument("name", help="who to greet")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"Hello, {args.name}!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
