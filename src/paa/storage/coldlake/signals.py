"""Raw signal intake — the cold lake's write-once front door (RFC §1.2, §6).

Every external event lands here verbatim before anything interprets it. That
ordering is the whole point of a cold lake: if extraction has a bug, or the
schema changes, or a model gets replaced, the original payload is still on disk
to re-derive from. Nothing in this module edits ``raw_payload`` after insert.

Two properties this repository owes its callers
------------------------------------------------
**Idempotency.** Ingestion adapters poll. A webhook retries. Both replay the
same event, and ``(channel, external_id)`` is the natural key that makes the
second delivery a no-op — :meth:`SignalRepository.record` returns the existing
row rather than filing a duplicate or raising. This mirrors how the ledger
treats a duplicate ``idempotency_key`` (see :mod:`paa.ledger.store`): at-least-
once delivery is made effectively-once at the storage boundary, not by asking
every adapter to be careful.

**Bounded row size.** A payload over ``inline_threshold_bytes`` goes to the
content-addressed store and the row keeps a ``cas://`` pointer. Without that, a
single 40 MB attachment turns every ``SELECT * FROM cold_lake_signals`` — the
unprocessed-signal poll runs constantly — into a 40 MB read. The threshold is a
constructor argument because the right value depends on the channel mix.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict

from paa.core.errors import StorageError
from paa.storage.coldlake.cas import BlobRef, ContentAddressedStore
from paa.storage.relational.database import Database, dumps, from_iso, to_iso, utc_now

__all__ = ["DEFAULT_INLINE_THRESHOLD_BYTES", "Signal", "SignalRepository", "SignalStatus"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "cold_lake_signals"

#: 64 KB. Comfortably above a chat message, a webhook body or a calendar event;
#: comfortably below anything that would hurt to read on every poll.
DEFAULT_INLINE_THRESHOLD_BYTES: Final = 64 * 1024

#: Keys of the stub left in ``raw_payload`` when the real payload is offloaded.
#: The column is ``NOT NULL`` with a ``json_valid`` CHECK, so an offloaded row
#: still has to hold *something* that parses — a self-describing pointer is more
#: useful there than an empty object.
_STUB_URI_KEY: Final = "_cas_uri"
_STUB_SIZE_KEY: Final = "_raw_bytes"

SignalStatus = str  # constrained by ck_signal_status; see _VALID_STATUSES

_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"unprocessed", "processing", "processed", "malformed", "quarantined"}
)

_COLUMNS: Final = """
    id, received_at, channel, external_id, raw_payload, content_hash,
    blob_uri, sync_status, processed_at, error_detail
"""


class Signal(BaseModel):
    """One raw external event.

    ``raw_payload`` holds the verbatim JSON *unless* the signal was offloaded,
    in which case it holds the CAS stub and the real bytes come from
    :meth:`SignalRepository.payload_bytes`. Callers should go through that method
    rather than reading the field, so the offload stays invisible to them.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    received_at: datetime
    channel: str
    external_id: str | None
    raw_payload: str
    content_hash: str
    blob_uri: str | None
    sync_status: str
    processed_at: datetime | None
    error_detail: str | None

    @property
    def is_offloaded(self) -> bool:
        """Whether the payload lives in the CAS rather than in the row."""
        return self.blob_uri is not None


class SignalRepository:
    """Read/write access to ``cold_lake_signals``.

    Claim lifecycle note for the integrator: :meth:`claim_unprocessed` moves rows
    to ``processing``, and the schema has no ``claimed_at`` column, so a worker
    that dies mid-claim leaves its rows in ``processing`` with nothing to time
    them out against. Recovering those is the queue layer's job (it owns the
    visibility timeout in :class:`paa.config.QueueSettings`); this repository
    deliberately does not invent a heuristic — guessing which claims are stale
    from ``received_at`` would re-deliver live work under load.
    """

    def __init__(
        self,
        db: Database,
        cas: ContentAddressedStore,
        *,
        inline_threshold_bytes: int = DEFAULT_INLINE_THRESHOLD_BYTES,
    ) -> None:
        self._db = db
        self._cas = cas
        self._threshold = inline_threshold_bytes

    # -- writes ------------------------------------------------------------

    async def record(
        self,
        channel: str,
        raw_payload: str | bytes | Mapping[str, Any] | Sequence[Any],
        external_id: str | None = None,
    ) -> Signal:
        """File a signal, or return the one already filed for this external id.

        Idempotent on ``(channel, external_id)``. The pre-check handles the
        common case without an exception; the ``IntegrityError`` catch handles
        the race where two adapters deliver the same event concurrently and both
        pass the pre-check. Both paths return the incumbent, so callers never
        have to distinguish "I wrote it" from "someone beat me to it".
        """
        if external_id is not None:
            existing = await self.get_by_external_id(channel, external_id)
            if existing is not None:
                log.debug(
                    "signal.duplicate_suppressed", channel=channel, external_id=external_id
                )
                return existing

        text = _as_json_text(raw_payload)
        payload_bytes = text.encode("utf-8")
        content_hash = hashlib.sha256(payload_bytes).hexdigest()

        stored_text = text
        blob_uri: str | None = None
        if len(payload_bytes) > self._threshold:
            # The CAS hashes raw bytes, so its digest is this same content_hash.
            # One value identifies the payload in both places.
            ref: BlobRef = self._cas.put(payload_bytes)
            blob_uri = ref.uri
            stored_text = dumps({_STUB_URI_KEY: ref.uri, _STUB_SIZE_KEY: ref.size_bytes})
            log.debug(
                "signal.payload_offloaded",
                channel=channel,
                bytes=ref.size_bytes,
                sha256=ref.sha256,
            )

        signal_id = str(uuid.uuid4())
        received_at = utc_now()
        try:
            await self._db.execute(
                f"INSERT INTO cold_lake_signals ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signal_id,
                    to_iso(received_at),
                    channel,
                    external_id,
                    stored_text,
                    content_hash,
                    blob_uri,
                    "unprocessed",
                    None,
                    None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if external_id is not None:
                racer = await self.get_by_external_id(channel, external_id)
                if racer is not None:
                    return racer
            raise StorageError(
                f"signal insert rejected: {exc}",
                substrate=_SUBSTRATE,
                channel=channel,
                external_id=external_id,
            ) from exc

        return Signal(
            id=signal_id,
            received_at=received_at,
            channel=channel,
            external_id=external_id,
            raw_payload=stored_text,
            content_hash=content_hash,
            blob_uri=blob_uri,
            sync_status="unprocessed",
            processed_at=None,
            error_detail=None,
        )

    async def claim_unprocessed(self, limit: int = 10) -> list[Signal]:
        """Atomically take up to ``limit`` unprocessed signals, oldest first.

        The select and the status flip share one transaction. Split apart, two
        pollers would read the same ids and both believe they owned them —
        which, since processing a signal writes facts, means duplicated memory.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")

        async with self._db.transaction() as conn:
            # Tiebreak on rowid, not id. Signals arriving in the same clock tick
            # share a `received_at`, and `id` is a random UUID — ordering by it
            # would shuffle same-instant arrivals into an arbitrary order, so
            # "oldest first" would silently stop being true under load, which is
            # exactly when ingestion order matters. rowid is monotonic in
            # insertion order and gives the real arrival sequence.
            async with conn.execute(
                f"SELECT {_COLUMNS} FROM cold_lake_signals "
                "WHERE sync_status = 'unprocessed' ORDER BY received_at ASC, rowid ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = list(await cur.fetchall())
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" * len(ids))
            await conn.execute(
                "UPDATE cold_lake_signals SET sync_status = 'processing' "
                f"WHERE id IN ({placeholders})",
                ids,
            )

        claimed = [_signal_from_row(row).model_copy(update={"sync_status": "processing"}) for row in rows]
        log.debug("signal.claimed", count=len(claimed))
        return claimed

    async def mark_processed(self, signal_id: str) -> bool:
        """Close a signal out successfully. Returns whether a row changed."""
        return await self._set_status(signal_id, "processed", error_detail=None)

    async def mark_malformed(self, signal_id: str, error: str) -> bool:
        """Park a signal that could not be parsed, keeping the reason with it.

        The payload is *not* deleted — a malformed signal is exactly the input a
        future parser fix needs to be tested against.
        """
        return await self._set_status(signal_id, "malformed", error_detail=error)

    async def mark_quarantined(self, signal_id: str, error: str) -> bool:
        """Hold a signal back from processing pending a human look."""
        return await self._set_status(signal_id, "quarantined", error_detail=error)

    async def release(self, signal_id: str) -> bool:
        """Return a claimed signal to the pool, e.g. after a retryable failure."""
        changed = await self._db.execute(
            "UPDATE cold_lake_signals SET sync_status = 'unprocessed', processed_at = NULL "
            "WHERE id = ? AND sync_status = 'processing'",
            (signal_id,),
        )
        return changed > 0

    async def _set_status(
        self, signal_id: str, status: str, *, error_detail: str | None
    ) -> bool:
        if status not in _VALID_STATUSES:  # pragma: no cover - callers pass literals
            raise StorageError(
                f"invalid signal status {status!r}", substrate=_SUBSTRATE, status=status
            )
        changed = await self._db.execute(
            "UPDATE cold_lake_signals SET sync_status = ?, processed_at = ?, error_detail = ? "
            "WHERE id = ?",
            (status, to_iso(utc_now()), error_detail, signal_id),
        )
        return changed > 0

    # -- reads -------------------------------------------------------------

    async def get(self, signal_id: str) -> Signal | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM cold_lake_signals WHERE id = ?", (signal_id,)
        )
        return _signal_from_row(row) if row is not None else None

    async def get_by_external_id(self, channel: str, external_id: str) -> Signal | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM cold_lake_signals WHERE channel = ? AND external_id = ?",
            (channel, external_id),
        )
        return _signal_from_row(row) if row is not None else None

    async def iter_by_channel(
        self,
        channel: str,
        *,
        status: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> AsyncIterator[Signal]:
        """Stream a channel's history.

        An async iterator rather than a list because backfill and replay walk
        whole channels, and the caller should not have to hold the entire
        channel in memory to look at it one signal at a time.
        """
        clauses = ["channel = ?"]
        params: list[Any] = [channel]
        if status is not None:
            if status not in _VALID_STATUSES:
                raise ValueError(f"unknown sync_status {status!r}")
            clauses.append("sync_status = ?")
            params.append(status)
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(to_iso(since))

        order = "DESC" if newest_first else "ASC"
        sql = (
            f"SELECT {_COLUMNS} FROM cold_lake_signals WHERE {' AND '.join(clauses)} "
            f"ORDER BY received_at {order}, id {order}"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        for row in await self._db.fetch_all(sql, params):
            yield _signal_from_row(row)

    async def count_by_status(self, channel: str | None = None) -> dict[str, int]:
        """Queue-depth snapshot, for the backpressure checks in RFC §6."""
        if channel is None:
            rows = await self._db.fetch_all(
                "SELECT sync_status, COUNT(*) AS n FROM cold_lake_signals GROUP BY sync_status"
            )
        else:
            rows = await self._db.fetch_all(
                "SELECT sync_status, COUNT(*) AS n FROM cold_lake_signals "
                "WHERE channel = ? GROUP BY sync_status",
                (channel,),
            )
        return {row["sync_status"]: row["n"] for row in rows}

    # -- payload access ----------------------------------------------------

    def payload_bytes(self, signal: Signal) -> bytes:
        """The original payload, wherever it ended up.

        Reading through the CAS means an offloaded payload is hash-verified on
        every read; an inline one was small enough that the row itself is the
        record. Either way the caller sees the bytes that arrived.
        """
        if signal.blob_uri is None:
            return signal.raw_payload.encode("utf-8")
        return self._cas.get(BlobRef.parse_uri(signal.blob_uri))

    def payload_json(self, signal: Signal) -> Any:
        """The payload parsed back into Python."""
        raw = self.payload_bytes(signal)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Only reachable if a row was written by something that bypassed
            # record()'s validation — worth a clear error rather than a traceback
            # from deep inside a consumer.
            raise StorageError(
                f"stored payload is not valid JSON: {exc}",
                substrate=_SUBSTRATE,
                signal_id=signal.id,
            ) from exc

    def verify_payload(self, signal: Signal) -> bool:
        """Re-hash the stored payload against ``content_hash``."""
        return hashlib.sha256(self.payload_bytes(signal)).hexdigest() == signal.content_hash


# ---------------------------------------------------------------------------


def _as_json_text(payload: str | bytes | Mapping[str, Any] | Sequence[Any]) -> str:
    """Coerce an incoming payload to the JSON text the column demands.

    ``raw_payload`` carries a ``json_valid`` CHECK, so a payload that is not
    JSON has to be rejected *here* — the alternative is a constraint violation
    surfacing as an opaque ``IntegrityError`` with no indication of which field
    was wrong. Strings and bytes are validated rather than re-encoded, so the
    stored text stays byte-identical to what arrived.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError(
                "signal payload is not valid UTF-8; wrap binary content in a JSON "
                "envelope or archive it as an artifact instead",
                substrate=_SUBSTRATE,
            ) from exc

    if isinstance(payload, str):
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"signal payload is not valid JSON: {exc}", substrate=_SUBSTRATE
            ) from exc
        return payload

    if isinstance(payload, (Mapping, Iterable)):
        return dumps(payload)

    raise StorageError(  # pragma: no cover - guarded by the type signature
        f"unsupported payload type {type(payload).__name__}", substrate=_SUBSTRATE
    )


def _signal_from_row(row: Any) -> Signal:
    return Signal(
        id=row["id"],
        received_at=from_iso(row["received_at"]),
        channel=row["channel"],
        external_id=row["external_id"],
        raw_payload=row["raw_payload"],
        content_hash=row["content_hash"],
        blob_uri=row["blob_uri"],
        sync_status=row["sync_status"],
        processed_at=from_iso(row["processed_at"]) if row["processed_at"] else None,
        error_detail=row["error_detail"],
    )
