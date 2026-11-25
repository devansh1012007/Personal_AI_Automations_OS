"""The greet-cli-flag contract: the plain path unchanged, the shout path loud."""

from __future__ import annotations

import contextlib
import io

from greeter import build_parser, main


def _greeting_for(argv: list[str]) -> str:
    """Run the CLI in-process and return exactly what it printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = main(argv)
    assert rc == 0
    return buffer.getvalue()


def test_the_plain_greeting_is_unchanged() -> None:
    assert _greeting_for(["Ada"]) == "Hello, Ada!\n"


def test_the_shout_flag_uppercases_and_adds_a_bang() -> None:
    assert _greeting_for(["Ada", "--shout"]) == "HELLO, ADA!!\n"


def test_the_flag_is_off_by_default() -> None:
    assert _greeting_for(["Grace"]) == "Hello, Grace!\n"


def test_the_flag_appears_in_the_help() -> None:
    assert "--shout" in build_parser().format_help()


def test_the_flag_takes_no_value() -> None:
    assert build_parser().parse_args(["Ada", "--shout"]).shout is True
    assert build_parser().parse_args(["Ada"]).shout is False
