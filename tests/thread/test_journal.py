"""The thread journal's event contract and its replay/recovery semantics.

What is pinned here (the ADR-0046 pattern, instantiated for C7.5)
------------------------------------------------------------------
* The v1 payload goldens of the four thread event types, read back
  through the real store — frozen contracts, tripwires for any drift.
* Operator free text never enters a payload: the title is attested
  (sha256 + char count + shape-only preview), the workspace redacted.
* Idempotency: a redelivered logical append dedupes under the ADR-0008
  key; one segment's STARTED and ENDED stay distinct rows.
* Phase inertness: all four live in ``_NO_PHASE_CHANGE``, none in
  ``_PHASE_MAP`` or ``TERMINAL_EVENTS`` — replay folds a thread-only
  history to the initial phase without raising, and an unrecognised
  event does not abort the fold.
* Recovery: a thread-only lineage is ``SKIPPED_THREAD_LINEAGE`` — never
  requeued as task work, nothing written by the sweep — while a lineage
  mixing thread rows with task rows stays on the ordinary path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest

from paa.config import PolicySettings
from paa.core.types import TERMINAL_EVENTS, EventType
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome, _is_thread_only_lineage
from paa.ledger.replay import _NO_PHASE_CHANGE, _PHASE_MAP, TaskPhase, replay
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database
from paa.thread.engine import ThreadEngine
from paa.thread.journal import ThreadJournal
from tests.conftest import RecordingRequeuer

CID = uuid.UUID("11111111-2222-3333-4444-555555555555")
TITLE = "the swap demo"
TITLE_SHA256 = "4111b040f80aa893ee797889b9a61bddb5179b928f4ac168875bda659abe39ef"

THREAD_TYPES = (
    EventType.THREAD_CREATED,
    EventType.THREAD_SEGMENT_STARTED,
    EventType.THREAD_SEGMENT_ENDED,
    EventType.THREAD_HARNESS_SWAPPED,
)

# -- golden v1 payloads (ADR-0046) -----------------------------------------
# Frozen contracts, same rule as the coder/chat/pipeline goldens: a payload
# change is a version bump and updates these rows in the same commit.

GOLDEN_PAYLOADS: dict[EventType, dict] = {
    EventType.THREAD_CREATED: {
        "v": 1,
        "thread_id": str(CID),
        "title_sha256": TITLE_SHA256,
        "title_chars": 13,
        "title_preview": "<str len=13 sha256=4111b040>",
        "workspace": "/tmp/ws",
        "status": "active",
    },
    EventType.THREAD_SEGMENT_STARTED: {
        "v": 1,
        "thread_id": str(CID),
        "segment_id": "seg-1",
        "harness": "fake-alpha",
        "packet_sha256": "a" * 64,
        "workspace": "/tmp/ws",
    },
    EventType.THREAD_SEGMENT_ENDED: {
        "v": 1,
        "thread_id": str(CID),
        "segment_id": "seg-1",
        "harness": "fake-alpha",
        "reason": "completed",
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 8000.0,
        "record_run_dir": None,
    },
    EventType.THREAD_HARNESS_SWAPPED: {
        "v": 1,
        "thread_id": str(CID),
        "from_harness": "fake-alpha",
        "to_harness": "fake-beta",
        "after_segment": "seg-1",
    },
}


async def _golden_lineage(ledger: LedgerStore) -> list[LedgerEvent]:
    """Emit one of each thread event against a real store, in order."""
    journal = ThreadJournal(ledger, CID)
    await journal.created(title=TITLE, workspace="/tmp/ws", status="active")
    await journal.segment_started(
        "seg-1", harness="fake-alpha", packet_sha256="a" * 64, workspace="/tmp/ws"
    )
    await journal.segment_ended(
        "seg-1",
        harness="fake-alpha",
        reason="completed",
        exit_code=0,
        timed_out=False,
        duration_ms=8000.0,
        record_run_dir=None,
    )
    await journal.harness_swapped(
        from_harness="fake-alpha", to_harness="fake-beta", after_segment="seg-1"
    )
    return await ledger.read_correlation(CID)


class TestGoldenV1Payloads:
    async def test_four_golden_payloads_are_pinned(self, ledger: LedgerStore) -> None:
        events = await _golden_lineage(ledger)
        assert [e.event_type for e in events] == list(THREAD_TYPES)
        for event, etype in zip(events, THREAD_TYPES, strict=True):
            assert event.payload == GOLDEN_PAYLOADS[etype], etype.value

    async def test_every_payload_carries_v1(self, ledger: LedgerStore) -> None:
        events = await _golden_lineage(ledger)
        for event in events:
            assert event.payload["v"] == 1

    async def test_payloads_round_trip_through_canonical_json(
        self, ledger: LedgerStore
    ) -> None:
        events = await _golden_lineage(ledger)
        for event in events:
            payload = event.payload
            reordered = dict(reversed(list(payload.items())))
            assert canonical_json(payload) == canonical_json(reordered)
            assert json.loads(canonical_json(payload)) == payload

    async def test_v1_payloads_carry_no_fact_id(self, ledger: LedgerStore) -> None:
        """Thread payloads never name a fact, so the tier fold ignores them."""
        events = await _golden_lineage(ledger)
        for event in events:
            assert "fact_id" not in event.payload


class TestFreeTextNeverEntersTheLedger:
    async def test_the_title_is_attested_not_disclosed(self, ledger: LedgerStore) -> None:
        events = await _golden_lineage(ledger)
        created = events[0].payload
        assert created["title_sha256"] == hashlib.sha256(TITLE.encode()).hexdigest()
        assert created["title_chars"] == len(TITLE)
        assert re.fullmatch(r"<str len=\d+ sha256=[0-9a-f]{8}>", created["title_preview"])
        for event in events:
            assert TITLE not in json.dumps(event.payload)

    async def test_a_secret_shaped_workspace_is_redacted(self, ledger: LedgerStore) -> None:
        journal = ThreadJournal(ledger, CID)
        await journal.created(
            title=TITLE,
            workspace="/tmp/ws?token=sk-ant-api03-supersecretvalue123",
            status="active",
        )
        events = await ledger.read_correlation(CID)
        assert "supersecretvalue123" not in json.dumps(events[-1].payload)


class TestIdempotency:
    async def test_redelivery_dedupes(self, ledger: LedgerStore) -> None:
        journal = ThreadJournal(ledger, CID)
        await journal.harness_swapped(
            from_harness="fake-alpha", to_harness="fake-beta", after_segment="seg-1"
        )
        await journal.harness_swapped(
            from_harness="fake-alpha", to_harness="fake-beta", after_segment="seg-1"
        )
        events = await ledger.read_correlation(CID)
        assert [e.event_type for e in events] == [EventType.THREAD_HARNESS_SWAPPED]

    async def test_one_segments_started_and_ended_stay_distinct(
        self, ledger: LedgerStore
    ) -> None:
        journal = ThreadJournal(ledger, CID)
        await journal.segment_started(
            "seg-1", harness="fake-alpha", packet_sha256="a" * 64, workspace="/tmp/ws"
        )
        await journal.segment_ended(
            "seg-1",
            harness="fake-alpha",
            reason="completed",
            exit_code=0,
            timed_out=False,
            duration_ms=1.0,
            record_run_dir=None,
        )
        events = await ledger.read_correlation(CID)
        assert [e.event_type for e in events] == [
            EventType.THREAD_SEGMENT_STARTED,
            EventType.THREAD_SEGMENT_ENDED,
        ]

    async def test_a_bumped_discriminator_is_a_new_row(self, ledger: LedgerStore) -> None:
        """Swapping to two different harnesses after one segment: two rows."""
        journal = ThreadJournal(ledger, CID)
        await journal.harness_swapped(
            from_harness="fake-alpha", to_harness="fake-beta", after_segment="seg-1"
        )
        await journal.harness_swapped(
            from_harness="fake-beta", to_harness="fake-alpha", after_segment="seg-1"
        )
        events = await ledger.read_correlation(CID)
        assert len(events) == 2

    async def test_an_unknown_reason_is_refused_before_any_append(
        self, ledger: LedgerStore
    ) -> None:
        journal = ThreadJournal(ledger, CID)
        with pytest.raises(ValueError, match="segment end reason"):
            await journal.segment_ended(
                "seg-1",
                harness="fake-alpha",
                reason="vanished",
                exit_code=None,
                timed_out=False,
                duration_ms=1.0,
                record_run_dir=None,
            )
        assert await ledger.read_correlation(CID) == []


class TestPhaseInertness:
    def test_all_four_are_in_no_phase_change(self) -> None:
        for etype in THREAD_TYPES:
            assert etype in _NO_PHASE_CHANGE, f"{etype.value} must be informational"
            assert etype not in _PHASE_MAP, f"{etype.value} must not move a phase"

    def test_none_is_terminal(self) -> None:
        for etype in THREAD_TYPES:
            assert etype not in TERMINAL_EVENTS, f"{etype.value} must never close a lineage"

    async def test_a_thread_only_history_folds_to_the_initial_phase(
        self, ledger: LedgerStore
    ) -> None:
        """The replay property: the fold does not raise, moves nothing."""
        events = await _golden_lineage(ledger)
        state = replay(events)
        assert state.phase is TaskPhase.CREATED
        assert not state.is_terminal

    async def test_an_unrecognised_event_does_not_abort_the_fold(
        self, ledger: LedgerStore
    ) -> None:
        """Forward compatibility: unknown rows are folded, never fatal."""
        events = await _golden_lineage(ledger)
        events.append(
            LedgerEvent.create(CID, EventType.RECOVERY_REPLAY_COMPLETED, payload={"v": 1})
        )
        state = replay(events)
        assert state.phase is TaskPhase.CREATED


class TestTheBootSweepExemptsThreads:
    async def test_a_thread_only_lineage_is_skipped_not_requeued(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        requeuer = RecordingRequeuer()
        await _golden_lineage(ledger)
        before = await ledger.read_correlation(CID)

        engine = RecoveryEngine(ledger, db, requeuer=requeuer)
        report = await engine.boot_sweep()

        assert report.scanned == 1
        assert report.recoveries[0].outcome is RecoveryOutcome.SKIPPED_THREAD_LINEAGE
        assert requeuer.calls == []
        # The sweep writes nothing on a thread lineage — not even a close.
        assert await ledger.read_correlation(CID) == before

    async def test_the_full_engine_lifecycle_is_still_never_task_work(
        self,
        ledger: LedgerStore,
        db: Database,
        workspace: Path,
        engine: ThreadEngine,
        policy: PolicySettings,
    ) -> None:
        """The integration shape: a real create/run/swap lineage, swept."""
        record = await engine.create_thread(
            title="sweep me",
            workspace=workspace,
            policy=policy,
            default_harness="fake-alpha",
        )
        await engine.run_segment(record.id)
        await engine.swap(record.id, harness="fake-beta")

        requeuer = RecordingRequeuer()
        sweep = RecoveryEngine(ledger, db, requeuer=requeuer)
        report = await sweep.boot_sweep()
        outcomes = {r.outcome for r in report.recoveries}
        assert RecoveryOutcome.SKIPPED_THREAD_LINEAGE in outcomes
        assert requeuer.calls == []

    def test_the_lineage_predicate_is_exact(self) -> None:
        """Unit level: what is and is not a thread-only lineage."""
        mandate = LedgerEvent.create(CID, EventType.MANDATE_GRANTED, payload={"v": 1})
        created = LedgerEvent.create(CID, EventType.THREAD_CREATED, payload={"v": 1})
        task = LedgerEvent.create(CID, EventType.TASK_REQUESTED, payload={})
        stale = LedgerEvent.create(
            CID, EventType.RECOVERY_REPLAY_COMPLETED, payload={"v": 1}
        )
        assert _is_thread_only_lineage([created])
        assert _is_thread_only_lineage([mandate, created])
        # A stale recovery audit row must not flip a thread lineage to task work.
        assert _is_thread_only_lineage([mandate, created, stale])
        # Mixing task rows in means the ordinary path recovers it.
        assert not _is_thread_only_lineage([created, task])
        # Empty is no lineage at all — the caller's problem, not ours.
        assert not _is_thread_only_lineage([])

    async def test_a_mixed_lineage_stays_on_the_ordinary_path(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        """Thread rows must not exempt real task work from the sweep."""
        await _golden_lineage(ledger)
        await ledger.append(
            LedgerEvent.create(CID, EventType.TASK_REQUESTED, payload={"goal": "x"})
        )
        requeuer = RecordingRequeuer()
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert (
            report.recoveries[0].outcome is not RecoveryOutcome.SKIPPED_THREAD_LINEAGE
        )
