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

import structlog

from paa.config import AllocatorSettings
from paa.context.allocator import AllocationResult, AttentionAllocator
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

    async def test_partial_cap_breach_emits_with_effective_limit(
        self, db: Database
    ) -> None:
        """The 2nd emit site: budget (1) < limit (100) still emits, with the
        *effective* limit recorded (TREV R-W5-004). Empty queue is fine —
        the emit precedes the pending fetch."""
        import asyncio

        store = FakeStore()
        creator = MemoryCreator(db, drain_max_per_second=2, ledger=store)
        creator._drain_window_started = asyncio.get_running_loop().time()
        creator._drain_window_indexed = 1
        assert await creator.drain_pending_embeddings(limit=100) == 0
        assert len(store.events) == 1
        (event,) = store.events.values()
        assert event.event_type is EventType.DRAIN_THROTTLED
        assert event.payload["effective_limit"] == 1
        assert event.payload["threshold"] == 2
        assert event.payload["limit"] == 100

    async def test_discriminator_carries_process_id(self, db: Database) -> None:
        """Same-window dedupe stays, but the key is per-process (REV R-W5-004):
        a restarted process's breach must not dedupe against a pre-restart
        event with the same monotonic-relative window stamps."""
        import asyncio

        import paa.memory.creator as creator_mod

        store = FakeStore()
        creator = MemoryCreator(db, drain_max_per_second=1, ledger=store)
        creator._drain_window_started = asyncio.get_running_loop().time()
        creator._drain_window_indexed = 1
        assert await creator.drain_pending_embeddings() == 0
        (event,) = store.events.values()
        assert event.discriminator is not None
        assert creator_mod._DRAIN_PROCESS_ID in event.discriminator
        assert event.discriminator.startswith("drain_throttled:")


class TestProducerLock:
    """Operator-fed lock (TREV R-W5-006): the docstring semantics are the
    contract, this grep is the lock. A synthetic producer that enqueues to
    workers:alloc anywhere other than the queue docstring example fails."""

    def test_no_workers_alloc_enqueue_outside_queue_docstring(self) -> None:
        """No ``.enqueue(...)`` call names WORKERS_ALLOC outside the queue
        docstring example. (Line-shaped lock on the call site: docstring
        prose *about* enqueueing is documentation, not a producer, and a
        multi-line call that puts the stream on a later line would evade
        it — the docstring contract is the other half of this lock.)"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src"
        allowed = {"paa/storage/queue/__init__.py"}
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in allowed:
                continue
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if ".enqueue(" in line and (
                    "WORKERS_ALLOC" in line or "workers:alloc" in line
                ):
                    offenders.append(f"{rel}:{line_no}: {line.strip()}")
        assert offenders == [], f"synthetic alloc producers: {offenders}"

    def test_operator_fed_statement_present(self) -> None:
        import paa.context.allocator as allocator_mod

        doc = allocator_mod.__doc__ or ""
        assert "operator-fed" in doc
        assert "workers:alloc" in doc


class TestDaemonScheduling:
    """The scheduling half of the allocation loop (TREV R-W5-003): the 5th
    supervised task exists in start(), and a tick failure is logged while
    the loop carries on (the R-W4-002 disease recurred one level up)."""

    async def test_start_creates_five_supervised_tasks(self) -> None:
        async def _noop() -> None:
            return None

        async def _idle() -> None:
            await daemon._stop.wait()

        rt = SimpleNamespace(boot_recovery=_noop)
        daemon = Daemon(rt)  # type: ignore[arg-type]
        for name in (
            "_dispatch_loop",
            "_ingestion_loop",
            "_maintenance_loop",
            "_reflection_loop",
            "_allocation_loop",
        ):
            setattr(daemon, name, _idle)
        with structlog.testing.capture_logs() as logs:
            await daemon.start()
        assert len(daemon._tasks) == 5
        assert any(
            entry["event"] == "daemon.started" and entry.get("loops") == 5
            for entry in logs
        )
        await daemon.stop()

    async def test_tick_raise_logs_and_loop_continues(self) -> None:
        class FlakyOrchestrator:
            def __init__(self) -> None:
                self.calls = 0

            async def run_allocation(self, *, limit: int = 25) -> AllocationResult:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("boom")
                return AllocationResult(outcome="underfed", claimed=0)

        flaky = FlakyOrchestrator()
        rt = SimpleNamespace(
            settings=SimpleNamespace(
                allocator=AllocatorSettings(enabled=True, max_results=5)
            ),
            orchestrator=flaky,
        )
        daemon = Daemon(rt)  # type: ignore[arg-type]
        sleeps = 0

        async def fake_sleep(seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps >= 2:
                daemon._stop.set()

        daemon._sleep = fake_sleep  # type: ignore[method-assign]
        with structlog.testing.capture_logs() as logs:
            await daemon._allocation_loop()
        assert flaky.calls == 2
        assert any(
            entry["event"] == "daemon.allocation_failed" for entry in logs
        )
        assert any(
            entry["event"] == "daemon.allocation_completed" for entry in logs
        )


class TestProductionWiring:
    """R-W5-001: the production construction path threads the real ledger
    into the creator — not just the creator-side emit with fakes."""

    def test_build_memory_creator_threads_ledger_identity(
        self, db: Database, ledger: Any, tmp_path: Any
    ) -> None:
        from paa.config import Settings
        from paa.runtime import _build_memory_creator

        settings = Settings(home=tmp_path / "paa_home")
        creator = _build_memory_creator(
            settings, db, None, ledger=ledger
        )
        assert creator._ledger is ledger

    async def test_runtime_build_creator_breach_emits_to_real_ledger(
        self, tmp_path: Any
    ) -> None:
        import asyncio

        from paa.config import Settings
        from paa.core.types import EventType
        from paa.runtime import Runtime

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        settings = Settings(home=home)
        settings.sandbox.backend = "subprocess"

        class _ScriptedModel:
            async def complete_structured(
                self, prompt: str, schema: dict[str, Any], **kwargs: Any
            ) -> dict[str, Any]:
                return {"text": "x", "tool_calls": []}

        rt = await Runtime.build(
            settings,
            model_adapter=_ScriptedModel(),
            run_recovery=False,
            enable_optional_backends=False,
        )
        try:
            creator = rt.memory_creator
            assert creator is not None
            creator._drain_window_started = asyncio.get_running_loop().time()
            creator._drain_window_indexed = creator._drain_max_per_second
            assert await creator.drain_pending_embeddings() == 0
            from paa.memory.creator import _DRAIN_THROTTLED_CORRELATION_ID

            events = await rt.ledger.read_correlation(
                _DRAIN_THROTTLED_CORRELATION_ID
            )
            throttles = [
                e for e in events if e.event_type is EventType.DRAIN_THROTTLED
            ]
            assert len(throttles) == 1
            assert throttles[0].payload["reason"] == "drain_throttle"
        finally:
            await rt.close()
