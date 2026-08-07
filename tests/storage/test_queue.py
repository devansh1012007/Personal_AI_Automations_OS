"""Durable queue, distributed lock and backpressure tests.

The load-bearing test in this file is ``test_concurrent_claims_deliver_each
_message_exactly_once``. Everything else verifies a behaviour you would notice
was broken; that one verifies a behaviour that breaks *only under concurrency*,
which is where a queue is both hardest to get right and most expensive to get
wrong.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest

from paa.config import QueueSettings, Settings
from paa.core.types import ComplexityModality
from paa.storage.queue import (
    BackpressureController,
    BackpressureState,
    SqliteDistributedLock,
    SqliteMessageQueue,
    StreamName,
    get_lock,
    get_queue,
)
from paa.storage.queue.base import CONTROL_PLANE_STREAMS, LockUnavailableError
from paa.storage.relational.database import Database

WORKERS = StreamName.WORKERS_ALLOC


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Database]:
    """A connected database on a throwaway file.

    Deliberately local rather than inherited from ``conftest``: several tests
    here reopen the same path to prove durability across a restart, so the
    fixture's file naming is part of what they assert on.
    """
    database = Database(tmp_path / "queue.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def settings() -> QueueSettings:
    return QueueSettings(
        backpressure_depth=10,
        shed_load_depth=50,
        max_delivery_attempts=3,
        visibility_timeout_seconds=300.0,
        entity_lock_ttl_seconds=60.0,
    )


@pytest.fixture
def queue(db: Database, settings: QueueSettings) -> SqliteMessageQueue:
    return SqliteMessageQueue(db, settings)


@pytest.fixture
def lock(db: Database, settings: QueueSettings) -> SqliteDistributedLock:
    return SqliteDistributedLock(db, settings)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


async def test_enqueue_claim_ack_round_trip(queue: SqliteMessageQueue) -> None:
    correlation_id = uuid.uuid4()
    enqueued = await queue.enqueue(
        WORKERS, {"task": "compile"}, correlation_id=correlation_id, priority=50
    )

    assert enqueued.stream is WORKERS
    assert enqueued.payload == {"task": "compile"}
    assert enqueued.attempts == 0
    assert await queue.depth(WORKERS) == 1

    claimed = await queue.claim(WORKERS, "worker-1")
    assert len(claimed) == 1
    assert claimed[0].id == enqueued.id
    assert claimed[0].correlation_id == correlation_id
    assert claimed[0].payload == {"task": "compile"}
    assert claimed[0].priority == 50

    # A claimed message is still outstanding work, so depth must not drop yet.
    assert await queue.depth(WORKERS) == 1

    # It is invisible to peers for the duration of the visibility window.
    assert await queue.claim(WORKERS, "worker-2") == []

    assert await queue.ack(claimed[0].id) is True
    assert await queue.depth(WORKERS) == 0
    assert await queue.claim(WORKERS, "worker-2") == []


async def test_ack_of_unknown_message_is_false(queue: SqliteMessageQueue) -> None:
    assert await queue.ack(str(uuid.uuid4())) is False


async def test_depth_is_per_stream(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"n": 1})
    await queue.enqueue(WORKERS, {"n": 2})
    await queue.enqueue(StreamName.CRITIC_AUDIT, {"n": 3})

    assert await queue.depth(WORKERS) == 2
    assert await queue.depth(StreamName.CRITIC_AUDIT) == 1
    assert await queue.depth(StreamName.PLANNER_STRAT) == 0


async def test_purge_removes_only_the_named_stream(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"n": 1})
    await queue.enqueue(StreamName.CRITIC_AUDIT, {"n": 2})

    assert await queue.purge(WORKERS) == 1
    assert await queue.depth(WORKERS) == 0
    assert await queue.depth(StreamName.CRITIC_AUDIT) == 1


# ---------------------------------------------------------------------------
# Concurrency — the critical property
# ---------------------------------------------------------------------------


async def test_concurrent_claims_deliver_each_message_exactly_once(
    queue: SqliteMessageQueue,
) -> None:
    """Racing consumers must partition the backlog, never duplicate it.

    Twenty consumers ask for five messages each against a backlog of fifty.
    Capacity (100) deliberately exceeds supply so every claim contends: if the
    select and the mark-claimed update were not one transaction, two consumers
    would read the same rows before either wrote, and the same message would be
    handed out twice.
    """
    total = 50
    enqueued = [await queue.enqueue(WORKERS, {"n": i}) for i in range(total)]

    batches = await asyncio.gather(
        *(queue.claim(WORKERS, f"worker-{i}", limit=5) for i in range(20))
    )

    claimed_ids = [message.id for batch in batches for message in batch]
    assert len(claimed_ids) == len(set(claimed_ids)), "a message was claimed more than once"
    assert set(claimed_ids) == {m.id for m in enqueued}, "some messages were never claimed"
    assert len(claimed_ids) == total

    # Payloads survive the race intact — no cross-message field bleed.
    payload_ns = sorted(m.payload["n"] for batch in batches for m in batch)
    assert payload_ns == list(range(total))


async def test_concurrent_claims_and_acks_drain_the_queue(queue: SqliteMessageQueue) -> None:
    """A full concurrent claim/ack cycle leaves nothing behind and loses nothing."""
    total = 30
    for i in range(total):
        await queue.enqueue(WORKERS, {"n": i})

    processed: list[int] = []

    async def worker(name: str) -> None:
        while True:
            messages = await queue.claim(WORKERS, name, limit=3)
            if not messages:
                return
            for message in messages:
                processed.append(message.payload["n"])
                await queue.ack(message.id)

    await asyncio.gather(*(worker(f"worker-{i}") for i in range(8)))

    assert sorted(processed) == list(range(total))
    assert await queue.depth(WORKERS) == 0


async def test_concurrent_acks_of_one_message_settle_once(queue: SqliteMessageQueue) -> None:
    message = await queue.enqueue(WORKERS, {"n": 1})
    await queue.claim(WORKERS, "worker-1")

    results = await asyncio.gather(*(queue.ack(message.id) for _ in range(5)))
    assert sum(results) == 1, "ack must be effectively-once, not idempotently-true"


# ---------------------------------------------------------------------------
# Retry and dead-lettering
# ---------------------------------------------------------------------------


async def test_nack_retries_then_dead_letters_at_max_attempts(
    queue: SqliteMessageQueue,
) -> None:
    message = await queue.enqueue(WORKERS, {"task": "poison"}, max_attempts=3)

    for expected_attempts in (1, 2):
        claimed = await queue.claim(WORKERS, "worker-1")
        assert len(claimed) == 1, f"expected redelivery for attempt {expected_attempts}"

        updated = await queue.nack(claimed[0].id, RuntimeError("boom"))
        assert updated is not None
        assert updated.attempts == expected_attempts
        assert await queue.depth(WORKERS) == 1
        assert await queue.dead_letters(WORKERS) == []

    # Third failure exhausts the budget: the message is parked, not retried.
    claimed = await queue.claim(WORKERS, "worker-1")
    assert len(claimed) == 1
    exhausted = await queue.nack(claimed[0].id, RuntimeError("boom"))
    assert exhausted is not None
    assert exhausted.attempts == 3
    assert exhausted.is_exhausted

    assert await queue.claim(WORKERS, "worker-1") == []
    assert await queue.depth(WORKERS) == 0

    dead = await queue.dead_letters(WORKERS)
    assert len(dead) == 1
    assert dead[0].id == message.id
    assert dead[0].payload == {"task": "poison"}
    assert dead[0].attempts == 3


async def test_nack_with_retry_delay_defers_redelivery(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"n": 1})
    claimed = await queue.claim(WORKERS, "worker-1")

    await queue.nack(claimed[0].id, "transient", retry_delay_seconds=60)
    assert await queue.claim(WORKERS, "worker-1") == [], "backoff window was not honoured"
    assert await queue.depth(WORKERS) == 1


async def test_nack_of_a_dead_message_is_a_no_op(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"n": 1}, max_attempts=1)
    claimed = await queue.claim(WORKERS, "worker-1")

    assert (await queue.nack(claimed[0].id, "fatal")) is not None
    assert await queue.nack(claimed[0].id, "fatal again") is None


async def test_nack_records_the_failure_reason(queue: SqliteMessageQueue, db: Database) -> None:
    await queue.enqueue(WORKERS, {"n": 1})
    claimed = await queue.claim(WORKERS, "worker-1")
    await queue.nack(claimed[0].id, ValueError("disk exploded"))

    stored = await db.fetch_value(
        "SELECT last_error FROM queue_messages WHERE id = ?", (claimed[0].id,)
    )
    assert "disk exploded" in stored


# ---------------------------------------------------------------------------
# Visibility timeout and crash recovery
# ---------------------------------------------------------------------------


async def test_expired_claim_is_redelivered_by_reclaim(queue: SqliteMessageQueue) -> None:
    """The crashed-worker path: claim, never ack, get it back."""
    message = await queue.enqueue(WORKERS, {"task": "orphan"})

    claimed = await queue.claim(WORKERS, "doomed-worker", visibility_timeout=0.05)
    assert len(claimed) == 1
    assert await queue.claim(WORKERS, "healthy-worker") == []

    await asyncio.sleep(0.1)  # let the visibility window lapse

    assert await queue.reclaim_expired() == 1

    redelivered = await queue.claim(WORKERS, "healthy-worker")
    assert len(redelivered) == 1
    assert redelivered[0].id == message.id
    # A timeout counts as a failed attempt, else a worker-killing message would
    # cycle forever without ever reaching the dead-letter ceiling.
    assert redelivered[0].attempts == 1


async def test_reclaim_ignores_claims_still_within_their_window(
    queue: SqliteMessageQueue,
) -> None:
    await queue.enqueue(WORKERS, {"n": 1})
    await queue.claim(WORKERS, "worker-1", visibility_timeout=300)

    assert await queue.reclaim_expired() == 0
    assert await queue.claim(WORKERS, "worker-2") == []


async def test_repeated_timeouts_eventually_dead_letter(queue: SqliteMessageQueue) -> None:
    """A message that kills every consumer must not cycle indefinitely."""
    await queue.enqueue(WORKERS, {"task": "worker-killer"}, max_attempts=2)

    await queue.claim(WORKERS, "worker-1", visibility_timeout=0.05)
    await asyncio.sleep(0.1)
    assert await queue.reclaim_expired() == 1

    await queue.claim(WORKERS, "worker-2", visibility_timeout=0.05)
    await asyncio.sleep(0.1)
    assert await queue.reclaim_expired() == 1

    assert await queue.claim(WORKERS, "worker-3") == []
    dead = await queue.dead_letters(WORKERS)
    assert len(dead) == 1
    assert dead[0].payload == {"task": "worker-killer"}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


async def test_dispatch_orders_by_priority_then_fifo(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"label": "normal-first"}, priority=100)
    await queue.enqueue(WORKERS, {"label": "normal-second"}, priority=100)
    await queue.enqueue(WORKERS, {"label": "urgent"}, priority=1)
    await queue.enqueue(WORKERS, {"label": "background"}, priority=900)

    claimed = await queue.claim(WORKERS, "worker-1", limit=4)

    assert [m.payload["label"] for m in claimed] == [
        "urgent",
        "normal-first",
        "normal-second",
        "background",
    ]


async def test_fifo_holds_within_a_priority_band(queue: SqliteMessageQueue) -> None:
    """Same-priority messages dispatch in insertion order.

    Enqueued in a tight loop on purpose: a coarse system clock can stamp
    several messages with an identical ``enqueued_at``, and ordering must still
    be insertion order rather than whatever the storage engine happens to return.
    """
    for i in range(20):
        await queue.enqueue(WORKERS, {"n": i})

    claimed = await queue.claim(WORKERS, "worker-1", limit=20)
    assert [m.payload["n"] for m in claimed] == list(range(20))


# ---------------------------------------------------------------------------
# Delayed delivery
# ---------------------------------------------------------------------------


async def test_delayed_message_is_invisible_until_due(queue: SqliteMessageQueue) -> None:
    delayed = await queue.enqueue(WORKERS, {"task": "later"}, delay_seconds=0.15)

    assert await queue.depth(WORKERS) == 1, "a delayed message is still owed work"
    assert await queue.claim(WORKERS, "worker-1") == []

    await asyncio.sleep(0.2)

    claimed = await queue.claim(WORKERS, "worker-1")
    assert len(claimed) == 1
    assert claimed[0].id == delayed.id


async def test_delayed_message_does_not_block_ready_ones(queue: SqliteMessageQueue) -> None:
    await queue.enqueue(WORKERS, {"label": "later"}, priority=1, delay_seconds=60)
    await queue.enqueue(WORKERS, {"label": "now"}, priority=500)

    claimed = await queue.claim(WORKERS, "worker-1", limit=5)
    assert [m.payload["label"] for m in claimed] == ["now"]


# ---------------------------------------------------------------------------
# Distributed lock
# ---------------------------------------------------------------------------


async def test_lock_excludes_a_second_holder(lock: SqliteDistributedLock) -> None:
    assert await lock.acquire("entity:42", "holder-a", 60) is True
    assert await lock.acquire("entity:42", "holder-b", 60) is False
    assert await lock.current_holder("entity:42") == "holder-a"


async def test_lock_reacquisition_by_the_same_holder_extends_it(
    lock: SqliteDistributedLock,
) -> None:
    assert await lock.acquire("entity:42", "holder-a", 60) is True
    assert await lock.acquire("entity:42", "holder-a", 60) is True


async def test_release_by_a_non_holder_is_refused(lock: SqliteDistributedLock) -> None:
    """The classic distributed-lock bug, guarded explicitly."""
    await lock.acquire("entity:42", "holder-a", 60)

    assert await lock.release("entity:42", "holder-b") is False
    assert await lock.current_holder("entity:42") == "holder-a"
    assert await lock.acquire("entity:42", "holder-b", 60) is False

    assert await lock.release("entity:42", "holder-a") is True
    assert await lock.acquire("entity:42", "holder-b", 60) is True


async def test_expired_lock_frees_itself_for_a_new_holder(lock: SqliteDistributedLock) -> None:
    """A crashed holder cannot run cleanup, so expiry must be checked on acquire."""
    assert await lock.acquire("entity:42", "crashed-holder", 0.05) is True

    await asyncio.sleep(0.1)

    assert await lock.current_holder("entity:42") is None
    assert await lock.acquire("entity:42", "new-holder", 60) is True
    assert await lock.current_holder("entity:42") == "new-holder"


async def test_refresh_extends_only_for_the_live_holder(lock: SqliteDistributedLock) -> None:
    await lock.acquire("entity:42", "holder-a", 0.05)

    assert await lock.refresh("entity:42", "holder-b", 60) is False

    await asyncio.sleep(0.1)
    assert await lock.refresh("entity:42", "holder-a", 60) is False, "a lapsed lease is not ours"


async def test_refresh_keeps_a_long_critical_section_alive(lock: SqliteDistributedLock) -> None:
    await lock.acquire("entity:42", "holder-a", 0.2)
    await asyncio.sleep(0.1)

    assert await lock.refresh("entity:42", "holder-a", 60) is True

    await asyncio.sleep(0.2)
    assert await lock.current_holder("entity:42") == "holder-a"


async def test_concurrent_acquire_yields_exactly_one_winner(lock: SqliteDistributedLock) -> None:
    results = await asyncio.gather(
        *(lock.acquire("entity:42", f"holder-{i}", 60) for i in range(25))
    )
    assert sum(results) == 1


async def test_hold_releases_on_exception(lock: SqliteDistributedLock) -> None:
    """An exception inside a critical section is exactly when release is forgotten."""
    with pytest.raises(RuntimeError, match="body failed"):
        async with lock.hold("entity:42", "holder-a", 60):
            assert await lock.current_holder("entity:42") == "holder-a"
            raise RuntimeError("body failed")

    assert await lock.current_holder("entity:42") is None
    assert await lock.acquire("entity:42", "holder-b", 60) is True


async def test_hold_refuses_when_the_lock_is_taken(lock: SqliteDistributedLock) -> None:
    await lock.acquire("entity:42", "holder-a", 60)

    with pytest.raises(LockUnavailableError):
        async with lock.hold("entity:42", "holder-b", 60):
            pytest.fail("should not have entered the critical section")

    # The refusal must not have disturbed the incumbent's lease.
    assert await lock.current_holder("entity:42") == "holder-a"


async def test_lock_rejects_a_non_positive_ttl(lock: SqliteDistributedLock) -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        await lock.acquire("entity:42", "holder-a", 0)


# ---------------------------------------------------------------------------
# Backpressure — RFC §6.2
# ---------------------------------------------------------------------------


def test_backpressure_transitions_at_the_thresholds(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)

    assert controller.assess(0) is BackpressureState.NORMAL
    assert controller.assess(9) is BackpressureState.NORMAL
    # Inclusive lower bound: the setting names the depth *at which* we degrade.
    assert controller.assess(10) is BackpressureState.DEGRADED
    assert controller.assess(49) is BackpressureState.DEGRADED
    assert controller.assess(50) is BackpressureState.SHEDDING
    assert controller.assess(5000) is BackpressureState.SHEDDING


def test_shedding_state_also_counts_as_degraded(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)

    assert controller.assess(10).is_degraded
    assert controller.assess(50).is_degraded, "shedding must not stop degrading modality"
    assert controller.assess(50).is_shedding
    assert not controller.assess(0).is_degraded


def test_modality_degradation_ladder(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)
    step = controller.degrade_modality

    assert step(ComplexityModality.MAX) is ComplexityModality.COMPLEX
    assert step(ComplexityModality.COMPLEX) is ComplexityModality.STANDARD
    assert step(ComplexityModality.STANDARD) is ComplexityModality.SIMPLE
    # SIMPLE already bypasses the LLM; there is nothing cheaper below it.
    assert step(ComplexityModality.SIMPLE) is ComplexityModality.SIMPLE


def test_degradation_covers_every_modality(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)
    for modality in ComplexityModality:
        assert controller.degrade_modality(modality) in ComplexityModality


def test_control_plane_streams_are_never_shed(settings: QueueSettings) -> None:
    """Shedding the control plane would deadlock recovery."""
    controller = BackpressureController(settings)

    for stream in CONTROL_PLANE_STREAMS:
        assert controller.should_accept(stream, BackpressureState.SHEDDING) is True

    assert controller.should_accept(StreamName.ORCHESTRATOR_CORE, BackpressureState.SHEDDING)
    assert controller.should_accept(StreamName.HEARTBEAT_PING, BackpressureState.SHEDDING)
    assert controller.should_accept(StreamName.DEAD_LETTER_POISON, BackpressureState.SHEDDING)


def test_non_essential_streams_are_shed_only_under_shedding(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)
    shed_able = [s for s in StreamName if s not in CONTROL_PLANE_STREAMS]
    assert shed_able, "the control plane must not be the whole stream set"

    for stream in shed_able:
        assert controller.should_accept(stream, BackpressureState.NORMAL) is True
        assert controller.should_accept(stream, BackpressureState.DEGRADED) is True
        assert controller.should_accept(stream, BackpressureState.SHEDDING) is False


def test_plan_combines_admission_and_degradation(settings: QueueSettings) -> None:
    controller = BackpressureController(settings)

    normal = controller.plan(StreamName.RAW_TELEMETRY, 0, ComplexityModality.MAX)
    assert normal.accepted and normal.modality is ComplexityModality.MAX

    degraded = controller.plan(StreamName.RAW_TELEMETRY, 10, ComplexityModality.MAX)
    assert degraded.accepted and degraded.modality is ComplexityModality.COMPLEX

    shed = controller.plan(StreamName.RAW_TELEMETRY, 50, ComplexityModality.MAX)
    assert not shed.accepted

    control = controller.plan(StreamName.ORCHESTRATOR_CORE, 50, ComplexityModality.COMPLEX)
    assert control.accepted and control.modality is ComplexityModality.STANDARD


async def test_backpressure_reads_real_queue_depth(
    queue: SqliteMessageQueue, settings: QueueSettings
) -> None:
    """The thresholds line up with what ``depth`` actually reports."""
    controller = BackpressureController(settings)

    assert controller.assess(await queue.depth(WORKERS)) is BackpressureState.NORMAL

    for i in range(settings.backpressure_depth):
        await queue.enqueue(WORKERS, {"n": i})
    assert controller.assess(await queue.depth(WORKERS)) is BackpressureState.DEGRADED

    for i in range(settings.shed_load_depth - settings.backpressure_depth):
        await queue.enqueue(WORKERS, {"n": 1000 + i})
    state = controller.assess(await queue.depth(WORKERS))
    assert state is BackpressureState.SHEDDING
    assert controller.should_accept(WORKERS, state) is False
    assert controller.should_accept(StreamName.HEARTBEAT_PING, state) is True


# ---------------------------------------------------------------------------
# Durability — the reason this backend exists
# ---------------------------------------------------------------------------


async def test_message_survives_a_database_restart(tmp_path, settings: QueueSettings) -> None:
    """Enqueue, lose the process, and still find the work waiting.

    This is the whole justification for ADR-0005: an in-memory broker would
    drop these messages, so a restart mid-flight would silently lose queued
    work with nothing to replay from.
    """
    path = tmp_path / "durable.db"
    correlation_id = uuid.uuid4()

    first = Database(path)
    await first.connect()
    queue = SqliteMessageQueue(first, settings)
    original = await queue.enqueue(
        WORKERS, {"task": "survive"}, correlation_id=correlation_id, priority=7
    )
    await queue.close()
    await first.close()

    second = Database(path)
    await second.connect()
    try:
        reopened = SqliteMessageQueue(second, settings)
        assert await reopened.depth(WORKERS) == 1

        claimed = await reopened.claim(WORKERS, "worker-after-restart")
        assert len(claimed) == 1
        assert claimed[0].id == original.id
        assert claimed[0].payload == {"task": "survive"}
        assert claimed[0].correlation_id == correlation_id
        assert claimed[0].priority == 7
        assert await reopened.ack(claimed[0].id) is True
    finally:
        await second.close()


async def test_in_flight_claim_survives_a_restart_and_is_reclaimable(
    tmp_path, settings: QueueSettings
) -> None:
    """A claim held by a process that dies is recovered, not stranded."""
    path = tmp_path / "inflight.db"

    first = Database(path)
    await first.connect()
    queue = SqliteMessageQueue(first, settings)
    await queue.enqueue(WORKERS, {"task": "in-flight"})
    await queue.claim(WORKERS, "doomed-worker", visibility_timeout=0.05)
    await first.close()  # process dies holding the claim

    await asyncio.sleep(0.1)

    second = Database(path)
    await second.connect()
    try:
        recovered = SqliteMessageQueue(second, settings)
        assert await recovered.reclaim_expired() == 1

        claimed = await recovered.claim(WORKERS, "replacement-worker")
        assert len(claimed) == 1
        assert claimed[0].payload == {"task": "in-flight"}
    finally:
        await second.close()


async def test_dead_letters_survive_a_restart(tmp_path, settings: QueueSettings) -> None:
    path = tmp_path / "dead.db"

    first = Database(path)
    await first.connect()
    queue = SqliteMessageQueue(first, settings)
    await queue.enqueue(WORKERS, {"task": "poison"}, max_attempts=1)
    claimed = await queue.claim(WORKERS, "worker-1")
    await queue.nack(claimed[0].id, "fatal")
    await first.close()

    second = Database(path)
    await second.connect()
    try:
        reopened = SqliteMessageQueue(second, settings)
        dead = await reopened.dead_letters(WORKERS)
        assert len(dead) == 1
        assert dead[0].payload == {"task": "poison"}
    finally:
        await second.close()


# ---------------------------------------------------------------------------
# Factories and stream vocabulary
# ---------------------------------------------------------------------------


def test_rfc_defines_exactly_eleven_streams() -> None:
    assert len(StreamName) == 11
    assert len({s.value for s in StreamName}) == 11


def test_factories_honour_the_configured_backend(db: Database, tmp_path) -> None:
    settings = Settings(home=tmp_path)
    assert settings.storage.backend_queue == "sqlite"

    assert isinstance(get_queue(settings, db), SqliteMessageQueue)
    assert isinstance(get_lock(settings, db), SqliteDistributedLock)


def test_sqlite_factories_require_a_database(tmp_path) -> None:
    from paa.core.errors import StorageError

    settings = Settings(home=tmp_path)
    with pytest.raises(StorageError, match="requires a connected Database"):
        get_queue(settings)
    with pytest.raises(StorageError, match="requires a connected Database"):
        get_lock(settings)


def test_redis_factories_build_the_redis_backend(tmp_path) -> None:
    """Backend selection must work even though Redis is not installed here.

    ``redis`` is imported lazily, so constructing the object is the furthest we
    can go without the extra — which is exactly the point of the lazy import.
    """
    pytest.importorskip("redis", reason="the redis extra is not installed")

    from paa.storage.queue.redis_backend import RedisDistributedLock, RedisMessageQueue

    settings = Settings(home=tmp_path)
    settings.storage.backend_queue = "redis"

    assert isinstance(get_queue(settings), RedisMessageQueue)
    assert isinstance(get_lock(settings), RedisDistributedLock)


def test_redis_backend_module_imports_without_the_extra() -> None:
    """The module must import cleanly on a machine with no redis installed."""
    from paa.storage.queue import redis_backend

    assert redis_backend.RedisMessageQueue is not None
    assert isinstance(redis_backend.redis_available(), bool)


# ---------------------------------------------------------------------------
# Redis backend — skips cleanly when no server is reachable
# ---------------------------------------------------------------------------


async def _redis_queue_or_skip(settings: QueueSettings):
    """Build a Redis queue against a live server, or skip the test."""
    pytest.importorskip("redis", reason="the redis extra is not installed")

    from paa.storage.queue.redis_backend import RedisMessageQueue

    namespace = f"paa:test:{uuid.uuid4().hex[:8]}"
    queue = RedisMessageQueue("redis://127.0.0.1:6379/0", settings, namespace=namespace)
    try:
        await queue.client.ping()
    except Exception as exc:  # any connection failure means "no server here"
        await queue.close()
        pytest.skip(f"no reachable redis server: {exc}")
    return queue


async def test_redis_enqueue_claim_ack_round_trip(settings: QueueSettings) -> None:
    queue = await _redis_queue_or_skip(settings)
    try:
        enqueued = await queue.enqueue(WORKERS, {"task": "compile"})
        assert await queue.depth(WORKERS) == 1

        claimed = await queue.claim(WORKERS, "worker-1")
        assert len(claimed) == 1
        assert claimed[0].payload == {"task": "compile"}
        assert claimed[0].id == enqueued.id

        assert await queue.ack(claimed[0].id) is True
        assert await queue.depth(WORKERS) == 0
    finally:
        await queue.purge(WORKERS)
        await queue.close()


async def test_redis_concurrent_claims_deliver_each_message_once(
    settings: QueueSettings,
) -> None:
    queue = await _redis_queue_or_skip(settings)
    try:
        for i in range(20):
            await queue.enqueue(WORKERS, {"n": i})

        batches = await asyncio.gather(
            *(queue.claim(WORKERS, f"worker-{i}", limit=4) for i in range(10))
        )
        ids = [m.id for batch in batches for m in batch]
        assert len(ids) == len(set(ids))
        assert len(ids) == 20
    finally:
        await queue.purge(WORKERS)
        await queue.close()


async def test_redis_nack_dead_letters_at_max_attempts(settings: QueueSettings) -> None:
    queue = await _redis_queue_or_skip(settings)
    try:
        await queue.enqueue(WORKERS, {"task": "poison"}, max_attempts=2)

        for _ in range(2):
            claimed = await queue.claim(WORKERS, "worker-1")
            assert len(claimed) == 1
            await queue.nack(claimed[0].id, "boom")

        assert await queue.claim(WORKERS, "worker-1") == []
        dead = await queue.dead_letters(WORKERS)
        assert len(dead) == 1
        assert dead[0].payload == {"task": "poison"}
    finally:
        await queue.purge(WORKERS)
        await queue.close()


async def test_redis_lock_release_verifies_the_holder(settings: QueueSettings) -> None:
    queue = await _redis_queue_or_skip(settings)
    from paa.storage.queue.redis_backend import RedisDistributedLock

    lock = RedisDistributedLock(client=queue.client)
    key = f"entity:{uuid.uuid4().hex[:8]}"
    try:
        assert await lock.acquire(key, "holder-a", 60) is True
        assert await lock.acquire(key, "holder-b", 60) is False
        assert await lock.release(key, "holder-b") is False
        assert await lock.release(key, "holder-a") is True
        assert await lock.acquire(key, "holder-b", 60) is True
    finally:
        await lock.release(key, "holder-b")
        await queue.close()
