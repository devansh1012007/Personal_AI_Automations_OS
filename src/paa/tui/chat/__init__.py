"""The chat TUI: ``paa chat`` — Textual front-end over :mod:`paa.chat.service`.

SPEC 2 §3.2 (C4.4). This package is the *only* place in the tree that imports
Textual, and it imports it lazily: :mod:`paa.cli.chat` pulls in
:mod:`paa.tui.chat.app` only on the TUI path, so ``paa chat --plain``
(:mod:`paa.tui.chat.plain`) runs without the ``[tui]`` extra installed and the
product never hard-requires it.

Modules:

* :mod:`paa.tui.chat.render`   — text shaping shared by both surfaces (textual-free)
* :mod:`paa.tui.chat.wiring`   — the shared :class:`ChatWiring` composition (textual-free)
* :mod:`paa.tui.chat.theme`    — the semantic palette (Textual theme variables)
* :mod:`paa.tui.chat.journal`  — structured event log (rotated file + F12 ring)
* :mod:`paa.tui.chat.errors`   — the error taxonomy and its microcopy
* :mod:`paa.tui.chat.widgets`  — message stream, memory rail, budget gauge, cards
* :mod:`paa.tui.chat.screens`  — the five screens and their modals
* :mod:`paa.tui.chat.app`      — :class:`~paa.tui.chat.app.ChatApp`, the wiring
* :mod:`paa.tui.chat.plain`    — the ``--plain`` fallback loop

Design language (spec §3.2): one header bar, body, two-row footer; no boxes in
boxes; 1 px rules; text + glyph first, colour second — every semantic colour is
a Textual theme variable set here, never a hardcoded hex in a widget.
"""

from __future__ import annotations

__all__ = [
    "app",
    "errors",
    "journal",
    "plain",
    "render",
    "screens",
    "theme",
    "widgets",
    "wiring",
]
