"""T-007b closer (PLAN:integration-fix:P007).

Covers the P007b half W4 left callable-only: the daemon allocation tick
(scheduled, gated, supervised — mirrors the reflection loop), the
operator-fed producer decision (no synthetic producer; the payload
contract is the ingress shape), and the DRAIN_THROTTLED emitter (breach
only, full-key idempotent, log-only when no ledger handle is supplied).

Reversal: deleting the daemon tick breaks the scheduled-path asserts;
deleting the emit call breaks the breach asserts; a synthetic producer
would break the docstring pin.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from paa.config import AllocatorSettings
from paa.context.allocator import AttentionAllocator
from paa.core.types import EventType
from paa.daemon import Daemon
from paa.ledger.events import LedgerEvent
from paa.memory.creator import MemoryCreator
from paa.storage.queue.base import QueueMessage, StreamName
from paa.storage.relational.database import Database


class FakeQueue:
    """Records claim/ack/nack/enqueue; messages leave only on ack."""

    def __init__(self, messages: list[QueueMessage]) -> None:
        self._pending = list(messages)
        self.claim_calls: list[tuple[Any, str, int]] = []
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str]] = []
        self.enqueued: list[dict[str, Any]] = []

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
    """`LedgerStore.append` semantics: dedupe by full key, return existing."""

    def __init__(self) -> None:
        self.events: dict[str, LedgerEvent] = {}
        self.appended: list[LedgerEvent] = []

    async def append(self, event: LedgerEvent) -> LedgerEvent:
        self.appended.append(event)
        key = event.idempotency_key
        if key in self.events:
            return self.events[key]
        self.events[key] = event
        return event


def _msg(
    msg_id: str, payload: dict[str, Any], *, correlation_id: uuid.UUID | None = None
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


class FakeOrchestrator:
    """Wiring fake: run_allocation delegates to the real allocator."""

    def __init__(self, allocator: AttentionAllocator) -> None:
        self._allocator = allocator
        self.calls = 0

    async def run_allocation(self, *, limit: int = 25) -> Any:
        self.calls += 1
        return await self._allocator.allocate_attention(limit=limit)


def _daemon(*, enabled: bool, queue: FakeQueue, store: FakeStore) -> Daemon:
    allocator = AttentionAllocator(
        queue, store, AllocatorSettings(enabled=enabled, max_results=5)
    )
    rt = SimpleNamespace(
        settings=SimpleNamespace(allocator=AllocatorSettings(enabled=enabled)),
        orchestrator=FakeOrchestrator(allocator),
    )
    return Daemon(rt)  # type: ignore[arg-type]


class TestAllocationScheduling:
    """R-W4-002: the tick runs when enabled, never claims when disabled."""

    async def test_enabled_tick_allocates_and_notifies(self) -> None:
        msgs = [
            _msg("m1", {"item_id": "item-1", "priority": 1.0, "density": 0.9}),
            _msg("m2", {"item_id": "item-2", "priority": 1.0, "density": 0.1}),
        ]
        queue, store = FakeQueue(msgs), FakeStore()
        daemon = _daemon(enabled=True, queue=queue, store=store)
        result = await daemon._run_allocation_tick()
        assert result is not None
        assert result.outcome == "allocated"
        assert queue.claim_calls == [(StreamName.WORKERS_ALLOC, "allocator", 25)]
        assert len(queue.enqueued) == 2
        assert {e["stream"] for e in queue.enqueued} == {StreamName.ORCHESTRATOR_CORE}
        assert len(store.events) == 2
        assert queue.acked == ["m1", "m2"]

    async def test_disabled_tick_never_touches_queue(self) -> None:
        queue, store = FakeQueue([_msg("m1", {"item_id": "item-1"})]), FakeStore()
        daemon = _daemon(enabled=False, queue=queue, store=store)
        result = await daemon._run_allocation_tick()
        assert result is None
        assert queue.claim_calls == []
        assert store.appended == []

    async def test_enabled_tick_empty_queue_underfed_no_notify(self) -> None:
        queue, store = FakeQueue([]), FakeStore()
        daemon = _daemon(enabled=True, queue=queue, store=store)
        result = await daemon._run_allocation_tick()
        assert result is not None and result.outcome == "underfed"
        assert queue.enqueued == [] and store.events == {}


class TestProducerDecision:
    """The queue is operator-fed; the docstring is the contract a W6 reader sees."""

    def test_allocator_module_documents_operator_fed_producer(self) -> None:
        import paa.context.allocator as allocator_mod

        doc = allocator_mod.__doc__ or ""
        assert "operator-fed" in doc
        assert "workers:alloc" in doc


class TestDrainThrottledEmit:
    """R-W4-006: breach-only emit, full-key idempotent, handle-optional."""

    async def test_breach_emits_one_event_with_threshold_payload(self, db: Database) -> None:
        store = FakeStore()
        creator = MemoryCreator(db, drain_max_per_second=1, ledger=store)
        # Pin the window so the breach is deterministic (no wall-clock wait).
        import asyncio

        creator._drain_window_started = asyncio.get_running_loop().time()
        creator._drain_window_indexed = 1
        assert await creator.drain_pending_embeddings() == 0
        assert len(store.events) == 1
        (event,) = store.events.values()
        assert event.event_type is EventType.DRAIN_THROTTLED
        assert event.attempt == 0
        payload = event.payload
        assert payload["threshold"] == 1 and payload["drain_max_per_second"] == 1
        assert payload["reason"] == "drain_throttle" and payload["fact"] == "embedding_drain"
        assert event.discriminator is not None and event.discriminator.startswith(
            "drain_throttled:"
        )

    async def test_second_breach_same_window_dedupes(self, db: Database) -> None:
        store = FakeStore()
        creator = MemoryCreator(db, drain_max_per_second=1, ledger=store)
        import asyncio

        creator._drain_window_started = asyncio.get_running_loop().time()
        creator._drain_window_indexed = 1
        assert await creator.drain_pending_embeddings() == 0
        assert await creator.drain_pending_embeddings() == 0
        assert len(store.appended) == 2
        assert len(store.events) == 1

    async def test_no_breach_no_event(self, db: Database) -> None:
        store = FakeStore()
        creator = MemoryCreator(db, drain_max_per_second=100, ledger=store)
        assert await creator.drain_pending_embeddings() == 0
        assert store.appended == []

    async def test_no_handle_stays_log_only(self, db: Database) -> None:
        creator = MemoryCreator(db, drain_max_per_second=1)
        import asyncio

        creator._drain_window_started = asyncio.get_running_loop().time()
        creator._drain_window_indexed = 1
        # Pre-existing constructors pass no ledger: breach must not raise.
        assert await creator.drain_pending_embeddings() == 0
