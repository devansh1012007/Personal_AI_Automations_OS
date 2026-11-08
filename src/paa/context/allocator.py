"""Ledger-wired attention allocator (PLAN:integration-fix:P007).

ADR-0036 consumer around the ADR-0012 `attention_allocation_score` in
:mod:`paa.context.metrics`: claim ``workers:alloc``, score each item, emit a
ledger event for every decided (top-N) item, notify the controller about the
top ``max_results`` (5 by default), and ack only after the event persists.

The loop is a claim-ack consumer, gated off and operator-fed (below), of a
stream that has never carried traffic, so its semantics are defensive:

* ``allocator.enabled=False`` gates the consumer: the queue is untouched
  (nothing is claimed, nothing becomes invisible) and the gatherer ranks
  exactly as before. The default comes from :class:`AllocatorSettings`.
* An empty queue is a no-op plus an ``allocator.underfed`` log line. The
  gatherer is synchronous and pure (`compile` in
  :mod:`paa.context.gatherer`), so it can never block on this loop; when
  the allocator has nothing, ranking simply stays gatherer-local.
* Decisions are ledger events, not side effects: the allocator (like
  :meth:`ChiefOrchestrator._emit`) builds a :class:`LedgerEvent` and
  persists it via the store **before** the notify enqueue and the ack —
  ledger-before-side-effect. `ATTENTION_ALLOCATED` maps to
  ``_NO_PHASE_CHANGE`` in :mod:`paa.ledger.replay` (never `_PHASE_MAP`):
  an allocation decision is information about a task, not a move of its
  lifecycle. The audit record is the event payload itself.
* Idempotency uses the full ADR-0008 key: ``correlation_id`` is the queued
  message's lineage (else derived deterministically from the item for
  messages without one), ``attempt`` is always 0 (re-claims dedupe by
  discriminator, never by bumping), ``discriminator`` is the ``item_id``.
  The store suppresses duplicates by that key and returns the existing
  event; comparing ``event_id`` tells a fresh append from a suppressed
  duplicate, which is how a visibility-lapse redelivery skips the notify
  but still acks. Notify is therefore at-most-once per decision; the
  decision is exactly-once. A crash between the emit and the notify loses
  the notification but never the decision — the gatherer fallback covers it.
* Payloads are opaque producer dicts and are validated before scoring:
  structural errors (missing identity, wrong types, booleans) and numeric
  errors (non-finite, outside the score's guarded ranges, ``F <= 0``) both
  nack the message. Backends dead-letter once ``attempts >= max_attempts``,
  so poison terminates instead of looping. The score itself is only ever
  evaluated with the ADR-0012 guards — never raw ``P*R``.

Producer decision (PLAN:integration-fix:P007b; ADR-0036 is silent on the
producer): **operator-fed**. No code in the tree enqueues to
``workers:alloc`` (the only enqueue site is the ``paa.storage.queue``
docstring example) — inventing a poll-loop producer that fabricates work
items would be worse than honest: the queue is fed by operator ingress
(CLI/signal): an external enqueue of the payload dict below, and until
one exists the daemon tick below is an ``underfed`` no-op (plus a log
line), never "live". A future ingress producer must enqueue that shape;
nothing here fabricates the work items it scores.

Producer payload contract (one claimed message = one dict):
``item_id: str | int`` (required identity; discriminator), optionally
``priority`` (task-request priority -> K, default 0.5; ``criticality``
overrides), ``density`` (-> I, else the relevance*confidence projection),
``planning_cost`` (-> F, default 1.0), ``pollution_ratio`` (-> P, default
0.0), ``risk``/``hallucination_risk`` (-> R, default 0.0). The allocator
sits above :mod:`paa.context.metrics` (which stays pure) and never inside
it; it imports no heavy substrate at module scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from paa.config import AllocatorSettings, get_settings
from paa.context.metrics import attention_allocation_score
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.storage.queue.base import MessageQueue, QueueMessage, StreamName

__all__ = ["AllocationDecision", "AllocationResult", "AttentionAllocator", "allocate_attention"]

log = structlog.get_logger(__name__)

#: Deterministic namespace for lineages of queue messages that carry no
#: correlation of their own: the same item always maps to the same lineage,
#: so a redelivery dedupes instead of forking a second one.
_ALLOCATOR_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_URL, "paa:allocator")

#: ADR-0036 derivation defaults for inputs with no producer:
#: K = 0.5 (task names no priority), F = 1.0 (unit effort, no plan-cost
#: estimate), R = 0.0 (no risk signal). The ``(1+P)(1+R)`` divisor guards
#: P/R = 0, so a missing signal never divides by zero and never inflates.
_K_DEFAULT = 0.5
_F_DEFAULT = 1.0
_P_DEFAULT = 0.0
_R_DEFAULT = 0.0


class _LedgerAppender(Protocol):
    """The single store capability the allocator needs: append one event.

    Structural (not the concrete :class:`~paa.ledger.store.LedgerStore`)
    so this module keeps the orchestrator's no-heavy-module-scope-import
    posture; dedupe by full key is the store's contract, not the
    allocator's to re-implement.
    """

    async def append(self, event: LedgerEvent) -> LedgerEvent: ...


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    """One scored item the controller was notified about."""

    item_id: str
    score: float
    message_id: str
    correlation_id: uuid.UUID
    inputs: dict[str, float]
    """The derived K/I/F/P/R inputs, exactly as recorded in the event."""


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """What one allocator run did. Counts only; decisions live in the ledger."""

    outcome: str
    """``"disabled"`` | ``"underfed"`` | ``"allocated"``."""

    claimed: int = 0
    scored: int = 0
    poisoned: int = 0
    selected: tuple[AllocationDecision, ...] = ()
    emitted: int = 0
    notified: int = 0
    acked: int = 0
    nacked: int = 0


@dataclass(frozen=True, slots=True)
class _ScoredItem:
    message: QueueMessage
    item_id: str
    correlation_id: uuid.UUID
    inputs: dict[str, float]
    score: float


def _number(payload: dict[str, Any], key: str) -> float | None:
    """Read an optional numeric field; wrong types are poison, not absence.

    ``None`` (absent or null) means "no producer supplied it" and the
    derivation default applies. A boolean is rejected explicitly (it
    subclasses ``int``) and anything else non-numeric is poison: a hostile
    or broken producer must not silently coerce to a mid-range score. The
    score's own guards (finite, [0,1] / >0) are the range check.
    """
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    return float(value)


def _item_id(payload: dict[str, Any]) -> str:
    """The required identity; also the ADR-0008 discriminator."""
    raw = payload.get("item_id")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("item_id must be a non-empty string or integer")
    if isinstance(raw, str) and not raw.strip():
        raise ValueError("item_id must be a non-empty string or integer")
    return str(raw)


def _correlation_for(message: QueueMessage, item_id: str) -> uuid.UUID:
    """This item's lineage: the message's, else a deterministic derivation."""
    if message.correlation_id is not None:
        return message.correlation_id
    raw = message.payload.get("correlation_id")
    if isinstance(raw, str) and raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return uuid.uuid5(_ALLOCATOR_NAMESPACE, item_id)


def _extract(message: QueueMessage) -> tuple[str, uuid.UUID, dict[str, float], float]:
    """Validate an opaque payload and score it (ADR-0036 derivations).

    Raises ``ValueError`` on poison (structural or numeric); the caller
    nacks. The score is evaluated exactly once, through the guarded
    formula — no raw ``P·R`` evaluation exists in this module.
    """
    payload = message.payload
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    item_id = _item_id(payload)

    k = _number(payload, "criticality")
    if k is None:
        k = _number(payload, "priority")
    if k is None:
        k = _K_DEFAULT

    density = _number(payload, "density")
    if density is not None:
        i_val = density
    else:
        relevance = _number(payload, "relevance")
        confidence = _number(payload, "confidence")
        if relevance is not None and confidence is not None:
            i_val = relevance * confidence
        elif relevance is not None:
            i_val = relevance
        elif confidence is not None:
            i_val = confidence
        else:
            i_val = 0.0

    f_val = _number(payload, "planning_cost")
    if f_val is None:
        f_val = _F_DEFAULT
    p_val = _number(payload, "pollution_ratio")
    if p_val is None:
        p_val = _P_DEFAULT
    r_val = _number(payload, "hallucination_risk")
    if r_val is None:
        r_val = _number(payload, "risk")
    if r_val is None:
        r_val = _R_DEFAULT

    score = attention_allocation_score(k, i_val, f_val, p_val, r_val)
    inputs = {"K": k, "I": i_val, "F": f_val, "P": p_val, "R": r_val}
    return item_id, _correlation_for(message, item_id), inputs, score


class AttentionAllocator:
    """Claim-ack loop over ``workers:alloc``.

    Constructed with the queue and a ledger appender; ``settings`` defaults
    to the process `AllocatorSettings` (`enabled=False`), so an unconfigured
    deployment never consumes. The gatherer is never wired to this class.
    """

    def __init__(
        self,
        queue: MessageQueue | None,
        store: _LedgerAppender,
        settings: AllocatorSettings | None = None,
    ) -> None:
        self._queue = queue
        self._store = store
        self._settings = settings if settings is not None else get_settings().allocator

    async def allocate_attention(self, *, limit: int = 25) -> AllocationResult:
        """Run one allocation pass. Never raises for poison/empty or a claim
        failure (a down queue degrades to ``underfed``); see module docstring.
        """
        settings = self._settings
        if not settings.enabled:
            log.info("allocator.disabled")
            return AllocationResult(outcome="disabled")
        if self._queue is None:
            log.info("allocator.underfed", reason="no_queue")
            return AllocationResult(outcome="underfed")
        queue = self._queue

        # REV-W4 R-W4-007: a queue backend that is down must degrade to an
        # underfed no-op, never raise out of the loop — the house posture is
        # degrade-and-log, and an outage here must not take the pass down.
        try:
            messages = await queue.claim(StreamName.WORKERS_ALLOC, "allocator", limit=limit)
        except Exception as exc:
            log.warning("allocator.claim_failed", error=str(exc))
            return AllocationResult(outcome="underfed", claimed=0)
        if not messages:
            log.info("allocator.underfed", reason="empty")
            return AllocationResult(outcome="underfed", claimed=0)

        scored: list[_ScoredItem] = []
        poisoned = 0
        nacked = 0
        for message in messages:
            try:
                item_id, correlation_id, inputs, score = _extract(message)
            except ValueError as exc:
                poisoned += 1
                log.warning(
                    "allocator.poison", message_id=message.id, error=str(exc)
                )
                await queue.nack(message.id, f"invalid allocation payload: {exc}")
                nacked += 1
                continue
            scored.append(
                _ScoredItem(
                    message=message,
                    item_id=item_id,
                    correlation_id=correlation_id,
                    inputs=inputs,
                    score=score,
                )
            )

        # Deterministic total order: score descending, item_id ascending —
        # mirrors the gatherer's `_rank_key` id tie-break, so equal scores
        # never depend on claim order, dict order, or process state.
        scored.sort(key=lambda item: (-item.score, item.item_id))
        top = scored[: settings.max_results]
        top_ids = tuple(item.item_id for item in top)

        selected: list[AllocationDecision] = []
        emitted = notified = acked = 0
        for item in top:
            message = item.message
            event = LedgerEvent(
                correlation_id=item.correlation_id,
                session_id=message.session_id,
                event_type=EventType.ATTENTION_ALLOCATED,
                payload={
                    "item_id": item.item_id,
                    "score": item.score,
                    "inputs": item.inputs,
                    "message_id": message.id,
                    "top_n": list(top_ids),
                },
                attempt=0,
                discriminator=item.item_id,
            )
            try:
                persisted = await self._store.append(event)
            except Exception as exc:  # failure path: preserve the message, never ack
                log.error(
                    "allocator.emit_failed", message_id=message.id, error=str(exc)
                )
                continue
            if persisted.event_id == event.event_id:
                emitted += 1
                try:
                    await queue.enqueue(
                        StreamName.ORCHESTRATOR_CORE,
                        {
                            "kind": "attention_allocation",
                            "correlation_id": str(item.correlation_id),
                            "item_id": item.item_id,
                            "score": item.score,
                            "inputs": item.inputs,
                            "event_id": str(persisted.event_id),
                            "idempotency_key": persisted.idempotency_key,
                        },
                        correlation_id=item.correlation_id,
                        session_id=message.session_id,
                    )
                except Exception as exc:  # decision stands; notify is lost (at-most-once)
                    # The comment used to say "notify waits for reclaim".
                    # That was false: on redelivery the store returns the
                    # EXISTING event (a different event_id than the fresh
                    # uuid4), so the duplicate branch below skips notify
                    # and acks — the notification is permanently lost.
                    # That is the accepted cost the module docstring
                    # records (at-most-once notify; the gatherer fallback
                    # covers ranking), and the unacked message costs one
                    # reclaim cycle on the way to it. An ack-on-notify-
                    # failure change would avoid that churn but is a
                    # separate ack-semantics decision — not made here;
                    # the brief explicitly accepts the reclaim cycle.
                    log.error(
                        "allocator.notify_failed",
                        message_id=message.id,
                        error=str(exc),
                    )
                    continue
                notified += 1
            # Fresh or duplicate: the event exists (ledger-before-ack), so ack.
            await queue.ack(message.id)
            acked += 1
            if persisted.event_id == event.event_id:
                # Freshly decided and notified this run (duplicates were
                # notified by the run that first emitted them).
                selected.append(
                    AllocationDecision(
                        item_id=item.item_id,
                        score=item.score,
                        message_id=message.id,
                        correlation_id=item.correlation_id,
                        inputs=item.inputs,
                    )
                )

        below = 0
        for item in scored[len(top) :]:
            below += 1
            await queue.ack(item.message.id)
            acked += 1

        log.info(
            "allocator.allocated",
            claimed=len(messages),
            scored=len(scored),
            poisoned=poisoned,
            selected=len(selected),
            below_cut=below,
            top_score=top[0].score if top else None,
        )
        return AllocationResult(
            outcome="allocated",
            claimed=len(messages),
            scored=len(scored),
            poisoned=poisoned,
            selected=tuple(selected),
            emitted=emitted,
            notified=notified,
            acked=acked,
            nacked=nacked,
        )


async def allocate_attention(
    queue: MessageQueue | None,
    store: _LedgerAppender,
    *,
    settings: AllocatorSettings | None = None,
    limit: int = 25,
) -> AllocationResult:
    """One allocation pass: build an :class:`AttentionAllocator` and run it."""
    return await AttentionAllocator(queue, store, settings).allocate_attention(limit=limit)
