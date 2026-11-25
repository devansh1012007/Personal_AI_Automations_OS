"""``fake-beta`` — the second fake coding harness (C7.5's swap target).

The same contract as ``fake-alpha`` with one deliberate difference: it
**continues** rather than starts. It appends to the same shared thread
notes (counting what went before, so the continuation is visible in the
file's own content) and *appends to* the work file ``fake-alpha`` created
rather than replacing it — the two-file story a mid-thread swap has to
tell for "context intact" to mean anything.

Deterministic by construction, like its sibling; same optional
environment markers, same always-zero exit.
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
            f"fake-beta: segment {segment} continued the thread "
            f"({len(prior)} note line(s) went before)\n"
        )
    # Append, never truncate: continuing the same work is the whole point.
    # A missing work file means alpha never ran — the line below says so in
    # the file itself rather than failing, so the swap demo still produces
    # inspectable state on a hand-authored workspace.
    with open(WORK_FILE, "a", encoding="utf-8") as handle:
        handle.write("fake-beta continued the same unit of work\n")
    print(f"fake-beta: notes now hold {len(prior) + 1} line(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
