"""R-W3-002: the tier sweeper lineage must not become a phantom task.

The sweeper's one deterministic lineage carries only ``TIER_*`` events
(non-terminal by design). Before the recovery exemption, the boot sweep
treated that permanently-open head as a resumable task: ``RESUMED`` plus
a re-queue to ``orchestrator:core`` on every boot. After the fix the
lineage is skipped — not requeued, not treated as resumable — while a
real task lineage is still resumed normally.

Tmp ``db`` + ``ledger`` fixtures only; no shared DB.
"""

from __future__ import annotations

import uuid

from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome
from paa.ledger.replay import TaskPhase, project
from paa.ledger.store import LedgerStore
from paa.memory.tier import TIER_SWEEPER_CORRELATION_ID
from paa.storage.relational.database import Database
from tests.conftest import RecordingRequeuer

CID = TIER_SWEEPER_CORRELATION_ID


def _engine(
    ledger: LedgerStore,
    db: Database,
    requeuer: RecordingRequeuer | None = None,
) -> RecoveryEngine:
    return RecoveryEngine(ledger, db, requeuer=requeuer)


async def _seed_tier_lineage(ledger: LedgerStore) -> None:
    """One TIER move on the sweeper's deterministic lineage."""
    fact_id = str(uuid.uuid4())
    await ledger.append(
        LedgerEvent.create(
            CID,
            EventType.TIER_DEMOTED,
            payload={
                "fact_id": fact_id,
                "from_tier": "T0",
                "to_tier": "T1",
                "reason": "ttl",
            },
            discriminator=fact_id + "T1",
            attempt=0,
        )
    )


async def _seed_real_task(ledger: LedgerStore) -> uuid.UUID:
    """One mid-execution task lineage (the control: must still resume)."""
    cid = new_correlation_id()
    await ledger.append(
        LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"goal": "x"})
    )
    await ledger.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))
    return cid


class TestTierPhantomExemption:
    async def test_tier_only_head_is_skipped_not_resumed(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        await _seed_tier_lineage(ledger)
        before = await ledger.count()

        report = await _engine(ledger, db, requeuer).boot_sweep()

        assert report.scanned == 1
        assert report.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 1
        assert report.count(RecoveryOutcome.RESUMED) == 0
        assert requeuer.calls == [], "a system lineage must not be re-queued"
        # No sweep audit event: repeated sweeps stay genuinely no-op.
        assert await ledger.count() == before
        # The lineage is still open (non-terminal) — that is why the
        # exemption is needed. It stays in open_correlations; recovery
        # simply does not treat it as a task (see the report FINDING).
        projection = await project(ledger, CID)
        assert projection.phase is TaskPhase.CREATED
        assert projection.is_terminal is False
        assert [h.correlation_id for h in await ledger.open_correlations()] == [CID]

    async def test_sweep_is_idempotent_for_the_sweeper_lineage(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        await _seed_tier_lineage(ledger)
        count_before = await ledger.count()

        first = await _engine(ledger, db, requeuer).boot_sweep()
        second = await _engine(ledger, db, requeuer).boot_sweep()

        assert first.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 1
        assert second.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 1
        assert await ledger.count() == count_before
        assert requeuer.calls == []

    async def test_mixed_tier_event_types_are_still_skipped(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        await _seed_tier_lineage(ledger)
        other_fact = str(uuid.uuid4())
        await ledger.append(
            LedgerEvent.create(
                CID,
                EventType.TIER_ARCHIVED,
                payload={
                    "fact_id": other_fact,
                    "from_tier": "T3",
                    "to_tier": "T4",
                    "reason": "ttl",
                },
                discriminator=other_fact + "T4",
                attempt=0,
            )
        )
        before = await ledger.count()

        report = await _engine(ledger, db, requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 1
        assert report.count(RecoveryOutcome.RESUMED) == 0
        assert requeuer.calls == []
        assert await ledger.count() == before

    async def test_real_task_alongside_is_still_resumed_and_requeued(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        """Exact-match control: only the sweeper CID is exempt.

        A real task in the same sweep must still be RESUMED and re-queued;
        the sweeper lineage is skipped. If the exemption were a prefix or
        pattern, this control would swallow the real task and fail.
        """
        await _seed_tier_lineage(ledger)
        real_cid = await _seed_real_task(ledger)

        report = await _engine(ledger, db, requeuer).boot_sweep()

        assert report.scanned == 2
        assert report.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 1
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
        payload = requeuer.calls[0]["payload"]
        assert uuid.UUID(payload["correlation_id"]) == real_cid
        assert payload["recovered"] is True

    async def test_non_tier_event_on_the_sweeper_cid_is_not_exempted(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        """The TIER-only half of the guard: id reuse is not exempted.

        If task events ever landed on the sweeper CID (a writer bug),
        recovery must treat the lineage as a real task rather than hiding
        it behind the exemption.
        """
        await _seed_tier_lineage(ledger)
        await ledger.append(
            LedgerEvent.create(CID, EventType.TASK_REQUESTED, payload={"goal": "x"})
        )

        report = await _engine(ledger, db, requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_SYSTEM_LINEAGE) == 0
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
