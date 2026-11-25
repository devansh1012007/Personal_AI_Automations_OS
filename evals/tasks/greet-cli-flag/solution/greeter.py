"""A one-argument greeting CLI."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """The CLI's surface, with the ``--shout`` flag."""
    parser = argparse.ArgumentParser(prog="greeter")
    parser.add_argument("name", help="who to greet")
    parser.add_argument(
        "--shout",
        action="store_true",
        help="upper-case the greeting and add an extra exclamation mark",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    greeting = f"Hello, {args.name}!"
    if args.shout:
        greeting = greeting.upper() + "!"
    print(greeting)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
