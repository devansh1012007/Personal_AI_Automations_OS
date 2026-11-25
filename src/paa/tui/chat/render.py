"""Text shaping shared by both chat surfaces — importable without Textual.

The TUI widgets (:mod:`paa.tui.chat.widgets`) and the plain loop
(:mod:`paa.tui.chat.plain`) shape hostile text and format the same numbers.
This module is where the shared half lives, deliberately free of any Textual
import so ``paa chat --plain`` works with the ``[tui]`` extra not installed
(the package docstring states the rule: this package is the only place in the
tree that imports Textual, and :mod:`paa.tui.chat.plain` never does).

Input robustness (spec §3.2 "input fuzz: unicode, CJK, ANSI injection on the
renderer"): every text that enters a rendered surface passes through
:func:`sanitize`, which strips ANSI escape sequences and control characters.
Textual renders Segments rather than passthrough bytes and a plain terminal
ignores nothing, but user text is hostile input and is neutralised at the
door anyway on both surfaces.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MAX_TEXT_CHARS", "fmt_tokens", "sanitize"]

#: One message is bounded on the screen: the ledger and the cold lake keep
#: the full text, the render surface shows a bounded prefix so one
#: pathological paste cannot stall the render loop.
MAX_TEXT_CHARS: Final = 100_000

#: ANSI CSI/OSC escapes and C0 controls except newline/tab.
_ANSI_RE: Final = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI … final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC … BEL/ST (unterminated too)
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_CTRL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    """Neutralise ANSI escapes and control characters in user-visible text.

    Newlines and tabs pass through (real content); everything a terminal
    would *execute* rather than *display* is dropped. A NUL byte truncates
    the text the way it would truncate a C string — explicitly, not as a
    rendering accident.
    """
    cleaned = _ANSI_RE.sub("", text)
    cleaned = _CTRL_RE.sub("", cleaned)
    if "\x00" in cleaned:
        cleaned = cleaned.split("\x00", 1)[0]
    return cleaned[:MAX_TEXT_CHARS]


def fmt_tokens(value: int) -> str:
    """``2300`` → ``2.3k``, ``870`` → ``870``. The gauge's number format."""
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)
