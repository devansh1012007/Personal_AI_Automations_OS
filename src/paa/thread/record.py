"""The thread record — durable per-thread state under ``PAA_HOME/threads``.

One JSON file per thread, ``<thread-id>.json``, written atomically (temp
file + replace, the recorder's summary discipline: a crash leaves either
the whole previous file or the whole new one, never a torn hybrid). The
file's identity tag is ``paa-thread/1``.

What the file is *for* matters more than what it holds: it is the fast
handle — the thing ``paa thread show`` reads — while the ledger is the
truth. Every lifecycle fact the file carries (created, segment started,
segment ended, harness swapped) is also journalled under the thread id as
its correlation id, *before* the effect it describes, and the file is a
projection of that chain. This is the same two-artifact shape the recorder
ships (``record.jsonl`` the chain, ``record.json`` the handle), for the
same reason: replayability does not require the handle to be the record.

Append-mostly, by enforced method rather than by convention:

* :meth:`ThreadRecord.open_segment` appends one segment with a fresh id —
  a duplicate id is refused, because two segments with one id is not a
  state a thread can be in, it is a bug wearing one;
* :meth:`ThreadRecord.close_segment` fills in **only** the end fields of
  one open segment, exactly once — an unknown id, or a segment that is
  already ended, is refused. A closed segment is history; the methods
  make rewriting it impossible rather than discouraged.

What is *not* enforced here, on purpose: the file on disk can be edited by
whoever owns the filesystem — that is why the ledger exists, and why the
hash chain, not this file, is what "unbroken" is asserted against.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from paa.thread.errors import ThreadError

__all__ = [
    "THREAD_RECORD_SCHEMA",
    "Segment",
    "ThreadRecord",
    "ThreadStatus",
    "list_threads",
    "load_thread",
    "save_thread",
    "thread_path",
]

#: The thread file's identity. Additive changes only within ``/1``; a
#: reader that meets a foreign tag refuses the file rather than guessing
#: at its shape — same rule as the recorder's summary and the observe
#: bundle.
THREAD_RECORD_SCHEMA = "paa-thread/1"

#: Why a segment ended. ``completed`` is the harness's own exit (clean or
#: not — the exit code is carried beside it); ``interrupted`` is the
#: control plane's marker for a stint a crash left open, closed by the
#: engine the next time the thread is touched. Two words, two facts, never
#: folded into one "finished".
_SEGMENT_REASONS = frozenset({"completed", "interrupted"})


class ThreadStatus(str, enum.Enum):
    """The thread's coarse state. ``active`` from birth; ``closed`` refuses
    further segments; ``paused`` exists in the vocabulary because the
    record is the operator's surface and a parked thread is a real thing
    to express — no v1 verb sets it, and saying so here is cheaper than a
    status field that silently cannot change."""

    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


def _utc_now_iso() -> str:
    """The house timestamp form: UTC, millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Segment(BaseModel):
    """One harness stint on a thread.

    Frozen: a segment is opened once and closed once, and both halves are
    written through :class:`ThreadRecord`'s methods — direct mutation is
    what the frozen model takes off the table. ``record_run_dir`` is the
    evidence pointer to the recorder's run directory when the stint ran
    under ``--record`` (``None`` means plainly contained, the T1 floor);
    ``packet_sha256`` is the handoff packet this stint *started from*,
    which is what makes "context intact" checkable across a swap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_id: str
    harness: str
    started_at: str
    packet_sha256: str = Field(min_length=64, max_length=64)
    packet_path: str | None = None
    ended_at: str | None = None
    ended_reason: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: float | None = None
    record_run_dir: str | None = None

    @property
    def is_open(self) -> bool:
        """A segment with no end is a stint still nominally in flight."""
        return self.ended_at is None


class ThreadRecord(BaseModel):
    """The durable handle for one thread (see the module docstring).

    ``id`` **is** the ledger correlation id — one thread is one lineage,
    the chat session's own rule (one session, one correlation), applied to
    the control plane so ``paa ledger <thread-id>`` walks the thread's
    whole chain with no mapping table. ``mandate`` is the serialized v1
    ``MANDATE_GRANTED`` payload the thread was created under, verbatim —
    the authority object a thread carries, per the spec's own sentence.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    status: ThreadStatus = ThreadStatus.ACTIVE
    mandate: dict[str, Any]
    default_harness: str | None = None
    segments: list[Segment] = Field(default_factory=list)

    # -- invariants, as methods --------------------------------------------

    def open_segment(self, segment: Segment) -> None:
        """Append one fresh segment. Refuses a duplicate id.

        Appending is the only way a segment enters the list; the order of
        the list is the order the stints happened, and both are load-bearing
        for ``paa thread show`` and the swap demo's assertions.
        """
        if any(existing.segment_id == segment.segment_id for existing in self.segments):
            raise ThreadError(
                "a segment with this id already exists on the thread",
                thread_id=self.id,
                segment_id=segment.segment_id,
            )
        self.segments.append(segment)
        self.updated_at = _utc_now_iso()

    def close_segment(
        self,
        segment_id: str,
        *,
        ended_at: str,
        reason: str,
        exit_code: int | None,
        timed_out: bool,
        duration_ms: float,
        record_run_dir: str | None,
    ) -> Segment:
        """Fill in one open segment's end fields, exactly once.

        Refuses an unknown id (nothing to close) and an already-ended
        segment (history is not rewritten — the second close is the
        recorder's torn-write class of bug, caught at the boundary). The
        replacement segment keeps its opened half untouched: only the end
        fields differ, which is the append-mostly contract in one method.
        """
        if reason not in _SEGMENT_REASONS:
            raise ThreadError(
                f"unknown segment end reason {reason!r}",
                thread_id=self.id,
                segment_id=segment_id,
                reasons=sorted(_SEGMENT_REASONS),
            )
        for index, existing in enumerate(self.segments):
            if existing.segment_id != segment_id:
                continue
            if existing.ended_at is not None:
                raise ThreadError(
                    "the segment is already closed — history is never rewritten",
                    thread_id=self.id,
                    segment_id=segment_id,
                )
            closed = existing.model_copy(
                update={
                    "ended_at": ended_at,
                    "ended_reason": reason,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "duration_ms": duration_ms,
                    "record_run_dir": record_run_dir,
                }
            )
            self.segments[index] = closed
            self.updated_at = _utc_now_iso()
            return closed
        raise ThreadError(
            "no such segment on this thread",
            thread_id=self.id,
            segment_id=segment_id,
        )

    def last_open_segment(self) -> Segment | None:
        """The trailing segment still in flight, if any.

        Only the *trailing* segment can be open — segments are opened in
        order and the engine closes a dangling one before opening the
        next — so a scan from the end that stops at the first closed
        segment is both correct and O(1) in the common case.
        """
        for segment in reversed(self.segments):
            if segment.is_open:
                return segment
            return None
        return None

    # -- views ---------------------------------------------------------------

    def correlation_id(self) -> uuid.UUID:
        """The thread id as the ledger's lineage key."""
        return uuid.UUID(self.id)


# ---------------------------------------------------------------------------
# The filesystem half
# ---------------------------------------------------------------------------


def thread_path(home: Path, thread_id: str) -> Path:
    """Where one thread's record lives: ``<home>/threads/<id>.json``."""
    return Path(home) / "threads" / f"{thread_id}.json"


def save_thread(home: Path, record: ThreadRecord) -> Path:
    """Write one thread's record atomically; returns the path written.

    Temp file beside the target, then ``replace`` — an atomic rename on
    both POSIX and Windows — so a crash mid-write leaves the previous
    whole file. No fsync queue depth is claimed beyond what the platform
    gives ``replace``; the ledger, not this file, is the durable record.
    """
    path = thread_path(home, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": THREAD_RECORD_SCHEMA, **record.model_dump(mode="json")}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def load_thread(home: Path, thread_id: str) -> ThreadRecord:
    """Read one thread's record; refuses absent and foreign files.

    A file whose schema tag is not ``paa-thread/1`` is not silently
    reinterpreted — the tag is the format's identity, and a ``<id>.json``
    that is not a thread record is either corruption or an operator's
    unrelated file that happens to share the name; both are refused with
    the tag that was found.
    """
    path = thread_path(home, thread_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ThreadError(
            f"no readable thread record at {path}: {exc}",
            thread_id=thread_id,
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema") != THREAD_RECORD_SCHEMA:
        found = raw.get("schema") if isinstance(raw, dict) else type(raw).__name__
        raise ThreadError(
            f"{path} is not a {THREAD_RECORD_SCHEMA} thread record",
            thread_id=thread_id,
            schema=found,
        )
    raw.pop("schema")
    try:
        return ThreadRecord.model_validate(raw)
    except Exception as exc:  # a malformed body is a refusal, not a crash
        raise ThreadError(
            f"the thread record at {path} does not validate: {exc}",
            thread_id=thread_id,
        ) from exc


def list_threads(home: Path) -> list[ThreadRecord]:
    """Every thread record under ``home``, oldest-created first.

    Creation order is read from ``created_at`` (ids are uuids — never
    monotonic, per the house id rule), with the id as the deterministic
    tiebreak. A ``.tmp`` leftover from an interrupted atomic write does
    not match the ``*.json`` glob and is invisible here by design; a
    foreign ``.json`` that fails to load is **not** skipped — a thread
    list that quietly drops a broken file is a list that lies about what
    exists.
    """
    threads_dir = Path(home) / "threads"
    if not threads_dir.is_dir():
        return []
    records = [
        load_thread(home, path.stem)
        for path in sorted(threads_dir.glob("*.json"))
        if path.is_file()
    ]
    records.sort(key=lambda record: (record.created_at, record.id))
    return records
