"""Durable message-queue and distributed-lock contracts.

SPEC DEVIATION (docs/adr/0005): RFC §6 specifies eleven Redis Streams as the
dispatch fabric. Redis is absent from the target machine, and the RFC's own
§17.4 states that Redis must never be the source of truth. The default backend
is therefore SQLite, which is durable *by construction*: a message leaves the
queue only when its consumer commits an ack, so a crash mid-flight redelivers
rather than loses. A Redis backend implements this same interface for
multi-process deployments, where it is a transport over a ledger that remains
authoritative.

Delivery semantics are **at-least-once**, deliberately. Exactly-once delivery
is unimplementable across a process boundary; the runtime instead makes
*processing* effectively-once through the ledger's idempotency key
(:func:`paa.ledger.events.compute_idempotency_key`). Consumers must therefore
tolerate redelivery — that tolerance is what makes crash recovery safe.
"""

from __future__ import annotations

import abc
import enum
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paa.core.errors import StorageError

__all__ = [
    "CONTROL_PLANE_STREAMS",
    "DEFAULT_PRIORITY",
    "DistributedLock",
    "LockUnavailableError",
    "MessageQueue",
    "QueueMessage",
    "StreamName",
]

#: Schema default (``queue_messages.priority``). Lower dispatches first, so a
#: mid-range default leaves room to express both "urgent" and "whenever".
DEFAULT_PRIORITY: int = 100


class StreamName(enum.StrEnum):
    """The eleven dispatch channels of RFC §6.

    Values are the wire keys: the Redis backend uses them as stream key
    suffixes, the SQLite backend stores them in ``queue_messages.stream``. They
    are stable identifiers — renaming one strands every message already
    enqueued under the old name.
    """

    RAW_TELEMETRY = "raw:telemetry"
    ORCHESTRATOR_CORE = "orchestrator:core"
    PLANNER_STRAT = "planner:strat"
    WORKERS_ALLOC = "workers:alloc"
    MEMORY_CURATION = "memory:curation"
    SANDBOX_RUN = "sandbox:run"
    CRITIC_AUDIT = "critic:audit"
    WATCHDOG_MONITOR = "watchdog:monitor"
    HEARTBEAT_PING = "heartbeat:ping"
    RETRY_FAILED = "retry:failed"
    DEAD_LETTER_POISON = "dead_letter:poison"


#: Streams that must keep flowing even when the runtime is shedding load.
#:
#: Shedding the control plane would deadlock recovery: the orchestrator could
#: not drain the backlog it is shedding for, the watchdog could not observe
#: liveness, and poison messages could not be parked. See
#: :meth:`paa.storage.queue.backpressure.BackpressureController.should_accept`.
CONTROL_PLANE_STREAMS: frozenset[StreamName] = frozenset(
    {
        StreamName.ORCHESTRATOR_CORE,
        StreamName.HEARTBEAT_PING,
        StreamName.DEAD_LETTER_POISON,
    }
)


class LockUnavailableError(StorageError):
    """A distributed lock is held by someone else and could not be taken."""

    def __init__(self, key: str, holder: str, *, substrate: str = "queue") -> None:
        super().__init__(
            "distributed lock is held by another holder",
            substrate=substrate,
            lock_key=key,
            requested_by=holder,
        )
        self.key = key
        self.holder = holder


class QueueMessage(BaseModel):
    """One unit of dispatchable work.

    Frozen: a claimed message is a *snapshot* of queue state at claim time. If
    consumers could mutate it they would drift from the row the backend will
    act on at ack/nack, and the divergence would only surface as a lost or
    double-processed message under load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """Backend-assigned handle, opaque to callers and unique within a backend.

    A ``str`` rather than a UUID because the identifier must be whatever the
    backend can ack: SQLite uses a UUID, but Redis Streams assign
    ``<millis>-<seq>`` entry ids that no UUID can represent. Callers pass this
    value back to :meth:`MessageQueue.ack` / :meth:`MessageQueue.nack` verbatim.
    """

    stream: StreamName
    correlation_id: uuid.UUID | None = None
    """Lineage key, threaded through to the ledger so a queued message can be
    traced to the task that produced it."""

    session_id: uuid.UUID | None = None

    priority: int = DEFAULT_PRIORITY
    """Lower dispatches first. Ties break FIFO on ``enqueued_at``."""

    payload: dict[str, Any] = Field(default_factory=dict)

    attempts: int = Field(default=0, ge=0)
    """Failed deliveries so far — bumped by nack and by visibility-timeout
    reclaim, since a consumer that died holding a message failed to process it
    just as surely as one that reported an error."""

    max_attempts: int = Field(default=3, ge=1)
    enqueued_at: datetime
    visible_after: datetime
    """Earliest dispatch time. Drives both delayed delivery and, once claimed,
    the visibility timeout after which the message is redelivered."""

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        """Naive timestamps break ordering across DST and restarts."""
        if self.enqueued_at.tzinfo is None or self.visible_after.tzinfo is None:
            raise ValueError("queue timestamps must be timezone-aware")
        return self

    @property
    def is_exhausted(self) -> bool:
        """Whether a further failure would dead-letter this message."""
        return self.attempts >= self.max_attempts

    @property
    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.attempts)

    def __repr__(self) -> str:
        return (
            f"QueueMessage({self.id} {self.stream.value} p{self.priority} "
            f"{self.attempts}/{self.max_attempts})"
        )


class MessageQueue(abc.ABC):
    """Durable at-least-once queue over the RFC §6 streams.

    The claim/ack protocol is the whole point of the abstraction: a consumer
    *claims* a message (making it invisible to peers for a bounded window) and
    only afterwards *acks* it (removing it). Any message whose window elapses
    without an ack is redelivered. That is what makes a killed worker's task
    recoverable without a separate bookkeeping layer.

    Implementations never own the substrate they are handed — closing a queue
    must not close a :class:`~paa.storage.relational.database.Database` that
    other subsystems are still using.
    """

    @abc.abstractmethod
    async def enqueue(
        self,
        stream: StreamName,
        payload: dict[str, Any],
        *,
        priority: int = DEFAULT_PRIORITY,
        correlation_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> QueueMessage:
        """Publish a message and return its persisted form.

        ``delay_seconds`` postpones visibility — the message exists and counts
        toward depth, but no consumer can claim it until its time arrives.
        ``max_attempts`` defaults to ``QueueSettings.max_delivery_attempts``.
        """

    @abc.abstractmethod
    async def claim(
        self,
        stream: StreamName,
        consumer: str,
        *,
        limit: int = 1,
        visibility_timeout: float | None = None,
    ) -> list[QueueMessage]:
        """Atomically take up to ``limit`` ready messages for ``consumer``.

        Atomicity is the load-bearing property: two consumers racing on the
        same stream must never receive the same message. Dispatch order is
        priority ascending, then ``enqueued_at`` ascending (FIFO within a
        priority band).
        """

    @abc.abstractmethod
    async def ack(self, message_id: str) -> bool:
        """Mark a claimed message done. Returns False if it was already gone."""

    @abc.abstractmethod
    async def nack(
        self,
        message_id: str,
        error: str | BaseException,
        *,
        retry_delay_seconds: float = 0.0,
    ) -> QueueMessage | None:
        """Report a failed delivery.

        Increments ``attempts``. Once ``attempts >= max_attempts`` the message
        is dead-lettered instead of retried — an unbounded retry loop on a
        poison message is how a queue takes a whole runtime down with it.
        """

    @abc.abstractmethod
    async def depth(self, stream: StreamName) -> int:
        """Outstanding work: ready plus claimed-but-unacked.

        In-flight messages count because they are still owed. Excluding them
        would let backpressure read "empty" while every consumer is saturated.
        """

    @abc.abstractmethod
    async def reclaim_expired(self) -> int:
        """Return timed-out claims to the ready pool. Returns how many moved.

        This is the crash-recovery path: a worker that dies holding a message
        never acks, its visibility window lapses, and the message becomes
        claimable again.
        """

    @abc.abstractmethod
    async def dead_letters(self, stream: StreamName, limit: int = 100) -> list[QueueMessage]:
        """Messages that exhausted their attempts, newest first."""

    @abc.abstractmethod
    async def purge(self, stream: StreamName) -> int:
        """Delete every message on a stream. Returns the count removed."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources this queue itself opened. Idempotent."""


class DistributedLock(abc.ABC):
    """Mutual exclusion with a TTL, for the RFC §1.4 ``lock:entity:*`` keys.

    Two invariants make this safe to build on:

    **Ownership-checked release.** :meth:`release` must verify the holder
    before deleting. Releasing a lock you no longer own is the classic
    distributed-lock bug: holder A stalls, its lease expires, holder B acquires,
    A wakes and releases *B's* lock, and now two processes believe they hold it.

    **Self-healing expiry.** Every lock carries a TTL checked on acquire, so a
    holder that dies without releasing cannot wedge the key forever. Long
    critical sections must call :meth:`refresh` rather than take a long TTL.
    """

    @abc.abstractmethod
    async def acquire(self, key: str, holder: str, ttl: float) -> bool:
        """Take the lock. Returns False if a live holder already has it."""

    @abc.abstractmethod
    async def release(self, key: str, holder: str) -> bool:
        """Release the lock, but only if ``holder`` actually owns it."""

    @abc.abstractmethod
    async def refresh(self, key: str, holder: str, ttl: float) -> bool:
        """Extend a lock you hold. Returns False if it lapsed or moved on."""

    @asynccontextmanager
    async def hold(self, key: str, holder: str, ttl: float) -> AsyncIterator[None]:
        """Scope a lock to a block, releasing even when the body raises.

        The ``finally`` is not a nicety: an exception inside a critical section
        is precisely when the lock must come back, and precisely when a manual
        release is forgotten.
        """
        if not await self.acquire(key, holder, ttl):
            raise LockUnavailableError(key, holder)
        try:
            yield
        finally:
            await self.release(key, holder)
