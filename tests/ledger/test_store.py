"""Ledger append semantics: idempotency, ordering, and the hash chain."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from paa.core.types import ComplexityModality, EventType, new_correlation_id, new_session_id
from paa.ledger.events import GENESIS_HASH, LedgerEvent, compute_idempotency_key
from paa.ledger.store import LedgerStore


async def _append(store: LedgerStore, cid, etype: EventType, **kw) -> LedgerEvent:
    return await store.append(LedgerEvent.create(cid, etype, **kw))


class TestAppend:
    async def test_assigns_sequence_and_version(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED)
        second = await _append(ledger, cid, EventType.TASK_QUEUED)

        assert first.state_version == 1
        assert second.state_version == 2
        assert second.sequence_id > first.sequence_id

    async def test_versions_are_per_correlation(self, ledger: LedgerStore) -> None:
        """Two lineages each start at version 1 despite sharing the sequence."""
        a, b = new_correlation_id(), new_correlation_id()
        ev_a = await _append(ledger, a, EventType.TASK_REQUESTED)
        ev_b = await _append(ledger, b, EventType.TASK_REQUESTED)

        assert ev_a.state_version == ev_b.state_version == 1
        assert ev_a.sequence_id != ev_b.sequence_id

    async def test_first_event_links_to_genesis(self, ledger: LedgerStore) -> None:
        ev = await _append(ledger, new_correlation_id(), EventType.TASK_REQUESTED)
        assert ev.prev_hash == GENESIS_HASH

    async def test_chain_links_successive_events(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED)
        second = await _append(ledger, cid, EventType.TASK_QUEUED)
        assert second.prev_hash == first.event_hash

    async def test_payload_round_trips(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        payload = {"nested": {"a": [1, 2, 3]}, "unicode": "héllo ✓", "n": None}
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload=payload)
        (stored,) = await ledger.read_correlation(cid)
        assert stored.payload == payload

    async def test_session_and_causation_persist(self, ledger: LedgerStore) -> None:
        cid, sid = new_correlation_id(), new_session_id()
        first = await _append(ledger, cid, EventType.TASK_REQUESTED, session_id=sid)
        second = await store_append_caused_by(ledger, cid, first)

        assert second.session_id == sid
        assert second.causation_id == first.event_id


async def store_append_caused_by(store: LedgerStore, cid, cause: LedgerEvent) -> LedgerEvent:
    return await store.append(
        LedgerEvent(
            correlation_id=cid,
            session_id=cause.session_id,
            causation_id=cause.event_id,
            event_type=EventType.TASK_QUEUED,
        )
    )


class TestIdempotency:
    """The RFC's scheme cannot express a retry; ours can. See ADR-0008."""

    async def test_exact_redelivery_is_suppressed(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        first = await _append(ledger, cid, EventType.EXECUTION_STARTED)
        again = await _append(ledger, cid, EventType.EXECUTION_STARTED)

        assert await ledger.count() == 1
        assert again.sequence_id == first.sequence_id
        assert again.event_hash == first.event_hash

    async def test_redelivery_returns_original_not_error(self, ledger: LedgerStore) -> None:
        """Suppression is the happy path for at-least-once transports."""
        cid = new_correlation_id()
        original = await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"v": 1})
        # Different payload, same logical event -> still suppressed, and the
        # ORIGINAL payload wins. Last-write-wins would corrupt history.
        dup = await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"v": 2})
        assert dup.payload == {"v": 1}
        assert dup.sequence_id == original.sequence_id

    async def test_genuine_retry_is_recorded(self, ledger: LedgerStore) -> None:
        """The bug fix: attempt N+1 is a distinct event, not a constraint error.

        Under the RFC's SHA-256(correlation + type + version) scheme this
        append is impossible, which would deadlock recovery on exactly the
        tasks it exists to rescue.
        """
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=0)
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=1)
        await _append(ledger, cid, EventType.EXECUTION_STARTED, attempt=2)
        assert await ledger.count() == 3

    async def test_discriminator_separates_concurrent_steps(self, ledger: LedgerStore) -> None:
        """Parallel steps of one plan emit the same event type legitimately."""
        cid = new_correlation_id()
        for step in range(4):
            await _append(
                ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"step-{step}"
            )
        assert await ledger.count() == 4

    def test_key_is_stable_and_sensitive(self) -> None:
        cid = uuid.uuid4()
        base = compute_idempotency_key(cid, EventType.EXECUTION_STARTED)
        assert base == compute_idempotency_key(cid, EventType.EXECUTION_STARTED)
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_STARTED, attempt=1)
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_STARTED, discriminator="s")
        assert base != compute_idempotency_key(cid, EventType.EXECUTION_COMPLETED)
        assert base != compute_idempotency_key(uuid.uuid4(), EventType.EXECUTION_STARTED)

    def test_negative_attempt_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            compute_idempotency_key(uuid.uuid4(), EventType.TASK_QUEUED, attempt=-1)

    async def test_concurrent_identical_appends_write_once(self, ledger: LedgerStore) -> None:
        """Racing coroutines appending the same event must produce one row."""
        cid = new_correlation_id()
        results = await asyncio.gather(
            *(_append(ledger, cid, EventType.TASK_REQUESTED) for _ in range(20))
        )
        assert await ledger.count() == 1
        assert len({r.sequence_id for r in results}) == 1

    async def test_concurrent_distinct_appends_all_land(self, ledger: LedgerStore) -> None:
        """Dense, gap-free versions under concurrency — the atomicity guarantee."""
        cid = new_correlation_id()
        await asyncio.gather(
            *(
                _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")
                for i in range(25)
            )
        )
        events = await ledger.read_correlation(cid)
        assert [e.state_version for e in events] == list(range(1, 26))


class TestChainIntegrity:
    async def test_clean_chain_verifies(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for etype in (
            EventType.TASK_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
        ):
            await _append(ledger, cid, etype)

        ok, problems = await ledger.verify_chain(cid)
        assert ok and problems == []

    async def test_payload_tampering_is_detected(self, ledger: LedgerStore, db) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"goal": "benign"})
        await _append(ledger, cid, EventType.PLAN_COMPILED)

        await db.execute(
            "UPDATE system_state_ledger SET payload = ? WHERE correlation_id = ? "
            "AND state_version = 1",
            ('{"goal":"exfiltrate"}', str(cid)),
        )

        ok, problems = await ledger.verify_chain(cid)
        assert not ok
        assert any("digest mismatch" in p for p in problems)

    async def test_deletion_is_detected_as_version_gap(self, ledger: LedgerStore, db) -> None:
        cid = new_correlation_id()
        for etype in (
            EventType.TASK_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
        ):
            await _append(ledger, cid, etype)

        await db.execute(
            "DELETE FROM system_state_ledger WHERE correlation_id = ? AND state_version = 2",
            (str(cid),),
        )

        ok, problems = await ledger.verify_chain(cid)
        assert not ok
        assert any("version gap" in p or "broken chain" in p for p in problems)

    async def test_verify_all_reports_only_broken_chains(self, ledger: LedgerStore, db) -> None:
        good, bad = new_correlation_id(), new_correlation_id()
        await _append(ledger, good, EventType.TASK_REQUESTED)
        await _append(ledger, bad, EventType.TASK_REQUESTED)
        await db.execute(
            "UPDATE system_state_ledger SET payload='{\"x\":1}' WHERE correlation_id = ?",
            (str(bad),),
        )

        report = await ledger.verify_all_chains()
        assert str(bad) in report
        assert str(good) not in report


class TestHeadProjection:
    """The bounded alternative to the RFC's unbounded partial index (ADR-0010)."""

    async def test_open_lineage_is_listed(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED)
        heads = await ledger.open_correlations()
        assert [h.correlation_id for h in heads] == [cid]

    async def test_terminal_lineage_drops_out(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.EXECUTION_STARTED)
        await _append(ledger, cid, EventType.MUTATION_COMMITTED)
        assert await ledger.open_correlations() == []

    async def test_head_does_not_grow_with_history(self, ledger: LedgerStore, db) -> None:
        """One row per lineage regardless of event count — the whole point."""
        cid = new_correlation_id()
        for i in range(40):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        rows = await db.fetch_value("SELECT COUNT(*) FROM system_state_correlation_head")
        assert rows == 1
        assert await ledger.count() == 40

    async def test_head_tracks_latest_event(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.PLAN_COMPILED)

        head = await ledger.head(cid)
        assert head is not None
        assert head.latest_event_type is EventType.PLAN_COMPILED
        assert head.latest_state_version == 2
        assert not head.is_terminal


class TestReads:
    async def test_read_correlation_is_ordered(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for i in range(10):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        events = await ledger.read_correlation(cid)
        assert [e.state_version for e in events] == sorted(e.state_version for e in events)

    async def test_read_from_version_is_exclusive(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for i in range(5):
            await _append(ledger, cid, EventType.EXECUTION_STARTED, discriminator=f"s{i}")

        tail = await ledger.read_correlation(cid, from_version=3)
        assert [e.state_version for e in tail] == [4, 5]

    async def test_events_since_filters_by_type(self, ledger: LedgerStore) -> None:
        from paa.storage.relational.database import utc_now

        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.USER_CORRECTION)

        start = utc_now().replace(year=2000)
        only = await ledger.events_since(start, event_types=[EventType.USER_CORRECTION])
        assert [e.event_type for e in only] == [EventType.USER_CORRECTION]

    async def test_latest_event(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        last = await _append(ledger, cid, EventType.PLAN_COMPILED)
        assert (await ledger.latest_event(cid)).event_id == last.event_id


class TestSnapshots:
    async def test_round_trip(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.save_snapshot(cid, 7, {"phase": "EXECUTING", "n": 3})
        loaded = await ledger.load_snapshot(cid)
        assert loaded == (7, {"phase": "EXECUTING", "n": 3})

    async def test_latest_snapshot_wins(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.save_snapshot(cid, 3, {"v": 3})
        await ledger.save_snapshot(cid, 9, {"v": 9})
        version, data = await ledger.load_snapshot(cid)
        assert (version, data) == (9, {"v": 9})

    async def test_missing_snapshot_is_none(self, ledger: LedgerStore) -> None:
        assert await ledger.load_snapshot(new_correlation_id()) is None


class TestModeAndMetadata:
    async def test_execution_mode_persists(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                cid, EventType.TASK_REQUESTED, execution_mode=ComplexityModality.MAX
            )
        )
        (stored,) = await ledger.read_correlation(cid)
        assert stored.execution_mode is ComplexityModality.MAX

    async def test_naive_timestamp_rejected(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="timezone-aware"):
            LedgerEvent(
                correlation_id=uuid.uuid4(),
                event_type=EventType.TASK_REQUESTED,
                recorded_at=datetime(2026, 1, 1, 12, 0, 0),
            )
