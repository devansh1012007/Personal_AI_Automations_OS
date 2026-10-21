"""The ``MODEL_ESCALATED`` audit trail — the one event that must never be lost.

Escalation is the moment reasoning material leaves the machine, so the ledger
record of it is the whole point of ADR-0015's second decision. The router is
the only component in the runtime that writes this event and nothing else
writes it, so these tests are the only place its properties are pinned.

Four of them, each previously untested:

* the event is written **before** the frontier call, not after — a request
  that leaves the machine and then times out has still left the machine, and
  logging on success would make exactly the failed escalations invisible;
* it carries the caller's correlation id when there is one;
* it gets a *synthetic* correlation id when there is not — an orphan lineage
  beats a missing audit record, and the escalation still happened;
* two escalations in one lineage are two events, because the per-router
  sequence number is the discriminator that keeps their idempotency keys
  apart. Without it the second would be suppressed as a duplicate and the
  audit trail would under-count.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog.testing

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.models.base import CompletionRequest, CompletionResponse, Message, ModelTier
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("diagnose this"),))


def _router(ledger: LedgerStore, *, fail_times: int = 1) -> EscalatingModelRouter:
    """A router whose local provider fails, with a frontier to escalate to."""
    local = EchoProvider(fail_times=fail_times)
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    return EscalatingModelRouter(
        local, frontier, settings=ModelSettings(), ledger_store=ledger
    )


async def _escalations(ledger: LedgerStore, correlation_id: uuid.UUID) -> list[Any]:
    events = await ledger.read_correlation(correlation_id)
    return [e for e in events if e.event_type is EventType.MODEL_ESCALATED]


async def test_an_escalation_writes_one_model_escalated_event(ledger: LedgerStore) -> None:
    """One escalation, one event, carrying the lineage it belongs to."""
    router = _router(ledger)
    correlation_id = uuid.uuid4()

    await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    escalations = await _escalations(ledger, correlation_id)
    assert len(escalations) == 1
    event = escalations[0]
    assert event.correlation_id == correlation_id
    # The discriminator is the per-router sequence number, so a second
    # escalation in the same lineage is not suppressed as a duplicate.
    assert event.discriminator == "escalation:1"
    assert event.payload["from_provider"] == "echo"
    assert event.payload["to_provider"] == "frontier-echo"
    assert event.payload["trigger"] == "local_provider_unavailable"
    assert event.payload["leaves_machine"] is True
    assert event.payload["modality"] == ComplexityModality.COMPLEX.value
    assert event.payload["permission_mode"] == PermissionMode.ASK.value


async def test_the_audit_record_is_written_before_the_frontier_call(
    ledger: LedgerStore,
) -> None:
    """The event is durable *before* the request leaves the machine.

    A request that escalates and then times out has still left the machine.
    Logging the escalation on success would make exactly the failed
    escalations invisible, so the write has to happen first — and the only
    way to pin that is to look at the ledger from inside the frontier call.
    """

    class _FrontierProbe(EchoProvider):
        """A frontier provider that reads the ledger while it is being called."""

        def __init__(self, store: LedgerStore, lineage: uuid.UUID) -> None:
            super().__init__(name="frontier-probe", tier=ModelTier.FRONTIER)
            self._store = store
            self._lineage = lineage
            self.escalations_seen_at_call_time = 0

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            events = await self._store.read_correlation(self._lineage)
            self.escalations_seen_at_call_time = sum(
                1 for e in events if e.event_type is EventType.MODEL_ESCALATED
            )
            return await super().complete(request)

    correlation_id = uuid.uuid4()
    probe = _FrontierProbe(ledger, correlation_id)
    router = EscalatingModelRouter(
        EchoProvider(fail_times=1),
        probe,
        settings=ModelSettings(),
        ledger_store=ledger,
    )

    await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    assert probe.escalations_seen_at_call_time == 1
    assert len(await _escalations(ledger, correlation_id)) == 1


async def test_an_unattributed_escalation_still_writes_the_audit_record(
    ledger: LedgerStore,
) -> None:
    """No correlation id means a synthetic one, not a dropped event.

    The event that must never go missing is the one that says work left the
    machine, so it is recorded against a lineage of its own rather than
    discarded.
    """
    router = _router(ledger)

    await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
    )

    heads = await ledger.open_correlations()
    assert len(heads) == 1
    escalations = await _escalations(ledger, heads[0].correlation_id)
    assert len(escalations) == 1
    assert escalations[0].correlation_id == heads[0].correlation_id


async def test_two_escalations_in_one_lineage_are_two_events(ledger: LedgerStore) -> None:
    """The sequence number keeps the idempotency keys apart.

    The idempotency key is derived from ``(correlation, event_type, attempt,
    discriminator)``. Without the per-router sequence number as the
    discriminator, the second escalation in one lineage would be suppressed
    as a duplicate and the audit trail would under-count.
    """
    router = _router(ledger, fail_times=2)
    correlation_id = uuid.uuid4()

    for _ in range(2):
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.ASK,
            correlation_id=correlation_id,
        )

    escalations = await _escalations(ledger, correlation_id)
    assert len(escalations) == 2
    assert [e.discriminator for e in escalations] == ["escalation:1", "escalation:2"]


async def test_an_escalation_without_a_ledger_is_not_fatal(ledger: LedgerStore) -> None:
    """An unaudited escalation warns; it does not fail the run.

    The router is usable without a store — in a unit test, a migration
    script, a CLI one-shot — and crossing the privacy boundary unaudited is
    a fact worth logging, not a reason to refuse the call.
    """
    router = EscalatingModelRouter(
        EchoProvider(fail_times=1),
        EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER),
        settings=ModelSettings(),
    )

    response = await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=uuid.uuid4(),
    )

    assert response.provider == "frontier-echo"
    assert await ledger.count() == 0


async def test_the_log_says_whether_the_lineage_was_attributed(
    ledger: LedgerStore,
) -> None:
    """``attributed`` is the operator-facing half of the correlation handling.

    The event is written either way — an orphan lineage beats a missing audit
    record — so the flag on the log line is what tells a reader that the
    record was filed against a lineage the caller never named.
    """
    router = _router(ledger)

    with structlog.testing.capture_logs() as logs:
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.ASK,
            correlation_id=uuid.uuid4(),
        )

    escalated = [entry for entry in logs if entry.get("event") == "model.escalated"]
    assert len(escalated) == 1
    assert escalated[0]["attributed"] is True


async def test_the_log_marks_an_unattributed_escalation(ledger: LedgerStore) -> None:
    """The same flag, the other way round.

    An escalation with no correlation id is recorded against a synthetic one,
    and the log says so — otherwise the record looks like it belongs to a
    lineage somebody asked for.
    """
    router = _router(ledger)

    with structlog.testing.capture_logs() as logs:
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.ASK,
        )

    escalated = [entry for entry in logs if entry.get("event") == "model.escalated"]
    assert len(escalated) == 1
    assert escalated[0]["attributed"] is False
