"""Direct tests for the ``run_allocation`` orchestrator callsite (P-007 / REV R-W4-007).

TREV-W4 finding R-W4-001: the one production callsite of the allocator had
no test calling it — a broken lazy import or a wrong store argument would
land silently because the allocator defaults to disabled and nothing in
production exercises the path until an operator enables it.

These tests drive the real :meth:`ChiefOrchestrator.run_allocation` against
the real ledger (tmp ``db``/``ledger`` fixtures) with a scripted queue:
the allocated path proves the callsite wires the orchestrator's store and
the process ``AllocatorSettings`` through to ``allocate_attention``, and the
claim-failure path proves REV R-W4-007 — a queue backend that raises on
claim degrades to ``underfed``, it never propagates.

Reversal: deleting the ``run_allocation`` body (or making it re-raise)
fails the allocated and the claim-failure test for the right reason.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from paa.agents.orchestrator import ChiefOrchestrator
from paa.config import AllocatorSettings

# R-W4b-001 (shape (a)): importing allocate_attention at MODULE scope runs
# paa.context.__init__ (gatherer, budget) and the allocator module at
# COLLECTION time — before any monkeypatch above/below is active — so those
# modules bind the REAL paa.config.get_settings. Without this, run_allocation's
# lazy import would be the first context import and would bind the _enable
# stub's lambda in gatherer/budget/allocator, poisoning every later test in
# the process (SimpleNamespace has no attribute 'context'). The name is used
# by the gatherer-binding pin test below, which is what keeps this import
# from being unused.
from paa.context.allocator import allocate_attention
from paa.core.types import new_correlation_id
from paa.ledger.store import LedgerStore
from paa.storage.queue.base import QueueMessage, StreamName

CID = new_correlation_id()


def _msg(msg_id: str, payload: dict[str, Any]) -> QueueMessage:
    now = datetime.now(UTC)
    return QueueMessage(
        id=msg_id,
        stream=StreamName.WORKERS_ALLOC,
        correlation_id=CID,
        payload=payload,
        enqueued_at=now,
        visible_after=now,
    )


class _Queue:
    """The claim-ack surface; records everything, delivers a fixed batch."""

    def __init__(self, messages: list[QueueMessage]) -> None:
        self._pending = list(messages)
        self.claim_calls: list[tuple[Any, str, int]] = []
        self.acked: list[str] = []
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
        raise AssertionError("nack is outside this callsite's contract")

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
        self.enqueued.append({"stream": stream, "payload": payload})
        return _msg(f"notify-{len(self.enqueued)}", payload)


class _RaisingQueue(_Queue):
    """A queue backend that is down: claim itself raises."""

    async def claim(
        self,
        stream: StreamName,
        consumer: str,
        *,
        limit: int = 1,
        visibility_timeout: float | None = None,
    ) -> list[QueueMessage]:
        raise RuntimeError("workers:alloc is down")


def _orchestrator(ledger: LedgerStore, queue: _Queue) -> ChiefOrchestrator:
    return ChiefOrchestrator(ledger, queue=queue)


def _enable(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Point the process settings at an allocator switch state.

    ``run_allocation`` reads the process settings by lazy import, so the
    seam here is the settings object itself — the same one production
    reads.

    INVARIANT (R-W4b-001): the ``paa.context`` modules are already bound
    at collection time (module-scope ``allocate_attention`` import
    above), so this patch never coincides with a first-time context
    import and can never rebind ``paa.context.gatherer`` /
    ``paa.context.budget`` / ``paa.context.allocator`` to the lambda
    below. The stub stays intentionally narrow (only ``.allocator``):
    widening it to a full Settings object would hide, not fix, a
    reintroduced first-import-during-patch.
    """
    settings = SimpleNamespace(
        allocator=AllocatorSettings(
            enabled=enabled, max_results=5
        )
    )
    monkeypatch.setattr("paa.config.get_settings", lambda: settings)


async def test_run_allocation_allocates_through_the_orchestrator(
    ledger: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callsite delegates to allocate_attention and returns its result.

    One enqueued item, allocator enabled: the run scores it, appends the
    decision to the orchestrator's ledger (never a side ledger), notifies
    the controller, acks — and returns the result typed as the allocator's
    own ``AllocationResult``.
    """
    queue = _Queue(
        [_msg("m1", {"item_id": "item-1", "priority": 1.0, "density": 0.9, "risk": 0.0})]
    )
    _enable(monkeypatch, enabled=True)
    result = await _orchestrator(ledger, queue).run_allocation()
    assert result.outcome == "allocated"
    assert [d.item_id for d in result.selected] == ["item-1"]
    assert queue.claim_calls == [(StreamName.WORKERS_ALLOC, "allocator", 25)]
    assert queue.acked == ["m1"]
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["stream"] == StreamName.ORCHESTRATOR_CORE
    rows = await ledger.read_correlation(CID)
    assert [e.event_type.value for e in rows] == ["ATTENTION_ALLOCATED"]


async def test_run_allocation_degrades_to_underfed_on_claim_failure(
    ledger: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV R-W4-007: a claim that raises is an underfed no-op, never a raise.

    The house posture is degrade-and-log; the outage surfaces as an
    ``allocator.claim_failed`` warning and an ``outcome="underfed"`` result
    — no exception reaches the caller and nothing is acked or emitted.
    """
    _enable(monkeypatch, enabled=True)
    with structlog.testing.capture_logs() as captured:
        result = await _orchestrator(ledger, _RaisingQueue([])).run_allocation()
    assert result.outcome == "underfed"
    assert result.claimed == 0
    assert [e for e in captured if e.get("event") == "allocator.claim_failed"]
    assert await ledger.read_correlation(CID) == []


async def test_run_allocation_disabled_leaves_the_queue_untouched(
    ledger: LedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-off switch keeps production untouched: no claim, no crash."""
    queue = _Queue(
        [_msg("m1", {"item_id": "item-1", "priority": 1.0, "density": 0.9, "risk": 0.0})]
    )
    _enable(monkeypatch, enabled=False)
    result = await _orchestrator(ledger, queue).run_allocation()
    assert result.outcome == "disabled"
    assert queue.claim_calls == []
    assert queue.acked == []


async def test_gatherer_binding_survives_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-W4b-001 pin: the patched window must not poison the gatherer.

    Reversal: removing the module-scope ``allocate_attention`` import
    makes this construction raise
    ``AttributeError: 'types.SimpleNamespace' object has no attribute
    'context'`` — the gatherer bound the ``_enable`` lambda on its first
    import, performed by ``run_allocation``'s lazy import inside the
    patched window, and the binding outlived monkeypatch teardown. With
    the early import the gatherer keeps the real process settings, so
    construction inside the patched window succeeds.
    """
    _enable(monkeypatch, enabled=True)
    from paa.context.gatherer import BoundedContextGatherer

    # The exact poisoned path (gatherer.py reads process settings via its
    # own module binding, not via the patched paa.config attribute).
    assert BoundedContextGatherer().settings is not None
    # The module-scope import is the object the lazy import resolves to:
    # run_allocation's `from paa.context.allocator import allocate_attention`
    # is a sys.modules hit, not a re-execution.
    assert sys.modules["paa.context.allocator"].allocate_attention is allocate_attention
