"""Durable queue layer — dispatch, backpressure and distributed locking.

SPEC DEVIATION (docs/adr/0005): RFC §6 specifies eleven Redis Streams. Redis is
not installed on the target machine, and the RFC's own §17.4 requires that
Redis never be the source of truth. The default backend is therefore SQLite —
durable by construction, sharing the ledger's database file and WAL — with the
Redis backend selectable behind the same interface for multi-process
deployments. Both are chosen through ``StorageSettings.backend_queue``.

Typical use::

    queue = get_queue(settings, db)
    await queue.enqueue(StreamName.WORKERS_ALLOC, {"task": "compile"})

    for message in await queue.claim(StreamName.WORKERS_ALLOC, "worker-1"):
        try:
            await handle(message)
        except Exception as exc:
            await queue.nack(message.id, exc, retry_delay_seconds=5)
        else:
            await queue.ack(message.id)
"""

from __future__ import annotations

from paa.config import Settings
from paa.core.errors import StorageError
from paa.storage.queue.backpressure import BackpressureController, BackpressureState
from paa.storage.queue.base import (
    CONTROL_PLANE_STREAMS,
    DEFAULT_PRIORITY,
    DistributedLock,
    LockUnavailableError,
    MessageQueue,
    QueueMessage,
    StreamName,
)
from paa.storage.queue.sqlite_backend import SqliteDistributedLock, SqliteMessageQueue
from paa.storage.relational.database import Database

__all__ = [
    "CONTROL_PLANE_STREAMS",
    "DEFAULT_PRIORITY",
    "BackpressureController",
    "BackpressureState",
    "DistributedLock",
    "LockUnavailableError",
    "MessageQueue",
    "QueueMessage",
    "SqliteDistributedLock",
    "SqliteMessageQueue",
    "StreamName",
    "get_lock",
    "get_queue",
]


def get_queue(settings: Settings, db: Database | None = None) -> MessageQueue:
    """Build the queue named by ``StorageSettings.backend_queue``.

    ``db`` is required for the SQLite backend and ignored by Redis. Passing a
    :class:`Database` rather than a path is deliberate: the queue must share the
    ledger's single writer, because a second connection to the same file would
    contend for SQLite's write lock instead of queueing behind it in-process.
    """
    if settings.storage.backend_queue == "redis":
        from paa.storage.queue.redis_backend import RedisMessageQueue

        return RedisMessageQueue(settings.storage.redis_url, settings.queue)

    if db is None:
        raise StorageError(
            "the sqlite queue backend requires a connected Database", substrate="sqlite"
        )
    return SqliteMessageQueue(db, settings.queue)


def get_lock(settings: Settings, db: Database | None = None) -> DistributedLock:
    """Build the distributed lock matching the configured queue backend.

    Locks follow the queue backend rather than taking their own setting: a lock
    in Redis guarding work queued in SQLite would put mutual exclusion in a
    substrate that can vanish independently of what it protects.
    """
    if settings.storage.backend_queue == "redis":
        from paa.storage.queue.redis_backend import RedisDistributedLock

        return RedisDistributedLock(settings.storage.redis_url, settings.queue)

    if db is None:
        raise StorageError(
            "the sqlite lock backend requires a connected Database", substrate="sqlite"
        )
    return SqliteDistributedLock(db, settings.queue)
