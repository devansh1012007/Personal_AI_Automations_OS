"""The chat event journal: structured events to a rotated file and a ring.

SPEC 2 §3.2's observability line for the chat surface: "everything also lands
in the rotated log file" and "F12 opens a log drawer with the structured
events". This module owns both halves:

* :attr:`Journal.ring` — a bounded in-memory :class:`collections.deque` the
  F12 log drawer renders (structured, newest last).
* a JSON-lines file under ``PAA_HOME/logs/chat.log`` — ISO-8601 UTC stamps
  (the runtime's timestamp convention), one JSON object per line, rotated by
  size with a small generation count so a long session cannot eat the disk.

It deliberately does **not** reconfigure structlog for the whole process: the
runtime's own log configuration (:mod:`paa.observability.logging`) belongs to
the CLI entry point. The chat app writes its *own* structured events here —
turn lifecycle, retries, denials, UI actions — so the file stays a complete
trace of what the chat surface did without coupling to whoever configured
stderr logging. The ledger remains the system of record; this file is the
operator's console history.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = ["Journal", "journal_for_home"]

#: Rotate when the active file passes this many bytes.
_MAX_BYTES: Final = 2 * 1024 * 1024

#: Generations kept next to the active file (``chat.log.1`` … ``chat.log.3``).
_KEEP_GENERATIONS: Final = 3

#: Ring capacity for the F12 drawer. A long session's last 500 events is far
#: more than a drawer scrolls; a deque keeps it O(1) to append forever.
_RING_CAPACITY: Final = 500


class Journal:
    """Structured chat events: in-memory ring + size-rotated JSONL file.

    Appends never raise: the log is an observability aid, and losing the log
    must never cost the operator a turn. A failed file append is recorded in
    the ring (so the F12 drawer still shows the event) and the write failure
    is swallowed by design.
    """

    def __init__(self, log_path: Path | None = None) -> None:
        self._path = log_path
        self._ring: deque[dict[str, Any]] = deque(maxlen=_RING_CAPACITY)
        if log_path is not None:
            self._rotate_if_needed()

    @property
    def ring(self) -> tuple[dict[str, Any], ...]:
        """The last events, oldest first — what the F12 drawer renders."""
        return tuple(self._ring)

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        """Record one structured event into the ring and the rotated file."""
        row: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event}
        row.update(fields)
        self._ring.append(row)
        self._write(row)
        return row

    def _write(self, row: dict[str, Any]) -> None:
        if self._path is None:
            return
        try:
            if self._path.exists() and self._path.stat().st_size >= _MAX_BYTES:
                self._rotate_if_needed(force=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        except OSError:
            # The ring already holds the event; a full disk or a vanished
            # directory must not take the chat surface with it.
            pass

    def _rotate_if_needed(self, *, force: bool = False) -> None:
        """Shift ``chat.log.N`` generations, trimming past the keep count.

        Called with ``force=True`` before every append that crossed the size
        budget, and once at start-up (which is what makes the file *rotated*
        across runs: each run starts a fresh generation when the last one
        reached the budget).
        """
        path = self._path
        if path is None:
            return
        try:
            if force and path.exists() and path.stat().st_size < _MAX_BYTES:
                return
            for generation in range(_KEEP_GENERATIONS, 0, -1):
                candidate = path.with_name(f"{path.name}.{generation}")
                if generation == _KEEP_GENERATIONS and candidate.exists():
                    candidate.unlink()
                elif candidate.exists():
                    candidate.replace(path.with_name(f"{path.name}.{generation + 1}"))
            if path.exists():
                path.replace(path.with_name(f"{path.name}.1"))
        except OSError:
            pass


def journal_for_home(home: Path | None) -> Journal:
    """A journal writing under ``PAA_HOME/logs/chat.log`` (or ring-only)."""
    if home is None:
        return Journal(None)
    logs = home / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Journal(None)
    return Journal(logs / "chat.log")
