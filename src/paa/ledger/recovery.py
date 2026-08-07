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
from paa.storage.relational.database import Database, dumps, loads, to_iso, utc_now

__all__ = [
    "CorrelationRecovery",
    "RecoveryEngine",
    "RecoveryOutcome",
    "RecoveryReport",
    "WorkspaceSnapshotter",
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


@dataclass(slots=True)
class CorrelationRecovery:
    """Result of recovering one lineage."""

    correlation_id: uuid.UUID
    outcome: RecoveryOutcome
    phase: TaskPhase
    drifted_paths: list[str] = field(default_factory=list)
    restored_paths: list[str] = field(default_factory=list)
    detail: str | None = None


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

    def summary(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "duration_seconds": round(self.duration_seconds, 4),
            "outcomes": {o.value: self.count(o) for o in RecoveryOutcome if self.count(o)},
            "chain_problems": len(self.chain_problems),
        }


class RecoveryEngine:
    """Rebuilds runtime state from the ledger after an unclean shutdown."""

    def __init__(
        self,
        store: LedgerStore,
        db: Database,
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
            "INSERT OR REPLACE INTO hot_serving_recovery_marks "
            "(id, correlation_id, state_version, workspace_path, manifest, manifest_hash, "
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        """Recover one lineage. Idempotent."""
        cid = head.correlation_id
        projection = await project(self._store, cid)

        if projection.is_terminal:
            # The head projection can lag if a crash landed between the ledger
            # insert and the head upsert. Repair it rather than reprocessing.
            await self._mark_head_terminal(cid)
            return CorrelationRecovery(cid, RecoveryOutcome.ALREADY_TERMINAL, projection.phase)

        if projection.phase is TaskPhase.AWAITING_HUMAN:
            # Never auto-clear a human gate. A crash must not be interpretable
            # as consent.
            log.info("recovery.parked_on_human_gate", correlation_id=str(cid))
            return CorrelationRecovery(
                cid,
                RecoveryOutcome.PARKED_HUMAN,
                projection.phase,
                detail=projection.awaiting_reason,
            )

        if projection.attempts >= self._max_attempts:
            await self._abandon(projection, reason="retry ceiling exceeded during recovery")
            return CorrelationRecovery(
                cid,
                RecoveryOutcome.ABANDONED,
                projection.phase,
                detail=f"attempts={projection.attempts} >= max={self._max_attempts}",
            )

        drifted, restored = await self._reconcile_workspace(projection)

        if drifted:
            await self._record_rollback(projection, drifted, restored)
            outcome = RecoveryOutcome.ROLLED_BACK
        else:
            outcome = RecoveryOutcome.RESUMED

        await self._requeue(projection)
        await self._record_sweep(projection, outcome, drifted)
        return CorrelationRecovery(cid, outcome, projection.phase, drifted, restored)

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
                session_id=projection.session_id and uuid.UUID(projection.session_id),
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
                session_id=projection.session_id and uuid.UUID(projection.session_id),
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
                session_id=projection.session_id and uuid.UUID(projection.session_id),
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
