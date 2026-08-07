"""Ledger event envelope, idempotency, and the tamper-evident hash chain.

The ledger is the runtime's only source of truth. Redis, in-process caches and
the filesystem are all derived views that may be rebuilt from these rows.

Three properties are enforced here:

1. **Idempotency** — an at-least-once transport may deliver the same logical
   event twice; the second write is suppressed rather than duplicated.
2. **Ordering** — every event carries a per-correlation monotonic
   ``state_version`` so replay is deterministic even if ``sequence_id`` gaps.
3. **Tamper evidence** — each event commits to its predecessor's digest,
   forming a hash chain that makes silent history edits detectable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from paa.core.types import ComplexityModality, CorrelationId, EventType, SessionId

__all__ = [
    "GENESIS_HASH",
    "LedgerEvent",
    "canonical_json",
    "compute_event_digest",
    "compute_idempotency_key",
]

#: The ``prev_hash`` of the first event in any correlation's chain.
GENESIS_HASH: str = "0" * 64


def canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing.

    Sorted keys, no insignificant whitespace, and non-ASCII preserved. Two
    structurally equal payloads must always produce byte-identical output,
    otherwise the hash chain would report false tampering.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_fallback,
    )


def _json_fallback(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    raise TypeError(f"{type(obj).__name__} is not JSON-serialisable in a ledger payload")


def compute_idempotency_key(
    correlation_id: CorrelationId | uuid.UUID,
    event_type: EventType,
    *,
    attempt: int = 0,
    discriminator: str | None = None,
) -> str:
    """Derive the deduplication key for one logical event.

    SPEC DEVIATION (docs/adr/0008). The RFC specifies
    ``SHA-256(correlation_id + event_type + state_version)`` under a global
    UNIQUE constraint. That makes a legitimate retry impossible: a task whose
    first ``EXECUTION_STARTED`` failed can never emit a second one, because the
    key collides and the append is rejected. Recovery would deadlock on exactly
    the tasks it exists to rescue.

    Two inputs are added to fix it:

    ``attempt``
        Bumped by the caller on each genuine retry, so retry *n* is a distinct
        event while a *redelivery* of attempt *n* is still suppressed.

    ``discriminator``
        Distinguishes same-typed events that are legitimately concurrent — e.g.
        ``EXECUTION_STARTED`` for step 3 vs step 4 of one plan. Typically the
        step index or a natural key.

    The payload is deliberately *excluded*: payloads routinely carry timestamps
    and durations, so hashing them would make every redelivery look novel and
    defeat deduplication entirely.
    """
    if attempt < 0:
        raise ValueError(f"attempt must be non-negative, got {attempt}")
    material = f"{correlation_id}|{event_type.value}|{attempt}|{discriminator or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def compute_event_digest(
    *,
    prev_hash: str,
    correlation_id: CorrelationId | uuid.UUID,
    event_type: EventType,
    state_version: int,
    payload: dict[str, Any],
    recorded_at: datetime,
) -> str:
    """Digest binding an event to its predecessor.

    Includes ``prev_hash``, so altering any historical row invalidates every
    digest after it. Verified by :func:`paa.ledger.store.LedgerStore.verify_chain`.
    """
    material = canonical_json(
        {
            "prev": prev_hash,
            "correlation_id": str(correlation_id),
            "event_type": event_type.value,
            "state_version": state_version,
            "payload": payload,
            "recorded_at": recorded_at.astimezone(UTC).isoformat(),
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LedgerEvent(BaseModel):
    """One immutable state transition.

    Instances are frozen. Fields assigned by the store at append time
    (``sequence_id``, ``state_version``, ``prev_hash``, ``event_hash``) are
    ``None`` on a freshly constructed event and populated on the persisted copy
    the store returns.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # -- assigned by the store inside the append transaction ---------------
    sequence_id: int | None = None
    """Global monotonic position. ``None`` until persisted."""

    state_version: int | None = None
    """Per-correlation monotonic counter, starting at 1."""

    prev_hash: str | None = None
    event_hash: str | None = None

    # -- caller-supplied ---------------------------------------------------
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID
    """Lineage key. One task from request through commit shares one value."""

    session_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    """The ``event_id`` that directly caused this one. Enables the DoD's
    "explain why it chose an action" by walking the causal chain backwards."""

    event_type: EventType
    execution_mode: ComplexityModality = ComplexityModality.STANDARD
    agent_role: str | None = None
    allocated_worker_image: str = "paa/base_worker:v4.1"

    attempt: int = Field(default=0, ge=0)
    discriminator: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        """Naive timestamps silently break ordering across DST and restarts."""
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        return self

    @field_serializer("recorded_at")
    def _serialize_ts(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @property
    def idempotency_key(self) -> str:
        """This event's deduplication key."""
        return compute_idempotency_key(
            self.correlation_id,
            self.event_type,
            attempt=self.attempt,
            discriminator=self.discriminator,
        )

    @property
    def is_terminal(self) -> bool:
        from paa.core.types import TERMINAL_EVENTS

        return self.event_type in TERMINAL_EVENTS

    def sealed(self, *, sequence_id: int, state_version: int, prev_hash: str) -> LedgerEvent:
        """Return the persisted form, with chain fields computed.

        Called by the store inside the append transaction once the sequence and
        version are known.
        """
        digest = compute_event_digest(
            prev_hash=prev_hash,
            correlation_id=self.correlation_id,
            event_type=self.event_type,
            state_version=state_version,
            payload=self.payload,
            recorded_at=self.recorded_at,
        )
        return self.model_copy(
            update={
                "sequence_id": sequence_id,
                "state_version": state_version,
                "prev_hash": prev_hash,
                "event_hash": digest,
            }
        )

    def verify_digest(self) -> bool:
        """Recompute this event's digest and compare it to the stored value."""
        if self.event_hash is None or self.prev_hash is None or self.state_version is None:
            return False
        expected = compute_event_digest(
            prev_hash=self.prev_hash,
            correlation_id=self.correlation_id,
            event_type=self.event_type,
            state_version=self.state_version,
            payload=self.payload,
            recorded_at=self.recorded_at,
        )
        return expected == self.event_hash

    @classmethod
    def create(
        cls,
        correlation_id: CorrelationId | uuid.UUID,
        event_type: EventType,
        *,
        session_id: SessionId | uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> LedgerEvent:
        """Ergonomic constructor for the common append path."""
        return cls(
            correlation_id=correlation_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )
