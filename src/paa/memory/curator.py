"""The Memory Curator — the nightly, macro-level memory optimiser.

RFC §2.1 agent 9. Where the :class:`~paa.memory.creator.MemoryCreator` works in
real time on one signal at a time, the curator runs infrequently and touches the
whole store: it decays stale facts, prunes weak relationship edges, consolidates
duplicates, sweeps orphans, and refreshes planner statistics.

Two invariants shape the design:

**A hard wall-clock budget.** RFC §2.1 gives the curator "exactly 2 hours" in the
nightly window. A maintenance pass that overruns into the working day defeats the
whole reason it is separated from the creator. So every phase checks the deadline
and the pass stops cleanly between phases — it never leaves a phase half-applied,
and it records where it stopped so the next run resumes there rather than always
restarting at phase one and starving the later phases.

**It never auto-resolves a contradiction.** RFC §2.1 is explicit: on conflicting
data the curator is "hardcoded to refuse auto-resolution". Consolidation here
merges only *exact* duplicates (same entity, predicate, and value); two rows that
share a key but disagree on the value are a contradiction, and those are left for
the :class:`~paa.memory.contradiction.ContradictionDetector` and a human, never
silently merged into a chosen winner.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from paa.memory.decay import DecaySweeper

if TYPE_CHECKING:
    from paa.config import MemorySettings
    from paa.storage.relational.database import Database

__all__ = ["CurationPhase", "CurationReport", "MemoryCurator"]

log = structlog.get_logger(__name__)


class CurationPhase(str, enum.Enum):
    """The ordered maintenance phases.

    Order matters: decay first (evict the dead), then prune edges (some now
    point at evicted facts), then consolidate duplicates, then sweep orphans
    (rows whose entity is gone), then reconcile the vector index against what
    survived, and finally refresh statistics so the planner sees the new shape.
    Running them out of order would leave danglers the later phases assume are
    already gone.
    """

    DECAY = "decay"
    PRUNE_EDGES = "prune_edges"
    CONSOLIDATE = "consolidate"
    ORPHANS = "orphans"
    RECONCILE_VECTORS = "reconcile_vectors"
    ANALYZE = "analyze"


_PHASE_ORDER: tuple[CurationPhase, ...] = (
    CurationPhase.DECAY,
    CurationPhase.PRUNE_EDGES,
    CurationPhase.CONSOLIDATE,
    CurationPhase.ORPHANS,
    CurationPhase.RECONCILE_VECTORS,
    CurationPhase.ANALYZE,
)


@dataclass(slots=True)
class CurationReport:
    """What one maintenance pass did."""

    started_at: datetime
    finished_at: datetime
    completed_phases: list[CurationPhase] = field(default_factory=list)
    stopped_early_at: CurationPhase | None = None
    facts_evicted: int = 0
    edges_pruned: int = 0
    duplicates_merged: int = 0
    orphans_removed: int = 0
    vectors_reconciled: int = 0
    errors: int = 0

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def completed_fully(self) -> bool:
        return self.stopped_early_at is None

    def summary(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "completed_fully": self.completed_fully,
            "stopped_early_at": self.stopped_early_at.value if self.stopped_early_at else None,
            "facts_evicted": self.facts_evicted,
            "edges_pruned": self.edges_pruned,
            "duplicates_merged": self.duplicates_merged,
            "orphans_removed": self.orphans_removed,
            "vectors_reconciled": self.vectors_reconciled,
            "errors": self.errors,
        }


class MemoryCurator:
    """Runs the nightly maintenance pass under a wall-clock budget."""

    def __init__(
        self,
        db: Database,
        *,
        settings: MemorySettings | None = None,
        sweeper: DecaySweeper | None = None,
        vector_store: Any = None,
        archive_writer: Any = None,
        batch_size: int = 500,
    ) -> None:
        from paa.config import get_settings

        self._db = db
        self._settings = settings or get_settings().memory
        self._sweeper = sweeper or DecaySweeper(
            db, archive_writer=archive_writer, batch_size=batch_size
        )
        self._vectors = vector_store
        self._batch = batch_size

    async def run_maintenance(
        self,
        *,
        budget_seconds: float | None = None,
        start_phase: CurationPhase | None = None,
        now: datetime | None = None,
    ) -> CurationReport:
        """Execute the maintenance phases until done or out of time.

        ``start_phase`` lets a follow-up run resume where a budget-limited
        previous run stopped, so no phase is perpetually starved by always
        restarting at :attr:`CurationPhase.DECAY`.
        """
        import time

        from paa.storage.relational.database import utc_now

        started = now or utc_now()
        budget = (
            budget_seconds
            if budget_seconds is not None
            else self._settings.curation_max_runtime_hours * 3600.0
        )
        deadline = time.monotonic() + budget
        report = CurationReport(started_at=started, finished_at=started)

        phases = _PHASE_ORDER
        if start_phase is not None:
            idx = _PHASE_ORDER.index(start_phase)
            phases = _PHASE_ORDER[idx:]

        handlers = {
            CurationPhase.DECAY: self._phase_decay,
            CurationPhase.PRUNE_EDGES: self._phase_prune_edges,
            CurationPhase.CONSOLIDATE: self._phase_consolidate,
            CurationPhase.ORPHANS: self._phase_orphans,
            CurationPhase.RECONCILE_VECTORS: self._phase_reconcile_vectors,
            CurationPhase.ANALYZE: self._phase_analyze,
        }

        for phase in phases:
            # The budget is checked *between* phases, never inside one, so a
            # phase is atomic — a half-consolidated store is worse than one
            # phase skipped until tomorrow.
            if time.monotonic() >= deadline:
                report.stopped_early_at = phase
                log.warning("curator.budget_exhausted", stopped_at=phase.value)
                break
            try:
                await handlers[phase](report, now=started)
                report.completed_phases.append(phase)
            except Exception as exc:
                report.errors += 1
                log.exception("curator.phase_failed", phase=phase.value, error=str(exc))

        report.finished_at = utc_now()
        log.info("curator.maintenance_completed", **report.summary())
        return report

    # -- phases ------------------------------------------------------------

    async def _phase_decay(self, report: CurationReport, *, now: datetime) -> None:
        result = await self._sweeper.sweep(now=now)
        report.facts_evicted = result.evicted

    async def _phase_prune_edges(self, report: CurationReport, *, now: datetime) -> None:
        """Sever relationship edges whose weight fell below the floor.

        RFC §4.1: relationship memory prunes at weight < 0.10. A weak edge is
        graph noise that inflates every multi-hop traversal it sits on.
        """
        floor = self._settings.relationship_prune_floor
        deleted = await self._db.execute(
            "DELETE FROM hot_serving_relationships WHERE weight < ? AND valid_to IS NULL",
            (floor,),
        )
        report.edges_pruned = deleted

    async def _phase_consolidate(self, report: CurationReport, *, now: datetime) -> None:
        """Merge exact-duplicate facts; never merge across differing values.

        Duplicates arise when the creator extracts the same fact from several
        signals. The survivor is the highest-confidence row; its use_count
        absorbs the others and its created_at is pulled back to the earliest, so
        the merged fact looks as old and as used as the group really is.

        Facts sharing (entity, predicate) but with *different* values are a
        contradiction, not a duplicate — they are deliberately excluded from the
        grouping key and left untouched (RFC §2.1 agent 9).
        """
        groups = await self._db.fetch_all(
            "SELECT entity_id, predicate, object_value, COUNT(*) AS n "
            "FROM hot_serving_active_facts WHERE superseded_by IS NULL "
            "GROUP BY entity_id, predicate, object_value HAVING COUNT(*) > 1"
        )
        merged = 0
        for group in groups:
            rows = await self._db.fetch_all(
                "SELECT id, initial_confidence, use_count, created_at "
                "FROM hot_serving_active_facts "
                "WHERE entity_id = ? AND predicate = ? AND object_value = ? "
                "  AND superseded_by IS NULL "
                "ORDER BY initial_confidence DESC, created_at ASC",
                (group["entity_id"], group["predicate"], group["object_value"]),
            )
            if len(rows) < 2:
                continue
            survivor = rows[0]
            losers = rows[1:]
            total_use = sum(r["use_count"] for r in rows)
            earliest = min(r["created_at"] for r in rows)

            async with self._db.transaction() as conn:
                await conn.execute(
                    "UPDATE hot_serving_active_facts "
                    "SET use_count = ?, created_at = ? WHERE id = ?",
                    (total_use, earliest, survivor["id"]),
                )
                await conn.executemany(
                    "DELETE FROM hot_serving_active_facts WHERE id = ?",
                    [(r["id"],) for r in losers],
                )
            merged += len(losers)
        report.duplicates_merged = merged

    async def _phase_orphans(self, report: CurationReport, *, now: datetime) -> None:
        """Delete facts whose entity is gone.

        The FK is ``ON DELETE CASCADE``, so this is belt-and-braces — but a
        cross-backend migration or a manual edit can leave danglers, and an
        orphaned fact is unreachable noise that still costs a decay scan.
        """
        deleted = await self._db.execute(
            "DELETE FROM hot_serving_active_facts WHERE entity_id NOT IN "
            "(SELECT id FROM hot_serving_entity_index)"
        )
        report.orphans_removed = deleted

    async def _phase_reconcile_vectors(self, report: CurationReport, *, now: datetime) -> None:
        """Drop vectors whose backing fact was evicted this pass.

        Without this the vector index accumulates points that resolve to no
        fact — retrieval returns a hit the relational layer cannot explain,
        which reads as memory corruption. Best-effort: a vector-store outage
        must not fail the whole maintenance pass.
        """
        if self._vectors is None:
            return
        try:
            live = {
                r["id"]
                for r in await self._db.fetch_all(
                    "SELECT id FROM hot_serving_active_facts WHERE embedding_status = 'indexed'"
                )
            }
            count = await self._vectors.count("active_facts")
            if not count:
                return
            # Reconciliation strategy is store-specific; we expose the live set
            # and let the store drop the difference. Stores that cannot
            # enumerate ids skip silently.
            reconciler = getattr(self._vectors, "retain_only", None)
            if reconciler is not None:
                report.vectors_reconciled = await reconciler("active_facts", live)
        except Exception as exc:
            report.errors += 1
            log.warning("curator.vector_reconcile_failed", error=str(exc))

    async def _phase_analyze(self, report: CurationReport, *, now: datetime) -> None:
        """Refresh the query planner's statistics after the churn above."""
        analyze = getattr(self._db, "analyze", None)
        if analyze is not None:
            await analyze()
