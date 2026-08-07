"""SQLite-backed durable queue and distributed lock.

SPEC DEVIATION (docs/adr/0005): this is the default dispatch fabric in place of
RFC §6's eleven Redis Streams. It trades cross-machine reach — which a
single-user local-first runtime does not need — for durability that costs
nothing to obtain: the queue lives in the same database file and the same WAL
as the ledger, so "message enqueued" and "event appended" survive a crash
together instead of one outliving the other.

Correctness rests on one property, and every method here is arranged to
preserve it: **a claim is a single transaction**. Selecting candidate rows and
marking them claimed cannot be interleaved, so two consumers racing on the same
stream can never both be handed the same message. Doing the select and the
update as separate statements outside a transaction is the natural way to write
this code, and it is silently wrong under concurrency — it duplicates work only
under load, which is exactly when duplication is most expensive.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

import structlog

from paa.config import QueueSettings
from paa.storage.queue.base import (
    DEFAULT_PRIORITY,
    DistributedLock,
    MessageQueue,
    QueueMessage,
    StreamName,
)
from paa.storage.relational.database import Database, dumps, from_iso, loads, to_iso, utc_now

__all__ = ["SqliteDistributedLock", "SqliteMessageQueue"]

log = structlog.get_logger(__name__)

#: Cap on a persisted failure string. A nacked message can carry a full
#: traceback, and an unbounded column would let one pathological consumer bloat
#: the database that the ledger also lives in.
_MAX_ERROR_CHARS = 2000

# Every statement below is a plain literal rather than an f-string over a shared
# column list. Repeating the projection costs a few lines and buys the guarantee
# that no query in this module is assembled at runtime — the only way to be
# certain a queue that stores caller-supplied payloads cannot construct SQL.

_INSERT_SQL = """
INSERT INTO queue_messages (
    id, stream, correlation_id, session_id, priority, payload,
    status, attempts, max_attempts, visible_after, enqueued_at
) VALUES (?, ?, ?, ?, ?, ?, 'ready', 0, ?, ?, ?)
"""

# Dispatch order is the RFC §6 contract: priority band first, FIFO inside it.
# ``rowid`` breaks exact ``enqueued_at`` ties — two messages enqueued inside the
# same clock tick must still dispatch in insertion order, and on Windows the
# system clock is coarse enough for that to happen in practice.
_SELECT_READY_SQL = """
SELECT id, stream, correlation_id, session_id, priority, payload,
       attempts, max_attempts, enqueued_at, visible_after
FROM queue_messages
WHERE stream = ? AND status = 'ready' AND visible_after <= ?
ORDER BY priority ASC, enqueued_at ASC, rowid ASC
LIMIT ?
"""

_MARK_CLAIMED_SQL = """
UPDATE queue_messages
SET status = 'claimed', claimed_by = ?, claimed_at = ?, visible_after = ?
WHERE id = ?
"""

_SELECT_EXPIRED_SQL = """
SELECT id, stream, correlation_id, session_id, priority, payload,
       attempts, max_attempts, enqueued_at, visible_after, claimed_by
FROM queue_messages
WHERE status = 'claimed' AND visible_after <= ?
ORDER BY visible_after ASC
"""

_SELECT_ONE_SQL = """
SELECT id, stream, correlation_id, session_id, priority, payload,
       attempts, max_attempts, enqueued_at, visible_after, status
FROM queue_messages
WHERE id = ?
"""

_SELECT_DEAD_SQL = """
SELECT id, stream, correlation_id, session_id, priority, payload,
       attempts, max_attempts, enqueued_at, visible_after
FROM queue_messages
WHERE stream = ? AND status = 'dead'
ORDER BY enqueued_at DESC
LIMIT ?
"""

_RECLAIM_TO_READY_SQL = """
UPDATE queue_messages
SET status = 'ready', attempts = ?, visible_after = ?, claimed_by = NULL,
    claimed_at = NULL, last_error = 'visibility timeout elapsed without ack'
WHERE id = ?
"""

_RECLAIM_TO_DEAD_SQL = """
UPDATE queue_messages
SET status = 'dead', attempts = ?, completed_at = ?,
    last_error = 'visibility timeout elapsed without ack; attempts exhausted'
WHERE id = ?
"""

_NACK_SQL = """
UPDATE queue_messages
SET attempts = ?, status = ?, visible_after = ?, completed_at = ?, last_error = ?,
    claimed_by = NULL, claimed_at = NULL
WHERE id = ?
"""

_ACQUIRE_LOCK_SQL = """
INSERT INTO queue_locks (lock_key, holder, acquired_at, expires_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(lock_key) DO UPDATE SET
    holder      = excluded.holder,
    acquired_at = excluded.acquired_at,
    expires_at  = excluded.expires_at
WHERE queue_locks.expires_at <= excluded.acquired_at
   OR queue_locks.holder = excluded.holder
"""


class SqliteMessageQueue(MessageQueue):
    """Durable queue over the existing ``queue_messages`` table.

    Uses the shared :class:`~paa.storage.relational.database.Database` rather
    than its own connections: SQLite permits exactly one writer, so a second
    connection pool would contend with the ledger for the write lock and
    convert a fast dispatch into an ``SQLITE_BUSY`` retry storm.
    """

    def __init__(self, db: Database, settings: QueueSettings | None = None) -> None:
        self._db = db
        self._settings = settings or QueueSettings()

    # -- publish -----------------------------------------------------------

    async def enqueue(
        self,
        stream: StreamName,
        payload: dict[str, object],
        *,
        priority: int = DEFAULT_PRIORITY,
        correlation_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        delay_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> QueueMessage:
        now = utc_now()
        visible_after = now + timedelta(seconds=max(0.0, delay_seconds))
        message = QueueMessage(
            id=str(uuid.uuid4()),
            stream=stream,
            correlation_id=correlation_id,
            session_id=session_id,
            priority=priority,
            payload=dict(payload),
            attempts=0,
            max_attempts=max_attempts or self._settings.max_delivery_attempts,
            enqueued_at=now,
            visible_after=visible_after,
        )

        await self._db.execute(
            _INSERT_SQL,
            (
                message.id,
                message.stream.value,
                str(correlation_id) if correlation_id else None,
                str(session_id) if session_id else None,
                message.priority,
                dumps(message.payload),
                message.max_attempts,
                to_iso(visible_after),
                to_iso(now),
            ),
        )

        log.debug(
            "queue.enqueued",
            message_id=message.id,
            stream=stream.value,
            priority=priority,
            delay_seconds=delay_seconds,
            correlation_id=str(correlation_id) if correlation_id else None,
        )
        return message

    # -- consume -----------------------------------------------------------

    async def claim(
        self,
        stream: StreamName,
        consumer: str,
        *,
        limit: int = 1,
        visibility_timeout: float | None = None,
    ) -> list[QueueMessage]:
        """Take up to ``limit`` messages, atomically.

        The select and the mark-claimed update share one transaction. That is
        the entire concurrency guarantee: ``Database.transaction`` holds an
        in-process asyncio lock *and* opens ``BEGIN IMMEDIATE``, so neither a
        sibling coroutine nor a second process can observe a row between it
        being chosen and it being marked.
        """
        if limit <= 0:
            return []

        timeout = (
            visibility_timeout
            if visibility_timeout is not None
            else self._settings.visibility_timeout_seconds
        )
        now = utc_now()
        now_iso = to_iso(now)
        deadline = now + timedelta(seconds=timeout)
        deadline_iso = to_iso(deadline)

        async with self._db.transaction() as conn:
            async with conn.execute(_SELECT_READY_SQL, (stream.value, now_iso, limit)) as cur:
                rows = list(await cur.fetchall())
            if not rows:
                return []
            await conn.executemany(
                _MARK_CLAIMED_SQL,
                [(consumer, now_iso, deadline_iso, row["id"]) for row in rows],
            )

        claimed = [
            _row_to_message(row).model_copy(update={"visible_after": deadline}) for row in rows
        ]
        log.debug(
            "queue.claimed",
            stream=stream.value,
            consumer=consumer,
            count=len(claimed),
            visibility_timeout=timeout,
        )
        return claimed

    async def ack(self, message_id: str) -> bool:
        """Retire a message.

        Accepts a message in either ``ready`` or ``claimed`` state so that an
        ack arriving just after a visibility-timeout reclaim still retires the
        work rather than erroring. The residual race — a late ack retiring a
        message a second consumer has since claimed — is handled a layer up:
        every consumer writes through the ledger's idempotency key, so the
        duplicate execution is suppressed rather than committed twice.
        """
        changed = await self._db.execute(
            "UPDATE queue_messages SET status = 'done', completed_at = ?, "
            "claimed_by = NULL, claimed_at = NULL "
            "WHERE id = ? AND status IN ('ready', 'claimed')",
            (to_iso(utc_now()), message_id),
        )
        if changed:
            log.debug("queue.acked", message_id=message_id)
        return changed > 0

    async def nack(
        self,
        message_id: str,
        error: str | BaseException,
        *,
        retry_delay_seconds: float = 0.0,
    ) -> QueueMessage | None:
        """Record a failure and either reschedule or dead-letter.

        Read-modify-write inside one transaction: ``attempts`` is compared
        against ``max_attempts`` in the same statement sequence that increments
        it, so two concurrent nacks on one message cannot both see the
        pre-increment value and grant an extra retry past the ceiling.
        """
        detail = str(error)[:_MAX_ERROR_CHARS]
        now = utc_now()

        async with self._db.transaction() as conn:
            async with conn.execute(_SELECT_ONE_SQL, (message_id,)) as cur:
                row = await cur.fetchone()

            if row is None or row["status"] in ("done", "dead"):
                return None

            attempts = int(row["attempts"]) + 1
            exhausted = attempts >= int(row["max_attempts"])
            if exhausted:
                status, visible_after, completed_at = "dead", now, to_iso(now)
            else:
                status = "ready"
                visible_after = now + timedelta(seconds=max(0.0, retry_delay_seconds))
                completed_at = None

            await conn.execute(
                _NACK_SQL,
                (attempts, status, to_iso(visible_after), completed_at, detail, message_id),
            )

        updated = _row_to_message(row).model_copy(
            update={"attempts": attempts, "visible_after": visible_after}
        )
        if exhausted:
            # Operationally significant: a dead letter is work the runtime has
            # given up on, and nothing else will surface it.
            log.warning(
                "queue.dead_lettered",
                message_id=message_id,
                stream=updated.stream.value,
                attempts=attempts,
                error=detail[:200],
            )
        else:
            log.debug(
                "queue.nacked",
                message_id=message_id,
                attempts=attempts,
                max_attempts=updated.max_attempts,
                retry_delay_seconds=retry_delay_seconds,
            )
        return updated

    # -- recovery ----------------------------------------------------------

    async def reclaim_expired(self) -> int:
        """Redeliver claims whose visibility window lapsed.

        A timeout is counted as a failed attempt. Without that, a message that
        reliably kills whichever worker picks it up would cycle forever: it
        never nacks (the consumer died before it could), so ``attempts`` would
        never reach the dead-letter ceiling and the poison message would take
        down worker after worker indefinitely.

        Returns the number of messages moved out of the expired-claim state,
        counting both those returned to ``ready`` and those dead-lettered.
        """
        now = utc_now()
        now_iso = to_iso(now)

        async with self._db.transaction() as conn:
            async with conn.execute(_SELECT_EXPIRED_SQL, (now_iso,)) as cur:
                rows = list(await cur.fetchall())
            if not rows:
                return 0

            revive: list[tuple[object, ...]] = []
            bury: list[tuple[object, ...]] = []
            for row in rows:
                attempts = int(row["attempts"]) + 1
                if attempts >= int(row["max_attempts"]):
                    bury.append((attempts, now_iso, row["id"]))
                else:
                    revive.append((attempts, now_iso, row["id"]))

            if revive:
                await conn.executemany(_RECLAIM_TO_READY_SQL, revive)
            if bury:
                await conn.executemany(_RECLAIM_TO_DEAD_SQL, bury)

        log.warning(
            "queue.reclaimed_expired",
            redelivered=len(revive),
            dead_lettered=len(bury),
        )
        return len(rows)

    # -- inspection --------------------------------------------------------

    async def depth(self, stream: StreamName) -> int:
        value = await self._db.fetch_value(
            "SELECT COUNT(*) FROM queue_messages "
            "WHERE stream = ? AND status IN ('ready', 'claimed')",
            (stream.value,),
        )
        return int(value or 0)

    async def dead_letters(self, stream: StreamName, limit: int = 100) -> list[QueueMessage]:
        rows = await self._db.fetch_all(_SELECT_DEAD_SQL, (stream.value, limit))
        return [_row_to_message(row) for row in rows]

    async def purge(self, stream: StreamName) -> int:
        removed = await self._db.execute(
            "DELETE FROM queue_messages WHERE stream = ?", (stream.value,)
        )
        log.info("queue.purged", stream=stream.value, removed=removed)
        return removed

    async def close(self) -> None:
        """No-op: the :class:`Database` belongs to the caller, not to us.

        Closing a substrate a queue merely borrowed would tear the ledger out
        from under every other subsystem sharing it.
        """
        return None


class SqliteDistributedLock(DistributedLock):
    """TTL lock over the existing ``queue_locks`` table.

    "Distributed" is accurate even here: the lock is enforced by SQLite's own
    write serialisation, so it holds across processes sharing the database file,
    not merely across coroutines in one interpreter.
    """

    def __init__(self, db: Database, settings: QueueSettings | None = None) -> None:
        self._db = db
        self._settings = settings or QueueSettings()

    @property
    def default_ttl(self) -> float:
        """RFC §1.4 ``lock:entity:*`` lease length."""
        return self._settings.entity_lock_ttl_seconds

    async def acquire(self, key: str, holder: str, ttl: float) -> bool:
        """Take the lock if it is free, expired, or already ours.

        Expiry is evaluated here rather than by a background reaper: a holder
        that crashed cannot run cleanup, so the only reliable moment to notice
        a dead lease is when someone next wants the key. Re-acquisition by the
        current holder is allowed and simply extends the lease, which keeps
        ``acquire`` idempotent under at-least-once retry.
        """
        if ttl <= 0:
            raise ValueError(f"lock ttl must be positive, got {ttl}")

        now = utc_now()
        now_iso = to_iso(now)
        expires_iso = to_iso(now + timedelta(seconds=ttl))

        async with self._db.transaction() as conn:
            await conn.execute(_ACQUIRE_LOCK_SQL, (key, holder, now_iso, expires_iso))
            # Re-read rather than trust the upsert's rowcount: a conditional
            # ``DO UPDATE ... WHERE`` reports zero changes both when we lost the
            # race and when the write was a no-op, and those must not be
            # conflated. The row itself is unambiguous.
            async with conn.execute(
                "SELECT holder, expires_at FROM queue_locks WHERE lock_key = ?", (key,)
            ) as cur:
                row = await cur.fetchone()

        won = row is not None and row["holder"] == holder and row["expires_at"] > now_iso
        log.debug("queue.lock_acquire", lock_key=key, holder=holder, acquired=won)
        return won

    async def release(self, key: str, holder: str) -> bool:
        """Release only if ``holder`` owns the lock.

        The ``holder`` predicate is the whole safety story. Without it a stalled
        process whose lease already expired and passed to someone else would
        delete the *new* owner's lock on wake-up, and two workers would then
        both believe they held exclusive access to the same entity.
        """
        changed = await self._db.execute(
            "DELETE FROM queue_locks WHERE lock_key = ? AND holder = ?", (key, holder)
        )
        released = changed > 0
        if not released:
            log.debug("queue.lock_release_refused", lock_key=key, holder=holder)
        return released

    async def refresh(self, key: str, holder: str, ttl: float) -> bool:
        """Extend a live lease we own.

        Refuses an already-expired lease: once it lapsed the key was up for
        grabs, and silently resurrecting it could steal the lock back from a
        holder that legitimately acquired it in the interim.
        """
        if ttl <= 0:
            raise ValueError(f"lock ttl must be positive, got {ttl}")

        now_iso = to_iso(utc_now())
        expires_iso = to_iso(utc_now() + timedelta(seconds=ttl))
        changed = await self._db.execute(
            "UPDATE queue_locks SET expires_at = ? "
            "WHERE lock_key = ? AND holder = ? AND expires_at > ?",
            (expires_iso, key, holder, now_iso),
        )
        return changed > 0

    async def current_holder(self, key: str) -> str | None:
        """Who holds ``key`` right now, ignoring lapsed leases. Diagnostics only."""
        row = await self._db.fetch_one(
            "SELECT holder, expires_at FROM queue_locks WHERE lock_key = ?", (key,)
        )
        if row is None or row["expires_at"] <= to_iso(utc_now()):
            return None
        return str(row["holder"])


def _row_to_message(row: sqlite3.Row) -> QueueMessage:
    return QueueMessage(
        id=row["id"],
        stream=StreamName(row["stream"]),
        correlation_id=uuid.UUID(row["correlation_id"]) if row["correlation_id"] else None,
        session_id=uuid.UUID(row["session_id"]) if row["session_id"] else None,
        priority=int(row["priority"]),
        payload=loads(row["payload"], {}),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        enqueued_at=from_iso(row["enqueued_at"]),
        visible_after=from_iso(row["visible_after"]),
    )
