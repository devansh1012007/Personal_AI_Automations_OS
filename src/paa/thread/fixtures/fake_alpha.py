"""``fake-alpha`` — the first fake coding harness (C7.5's demonstrable hand).

A deterministic, offline stand-in for a real harness (Claude Code, Codex,
Cursor, OpenCode): it does what a harness does in miniature — leaves a line
in the shared thread notes and performs one small file edit in the
workspace it runs in — so that a mid-thread swap to ``fake-beta`` can be
*seen* continuing the same work rather than asserted to.

Deterministic by construction: given the same workspace state (the notes
file and the work file), it produces the same output state. It reads two
optional environment markers the thread engine sets (``PAA_THREAD_ID``,
``PAA_THREAD_SEGMENT``) and nothing else; run standalone they default to
``standalone`` so the script is testable on its own.

Exit status: ``0`` always — the fake harness succeeds, because the thing
under test is the thread's bookkeeping, not the harness's failures.
"""

from __future__ import annotations

import os
import sys

NOTES_FILE = "thread-notes.md"
WORK_FILE = "alpha-work.txt"


def _notes_lines() -> list[str]:
    """The shared thread notes as they stand, without a trailing blank."""
    try:
        with open(NOTES_FILE, encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def main() -> int:
    segment = os.environ.get("PAA_THREAD_SEGMENT") or "standalone"
    prior = _notes_lines()
    with open(NOTES_FILE, "a", encoding="utf-8") as handle:
        handle.write(
            f"fake-alpha: segment {segment} took the thread "
            f"({len(prior)} note line(s) went before)\n"
        )
    with open(WORK_FILE, "w", encoding="utf-8") as handle:
        handle.write("fake-alpha did a unit of work\n")
    print(f"fake-alpha: notes now hold {len(prior) + 1} line(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
