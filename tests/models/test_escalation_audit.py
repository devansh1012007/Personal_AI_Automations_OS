"""The ``MODEL_ESCALATED`` audit trail — the one event that must never be lost.

Escalation is the moment reasoning material leaves the machine, so the ledger
record of it is the whole point of ADR-0015's second decision. The router is
the only component in the runtime that writes this event and nothing else
writes it, so these tests are the only place its properties are pinned.

Five of them, each previously untested:

* the event is written **before** the frontier call, not after — a request
  that leaves the machine and then times out has still left the machine, and
  logging on success would make exactly the failed escalations invisible;
* it carries the caller's correlation id when there is one;
* it gets a *synthetic* correlation id when there is not — an orphan lineage
  beats a missing audit record, and the escalation still happened;
* two escalations in one lineage are two events, because the discriminator
  keeps their idempotency keys apart;
* and that holds **across a restart** — a new router over the same ledger
  does not re-use its predecessor's key, which is the case the ledger's
  idempotency key would otherwise suppress in silence.

**The discriminator is the event's own id.** It used to be a counter on the
router instance, which made it a counter on the *process*: a restarted
router began again at ``escalation:1``, re-used the key its predecessor had
spent, and the ledger silently suppressed the second audit record. For a
while it was ``escalation:<ordinal>:<nonce>``, where the ordinal was the
lineage's escalation count *read from the ledger* — a denormalised copy of a
number the ledger already holds exactly, bought with a read of the whole
lineage and with a race (two writers that read at the same moment took the
same ordinal). It is ``escalation:<nonce>`` now: the uniqueness argument is
the ledger's own, and the lineage's sequence is the ledger's own ordering
(``state_version``, dense and assigned under the lineage's lock) rather than
a second number derived from it.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog.testing

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, CorrelationId, EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.models.base import CompletionRequest, CompletionResponse, Message, ModelTier
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter
from paa.storage.relational.database import Database


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("diagnose this"),))


def _router(ledger: LedgerStore, *, fail_times: int = 1) -> EscalatingModelRouter:
    """A router whose local provider fails, with a frontier to escalate to."""
    local = EchoProvider(fail_times=fail_times)
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    return EscalatingModelRouter(
        local, frontier, settings=ModelSettings(), ledger_store=ledger
    )


def _nonce(discriminator: str | None) -> str:
    """The nonce a discriminator carries.

    The discriminator is ``escalation:<nonce>``, and the nonce is the
    **event's own uuid4** — the ledger's own argument for uniqueness. There
    is no ordinal in it any more: the lineage's escalation *sequence* is the
    ledger's own ordering (``state_version``), which is dense, per-lineage
    and assigned under the lineage's lock, so it is a total order even when
    two writers escalate at the same moment. The router's read-then-count
    ordinal could not be that: two writers that read the lineage together
    took the same number.
    """
    assert discriminator is not None, "the escalation event carries no discriminator"
    parts = discriminator.split(":")
    assert len(parts) == 2 and parts[0] == "escalation", (
        f"unexpected discriminator: {discriminator!r}"
    )
    return parts[1]


async def _escalations(ledger: LedgerStore, correlation_id: uuid.UUID) -> list[Any]:
    events = await ledger.read_correlation(correlation_id)
    return [e for e in events if e.event_type is EventType.MODEL_ESCALATED]


def _versions(escalations: list[Any]) -> list[int]:
    """The ledger's own numbering of a lineage's escalations.

    ``state_version`` is dense and per-correlation, and the store assigns it
    under the lineage's lock, so the versions of a lineage's escalations
    never tie: the lineage's escalation *sequence* is the ledger's own
    ordering, and it is a **total order** even when two writers escalate at
    the same moment. The router's old read-then-count ordinal could not
    promise that — two writers that read the lineage together took the same
    number, so the label tied.

    What is asserted about it is the *absence of a tie*, not the numbers
    themselves: the numbers are the store's business, and a test that
    asserted them would be asserting the ledger's implementation.
    """
    return [event.state_version for event in escalations]


class _CountingLedger(LedgerStore):
    """The real ledger, counting the lineage reads asked of it.

    A subclass rather than a stand-in, so it *is* a ``LedgerStore``: the
    router's parameter is typed as one, and a duck-typed proxy would be
    testing the proxy as much as the router.
    """

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.lineage_reads = 0

    async def read_correlation(
        self, correlation_id: CorrelationId | uuid.UUID, *, from_version: int = 0
    ) -> list[LedgerEvent]:
        self.lineage_reads += 1
        return await super().read_correlation(correlation_id, from_version=from_version)



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
    # The discriminator is the event's own id, so "the discriminator is
    # unique" is exactly as strong as "the event id is unique" — and a
    # second escalation in the same lineage is a second event, not a
    # duplicate the ledger suppresses.
    assert _nonce(event.discriminator) == event.event_id.hex
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
    """The discriminator keeps the idempotency keys apart.

    The idempotency key is derived from ``(correlation, event_type, attempt,
    discriminator)``. Without a discriminator of its own the second
    escalation in one lineage would be suppressed as a duplicate and the
    audit trail would under-count.
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
    # Two distinct discriminators, not one and a copy of it.
    assert len({e.discriminator for e in escalations}) == 2
    # And the ledger's own order of them is a total order — no tie — so the
    # lineage's escalation sequence is the ledger's, not a number the router
    # read and counted (see :func:`_versions`).
    assert len(set(_versions(escalations))) == 2


async def test_the_escalation_path_reads_no_lineage(db: Database) -> None:
    """The audit write does not read the lineage it is writing to.

    The discriminator used to carry the lineage's escalation ordinal, and
    the router computed that ordinal by reading the **whole lineage** — every
    row of it, parsed, to count a number. Three things were wrong with that,
    and all three are the same thing: the ordinal was a denormalised copy of
    a fact the ledger already holds exactly.

    * It was **expensive**: O(lineage) rows to count a number, on the path
      that writes the audit record.
    * It was a **new failure point**: a store whose read failed aborted the
      escalation *before* the frontier call, so a *label* could cost a
      crossing of the privacy boundary.
    * It was **racy**: two writers that read the lineage at the same moment
      took the same ordinal, so the "per-correlation ordinal" was a sequence
      only in the sequential case.

    The discriminator is the event's own id now, so there is nothing to read
    before the write — and the lineage's sequence is the ledger's own
    ordering, which is deterministic where the read-then-count ordinal was
    not.
    """
    ledger = _CountingLedger(db)
    router = EscalatingModelRouter(
        EchoProvider(fail_times=1),
        EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER),
        settings=ModelSettings(),
        ledger_store=ledger,
    )

    await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=uuid.uuid4(),
    )

    assert ledger.lineage_reads == 0


async def test_a_restarted_router_does_not_suppress_the_second_audit_record(
    ledger: LedgerStore,
) -> None:
    """A restarted router must not re-use the key its predecessor spent.

    The discriminator used to be a counter on the router *instance*, which
    made it a counter on the *process*. A restarted router began again at
    ``escalation:1``, re-used the idempotency key its predecessor had
    already spent, and the ledger **silently suppressed** the second audit
    record: the trail said one escalation where there had been two, and
    nothing anywhere said so.

    The discriminator is the event's own id now, so it is unique across
    processes and restarts by construction — there is no state for a
    restart to lose, and no lineage to read to find out where the
    predecessor got to.
    """
    correlation_id = uuid.uuid4()

    before_restart = _router(ledger)
    await before_restart.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    # A *new* router over the same ledger: the shape a restart has.
    after_restart = _router(ledger)
    await after_restart.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    escalations = await _escalations(ledger, correlation_id)
    assert len(escalations) == 2
    # The second router's escalation is a second *event*, with its own id —
    # the second crossing of that lineage, whichever process wrote it.
    assert len({e.discriminator for e in escalations}) == 2
    # The ledger's order of the two is a total order: no tie.
    assert len(set(_versions(escalations))) == 2
    # Both crossings are recorded, and both say the same thing: the same
    # trigger, the same lineage, the same providers.
    assert {e.payload["trigger"] for e in escalations} == {"local_provider_unavailable"}
    assert {e.payload["to_provider"] for e in escalations} == {"frontier-echo"}


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
