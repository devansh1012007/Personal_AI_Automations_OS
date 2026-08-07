"""Redis Streams backend. RFC §6, for multi-process deployments.

This is the RFC's original dispatch fabric, kept behind the same interface as
the SQLite default (ADR-0005). Use it when consumers live in separate
processes or hosts, where SQLite's single-writer model becomes the bottleneck.

It is emphatically *not* the source of truth. Per RFC §17.4 the ledger remains
authoritative; Redis holds only in-flight dispatch state, and losing the whole
instance costs redelivery, not history.

Two honest limitations versus the SQLite backend, both structural to Redis
Streams rather than accidents of this implementation:

* **No priority.** Streams are strictly append-ordered; there is no cheap way
  to dispatch a low-priority-value entry ahead of an older one. ``priority`` is
  carried through so a message round-trips unchanged, but dispatch is FIFO.
  Priority-sensitive work should either use the SQLite backend or split across
  per-band streams.
* **Delayed delivery is a side table.** Streams cannot hide an entry until a
  future time, so delayed messages wait in a sorted set and are promoted on the
  next claim. A stream with no consumers therefore promotes nothing — delayed
  delivery is "no earlier than", never "exactly at".

``redis`` is imported lazily so this module stays importable — and the package
stays installable — without the extra. Only constructing the backend requires
it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import structlog

from paa.config import QueueSettings
from paa.core.errors import StorageError
from paa.storage.queue.base import (
    DEFAULT_PRIORITY,
    DistributedLock,
    MessageQueue,
    QueueMessage,
    StreamName,
)
from paa.storage.relational.database import dumps, from_iso, loads, to_iso, utc_now

__all__ = ["RedisDistributedLock", "RedisMessageQueue", "redis_available"]

log = structlog.get_logger(__name__)

_DEFAULT_NAMESPACE = "paa:q"
_DEFAULT_GROUP = "paa"
_MAX_ERROR_CHARS = 2000

#: Separator between the stream name and the backend entry id inside a
#: ``QueueMessage.id``. XACK needs the stream key, and the interface only hands
#: back an id, so the id has to carry both. ``#`` cannot appear in a
#: :class:`StreamName` value, which keeps the split unambiguous.
_ID_SEP = "#"
_DELAYED_PREFIX = "delayed:"
_DEAD_PREFIX = "dead:"

# Compare-and-act lock scripts. Each is a single atomic step, which is the
# whole point: a get-then-delete pair from the client would let a lease expire
# between the two calls and delete a lock that had already passed to someone
# else.
_ACQUIRE_LUA = """
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then return 1 end
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('pexpire', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_REFRESH_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


def _redis_asyncio() -> Any:
    """Import ``redis.asyncio`` on demand, with an actionable failure."""
    try:
        import redis.asyncio as redis_asyncio
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise StorageError(
            "the redis queue backend requires the 'redis' extra (pip install 'paa[redis]')",
            substrate="redis",
        ) from exc
    return redis_asyncio


def _redis_exceptions() -> Any:
    import redis.exceptions as exceptions

    return exceptions


def redis_available() -> bool:
    """Whether the ``redis`` package is importable. Used to skip tests."""
    try:
        import redis.asyncio  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


class _RedisKeys:
    """Key layout for one namespace. Centralised so a rename cannot half-apply."""

    __slots__ = ("group", "namespace")

    def __init__(self, namespace: str, group: str) -> None:
        self.namespace = namespace
        self.group = group

    def stream(self, stream: StreamName) -> str:
        return f"{self.namespace}:{stream.value}"

    def delayed(self, stream: StreamName) -> str:
        return f"{self.namespace}:{stream.value}:delayed"

    def delayed_body(self, stream: StreamName) -> str:
        return f"{self.namespace}:{stream.value}:delayed:body"

    def dead(self, stream: StreamName) -> str:
        return f"{self.namespace}:{stream.value}:dead"

    def lock(self, key: str) -> str:
        return f"{self.namespace}:lock:{key}"


class RedisMessageQueue(MessageQueue):
    """At-least-once queue over Redis Streams and consumer groups.

    Claim/ack maps onto the group protocol directly: ``XREADGROUP`` moves an
    entry into the consumer's Pending Entries List, ``XACK`` clears it, and
    ``XAUTOCLAIM`` re-delivers anything that has sat in a PEL longer than the
    visibility timeout. Redis's idle-time bookkeeping *is* the visibility
    window, so no deadline needs to be stored.
    """

    def __init__(
        self,
        url: str | None = None,
        settings: QueueSettings | None = None,
        *,
        client: Any | None = None,
        namespace: str = _DEFAULT_NAMESPACE,
        group: str = _DEFAULT_GROUP,
    ) -> None:
        self._settings = settings or QueueSettings()
        self._keys = _RedisKeys(namespace, group)
        self._owns_client = client is None
        self._client: Any = client
        self._url = url
        self._groups_ready: set[StreamName] = set()

    @property
    def client(self) -> Any:
        """The underlying client, created on first use.

        Injected clients must use ``decode_responses=True``; everything here
        treats stream fields as ``str``.
        """
        if self._client is None:
            if not self._url:
                raise StorageError("redis backend needs a url or a client", substrate="redis")
            self._client = _redis_asyncio().from_url(self._url, decode_responses=True)
        return self._client

    async def _ensure_group(self, stream: StreamName) -> None:
        """Create the consumer group once per stream, tolerating a race.

        ``MKSTREAM`` creates the stream too, so a consumer may legitimately
        start before any producer has published.
        """
        if stream in self._groups_ready:
            return
        try:
            await self.client.xgroup_create(
                self._keys.stream(stream), self._keys.group, id="0", mkstream=True
            )
        except _redis_exceptions().ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups_ready.add(stream)

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
        envelope = {
            "correlation_id": str(correlation_id) if correlation_id else None,
            "session_id": str(session_id) if session_id else None,
            "priority": priority,
            "payload": dict(payload),
            "attempts": 0,
            "max_attempts": max_attempts or self._settings.max_delivery_attempts,
            "enqueued_at": to_iso(now),
            "visible_after": to_iso(visible_after),
        }

        await self._ensure_group(stream)
        if delay_seconds > 0:
            handle = await self._park_delayed(stream, envelope, visible_after)
        else:
            entry_id = await self.client.xadd(self._keys.stream(stream), {"d": dumps(envelope)})
            handle = str(entry_id)

        message = _envelope_to_message(f"{stream.value}{_ID_SEP}{handle}", stream, envelope)
        log.debug("queue.enqueued", message_id=message.id, stream=stream.value, backend="redis")
        return message

    async def _park_delayed(
        self, stream: StreamName, envelope: dict[str, Any], visible_after: Any
    ) -> str:
        """Hold a message in the delay side table until its time arrives."""
        nonce = uuid.uuid4().hex
        await self.client.hset(self._keys.delayed_body(stream), nonce, dumps(envelope))
        await self.client.zadd(self._keys.delayed(stream), {nonce: visible_after.timestamp()})
        return f"{_DELAYED_PREFIX}{nonce}"

    async def _promote_delayed(self, stream: StreamName, *, batch: int = 128) -> int:
        """Move due delayed messages onto the stream.

        ``ZREM`` is the arbiter: whichever consumer's removal returns 1 owns the
        promotion, so concurrent claimers cannot publish the same delayed
        message twice.
        """
        due = await self.client.zrangebyscore(
            self._keys.delayed(stream), "-inf", utc_now().timestamp(), start=0, num=batch
        )
        promoted = 0
        for nonce in due:
            if not await self.client.zrem(self._keys.delayed(stream), nonce):
                continue
            raw = await self.client.hget(self._keys.delayed_body(stream), nonce)
            await self.client.hdel(self._keys.delayed_body(stream), nonce)
            if raw:
                await self.client.xadd(self._keys.stream(stream), {"d": raw})
                promoted += 1
        return promoted

    # -- consume -----------------------------------------------------------

    async def claim(
        self,
        stream: StreamName,
        consumer: str,
        *,
        limit: int = 1,
        visibility_timeout: float | None = None,
    ) -> list[QueueMessage]:
        """Take up to ``limit`` entries: overdue redeliveries first, then new.

        Redeliveries are drained before fresh work so a message stranded by a
        dead consumer cannot be starved behind a continuously busy stream.
        """
        if limit <= 0:
            return []

        timeout = (
            visibility_timeout
            if visibility_timeout is not None
            else self._settings.visibility_timeout_seconds
        )
        await self._ensure_group(stream)
        await self._promote_delayed(stream)

        messages = await self._autoclaim(stream, consumer, limit, timeout)
        if len(messages) < limit:
            messages.extend(await self._read_new(stream, consumer, limit - len(messages)))

        log.debug(
            "queue.claimed",
            stream=stream.value,
            consumer=consumer,
            count=len(messages),
            backend="redis",
        )
        return messages

    async def _autoclaim(
        self, stream: StreamName, consumer: str, limit: int, idle_seconds: float
    ) -> list[QueueMessage]:
        result = await self.client.xautoclaim(
            self._keys.stream(stream),
            self._keys.group,
            consumer,
            min_idle_time=int(idle_seconds * 1000),
            start_id="0-0",
            count=limit,
        )
        # Redis 7 returns (cursor, entries, deleted); Redis 6 returns
        # (cursor, entries). Index from the front so both shapes work.
        entries = result[1] if len(result) > 1 else []
        return self._decode_entries(stream, entries)

    async def _read_new(
        self, stream: StreamName, consumer: str, limit: int
    ) -> list[QueueMessage]:
        response = await self.client.xreadgroup(
            self._keys.group,
            consumer,
            {self._keys.stream(stream): ">"},
            count=limit,
        )
        if not response:
            return []
        return self._decode_entries(stream, response[0][1])

    def _decode_entries(self, stream: StreamName, entries: Any) -> list[QueueMessage]:
        """Turn raw stream entries into messages, skipping tombstones.

        ``XAUTOCLAIM`` reports entries that were ``XDEL``-ed while pending as a
        ``None`` body. They are dead weight in the PEL, so drop them here and
        let the caller's ack sweep clear them.
        """
        messages: list[QueueMessage] = []
        for entry_id, fields in entries:
            if not fields or "d" not in fields:
                continue
            envelope = loads(fields["d"], {})
            if not envelope:
                continue
            messages.append(
                _envelope_to_message(f"{stream.value}{_ID_SEP}{entry_id}", stream, envelope)
            )
        return messages

    async def ack(self, message_id: str) -> bool:
        """Acknowledge, or cancel a message still waiting in the delay table."""
        stream, handle = _split_id(message_id)
        if handle.startswith(_DELAYED_PREFIX):
            nonce = handle[len(_DELAYED_PREFIX) :]
            removed = await self.client.zrem(self._keys.delayed(stream), nonce)
            await self.client.hdel(self._keys.delayed_body(stream), nonce)
            return bool(removed)

        key = self._keys.stream(stream)
        acked = await self.client.xack(key, self._keys.group, handle)
        # XDEL as well as XACK: XACK only clears the pending entry, leaving the
        # entry itself in the stream, where it would keep inflating XLEN — the
        # number `depth` reports and backpressure reads.
        await self.client.xdel(key, handle)
        return bool(acked)

    async def nack(
        self,
        message_id: str,
        error: str | BaseException,
        *,
        retry_delay_seconds: float = 0.0,
    ) -> QueueMessage | None:
        """Retire the failed entry and republish a successor, or dead-letter it.

        Stream entries are immutable, so "increment attempts" means writing a
        new entry with a bumped counter and dropping the old one — the retry is
        a different entry with the same logical identity.
        """
        stream, handle = _split_id(message_id)
        if handle.startswith((_DELAYED_PREFIX, _DEAD_PREFIX)):
            return None

        key = self._keys.stream(stream)
        entries = await self.client.xrange(key, min=handle, max=handle)
        if not entries:
            return None

        envelope = loads(entries[0][1].get("d"), {})
        if not envelope:
            return None

        attempts = int(envelope.get("attempts", 0)) + 1
        envelope["attempts"] = attempts
        envelope["last_error"] = str(error)[:_MAX_ERROR_CHARS]

        await self.client.xack(key, self._keys.group, handle)
        await self.client.xdel(key, handle)

        if attempts >= int(envelope.get("max_attempts", self._settings.max_delivery_attempts)):
            dead_id = await self.client.xadd(self._keys.dead(stream), {"d": dumps(envelope)})
            log.warning(
                "queue.dead_lettered",
                message_id=message_id,
                stream=stream.value,
                attempts=attempts,
                backend="redis",
            )
            return _envelope_to_message(
                f"{stream.value}{_ID_SEP}{_DEAD_PREFIX}{dead_id}", stream, envelope
            )

        now = utc_now()
        visible_after = now + timedelta(seconds=max(0.0, retry_delay_seconds))
        envelope["visible_after"] = to_iso(visible_after)
        if retry_delay_seconds > 0:
            handle = await self._park_delayed(stream, envelope, visible_after)
        else:
            handle = str(await self.client.xadd(key, {"d": dumps(envelope)}))

        return _envelope_to_message(f"{stream.value}{_ID_SEP}{handle}", stream, envelope)

    # -- recovery ----------------------------------------------------------

    async def reclaim_expired(self) -> int:
        """Dead-letter exhausted pending entries; leave the rest to redelivery.

        Entries below the attempt ceiling need no action: they are already idle
        in a PEL, and the next :meth:`claim` on that stream picks them up via
        ``XAUTOCLAIM``. Only messages that have burned every attempt need a
        decision made for them here.
        """
        timeout_ms = int(self._settings.visibility_timeout_seconds * 1000)
        ceiling = self._settings.max_delivery_attempts
        handled = 0

        for stream in StreamName:
            key = self._keys.stream(stream)
            try:
                pending = await self.client.xpending_range(
                    key, self._keys.group, min="-", max="+", count=256, idle=timeout_ms
                )
            except _redis_exceptions().ResponseError as exc:
                if "NOGROUP" in str(exc):
                    continue
                raise

            for entry in pending:
                handled += 1
                if int(entry.get("times_delivered", 0)) < ceiling:
                    continue
                entry_id = entry["message_id"]
                rows = await self.client.xrange(key, min=entry_id, max=entry_id)
                if rows:
                    envelope = loads(rows[0][1].get("d"), {}) or {}
                    envelope["attempts"] = int(entry.get("times_delivered", ceiling))
                    envelope["last_error"] = "visibility timeout elapsed without ack"
                    await self.client.xadd(self._keys.dead(stream), {"d": dumps(envelope)})
                await self.client.xack(key, self._keys.group, entry_id)
                await self.client.xdel(key, entry_id)

        if handled:
            log.warning("queue.reclaimed_expired", handled=handled, backend="redis")
        return handled

    # -- inspection --------------------------------------------------------

    async def depth(self, stream: StreamName) -> int:
        """Live entries plus those still parked in the delay table."""
        live = await self.client.xlen(self._keys.stream(stream))
        delayed = await self.client.zcard(self._keys.delayed(stream))
        return int(live) + int(delayed)

    async def dead_letters(self, stream: StreamName, limit: int = 100) -> list[QueueMessage]:
        entries = await self.client.xrevrange(self._keys.dead(stream), count=limit)
        messages: list[QueueMessage] = []
        for entry_id, fields in entries:
            envelope = loads(fields.get("d"), {})
            if envelope:
                messages.append(
                    _envelope_to_message(
                        f"{stream.value}{_ID_SEP}{_DEAD_PREFIX}{entry_id}", stream, envelope
                    )
                )
        return messages

    async def purge(self, stream: StreamName) -> int:
        removed = await self.depth(stream)
        await self.client.delete(
            self._keys.stream(stream),
            self._keys.delayed(stream),
            self._keys.delayed_body(stream),
            self._keys.dead(stream),
        )
        self._groups_ready.discard(stream)
        log.info("queue.purged", stream=stream.value, removed=removed, backend="redis")
        return removed

    async def close(self) -> None:
        """Close only a client we created; an injected one belongs to the caller."""
        if self._client is not None and self._owns_client:
            closer = getattr(self._client, "aclose", None) or self._client.close
            await closer()
            self._client = None


class RedisDistributedLock(DistributedLock):
    """``SET NX PX`` lock with Lua compare-and-act release.

    The value stored at the key is the holder id, and every mutating operation
    checks it inside a Lua script. Scripts run atomically in Redis, which is
    what closes the window a client-side get-then-delete would leave open —
    the window in which a lease expires, passes to a new holder, and the old
    holder deletes a lock it no longer owns.
    """

    def __init__(
        self,
        url: str | None = None,
        settings: QueueSettings | None = None,
        *,
        client: Any | None = None,
        namespace: str = _DEFAULT_NAMESPACE,
    ) -> None:
        self._settings = settings or QueueSettings()
        self._keys = _RedisKeys(namespace, _DEFAULT_GROUP)
        self._owns_client = client is None
        self._client: Any = client
        self._url = url

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._url:
                raise StorageError("redis lock needs a url or a client", substrate="redis")
            self._client = _redis_asyncio().from_url(self._url, decode_responses=True)
        return self._client

    async def acquire(self, key: str, holder: str, ttl: float) -> bool:
        if ttl <= 0:
            raise ValueError(f"lock ttl must be positive, got {ttl}")
        result = await self.client.eval(
            _ACQUIRE_LUA, 1, self._keys.lock(key), holder, int(ttl * 1000)
        )
        return bool(result)

    async def release(self, key: str, holder: str) -> bool:
        result = await self.client.eval(_RELEASE_LUA, 1, self._keys.lock(key), holder)
        return bool(result)

    async def refresh(self, key: str, holder: str, ttl: float) -> bool:
        if ttl <= 0:
            raise ValueError(f"lock ttl must be positive, got {ttl}")
        result = await self.client.eval(
            _REFRESH_LUA, 1, self._keys.lock(key), holder, int(ttl * 1000)
        )
        return bool(result)

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            closer = getattr(self._client, "aclose", None) or self._client.close
            await closer()
            self._client = None


def _split_id(message_id: str) -> tuple[StreamName, str]:
    """Recover the stream and backend handle from a composite message id."""
    stream_value, separator, handle = message_id.partition(_ID_SEP)
    if not separator:
        raise StorageError(
            "malformed redis queue message id", substrate="redis", message_id=message_id
        )
    try:
        return StreamName(stream_value), handle
    except ValueError as exc:
        raise StorageError(
            "unknown stream in queue message id", substrate="redis", message_id=message_id
        ) from exc


def _envelope_to_message(
    message_id: str, stream: StreamName, envelope: dict[str, Any]
) -> QueueMessage:
    return QueueMessage(
        id=message_id,
        stream=stream,
        correlation_id=(
            uuid.UUID(envelope["correlation_id"]) if envelope.get("correlation_id") else None
        ),
        session_id=uuid.UUID(envelope["session_id"]) if envelope.get("session_id") else None,
        priority=int(envelope.get("priority", DEFAULT_PRIORITY)),
        payload=envelope.get("payload") or {},
        attempts=int(envelope.get("attempts", 0)),
        max_attempts=int(envelope.get("max_attempts", 3)),
        enqueued_at=from_iso(envelope["enqueued_at"]),
        visible_after=from_iso(envelope["visible_after"]),
    )
