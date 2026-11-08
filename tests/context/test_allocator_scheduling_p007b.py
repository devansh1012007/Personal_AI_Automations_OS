"""T-007b closer (PLAN:integration-fix:P007).

Covers the P007b half W4 left callable-only: the daemon allocation tick
(scheduled, gated, supervised — mirrors the reflection loop), the
operator-fed producer decision (no synthetic producer; the payload
contract is the ingress shape), and the DRAIN_THROTTLED emitter (breach
only, full-key idempotent, log-only when no ledger handle is supplied).

Reversal: deleting the daemon tick breaks the scheduled-path asserts;
deleting the emit call breaks the breach asserts; an ``enqueue`` call
naming ``StreamName.WORKERS_ALLOC`` (or the ``"workers:alloc"`` string)
anywhere under ``src/`` breaks the producer lock — the queue docstring
example is a string literal, not a call node, so the AST scan never
counts it.
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
        self.last_settings: AllocatorSettings | None = None
        self.seen_settings: list[AllocatorSettings | None] = []

    async def run_allocation(
        self, *, limit: int = 25, settings: AllocatorSettings | None = None
    ) -> Any:
        self.calls += 1
        self.last_settings = settings
        self.seen_settings.append(settings)
        if settings is None:
            return await self._allocator.allocate_attention(limit=limit)
        from paa.context.allocator import allocate_attention

        return await allocate_attention(
            self._allocator._queue,
            self._allocator._store,
            settings=settings,
            limit=limit,
        )


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


def _workers_alloc_enqueue_sites(source: str, filename: str) -> list[str]:
    """Every ``enqueue(...)`` call in ``source`` that names ``workers:alloc``.

    The producer-lock predicate (TREV R-W5-006, hardened T-03): an AST
    scan, not a line grep — a multi-line call that puts the stream on a
    later line is still one ``ast.Call`` node, so line breaks cannot hide
    a producer the way they hid it from the old line-shaped predicate.
    A call counts when its function is an ``enqueue`` attribute and any
    argument names the stream: ``StreamName.WORKERS_ALLOC``, a bare
    ``WORKERS_ALLOC`` name, or the ``"workers:alloc"`` string. The queue
    package's docstring example never matches: it is a string literal,
    not a call node, so the scan needs no file exemption. Residual gaps
    the predicate does not catch: variable indirection
    (``s = StreamName.WORKERS_ALLOC; enqueue(s)``), string concatenation
    (``"workers:" + "alloc"``), and dynamic ``getattr`` —
    ``getattr(StreamName, "WORKERS_ALLOC")`` — so the lock is not total.
    """
    import ast

    tree = ast.parse(source, filename=filename)
    sites: list[str] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "enqueue"
        ):
            continue
        operands = [*node.args, *(kw.value for kw in node.keywords)]
        if any(
            (
                isinstance(sub, ast.Attribute) and sub.attr == "WORKERS_ALLOC"
            )
            or (isinstance(sub, ast.Name) and sub.id == "WORKERS_ALLOC")
            or (isinstance(sub, ast.Constant) and sub.value == "workers:alloc")
            for operand in operands
            for sub in ast.walk(operand)
        ):
            sites.append(f"{filename}:{node.lineno}")
    return sites


class TestProducerLock:
    """Operator-fed lock (TREV R-W5-006, hardened T-03): the docstring
    semantics are the contract, this AST scan is the lock. A synthetic
    producer that enqueues to workers:alloc anywhere under ``src/`` —
    on one line or split across lines — fails."""

    def test_no_workers_alloc_enqueue_in_src(self) -> None:
        """No ``enqueue`` call names WORKERS_ALLOC anywhere under ``src/``.

        AST-shaped (see ``_workers_alloc_enqueue_sites``): neither line
        breaks nor the queue docstring example can evade it — the
        docstring example is a string literal, not a call node.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src"
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            offenders.extend(
                _workers_alloc_enqueue_sites(path.read_text(encoding="utf-8"), rel)
            )
        assert offenders == [], f"synthetic alloc producers: {offenders}"

    def test_multiline_enqueue_evasion_is_caught(self) -> None:
        """Negative control: the multi-line shape the old line predicate missed.

        The synthetic producer puts ``.enqueue(`` and ``WORKERS_ALLOC``
        on different lines — the old per-line predicate (reproduced
        below) reports no offender, so under the old lock this producer
        would have passed silently. The AST predicate must flag it, or
        the lock is apparent, not executed.
        """
        synthetic = (
            "from paa.storage.queue.base import StreamName\n"
            "\n"
            "async def _ingress(queue, payload):\n"
            "    await queue.enqueue(\n"
            "        StreamName.WORKERS_ALLOC,\n"
            "        payload,\n"
            "    )\n"
        )
        # The pre-T-03 line-shaped predicate, reproduced to prove evasion.
        old_offenders = [
            line
            for line in synthetic.splitlines()
            if ".enqueue(" in line
            and ("WORKERS_ALLOC" in line or "workers:alloc" in line)
        ]
        assert old_offenders == []
        assert _workers_alloc_enqueue_sites(synthetic, "<synthetic>") != []

    def test_queue_docstring_example_is_not_a_call_site(self) -> None:
        """The queue docstring example is prose, not a producer: pinned twice.

        The example text must still be in the docstring (the operator's
        contract) and must NOT be flagged — it is a string literal, so
        the AST scan cannot see it as a call. The old file-level
        exemption would have hidden real producers in that file too.
        """
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "paa"
            / "storage"
            / "queue"
            / "__init__.py"
        )
        source = path.read_text(encoding="utf-8")
        assert ".enqueue(" in source and "WORKERS_ALLOC" in source
        sites = _workers_alloc_enqueue_sites(source, "paa/storage/queue/__init__.py")
        assert sites == []

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
                self.last_settings: AllocatorSettings | None = None
                self.seen_settings: list[AllocatorSettings | None] = []

            async def run_allocation(
                self,
                *,
                limit: int = 25,
                settings: AllocatorSettings | None = None,
            ) -> AllocationResult:
                self.calls += 1
                self.last_settings = settings
                self.seen_settings.append(settings)
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

    async def test_allocator_gate_agrees_on_non_global_settings(
        self, monkeypatch: Any
    ) -> None:
        """R-W5-006 / W5-FIX-02: daemon and allocator agree on one source.

        The daemon gate reads the runtime Settings; the orchestrator must
        run the allocator with that same object, not the process-global
        singleton. Non-global ``enabled=True`` with a disabled global must
        allocate — never a ``"disabled"`` outcome logged as completed —
        and ``enabled=False`` with an enabled global must silence the
        tick without touching the queue.
        """
        # Non-global enabled while the global is disabled: the tick allocates.
        monkeypatch.setattr(
            "paa.config.get_settings",
            lambda: SimpleNamespace(
                allocator=AllocatorSettings(enabled=False, max_results=5)
            ),
        )
        queue = FakeQueue(
            [_msg("m1", {"item_id": "item-1", "priority": 1.0, "density": 0.9})]
        )
        store = FakeStore()
        rt_alloc = AllocatorSettings(enabled=True, max_results=5)
        # Baked disabled mirrors the disabled global, so without forwarding
        # the fake would return "disabled" and the asserts below fail.
        allocator = AttentionAllocator(
            queue, store, AllocatorSettings(enabled=False, max_results=5)
        )
        fake = FakeOrchestrator(allocator)
        rt = SimpleNamespace(
            settings=SimpleNamespace(allocator=rt_alloc), orchestrator=fake
        )
        daemon = Daemon(rt)  # type: ignore[arg-type]
        with structlog.testing.capture_logs() as logs:
            result = await daemon._run_allocation_tick()
        assert result is not None
        assert result.outcome == "allocated"
        assert fake.calls == 1
        assert fake.last_settings is rt_alloc
        completed = [e for e in logs if e["event"] == "daemon.allocation_completed"]
        assert completed
        assert all(e.get("outcome") != "disabled" for e in completed)
        assert queue.claim_calls != []

        # Inverse: runtime disabled while the global is enabled stays quiet.
        monkeypatch.setattr(
            "paa.config.get_settings",
            lambda: SimpleNamespace(
                allocator=AllocatorSettings(enabled=True, max_results=5)
            ),
        )
        queue2 = FakeQueue([_msg("m2", {"item_id": "item-2"})])
        store2 = FakeStore()
        rt_alloc2 = AllocatorSettings(enabled=False, max_results=5)
        allocator2 = AttentionAllocator(
            queue2, store2, AllocatorSettings(enabled=True, max_results=5)
        )
        fake2 = FakeOrchestrator(allocator2)
        rt2 = SimpleNamespace(
            settings=SimpleNamespace(allocator=rt_alloc2), orchestrator=fake2
        )
        daemon2 = Daemon(rt2)  # type: ignore[arg-type]
        result2 = await daemon2._run_allocation_tick()
        assert result2 is None
        assert fake2.calls == 0
        assert queue2.claim_calls == []
        assert store2.appended == []

    async def test_daemon_cost_gate_uses_runtime_settings(
        self, db: Database, paa_home: Any, monkeypatch: Any
    ) -> None:
        """The daemon curator owns the runtime cost gate, not the global.

        Non-global ``cost_gate.enabled=True`` with a disabled global must
        suggest (the injected-path mirror of the curator injection tests);
        inverse — runtime disabled, global enabled — must stay quiet.
        """
        from paa.config import CostGateSettings, MemorySettings, reset_settings_cache
        from paa.memory.facts import FactRepository

        monkeypatch.setenv("PAA_COST_GATE__ENABLED", "false")
        reset_settings_cache()
        repo = FactRepository(db)
        eid = await repo.upsert_entity("project", "Alpha")
        await repo.add_fact(eid, "status", "fresh", confidence=1.0, importance=0.0)
        rt_cost_gate = CostGateSettings(enabled=True, threshold=1.0)
        rt = SimpleNamespace(
            settings=SimpleNamespace(
                memory=MemorySettings(),
                cost_gate=rt_cost_gate,
                allocator=AllocatorSettings(enabled=False),
            ),
            db=db,
            vector_store=None,
            archive_writer=None,
            orchestrator=FakeOrchestrator(
                AttentionAllocator(FakeQueue([]), FakeStore(), None)
            ),
        )
        daemon = Daemon(rt)  # type: ignore[arg-type]
        report = await daemon._run_curation_tick()
        assert report is not None
        assert len(report.cost_gate_suggestions) == 1

        monkeypatch.setenv("PAA_COST_GATE__ENABLED", "true")
        reset_settings_cache()
        rt2 = SimpleNamespace(
            settings=SimpleNamespace(
                memory=MemorySettings(),
                cost_gate=CostGateSettings(enabled=False, threshold=1.0),
                allocator=AllocatorSettings(enabled=False),
            ),
            db=db,
            vector_store=None,
            archive_writer=None,
            orchestrator=FakeOrchestrator(
                AttentionAllocator(FakeQueue([]), FakeStore(), None)
            ),
        )
        daemon2 = Daemon(rt2)  # type: ignore[arg-type]
        # Fresh daemon: the first tick previews again, so the quiet assertion
        # is about suggestions, not about preview state.
        report2 = await daemon2._run_curation_tick()
        assert report2 is not None
        assert report2.cost_gate_suggestions == []


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
