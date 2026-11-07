"""Ledger-wired attention allocator + drain throttle (PLAN:integration-fix:P007).

Covers ADR-0036: `AttentionAllocator` consuming ``workers:alloc`` (claim-ack),
scoring via the ADR-0012 `attention_allocation_score`, top-N ledger event
before notify (ledger-before-side-effect), empty -> underfed no-op, disabled ->
queue untouched, idempotent re-claim, poison -> nack, and the windowed
`drain_pending_embeddings` rate cap (T08/T15).

Reversal: deleting the allocator emission breaks the replay/idempotency
asserts; deleting the throttle window breaks the T15 cap asserts; scoring with
raw P*R crashes the P/R=0 case — the guards in metrics prevent it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing

from paa.config import AllocatorSettings, ContextSettings
from paa.context.allocator import AttentionAllocator, allocate_attention
from paa.context.budget import CharEstimator
from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.context.metrics import attention_allocation_score
from paa.core.types import TERMINAL_EVENTS, EventType, new_correlation_id
from paa.ledger.events import LedgerEvent, compute_idempotency_key
from paa.ledger.replay import replay
from paa.memory.creator import MemoryCreator
from paa.memory.facts import Fact, FactRepository
from paa.storage.queue.base import QueueMessage, StreamName
from paa.storage.relational.database import Database

CID = new_correlation_id()


def _msg(
    msg_id: str, payload: dict[str, Any], *, correlation_id: uuid.UUID | None = CID
) -> QueueMessage:
    now = datetime.now(UTC)
    return QueueMessage(
        id=msg_id,
        stream=StreamName.WORKERS_ALLOC,
        correlation_id=correlation_id,
        payload=payload,
        enqueued_at=now,
        visible_after=now,
    )


class FakeQueue:
    """Records claim/ack/nack/enqueue; messages leave only on ack (at-least-once)."""

    def __init__(self, messages: list[QueueMessage], *, order: list[str] | None = None) -> None:
        self._pending = list(messages)
        self.claim_calls: list[tuple[Any, str, int]] = []
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str]] = []
        self.enqueued: list[dict[str, Any]] = []
        self._order = order

    def note(self, label: str) -> None:
        if self._order is not None:
            self._order.append(label)

    def restore(self, msg: QueueMessage) -> None:
        """Re-present a message (visibility-lapse redelivery)."""
        self._pending.append(msg)

    async def claim(
        self,
        stream: StreamName,
        consumer: str,
        *,
        limit: int = 1,
        visibility_timeout: float | None = None,
    ) -> list[QueueMessage]:
        self.claim_calls.append((stream, consumer, limit))
        out = self._pending[:limit]
        self._pending = self._pending[len(out) :]
        return out

    async def ack(self, message_id: str) -> bool:
        self.acked.append(message_id)
        self.note(f"ack:{message_id}")
        return True

    async def nack(
        self,
        message_id: str,
        error: str | BaseException,
        *,
        retry_delay_seconds: float = 0.0,
    ) -> QueueMessage | None:
        self.nacked.append((message_id, str(error)))
        return None

    async def enqueue(
        self,
        stream: StreamName,
        payload: dict[str, Any],
        *,
        priority: int = 100,
        correlation_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> QueueMessage:
        self.enqueued.append(
            {"stream": stream, "payload": payload, "correlation_id": correlation_id}
        )
        self.note("notify")
        return _msg(f"notify-{len(self.enqueued)}", payload, correlation_id=correlation_id)

    async def depth(self, stream: StreamName) -> int:
        return len(self._pending)

    async def reclaim_expired(self) -> int:
        return 0

    async def dead_letters(
        self, stream: StreamName, limit: int = 100
    ) -> list[QueueMessage]:
        return []

    async def purge(self, stream: StreamName) -> int:
        return 0

    async def close(self) -> None:
        return None


class FakeStore:
    """`LedgerStore.append` semantics: dedupe by full key, return existing on dup."""

    def __init__(self, *, order: list[str] | None = None) -> None:
        self.events: dict[str, LedgerEvent] = {}
        self.appended: list[LedgerEvent] = []
        self._order = order

    async def append(self, event: LedgerEvent) -> LedgerEvent:
        self.appended.append(event)
        if self._order is not None:
            self._order.append("emit")
        key = event.idempotency_key
        if key in self.events:
            return self.events[key]
        self.events[key] = event
        return event

    async def read_correlation(
        self, correlation_id: Any, *, from_version: int = 0
    ) -> list[LedgerEvent]:
        return [
            e
            for e in self.events.values()
            if str(e.correlation_id) == str(correlation_id)
            and (e.state_version or 0) >= from_version
        ]


def _enabled(**overrides: Any) -> AllocatorSettings:
    params: dict[str, Any] = {"enabled": True, "max_results": 5}
    params.update(overrides)
    return AllocatorSettings(**params)


def _events(captured: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == name]


class TestAllocatorT08:
    """T08: claim -> score -> top-5 notify, sorted; empty underfed; gate."""

    async def test_top5_notified_sorted_by_score_from_6_items(self) -> None:
        # K=priority=1, F=1, P=R=0 -> score == density; distinct densities
        # give a known desc order: item-6 (0.95) ... item-2 (0.55); item-1 out.
        msgs = [
            _msg(
                f"m{i}",
                {"item_id": f"item-{i}", "priority": 1.0, "density": i / 10, "risk": 0.0},
            )
            for i in range(1, 7)
        ]
        queue, store = FakeQueue(msgs), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert result.outcome == "allocated"
        assert [d.item_id for d in result.selected] == [
            "item-6",
            "item-5",
            "item-4",
            "item-3",
            "item-2",
        ]
        assert result.claimed == 6 and result.scored == 6 and result.poisoned == 0
        assert result.acked == 6 and result.notified == 5 and result.nacked == 0
        assert len(queue.enqueued) == 5
        assert {e["stream"] for e in queue.enqueued} == {StreamName.ORCHESTRATOR_CORE}
        assert len(store.events) == 5
        payloads = [e["payload"] for e in queue.enqueued]
        assert payloads[0]["item_id"] == "item-6"
        # Purist: the selected score equals the ADR-0012 formula on derived inputs.
        first = result.selected[0]
        assert first.score == attention_allocation_score(1.0, 0.6, 1.0, 0.0, 0.0)
        # Ledger event carries score + inputs (the auditable record).
        ev = next(iter(store.events.values()))
        assert ev.event_type is EventType.ATTENTION_ALLOCATED
        assert {"item_id", "score", "inputs"} <= set(ev.payload)

    async def test_empty_queue_underfed_noop(self) -> None:
        queue, store = FakeQueue([]), FakeStore()
        with structlog.testing.capture_logs() as captured:
            result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert result.outcome == "underfed"
        assert result.claimed == 0
        assert len(_events(captured, "allocator.underfed")) == 1
        assert store.events == {} and queue.enqueued == [] and queue.acked == []

    async def test_disabled_leaves_queue_untouched(self) -> None:
        queue, store = FakeQueue([_msg("m1", {"item_id": "item-1"})]), FakeStore()
        result = await AttentionAllocator(queue, store, AllocatorSettings()).allocate_attention()
        assert result.outcome == "disabled"
        assert queue.claim_calls == []  # disabled: never claims, never hides work
        assert store.events == {} and queue.enqueued == [] and queue.acked == []

    async def test_settings_default_disabled_via_get_settings(self, paa_home: object) -> None:
        # Production default: the loop never runs unless the operator enables it.
        queue, store = FakeQueue([_msg("m1", {"item_id": "item-1"})]), FakeStore()
        result = await AttentionAllocator(queue, store).allocate_attention()
        assert result.outcome == "disabled"
        assert queue.claim_calls == []

    async def test_gatherer_serves_without_allocator(self) -> None:
        # Pure compile: the gatherer never blocks on (or calls) the allocator.
        gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
        packet = gatherer.compile(
            [ContextElement(id="f1", token_cost=10, slot="s0")], [], ["s0"]
        )
        assert [e.id for e in packet.elements] == ["f1"]
        assert packet.density > 0

    async def test_top_respects_max_results(self) -> None:
        msgs = [
            _msg(f"m{i}", {"item_id": f"item-{i}", "priority": 1.0, "density": 0.5})
            for i in range(6)
        ]
        queue, store = FakeQueue(msgs), FakeStore()
        small = _enabled(max_results=2)
        result = await AttentionAllocator(queue, store, small).allocate_attention()
        assert len(result.selected) == 2 and len(queue.enqueued) == 2
        assert result.acked == 6  # all scored are consumed in this run

    async def test_deterministic_tie_break_by_item_id(self) -> None:
        # Equal scores break on item_id asc (total order, cf. _rank_key).
        msgs = [
            _msg(f"m-{c}", {"item_id": c, "priority": 0.8, "density": 0.5})
            for c in ("c", "a", "b")
        ]
        queue, store = FakeQueue(msgs), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert [d.item_id for d in result.selected] == ["a", "b", "c"]

    async def test_kfr_derivation_defaults(self) -> None:
        # Nothing known: K=0.5, F=1.0, P=0.0, R=0.0, I=0.0 -> score 0.0.
        queue, store = FakeQueue([_msg("m1", {"item_id": "x"})]), FakeStore()
        await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        ev = next(iter(store.events.values()))
        assert ev.payload["score"] == 0.0
        inputs = ev.payload["inputs"]
        assert inputs == {"K": 0.5, "I": 0.0, "F": 1.0, "P": 0.0, "R": 0.0}

    async def test_kfr_derivation_explicit_and_projection(self) -> None:
        # I falls back to relevance*confidence; K from priority; F from
        # planning_cost; P from pollution_ratio; R from risk.
        payload = {
            "item_id": "x",
            "priority": 0.9,
            "relevance": 0.8,
            "confidence": 0.5,
            "planning_cost": 2.0,
            "pollution_ratio": 0.25,
            "risk": 0.1,
        }
        queue, store = FakeQueue([_msg("m1", payload)]), FakeStore()
        await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        ev = next(iter(store.events.values()))
        assert ev.payload["inputs"] == {"K": 0.9, "I": 0.4, "F": 2.0, "P": 0.25, "R": 0.1}
        assert ev.payload["score"] == attention_allocation_score(0.9, 0.4, 2.0, 0.25, 0.1)

    async def test_p_and_r_zero_never_raise(self) -> None:
        # P=0/R=0 are the best inputs; only the (1+P)(1+R) guards reach them.
        payload = {"item_id": "x", "priority": 1.0, "density": 1.0, "pollution_ratio": 0.0}
        queue, store = FakeQueue([_msg("m1", payload)]), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert result.selected[0].score == 1.0


class TestAllocatorIdempotency:
    """Full ADR-0008 key: re-claim after visibility lapse notifies once."""

    async def test_reclaim_single_notify(self) -> None:
        msg = _msg("m1", {"item_id": "item-1", "priority": 1.0, "density": 0.9})
        queue, store = FakeQueue([msg]), FakeStore()
        first = await allocate_attention(queue, store, settings=_enabled())
        assert (first.scored, first.notified, first.acked) == (1, 1, 1)
        assert len(store.events) == 1 and len(queue.enqueued) == 1

        # Visibility lapsed before ack: the same message is redelivered.
        queue.restore(msg)
        second = await allocate_attention(queue, store, settings=_enabled())
        assert second.scored == 1 and second.acked == 1 and second.nacked == 0
        assert len(store.events) == 1  # dedupe, still one logical event
        assert len(queue.enqueued) == 1  # and still one notify
        key = compute_idempotency_key(
            CID, EventType.ATTENTION_ALLOCATED, attempt=0, discriminator="item-1"
        )
        assert key in store.events

    async def test_ledger_event_before_notify_before_ack(self) -> None:
        order: list[str] = []
        queue, store = FakeQueue([_msg("m1", {"item_id": "x", "density": 0.9})], order=order), (
            FakeStore(order=order)
        )
        await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        # INVARIANT: emit -> notify -> ack, never ack before the event persists.
        assert order == ["emit", "notify", "ack:m1"]

    async def test_derived_correlation_dedupes(self, paa_home: object) -> None:
        # No lineup key on the message: a deterministic uuid5 lineage is
        # derived from the item, so a redelivery still dedupes.
        msg = _msg("m1", {"item_id": "item-9", "density": 0.9}, correlation_id=None)
        queue, store = FakeQueue([msg]), FakeStore()
        first = await allocate_attention(queue, store, settings=_enabled())
        assert (first.scored, first.notified) == (1, 1)
        corr = first.selected[0].correlation_id
        assert corr != CID  # derived, not the module default lineage
        queue.restore(msg)
        second = await allocate_attention(queue, store, settings=_enabled())
        assert second.scored == 1 and second.acked == 1
        assert second.selected == ()  # duplicate: already notified, not re-notified
        assert len(store.events) == 1 and len(queue.enqueued) == 1

    async def test_store_failure_preserves_message(self) -> None:
        # A store that refuses the append must never ack or notify: the
        # message stays claimed for visibility-timeout reclaim instead.
        class _FailingStore(FakeStore):
            async def append(self, event: LedgerEvent) -> LedgerEvent:
                raise ConnectionError("ledger down")

        msg = _msg("m1", {"item_id": "x", "density": 0.9})
        queue = FakeQueue([msg])
        result = await AttentionAllocator(queue, _FailingStore(), _enabled()).allocate_attention()
        assert result.scored == 1 and result.emitted == 0 and result.notified == 0
        assert queue.acked == [] and queue.nacked == [] and queue.enqueued == []

    async def test_notify_failure_preserves_message(self) -> None:
        class _FailingQueue(FakeQueue):
            async def enqueue(
                self,
                stream: StreamName,
                payload: dict[str, Any],
                *,
                priority: int = 100,
                correlation_id: uuid.UUID | None = None,
                session_id: uuid.UUID | None = None,
                delay_seconds: float = 0.0,
                max_attempts: int | None = None,
            ) -> QueueMessage:
                raise ConnectionError("queue down")

        msg = _msg("m1", {"item_id": "x", "density": 0.9})
        queue, store = _FailingQueue([msg]), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        # Decision persisted, notify failed: no ack, so reclaim can retry.
        assert result.emitted == 1 and result.notified == 0 and result.acked == 0
        assert len(store.events) == 1
        assert queue.acked == []


class TestAllocatorPoison:
    """Opaque payloads are validated; poison nacks (dead-letter), never loops."""

    async def test_poison_nacked_valid_processed(self) -> None:
        bad = [
            {},  # missing item_id
            {"item_id": "x", "density": "high"},  # wrong type
            {"item_id": "x", "priority": 2.0},  # out of [0,1]
            {"item_id": "x", "density": True},  # bool is not a float
            {"item_id": "x", "planning_cost": 0.0},  # F must be > 0
            {"item_id": "x", "density": float("nan")},  # non-finite
        ]
        msgs = [_msg(f"bad{i}", p) for i, p in enumerate(bad)]
        msgs.append(_msg("good", {"item_id": "ok", "density": 0.5, "priority": 1.0}))
        queue, store = FakeQueue(msgs), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert result.poisoned == len(bad) and result.scored == 1 and result.nacked == len(bad)
        assert [m for m, _ in queue.nacked] == [f"bad{i}" for i in range(len(bad))]
        assert queue.acked == ["good"]
        assert len(store.events) == 1 and len(queue.enqueued) == 1
        assert next(iter(store.events.values())).payload["item_id"] == "ok"

    async def test_all_poison_terminates_without_events(self) -> None:
        msgs = [_msg(f"b{i}", {"priority": "x"}) for i in range(3)]
        queue, store = FakeQueue(msgs), FakeStore()
        result = await AttentionAllocator(queue, store, _enabled()).allocate_attention()
        assert result.scored == 0 and result.selected == ()
        assert store.events == {} and queue.enqueued == [] and queue.acked == []
        assert result.nacked == 3


class TestAllocatorReplay:
    """Vocab: ATTENTION_ALLOCATED/DRAIN_THROTTLED fold as _NO_PHASE_CHANGE."""

    def test_members_exist_and_are_not_terminal(self) -> None:
        assert EventType.ATTENTION_ALLOCATED is EventType("ATTENTION_ALLOCATED")
        assert EventType.DRAIN_THROTTLED is EventType("DRAIN_THROTTLED")
        assert EventType.ATTENTION_ALLOCATED not in TERMINAL_EVENTS
        assert EventType.DRAIN_THROTTLED not in TERMINAL_EVENTS

    def test_replay_phase_unchanged_and_total(self) -> None:
        cid = new_correlation_id()
        base = [
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.TASK_REQUESTED,
                state_version=1,
                payload={"request": {"goal": "x"}},
            )
        ]
        before = replay(base)
        with_alloc = [
            base[0],
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.ATTENTION_ALLOCATED,
                state_version=2,
                payload={
                    "item_id": "f-1",
                    "score": 0.4,
                    "inputs": {"K": 0.9, "I": 0.4, "F": 1.0, "P": 0.0, "R": 0.0},
                },
                discriminator="f-1",
            ),
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.DRAIN_THROTTLED,
                state_version=3,
                payload={"indexed": 0, "cap": 5},
            ),
        ]
        after = replay(with_alloc)
        assert after.phase is before.phase
        assert after.unknown_events == []

    def test_replay_never_raises_on_hostile_payload(self) -> None:
        cid = new_correlation_id()
        events = [
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.ATTENTION_ALLOCATED,
                state_version=1,
                payload={"item_id": "x" * 10_000, "score": 0.0, "inputs": {"K": "nope"}},
            )
        ]
        state = replay(events)
        assert state.unknown_events == []


class _RecordingSink:
    """An embedding sink that records what it was handed."""

    def __init__(self) -> None:
        self.batches: list[list[Fact]] = []

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        self.batches.append(list(facts))
        return [fact.id for fact in facts]


async def _seed_pending(db: Database, *, n: int, prefix: str) -> list[str]:
    repo = FactRepository(db)
    eid = await repo.upsert_entity("concept", f"{prefix}-entity")
    return [await repo.add_fact(eid, "status", f"{prefix}-v{i}") for i in range(n)]


class TestDrainThrottleT15:
    """T15: embed > max_per_second -> backpressure log + rate cap, no drop."""

    async def test_cap_limits_batch_and_logs_backpressure(self, db: Database) -> None:
        await _seed_pending(db, n=100, prefix="throttle")
        sink = _RecordingSink()
        creator = MemoryCreator(db, embedding_sink=sink, drain_max_per_second=5)
        with structlog.testing.capture_logs() as captured:
            assert await creator.drain_pending_embeddings() == 5
        assert len([b for b in sink.batches]) == 1 and len(sink.batches[0]) == 5
        # Never a silent drop: everything else stays pending.
        assert len(await FactRepository(db).pending_embeddings()) == 95
        assert _events(captured, "memory.embedding_drain_throttled") != []

    async def test_window_exhaustion_defers_fully(self, db: Database) -> None:
        await _seed_pending(db, n=100, prefix="throttle-full")
        creator = MemoryCreator(db, embedding_sink=_RecordingSink(), drain_max_per_second=5)
        assert await creator.drain_pending_embeddings() == 5
        with structlog.testing.capture_logs() as captured:
            assert await creator.drain_pending_embeddings() == 0
        assert len(await FactRepository(db).pending_embeddings()) == 95
        assert _events(captured, "memory.embedding_drain_throttled") != []

    async def test_default_cap_drains_full_batch(self, db: Database) -> None:
        # No explicit cap: the Settings default (100) lets a full batch through.
        await _seed_pending(db, n=100, prefix="throttle-default")
        creator = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await creator.drain_pending_embeddings() == 100

    async def test_invalid_cap_rejected(self, db: Database) -> None:
        with pytest.raises(ValueError):
            MemoryCreator(db, drain_max_per_second=0)
