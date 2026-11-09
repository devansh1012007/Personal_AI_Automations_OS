"""Post-crash boot sweep and workspace reconciliation.

Implements the RFC's "Sad Path": the machine dies mid-execution, and on the
next boot the runtime must rebuild exactly where it was without losing task
lineage or leaving half-applied edits on disk.

The sequence is:

1. Read open lineages from the head projection — **never** from the queue.
   Redis (or any queue) is volatile and may have lost messages; the ledger has
   not. This is RFC §17.4 made operational.
2. Replay each lineage to a :class:`~paa.ledger.replay.TaskProjection`.
3. Compare the projected workspace checkpoint against the live filesystem.
4. On drift, restore the checkpoint and record ``STATE_ROLLBACK_TRIGGERED``.
5. Re-queue what is safely resumable; park what is not.

Design rule: recovery is **idempotent**. Running the sweep twice must produce
the same end state, because a crash *during* recovery is entirely possible.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskPhase, TaskProjection, project
from paa.ledger.store import CorrelationHead, LedgerStore
from paa.storage.relational.base import RelationalBackend
from paa.storage.relational.database import dumps, loads, to_iso, utc_now

__all__ = [
    "CorrelationRecovery",
    "InterruptedCall",
    "InterruptionState",
    "RecoveryEngine",
    "RecoveryOutcome",
    "RecoveryReport",
    "WorkspaceSnapshotter",
    "classify_interrupted",
    "close_interrupted_calls",
    "find_interrupted_calls",
    "find_interrupted_calls_for_session",
]

log = structlog.get_logger(__name__)


@runtime_checkable
class WorkspaceSnapshotter(Protocol):
    """Filesystem integrity provider.

    Structural typing keeps recovery decoupled from
    ``paa.validation.workspace``: recovery only needs *some* way to hash a
    tree and put it back, and tests supply a trivial in-memory double.
    """

    def manifest(self, root: Path) -> dict[str, str]:
        """Map relative path -> sha256 for every file under ``root``."""
        ...

    def manifest_hash(self, manifest: dict[str, str]) -> str:
        """Stable digest over a manifest. Must be order-independent."""
        ...

    def restore(self, root: Path, manifest: dict[str, str]) -> list[str]:
        """Force ``root`` back to ``manifest``. Returns the paths changed."""
        ...


@runtime_checkable
class Requeuer(Protocol):
    """Anything that can put a recovered task back into flight."""

    async def enqueue(self, stream: Any, payload: dict[str, Any], **kwargs: Any) -> Any: ...


class RecoveryOutcome(str, enum.Enum):
    """What the sweep decided for one lineage."""

    RESUMED = "RESUMED"
    """Clean state; re-queued to continue where it left off."""

    ROLLED_BACK = "ROLLED_BACK"
    """Filesystem had drifted; restored to checkpoint, then re-queued."""

    PARKED_HUMAN = "PARKED_HUMAN"
    """Waiting on a human gate. Deliberately left waiting."""

    ABANDONED = "ABANDONED"
    """Exceeded the retry ceiling or is unrecoverable; closed out."""

    UNRECOVERABLE = "UNRECOVERABLE"
    """Checkpoint missing or corrupt — needs manual intervention."""

    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    """Head projection was stale; the lineage had in fact finished."""

    SKIPPED_SYSTEM_LINEAGE = "SKIPPED_SYSTEM_LINEAGE"
    """The tier sweeper's deterministic TIER-only lineage.

    PLAN:integration-fix:P009 — the sweeper's one lineage carries only
    ``TIER_*`` events (non-terminal) by design, so it is permanently open
    and is never a resumable task. Recovery leaves it open without
    re-queuing and without a sweep audit event; it is skipped, not
    resumed, rolled back, abandoned or marked terminal (it must stay
    open to receive the next tick's moves)."""

    SKIPPED_CODER_LINEAGE = "SKIPPED_CODER_LINEAGE"
    """A coder-session / mandate-only lineage: audit, not a task.

    N2.3 (ADR-0046 open item) — a lineage carrying only coder-session
    and mandate audit events (``CODER_SESSION_*``, ``TOOL_CALL_*``,
    ``MANDATE_*``) projects to the initial phase and is never terminal,
    because all eight types are ``_NO_PHASE_CHANGE``. Without an
    exemption the sweep would find it as resumable work and re-queue it
    as a task on every boot. It is never re-queued and never folded into
    phase state; its only sweep work is closing ``PROPOSED`` tool calls
    that have no outcome (``TOOL_CALL_FAILED(reason=interrupted)``).
    A previous run's ``RECOVERY_REPLAY_COMPLETED`` audit row on such a
    lineage is ignored for this determination, because the pre-N2.3
    sweep could have written one onto a coder lineage it should never
    have resumed.
    """


class InterruptionState(str, enum.Enum):
    """What an interrupted tool call did before the crash.

    The states ``applied`` / ``not_applied`` / ``unknown`` are the
    plan's (N2.3, T-N2-5) and the spec's (``docs/2026-11-09-next-spec``)
    vocabulary verbatim; the brief's ``pending`` names the same second
    state — the call never landed, so the work is still pending, and
    this member is that state under its plan name so a reader can grep
    one string across spec, plan and code.

    ``unknown`` is the D-6 state: the sweep must report it and must
    never auto-repeat the call. Repeating a shell or git effect whose
    outcome is unknown can duplicate a commit or a network call; asking
    is cheaper than duplicating.
    """

    APPLIED = "applied"
    """The tool ran far enough that its effect is on disk."""

    NOT_APPLIED = "not_applied"
    """The crash landed before the tool ran; the file still matches ``pre``."""

    UNKNOWN = "unknown"
    """Cannot decide from the persisted intent; never auto-repeat."""


@dataclass(slots=True)
class InterruptedCall:
    """One ``TOOL_CALL_PROPOSED`` with no outcome, and its classification.

    The identity fields are copied from the PROPOSED payload (the
    ledger's `session:call_id:kind` discriminator family — a redelivered
    close dedupes by the same key, so closing is idempotent). ``state``
    is :class:`InterruptionState`; at sweep time it is ``UNKNOWN`` for
    every tool, because the ledger carries hashes but never the target
    path (see :func:`classify_interrupted`), and the precise file-edit
    verdict is computed at resume time, when the session transcript
    names the path.
    """

    correlation_id: uuid.UUID
    session_id: str
    call_id: str
    tool: str
    state: InterruptionState = InterruptionState.UNKNOWN
    args_sha256: str | None = None
    isolation_level: str | None = None
    pre_sha256: str | None = None
    post_sha256: str | None = None
    patch_sha256: str | None = None


@dataclass(slots=True)
class CorrelationRecovery:
    """Result of recovering one lineage."""

    correlation_id: uuid.UUID
    outcome: RecoveryOutcome
    phase: TaskPhase
    drifted_paths: list[str] = field(default_factory=list)
    restored_paths: list[str] = field(default_factory=list)
    detail: str | None = None
    interrupted: list[InterruptedCall] = field(default_factory=list)
    """Coder tool calls the sweep closed on this lineage (possibly empty).

    Empty when the lineage held no interrupted call — the common case.
    Present on coder-only (``SKIPPED_CODER_LINEAGE``) and mixed lineages
    alike: a mixed lineage reports the calls it closed whatever the task
    outcome is, because "closed but unreported" is the exact gap the
    resume note exists to close.
    """


@dataclass(slots=True)
class RecoveryReport:
    """Aggregate result of one boot sweep."""

    started_at: datetime
    finished_at: datetime
    scanned: int = 0
    recoveries: list[CorrelationRecovery] = field(default_factory=list)
    chain_problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def count(self, outcome: RecoveryOutcome) -> int:
        return sum(1 for r in self.recoveries if r.outcome is outcome)

    @property
    def interrupted_count(self) -> int:
        """Total interrupted coder calls closed across every lineage.

        Kept off :meth:`summary` so the logged dict keeps its frozen
        keys; observers that want the number read this property (or the
        per-lineage ``interrupted`` lists) instead.
        """
        return sum(len(r.interrupted) for r in self.recoveries)

    def summary(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "duration_seconds": round(self.duration_seconds, 4),
            "outcomes": {o.value: self.count(o) for o in RecoveryOutcome if self.count(o)},
            "chain_problems": len(self.chain_problems),
        }


# ---------------------------------------------------------------------------
# Coder interrupted-call classification (N2.3)
# ---------------------------------------------------------------------------

#: Tools whose calls touch one jailed file exactly: the persisted intent
#: (``pre`` / ``post`` hashes of that file) can decide what a crash did.
#: ``apply_patch`` is deliberately absent — a patch can touch many
#: files and its intent is only a digest, so no file hash can prove it
#: landed. Every other tool records three explicit nulls in its
#: PROPOSED payload for the same reason.
_CLASSIFIABLE_TOOLS = frozenset({"write_file", "edit_file"})

#: Event types that mark a lineage as coder/mandate audit rather than
#: task work. All eight ADR-0046 types are ``_NO_PHASE_CHANGE``: none
#: advances a phase, none is terminal, so a lineage carrying only these
#: projects to the initial phase exactly like a tier-only one did
#: (P009). The mandate types are included because a bare
#: ``MANDATE_GRANTED`` (what ``paa agent`` writes on its own lineage,
#: once per run) or a ``MANDATE_REFUSED`` refusal lineage is equally
#: not resumable task work.
_CODER_EVENT_TYPES = frozenset(
    {
        EventType.CODER_SESSION_STARTED,
        EventType.TOOL_CALL_PROPOSED,
        EventType.TOOL_CALL_DENIED,
        EventType.TOOL_CALL_COMPLETED,
        EventType.TOOL_CALL_FAILED,
        EventType.CODER_SESSION_ENDED,
        EventType.MANDATE_GRANTED,
        EventType.MANDATE_REFUSED,
    }
)

#: The one type ignored when deciding whether a lineage is coder-only:
#: a pre-N2.3 sweep could have written a ``RECOVERY_REPLAY_COMPLETED``
#: onto a coder lineage it should never have resumed, and that stale
#: audit row must not flip the lineage back to "task work".
_CODER_NEUTRAL_TYPES = frozenset({EventType.RECOVERY_REPLAY_COMPLETED})

#: Outcomes that resolve a tool call: a ``TOOL_CALL_*`` event that is
#: not ``PROPOSED``, keyed the same way (payload ``session_id`` +
#: ``call_id``). A ``DENIED`` without a ``PROPOSED`` is proof of
#: non-execution (the hook-block path journals DENIED alone), never an
#: interrupted call — it is simply absent from the proposed set, so it
#: is never reported.
_OUTCOME_TYPES = frozenset(
    {
        EventType.TOOL_CALL_DENIED,
        EventType.TOOL_CALL_COMPLETED,
        EventType.TOOL_CALL_FAILED,
    }
)


def classify_interrupted(
    tool: str,
    *,
    pre_sha256: str | None,
    post_sha256: str | None,
    patch_sha256: str | None,
    live_sha256: str | None = None,
    live_checked: bool = False,
) -> InterruptionState:
    """Classify an interrupted call from its persisted intent (N2.3).

    The verdict is decided by comparing the live file (``live_sha256``)
    against the hashes the journal persisted at PROPOSED time:

    * a file edit (``write_file`` / ``edit_file``) with a predicted
      ``post`` that the live file matches is ``APPLIED``; one matching
      ``pre`` is ``NOT_APPLIED`` (the brief's ``pending`` — the crash
      landed before the tool ran). A creation whose file is absent is
      ``NOT_APPLIED`` (``pre`` is ``None`` and nothing exists).
    * everything else is ``UNKNOWN``: shell / git / ``apply_patch`` /
      read-only tools (``patch_sha256`` alone can never prove a patch
      landed, and a null intent proves nothing), an edit whose ``post``
      was unmakeable (the journal predicts ``None`` when the tool would
      refuse anyway — a guess there would be a lie), and a live file
      that matches neither hash (a torn write, a CRLF prior the applier
      re-joined differently, or a later overwrite) — ``UNKNOWN``, never
      a guess in either direction, and by D-6 never auto-repeated.

    ``live_checked`` is the ledger-vs-resume distinction, stated once:
    the boot sweep holds hashes but never the target path (the PROPOSED
    payload carries no path — the preview is a shape summary by
    construction), so it passes ``live_checked=False`` and every
    interrupted call classifies ``UNKNOWN`` at sweep time. The precise
    file-edit verdict is computed at resume time, when the session
    transcript names the path and the live hash can actually be read.
    """
    if tool not in _CLASSIFIABLE_TOOLS or not live_checked:
        return InterruptionState.UNKNOWN
    if post_sha256 is None:
        # The prediction was unmakeable and the journal refused to
        # guess; the sweep must not guess either.
        return InterruptionState.UNKNOWN
    if live_sha256 is None:
        # No file at the target. For a creation (``pre`` is ``None``)
        # that proves the tool never landed. For an edit against an
        # existing file it proves something deleted the file — not a
        # verdict either way.
        return (
            InterruptionState.NOT_APPLIED if pre_sha256 is None else InterruptionState.UNKNOWN
        )
    if live_sha256 == post_sha256:
        return InterruptionState.APPLIED
    if pre_sha256 is not None and live_sha256 == pre_sha256:
        return InterruptionState.NOT_APPLIED
    return InterruptionState.UNKNOWN


def _payload_text(payload: dict[str, Any], key: str) -> str | None:
    """A payload field as text, or ``None`` when absent or not text.

    The fold is total: a malformed payload (missing keys, wrong types)
    must not stop the sweep from closing the well-formed calls beside
    it, so anything unusable reads as absent.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def find_interrupted_calls(events: list[LedgerEvent]) -> list[InterruptedCall]:
    """Every PROPOSED with no DENIED / COMPLETED / FAILED, in order.

    Keyed by payload ``(session_id, call_id)`` — the same discriminator
    family the journal writes — and order-independent by construction:
    the proposed set and the outcome set are collected first, then
    subtracted, so a stray outcome that sorts oddly cannot hide a
    genuinely unclosed call. Malformed rows (a PROPOSED without usable
    session/call/tool text) are skipped rather than raised: the sweep
    survives a bad lineage by design, and one bad row must not cost the
    lineage its well-formed closes.
    """
    proposed: dict[tuple[str, str], LedgerEvent] = {}
    resolved: set[tuple[str, str]] = set()
    for event in events:
        payload = event.payload
        if event.event_type is EventType.TOOL_CALL_PROPOSED:
            session_id = _payload_text(payload, "session_id")
            call_id = _payload_text(payload, "call_id")
            if session_id is None or call_id is None:
                continue
            proposed.setdefault((session_id, call_id), event)
        elif event.event_type in _OUTCOME_TYPES:
            session_id = _payload_text(payload, "session_id")
            call_id = _payload_text(payload, "call_id")
            if session_id is None or call_id is None:
                continue
            resolved.add((session_id, call_id))
    interrupted: list[InterruptedCall] = []
    for (session_id, call_id), event in proposed.items():
        if (session_id, call_id) in resolved:
            continue
        payload = event.payload
        tool = _payload_text(payload, "tool") or ""
        interrupted.append(
            InterruptedCall(
                correlation_id=event.correlation_id,
                session_id=session_id,
                call_id=call_id,
                tool=tool,
                args_sha256=_payload_text(payload, "args_sha256"),
                isolation_level=_payload_text(payload, "isolation_level"),
                pre_sha256=_payload_text(payload, "pre_sha256"),
                post_sha256=_payload_text(payload, "post_sha256"),
                patch_sha256=_payload_text(payload, "patch_sha256"),
            )
        )
    return interrupted


async def find_interrupted_calls_for_session(
    store: LedgerStore, session_id: str, *, limit: int = 10_000
) -> list[InterruptedCall]:
    """Interrupted calls for one coder session, across every lineage.

    A resumed session's calls do not live on the correlation the new
    agent build mints (`CoderAgent.build` journalises into one, and the
    CLI passes none on ``--resume`` — see ``cli/coder.py``), so this
    scans the ledger's tool-call events and keeps the rows whose
    payload names ``session_id``. Session ids are uuid hex; a payload
    from another session cannot collide. The scan is ledger-bounded
    (``limit`` rows, newest-first protection is the caller's); on a
    local-first home this is a small set.
    """
    since = datetime(1970, 1, 1, tzinfo=UTC)
    events = await store.events_since(
        since,
        event_types=[
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_DENIED,
            EventType.TOOL_CALL_COMPLETED,
            EventType.TOOL_CALL_FAILED,
        ],
        limit=limit,
    )
    return [
        call
        for call in find_interrupted_calls(
            [e for e in events if _payload_text(e.payload, "session_id") == session_id]
        )
    ]


async def close_interrupted_calls(
    store: LedgerStore, calls: list[InterruptedCall]
) -> list[InterruptedCall]:
    """Close each interrupted call as ``FAILED(reason="interrupted")``.

    The row is the golden v1 FAILED shape, field for field (ADR-0046):
    ``v``, ``session_id``, ``call_id``, ``tool``, ``reason``,
    ``result_sha256`` (always ``None`` — a failed call attests no
    partial result), ``args_sha256`` and ``isolation_level``, copied
    from the PROPOSED row. No ``state`` field is added inside ``v=1``:
    ADR-0046 rule 4 forbids new fields inside an existing version, and
    the classification travels in the recovery report and the resume
    note instead. The discriminator is ``session:call_id:FAILED`` with
    ``attempt=0``, so a redelivered close dedupes in the store's own
    transaction and a second sweep is a no-op (each call is also found
    at most once: the sweep only reports calls with no outcome).
    """
    for call in calls:
        await store.append(
            LedgerEvent.create(
                call.correlation_id,
                EventType.TOOL_CALL_FAILED,
                attempt=0,
                discriminator=f"{call.session_id}:{call.call_id}:FAILED",
                payload={
                    "v": 1,
                    "session_id": call.session_id,
                    "call_id": call.call_id,
                    "tool": call.tool,
                    "reason": "interrupted",
                    "result_sha256": None,
                    "args_sha256": call.args_sha256,
                    "isolation_level": call.isolation_level or "NONE",
                },
            )
        )
    return calls


def _is_coder_only_lineage(events: list[LedgerEvent]) -> bool:
    """Whether a lineage is coder/mandate audit, never task work.

    Every non-neutral event type must be in :data:`_CODER_EVENT_TYPES`
    and there must be at least one: an empty history (a stale head whose
    rows were never written) is not evidence of anything, and a pure
    ``RECOVERY_REPLAY_COMPLETED`` history is not a coder lineage.
    """
    kinds = {e.event_type for e in events} - _CODER_NEUTRAL_TYPES
    return bool(kinds) and kinds <= _CODER_EVENT_TYPES


class RecoveryEngine:
    """Rebuilds runtime state from the ledger after an unclean shutdown."""

    def __init__(
        self,
        store: LedgerStore,
        db: RelationalBackend,
        *,
        snapshotter: WorkspaceSnapshotter | None = None,
        requeuer: Requeuer | None = None,
        max_attempts: int = 3,
        verify_chains: bool = True,
    ) -> None:
        self._store = store
        self._db = db
        self._snapshotter = snapshotter
        self._requeuer = requeuer
        self._max_attempts = max_attempts
        self._verify_chains = verify_chains

    # -- checkpointing -----------------------------------------------------

    async def checkpoint(
        self,
        correlation_id: uuid.UUID,
        state_version: int,
        workspace_path: Path,
    ) -> str:
        """Record a verified filesystem checkpoint. Returns the manifest hash.

        Called before a sandbox is allowed to mutate a workspace, so there is
        always a known-good state to fall back to. Without a snapshotter this
        degrades to a no-op marker rather than failing — a runtime with no
        workspace mutation still needs recovery to work.
        """
        if self._snapshotter is None:
            log.warning("recovery.checkpoint_skipped", reason="no snapshotter configured")
            return ""

        manifest = self._snapshotter.manifest(workspace_path)
        manifest_hash = self._snapshotter.manifest_hash(manifest)

        await self._db.execute(
            "INSERT INTO hot_serving_recovery_marks "
            "(id, correlation_id, state_version, workspace_path, manifest, manifest_hash, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(correlation_id, state_version) DO UPDATE SET "
            "workspace_path = excluded.workspace_path, manifest = excluded.manifest, "
            "manifest_hash = excluded.manifest_hash, created_at = excluded.created_at",
            (
                str(uuid.uuid4()),
                str(correlation_id),
                state_version,
                str(workspace_path),
                dumps(manifest),
                manifest_hash,
                to_iso(utc_now()),
            ),
        )
        log.debug(
            "recovery.checkpointed",
            correlation_id=str(correlation_id),
            state_version=state_version,
            files=len(manifest),
            manifest_hash=manifest_hash[:12],
        )
        return manifest_hash

    async def _latest_mark(self, correlation_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self._db.fetch_one(
            "SELECT state_version, workspace_path, manifest, manifest_hash "
            "FROM hot_serving_recovery_marks WHERE correlation_id = ? "
            "ORDER BY state_version DESC LIMIT 1",
            (str(correlation_id),),
        )
        if row is None:
            return None
        return {
            "state_version": row["state_version"],
            "workspace_path": row["workspace_path"],
            "manifest": loads(row["manifest"], {}),
            "manifest_hash": row["manifest_hash"],
        }

    # -- the sweep ---------------------------------------------------------

    async def boot_sweep(self, *, limit: int = 1000) -> RecoveryReport:
        """Scan every open lineage and bring the runtime back to a sane state."""
        started = utc_now()
        report = RecoveryReport(started_at=started, finished_at=started)

        if self._verify_chains:
            # A broken hash chain means history was edited or truncated. That
            # is reported, never auto-repaired — silently "fixing" tampered
            # audit history would defeat the point of having it.
            report.chain_problems = await self._store.verify_all_chains()
            if report.chain_problems:
                log.error(
                    "recovery.chain_integrity_failure",
                    affected=len(report.chain_problems),
                    detail={k[:8]: v[:2] for k, v in list(report.chain_problems.items())[:5]},
                )

        heads = await self._store.open_correlations(limit=limit)
        report.scanned = len(heads)
        log.info("recovery.sweep_started", open_correlations=len(heads))

        for head in heads:
            try:
                report.recoveries.append(await self.recover_correlation(head))
            except Exception as exc:  # one bad lineage must not abort the sweep
                log.exception(
                    "recovery.correlation_failed", correlation_id=str(head.correlation_id)
                )
                report.recoveries.append(
                    CorrelationRecovery(
                        correlation_id=head.correlation_id,
                        outcome=RecoveryOutcome.UNRECOVERABLE,
                        phase=TaskPhase.FAILED,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )

        report.finished_at = utc_now()
        log.info("recovery.sweep_completed", **report.summary())
        return report

    async def recover_correlation(self, head: CorrelationHead) -> CorrelationRecovery:
        """Recover one lineage. Idempotent.

        Coder sessions enter through the dedicated branch below
        (N2.3): a lineage of only coder/mandate audit events is never
        re-queued as a task, and its unclosed ``TOOL_CALL_PROPOSED``
        calls are closed as ``FAILED(reason=interrupted)``. A *mixed*
        lineage (task lifecycle plus coder audit) closes its interrupted
        coder calls too, then follows the ordinary task path — the
        closures are phase-neutral audit rows, so they disturb nothing
        downstream, and every return path below carries them for the
        operator and the resume note.
        """
        cid = head.correlation_id
        projection = await project(self._store, cid)

        if projection.is_terminal:
            # The head projection can lag if a crash landed between the ledger
            # insert and the head upsert. Repair it rather than reprocessing.
            await self._mark_head_terminal(cid)
            return CorrelationRecovery(cid, RecoveryOutcome.ALREADY_TERMINAL, projection.phase)

        if await _is_system_tier_lineage(self._store, cid):
            # PLAN:integration-fix:P009 (R-W3-002): the tier sweeper's one
            # deterministic lineage is permanently open by design and is
            # never a resumable task. Leave it open without re-queuing and
            # without a sweep audit event (an audit event would grow the
            # ledger by one per boot on a lineage that never progresses).
            log.info("recovery.skipped_system_lineage", correlation_id=str(cid))
            return CorrelationRecovery(
                cid, RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE, projection.phase
            )

        events = await self._store.read_correlation(cid)
        interrupted = find_interrupted_calls(events)
        if _is_coder_only_lineage(events):
            # Coder/mandate audit only (ADR-0046): never task work. No
            # workspace reconcile — a blanket rollback would discard
            # applied edits the model must be told about (D-6) — no
            # requeue, no sweep audit event; the closes below are the
            # only rows this sweep writes here.
            await close_interrupted_calls(self._store, interrupted)
            log.info(
                "recovery.skipped_coder_lineage",
                correlation_id=str(cid),
                interrupted=len(interrupted),
            )
            return CorrelationRecovery(
                cid,
                RecoveryOutcome.SKIPPED_CODER_LINEAGE,
                projection.phase,
                interrupted=interrupted,
            )

        await close_interrupted_calls(self._store, interrupted)

        if projection.phase is TaskPhase.AWAITING_HUMAN:
            # Never auto-clear a human gate. A crash must not be interpretable
            # as consent.
            log.info("recovery.parked_on_human_gate", correlation_id=str(cid))
            return CorrelationRecovery(
                cid,
                RecoveryOutcome.PARKED_HUMAN,
                projection.phase,
                detail=projection.awaiting_reason,
                interrupted=interrupted,
            )

        if projection.attempts >= self._max_attempts:
            await self._abandon(projection, reason="retry ceiling exceeded during recovery")
            return CorrelationRecovery(
                cid,
                RecoveryOutcome.ABANDONED,
                projection.phase,
                detail=f"attempts={projection.attempts} >= max={self._max_attempts}",
                interrupted=interrupted,
            )

        drifted, restored = await self._reconcile_workspace(projection)

        if drifted:
            await self._record_rollback(projection, drifted, restored)
            outcome = RecoveryOutcome.ROLLED_BACK
        else:
            outcome = RecoveryOutcome.RESUMED

        await self._requeue(projection)
        await self._record_sweep(projection, outcome, drifted)
        return CorrelationRecovery(
            cid, outcome, projection.phase, drifted, restored, interrupted=interrupted
        )

    async def _record_sweep(
        self,
        projection: TaskProjection,
        outcome: RecoveryOutcome,
        drifted: Sequence[str],
    ) -> None:
        """Append the recovery audit event, unless nothing has changed since
        the last sweep.

        Idempotency subtlety: keying the event on ``state_version`` does not
        work, because appending the audit event *itself* bumps that version.
        Sweep N would write ``sweep-vK``, leaving the lineage at ``K+1``, and
        sweep N+1 would then write a distinct ``sweep-v(K+1)`` — the ledger
        would grow by one event per boot forever on a task that never
        progresses.

        Instead: if the newest event in the lineage is already a recovery
        audit event, no real work has happened in between, so there is nothing
        new to record. Suppressing it here keeps repeated sweeps genuinely
        no-op while still recording every sweep that follows real progress.
        """
        cid = uuid.UUID(projection.correlation_id)
        latest = await self._store.latest_event(cid)
        if latest is not None and latest.event_type is EventType.RECOVERY_REPLAY_COMPLETED:
            log.debug("recovery.sweep_event_suppressed", correlation_id=str(cid))
            return

        await self._store.append(
            LedgerEvent.create(
                cid,
                EventType.RECOVERY_REPLAY_COMPLETED,
                session_id=_session_uuid(projection),
                payload={
                    "outcome": outcome.value,
                    "phase": projection.phase.value,
                    "replayed_to_version": projection.state_version,
                    "drifted_paths": list(drifted)[:50],
                },
                discriminator=f"sweep-v{projection.state_version}",
            )
        )

    # -- workspace reconciliation -----------------------------------------

    async def _reconcile_workspace(
        self, projection: TaskProjection
    ) -> tuple[list[str], list[str]]:
        """Compare disk against the last checkpoint; restore on divergence.

        Returns ``(drifted_paths, restored_paths)``. Both empty means the
        filesystem matched the ledger's view and nothing was touched.
        """
        cid = uuid.UUID(projection.correlation_id)
        if self._snapshotter is None or not projection.workspace_path:
            return [], []

        mark = await self._latest_mark(cid)
        if mark is None:
            # Nothing was ever checkpointed, so nothing can have drifted from
            # a checkpoint. Not an error — tasks that never touched disk are
            # the common case.
            return [], []

        root = Path(mark["workspace_path"])
        if not root.exists():
            log.warning("recovery.workspace_missing", correlation_id=str(cid), path=str(root))
            return [], []

        expected: dict[str, str] = mark["manifest"]
        actual = self._snapshotter.manifest(root)

        if self._snapshotter.manifest_hash(actual) == mark["manifest_hash"]:
            return [], []

        drifted = sorted(
            set(expected) ^ set(actual)
            | {p for p in set(expected) & set(actual) if expected[p] != actual[p]}
        )
        log.warning(
            "recovery.workspace_drift_detected",
            correlation_id=str(cid),
            drifted=len(drifted),
            sample=drifted[:5],
        )
        restored = self._snapshotter.restore(root, expected)
        log.info("recovery.workspace_restored", correlation_id=str(cid), restored=len(restored))
        return drifted, restored

    async def _record_rollback(
        self, projection: TaskProjection, drifted: Sequence[str], restored: Sequence[str]
    ) -> None:
        cid = uuid.UUID(projection.correlation_id)
        mark = await self._latest_mark(cid)
        await self._store.append(
            LedgerEvent.create(
                cid,
                EventType.STATE_ROLLBACK_TRIGGERED,
                session_id=_session_uuid(projection),
                payload={
                    "reason": "post_crash_workspace_drift",
                    "drifted_paths": list(drifted)[:100],
                    "restored_paths": list(restored)[:100],
                    "restored_manifest_hash": mark["manifest_hash"] if mark else None,
                    "restored_step_index": min(projection.completed_steps, default=0),
                },
                attempt=projection.rollback_count,
                discriminator=f"recovery-v{projection.state_version}",
            )
        )

    async def _abandon(self, projection: TaskProjection, *, reason: str) -> None:
        await self._store.append(
            LedgerEvent.create(
                uuid.UUID(projection.correlation_id),
                EventType.TASK_ABANDONED,
                session_id=_session_uuid(projection),
                payload={"reason": reason, "attempts": projection.attempts},
                discriminator=f"recovery-v{projection.state_version}",
            )
        )
        log.warning(
            "recovery.abandoned", correlation_id=projection.correlation_id, reason=reason
        )

    async def _mark_head_terminal(self, correlation_id: uuid.UUID) -> None:
        await self._db.execute(
            "UPDATE system_state_correlation_head SET is_terminal = 1, updated_at = ? "
            "WHERE correlation_id = ?",
            (to_iso(utc_now()), str(correlation_id)),
        )

    async def _requeue(self, projection: TaskProjection) -> None:
        """Put a recovered lineage back into flight.

        Enqueue failure is logged but non-fatal: the ledger already records the
        task as open, so a later sweep will retry. Losing the queue message is
        recoverable; losing the sweep is not.
        """
        if self._requeuer is None:
            return
        try:
            from paa.storage.queue.base import StreamName

            stream = StreamName.ORCHESTRATOR_CORE
        except Exception:  # queue package not present in a minimal install
            stream = "orchestrator:core"  # type: ignore[assignment]

        try:
            await self._requeuer.enqueue(
                stream,
                {
                    "correlation_id": projection.correlation_id,
                    "session_id": projection.session_id,
                    "resume_phase": projection.phase.value,
                    "resume_step_index": projection.current_step_index,
                    "recovered": True,
                },
                correlation_id=projection.correlation_id,
            )
        except Exception as exc:
            log.error(
                "recovery.requeue_failed",
                correlation_id=projection.correlation_id,
                error=str(exc),
            )


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _is_system_tier_lineage(store: LedgerStore, cid: uuid.UUID) -> bool:
    """Whether ``cid`` is the sweeper's deterministic TIER-only lineage.

    The match is **exact** — one UUID, never a prefix or pattern — so no
    real task can be exempted by a near-miss id. The TIER-only half
    guards the other direction: the sweeper CID with any non-``TIER_*``
    event is not exempted (that history would mean the id was reused
    for task work, which must stay recoverable).

    The CID constant is imported function-locally: ``memory.tier`` writes
    through ``ledger.store``, and keeping ``ledger.recovery`` importable
    without the tier module on the path mirrors the deferred-import
    precedent in :meth:`RecoveryEngine._requeue` (``StreamName``). The
    constant is a leaf ``uuid5`` of a fixed namespace, so deferring the
    import costs nothing at call time.
    """
    from paa.memory.tier import TIER_SWEEPER_CORRELATION_ID

    if cid != TIER_SWEEPER_CORRELATION_ID:
        return False
    events = await store.read_correlation(cid)
    return bool(events) and all(
        event.event_type
        in (EventType.TIER_DEMOTED, EventType.TIER_ARCHIVED)
        for event in events
    )


def _session_uuid(projection: TaskProjection) -> uuid.UUID | None:
    """The projection's session id as a UUID, or ``None`` when it has none.

    The projection carries the session id as *text* — it is a replayed field
    and the projection has to stay JSON-serialisable — while the ledger event
    carries it as a UUID. The two are not the same value type, and the
    conversion has to be explicit.

    An empty string is not a session id: a projection carrying one is a
    projection with no session. The ``and`` idiom that used to stand here
    returned the empty string *itself* — neither a UUID nor ``None`` — which
    the event's validation then rejected. The rejection did not stop the
    sweep, which survives a bad lineage by design, but it cost that lineage
    its recovery record: no ``RECOVERY_REPLAY_COMPLETED`` was written, and the
    lineage was reported as UNRECOVERABLE — a request for a human to look at
    it. Guarding on the value rather than on its truth keeps the two cases
    apart: no session is ``None``, and a session is a UUID.

    A value that is neither empty nor a UUID is *corruption* rather than an
    absent session, and it is left to raise: ``uuid.UUID`` rejects it, and
    the sweep reports that lineage as unrecoverable rather than writing an
    audit event that claims a session the projection cannot name. The
    ``NewType`` discipline over ``CorrelationId``/``SessionId``/``TaskId``
    exists to catch a transposed id, and a value that is not a UUID at all is
    the same class of mistake.
    """
    return uuid.UUID(projection.session_id) if projection.session_id else None
