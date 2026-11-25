"""The flight record — a hash-chained journal of one recorded run (C7.3).

``paa record`` is the harness-neutral flight recorder: the harness runs
inside PAA's evidence boundary and everything the boundary sees is
appended to one file, ``record.jsonl``, as it happens — not
reconstructed after the fact from a database the reviewer cannot carry.
The format's identity is ``paa-record/1``.

Every line is one event::

    {"schema": "paa-record/1", "seq": 0, "at": "…", "kind": "run_started",
     "data": {…}, "prev": "genesis", "sha256": "…"}

``sha256`` is the SHA-256 of the event's canonical JSON *without* the
``sha256`` field; ``prev`` is the previous event's ``sha256`` (the word
``"genesis"`` for the first — a word, not a hash, so it can never
collide with a real predecessor's digest). It is the same construction
the attested bundle's chain rides
(:func:`paa.attest.format.chain_steps` folds the same shape), so one
mental model serves both artifacts: substitute a payload, reorder,
truncate or forge a line and the chain breaks at the first divergence.

Three properties the format holds by construction:

* **Append-only and crash-safe.** Events are written one line at a time,
  flushed and ``fsync``-ed before the writer proceeds. A recorder that
  dies mid-run leaves every event it had already promised on disk, and
  the summary beside it still saying ``phase="started"`` — the crash
  marker the observe bundle and the attest manifest both use, applied
  to the journal itself. A final line without its newline is a torn
  write and is reported as such by :func:`verify_record`, never
  silently dropped.
* **One chain, sequential writers.** The recorder core and the MCP
  policy proxy are two processes appending to one chain, and the
  handoff is the design: the core writes before the harness spawns and
  after it exits; the proxy writes in between. Each append takes an
  exclusive ``flock``, re-reads the on-disk tail, *verifies that tail's
  own digest*, and extends it — so a second writer's events extend the
  same chain instead of splicing a parallel one, and a tail that does
  not verify (a tampered or corrupt last line) is refused, loudly.
  (On hosts without ``fcntl`` the lock degrades to that tail check
  alone, and the tail check still holds the line.)
* **It never claims mediation it did not do.** The summary's
  ``honesty`` block states what was mediated (tool calls through the
  proxy) and what was merely observed (everything the harness did
  outside it — visible only as manifest and git deltas, the observe
  bundle's honest T1 floor).

The summary, ``record.json``, is the fast handle: schema, phase, run
identity, the policy that was in force, event counts, the chain root
and the artifact paths. It is written twice (``started`` before the
harness spawns, ``final`` after the last event), atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.ledger.events import canonical_json

try:  # POSIX only; the tail check below holds the line where it is absent.
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "EVENT_KINDS",
    "GENESIS",
    "RECORD_SCHEMA",
    "RecordError",
    "RecordVerdict",
    "RunRecord",
    "read_summary",
    "verify_record",
]

#: The record format's identity. Additive changes only within ``/1``.
RECORD_SCHEMA = "paa-record/1"

#: The seed the first event's ``prev`` carries. A word, not a hash, so it
#: can never collide with a real predecessor's digest — the same rule the
#: attested bundle's chain holds.
GENESIS = "genesis"

#: The event kinds v1 emits. A closed vocabulary, so a reader never meets
#: a kind it cannot interpret; additive kinds ride ``/2``.
EVENT_KINDS: tuple[str, ...] = (
    "run_started",
    "policy_config",
    "snapshot_before",
    "child_spawned",
    "proxy_started",
    "tool_call",
    "tool_result",
    "policy_denied",
    "proxy_closed",
    "child_exit",
    "snapshot_after",
    "record_finalised",
)

#: The event envelope's fixed fields — exactly what the digest covers.
_ENVELOPE: tuple[str, ...] = ("schema", "seq", "at", "kind", "data", "prev")


class RecordError(PaaError):
    """The record refused an operation that would break its own honesty."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _event_digest(envelope: Mapping[str, Any]) -> str:
    """The SHA-256 over the event's canonical JSON, sans the digest field.

    Deterministic by construction: ``canonical_json`` sorts keys and
    drops insignificant whitespace, so two structurally equal events
    hash identically — or the chain would report false tampering.
    """
    payload = {key: envelope[key] for key in _ENVELOPE}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_summary(run_dir: Path) -> dict[str, Any]:
    """Read ``record.json``; raises :class:`RecordError` if absent/foreign.

    A summary that is not a ``paa-record/1`` summary must not be
    silently reinterpreted — the schema tag is the format's identity.
    """
    path = Path(run_dir) / "record.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError(f"{path} is not a readable record summary: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != RECORD_SCHEMA:
        raise RecordError(
            f"{path} is not a {RECORD_SCHEMA} summary "
            f"(schema={raw.get('schema') if isinstance(raw, dict) else raw!r})"
        )
    return raw


def _envelope_defect(
    event: Any, index: int, expected_seq: int, prev: str
) -> str | None:
    """Why ``event`` is not a trustworthy next link, or ``None`` if it is."""
    if not isinstance(event, dict):
        return f"line {index} is not a JSON object"
    for key in _ENVELOPE:
        if key not in event:
            return f"line {index} is missing the {key!r} field"
    if event["schema"] != RECORD_SCHEMA:
        return f"line {index} carries schema {event['schema']!r}, not {RECORD_SCHEMA!r}"
    if event["kind"] not in EVENT_KINDS:
        return f"line {index} carries unknown kind {event['kind']!r}"
    if event["seq"] != expected_seq:
        return (
            f"line {index} carries seq {event['seq']!r}, expected "
            f"{expected_seq} — the sequence skipped or restarted"
        )
    if event["prev"] != prev:
        return (
            f"line {index} carries prev {event['prev']!r}, expected "
            f"{prev!r} — the chain diverges here"
        )
    if _event_digest(event) != event["sha256"]:
        return f"line {index}'s digest does not match its payload — tampered"
    return None


@dataclass(frozen=True, slots=True)
class _Scan:
    """One sweep of the events file: the verified prefix, and the defects.

    The sweep stops at the first defect — everything after a broken
    link is unverifiable by construction, so reporting "the chain is
    sound through event N, broken at N+1" is the honest boundary, not a
    limitation papered over.
    """

    events: tuple[dict[str, Any], ...] = ()
    torn_tail: bool = False
    problems: tuple[str, ...] = ()


def _scan_events(path: Path, *, strict: bool) -> _Scan:
    """Read and verify every event line; ``strict`` raises on any defect."""
    problems: list[str] = []
    verified: list[dict[str, Any]] = []
    if not path.exists():
        if strict:
            raise RecordError(f"{path} does not exist — no record to read")
        return _Scan(events=(), problems=(f"{path} does not exist",))
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    torn = bool(lines) and lines[-1] != ""
    if torn:
        lines = lines[:-1]
        problems.append(
            "the final line is not newline-terminated — a torn write from "
            "a crashed writer, excluded from the chain"
        )
    expected_seq = 0
    prev = GENESIS
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            message = f"line {index} is not valid JSON: {exc}"
            if strict:
                raise RecordError(message) from exc
            problems.append(message)
            return _Scan(tuple(verified), torn, tuple(problems))
        defect = _envelope_defect(event, index, expected_seq, prev)
        if defect is not None:
            if strict:
                raise RecordError(defect)
            problems.append(defect)
            return _Scan(tuple(verified), torn, tuple(problems))
        verified.append(event)
        expected_seq += 1
        prev = str(event["sha256"])
    return _Scan(tuple(verified), torn, tuple(problems))


def _tail_sha256(handle: Any) -> str | None:
    """The last complete line's ``sha256``, or ``None`` for an empty file.

    The last line's own digest is verified before it is returned: a
    tail that does not verify is a tail this record refuses to extend,
    whatever wrote it. A final line without its newline is a torn
    write; it is not a tail to extend either, so the caller refuses
    instead.
    """
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if size == 0:
        return None
    window = min(size, 1 << 20)
    handle.seek(size - window)
    blob = handle.read(window)
    if not blob.endswith(b"\n"):
        raise RecordError(
            "the events file ends mid-line — a torn write from a crashed "
            "writer; refusing to extend a record whose last event is "
            "incomplete (verify it: paa record verify)"
        )
    last = blob.rsplit(b"\n", 2)[-2] if b"\n" in blob[:-1] else blob[:-1]
    try:
        event = json.loads(last.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RecordError(
            f"the events file's last complete line is not a readable event: {exc}"
        ) from exc
    if not isinstance(event, dict) or _event_digest(event) != event.get("sha256"):
        raise RecordError(
            "the events file's last complete line does not verify against "
            "its own digest — a tampered or corrupt tail; refusing to "
            "extend it"
        )
    return str(event["sha256"])


@dataclass(frozen=True, slots=True)
class RecordVerdict:
    """What :func:`verify_record` found, check by check.

    Every check reports independently — a reviewer asks *where* the
    record stops being trustworthy, not *whether* it does, and a single
    boolean would hide exactly the boundary the question wants.
    """

    run_dir: Path
    schema_ok: bool
    chain_ok: bool
    seq_ok: bool
    summary_ok: bool
    phase: str
    events: int
    chain_root: str | None
    torn_tail: bool
    histogram: dict[str, int] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Green only when every door held *and* the run finalised."""
        return (
            self.schema_ok
            and self.chain_ok
            and self.seq_ok
            and self.summary_ok
            and not self.torn_tail
            and self.phase == "final"
        )


class RunRecord:
    """One run's append-only, hash-chained event journal.

    Construct with :meth:`create` for a new run (writes the ``started``
    summary and the ``run_started`` event) or :meth:`open_existing` to
    resume a record already on disk — the proxy's door: it validates
    the chain as it loads, so a proxy that joins a corrupted record
    refuses to extend it rather than adding honest events to a
    dishonest file.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.events_path = self.run_dir / "record.jsonl"
        self.summary_path = self.run_dir / "record.json"
        self._seq = 0
        self._tail = GENESIS
        self._counts: dict[str, int] = {}
        self._phase = "started"

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        run_dir: Path,
        *,
        run_id: str,
        harness: tuple[str, ...],
        workspace: Path,
        policy: Mapping[str, Any],
        honesty: Mapping[str, Any],
        recorder_version: str,
    ) -> RunRecord:
        """Start a new record: the ``started`` summary, then event zero.

        The summary lands before anything else happens, so a recorder
        that dies immediately still leaves an honest marker naming the
        run it was attempting — the crash discipline the observe bundle
        and the attest manifest both hold, applied to the journal.
        """
        run_dir = Path(run_dir)
        if (run_dir / "record.jsonl").exists():
            raise RecordError(
                f"{run_dir} already holds a record; a run directory is one "
                "run — resume it with open_existing or choose a new one"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        record = cls(run_dir)
        record._write_summary(
            phase="started",
            run_id=run_id,
            harness=list(harness),
            workspace=str(workspace),
            policy=dict(policy),
            honesty=dict(honesty),
            recorder_version=recorder_version,
        )
        record.append(
            "run_started",
            {
                "run_id": run_id,
                "harness": list(harness),
                "workspace": str(workspace),
                "recorder_version": recorder_version,
                "pid": os.getpid(),
                "python": sys.version.split()[0],
            },
        )
        return record

    @classmethod
    def open_existing(cls, run_dir: Path) -> RunRecord:
        """Load and fully validate a record on disk, ready to extend."""
        record = cls(run_dir)
        summary = read_summary(run_dir)
        phase = summary.get("phase")
        record._phase = str(phase) if isinstance(phase, str) else "started"
        for event in _scan_events(record.events_path, strict=True).events:
            record._adopt(event)
        return record

    def _adopt(self, event: Mapping[str, Any]) -> None:
        """Take an already-verified event as the chain's new tail."""
        self._seq = int(event["seq"]) + 1
        self._tail = str(event["sha256"])
        kind = str(event["kind"])
        self._counts[kind] = self._counts.get(kind, 0) + 1

    # -- reading -----------------------------------------------------------

    def events(self) -> tuple[dict[str, Any], ...]:
        """Every verified event, oldest first. Re-reads from disk."""
        return _scan_events(self.events_path, strict=True).events

    def summary(self) -> dict[str, Any]:
        return read_summary(self.run_dir)

    # -- writing -----------------------------------------------------------

    def append(self, kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """Append one event; returns the event as written.

        The append takes the exclusive lock, re-reads the on-disk tail,
        verifies it, and extends it — the sequential-writer handoff
        (recorder core before and after the harness's life, proxy in
        between) made mechanical: whoever appends last extends the one
        chain, and a tail that does not verify is refused rather than
        built upon. The write is one ``write()`` of a newline-terminated
        line, then ``fsync``, so a crash leaves either the whole event
        or none of it.
        """
        if kind not in EVENT_KINDS:
            raise RecordError(
                f"event kind {kind!r} is not one of {EVENT_KINDS}; the "
                "vocabulary is closed so a reader never meets a kind it "
                "cannot interpret"
            )

        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        # ``a+b``: atomic-append writes with a readable position for the
        # tail check. One fd for both, so the flock covers the pair.
        with self.events_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                on_disk_tail = _tail_sha256(handle)
                if on_disk_tail is not None and on_disk_tail != self._tail:
                    # Another writer extended the chain since we last
                    # looked (the proxy during the harness's life, the
                    # core after it). Adopt the verified tail and keep
                    # the chain one line — that is the handoff working,
                    # not a conflict.
                    self._resync_from_tail()
                envelope: dict[str, Any] = {
                    "schema": RECORD_SCHEMA,
                    "seq": self._seq,
                    "at": _utc_now_iso(),
                    "kind": kind,
                    "data": json.loads(json.dumps(data, default=str)),
                    "prev": self._tail,
                }
                envelope["sha256"] = _event_digest(envelope)
                handle.write((canonical_json(envelope) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self._adopt(envelope)
        return envelope

    def _resync_from_tail(self) -> None:
        """Re-load counts and sequence from the verified on-disk chain.

        Called only from inside :meth:`append`'s lock, with the disk
        tail already digest-verified; the full re-read keeps the
        summary's event counts honest when the writer that extended the
        chain was another process.
        """
        scan = _scan_events(self.events_path, strict=True)
        self._seq = 0
        self._tail = GENESIS
        self._counts = {}
        for event in scan.events:
            self._adopt(event)

    def finalise(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Write the ``record_finalised`` event and the ``final`` summary.

        The finalise event is the recorder's own last word: it cites the
        artifacts the run produced (observe bundle, attested bundle) and
        the classification, so the chain root a reviewer verifies covers
        the run's own account of itself.
        """
        event = self.append(
            "record_finalised",
            {
                "events_before": self._seq,
                "counts": dict(self._counts),
                **(dict(extra) if extra else {}),
            },
        )
        summary = read_summary(self.run_dir)
        summary["phase"] = "final"
        summary["finalised_at"] = _utc_now_iso()
        summary["events"] = self._seq
        summary["chain_root"] = self._tail
        summary["counts"] = dict(self._counts)
        _write_json_atomic(self.summary_path, summary)
        self._phase = "final"
        return event

    def note_artifacts(self, artifacts: Mapping[str, Any]) -> None:
        """Record the run's artifact paths in the summary (still started)."""
        summary = read_summary(self.run_dir)
        summary["artifacts"] = dict(artifacts)
        _write_json_atomic(self.summary_path, summary)

    def note_containment(self, containment: Mapping[str, Any]) -> None:
        """Record what containment was actually in force, and its limits."""
        summary = read_summary(self.run_dir)
        summary["containment"] = dict(containment)
        _write_json_atomic(self.summary_path, summary)

    # -- state -------------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def chain_root(self) -> str:
        """The tail digest — ``GENESIS`` until the first event lands."""
        return self._tail

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def _write_summary(
        self,
        *,
        phase: str,
        run_id: str,
        harness: list[str],
        workspace: str,
        policy: dict[str, Any],
        honesty: dict[str, Any],
        recorder_version: str,
    ) -> None:
        _write_json_atomic(
            self.summary_path,
            {
                "schema": RECORD_SCHEMA,
                "phase": phase,
                "run_id": run_id,
                "harness": harness,
                "workspace": workspace,
                "recorder_version": recorder_version,
                "created_at": _utc_now_iso(),
                "policy": policy,
                "honesty": honesty,
                "events": 0,
                "chain_root": GENESIS,
                "counts": {},
                "artifacts": {},
                "containment": {},
            },
        )


# ---------------------------------------------------------------------------
# Verification — the reviewer's door
# ---------------------------------------------------------------------------


def verify_record(run_dir: Path) -> RecordVerdict:
    """Verify a record directory: schema, chain, sequence, summary.

    Collects every problem rather than stopping at the first, because
    the reviewer's question is *where* the record stops being
    trustworthy. A record still at ``phase="started"`` verifies as a
    chain but reports that the run never finalised — the crash marker,
    stated as data.
    """
    run_dir = Path(run_dir)
    problems: list[str] = []
    schema_ok = True
    summary: dict[str, Any] = {}
    try:
        summary = read_summary(run_dir)
    except RecordError as exc:
        schema_ok = False
        problems.append(exc.message)

    record = RunRecord(run_dir)
    histogram: dict[str, int] = {}
    chain_ok = True
    seq_ok = True
    events = 0
    last_sha: str | None = None
    torn = False
    if schema_ok:
        scan = _scan_events(record.events_path, strict=False)
        torn = scan.torn_tail
        problems.extend(scan.problems)
        for event in scan.events:
            kind = str(event["kind"])
            histogram[kind] = histogram.get(kind, 0) + 1
            events += 1
            last_sha = str(event["sha256"])
        for problem in scan.problems:
            if "seq" in problem:
                seq_ok = False
            elif (
                "digest" in problem
                or "chain diverges" in problem
                or "not valid JSON" in problem
                or "not a JSON object" in problem
                or "does not exist" in problem
            ):
                chain_ok = False

    summary_ok = True
    phase = "unknown"
    if schema_ok:
        phase = str(summary.get("phase") or "started")
        if phase == "final":
            if summary.get("chain_root") != last_sha:
                summary_ok = False
                problems.append(
                    "the summary's chain_root does not match the last event's digest"
                )
            if summary.get("events") != events:
                summary_ok = False
                problems.append("the summary's event count does not match the file")
        else:
            problems.append(
                "the record was never finalised — the run died before its "
                "last word (phase=started is the crash marker)"
            )

    return RecordVerdict(
        run_dir=run_dir,
        schema_ok=schema_ok,
        chain_ok=chain_ok,
        seq_ok=seq_ok,
        summary_ok=summary_ok,
        phase=phase,
        events=events,
        chain_root=last_sha,
        torn_tail=torn,
        histogram=histogram,
        problems=tuple(problems),
    )
