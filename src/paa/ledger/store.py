"""Append-only ledger store.

Every write goes through :meth:`LedgerStore.append`, which inside a single
transaction:

1. suppresses duplicates via the idempotency key,
2. assigns the next per-correlation ``state_version``,
3. links the event into the hash chain,
4. inserts the row,
5. updates the open-correlation projection.

Steps 2-5 must be atomic. If ``state_version`` were assigned outside the
transaction two concurrent appends could claim the same version, and the
``uq_ledger_version`` constraint would reject one of them after the caller had
already been told it succeeded.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from paa.core.errors import LedgerError
from paa.core.types import TERMINAL_EVENTS, CorrelationId, EventType
from paa.ledger.events import GENESIS_HASH, LedgerEvent
from paa.storage.relational.database import Database, dumps, from_iso, loads, to_iso, utc_now

__all__ = ["CorrelationHead", "LedgerStore"]

log = structlog.get_logger(__name__)

_INSERT_SQL = """
INSERT INTO system_state_ledger (
    event_id, correlation_id, session_id, causation_id, state_version,
    idempotency_key, attempt, discriminator, event_type, execution_mode,
    agent_role, allocated_worker_image, payload, prev_hash, event_hash, recorded_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_HEAD_UPSERT_SQL = """
INSERT INTO system_state_correlation_head (
    correlation_id, session_id, latest_sequence_id, latest_state_version,
    latest_event_type, latest_event_hash, execution_mode, is_terminal,
    opened_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(correlation_id) DO UPDATE SET
    session_id           = COALESCE(excluded.session_id, system_state_correlation_head.session_id),
    latest_sequence_id   = excluded.latest_sequence_id,
    latest_state_version = excluded.latest_state_version,
    latest_event_type    = excluded.latest_event_type,
    latest_event_hash    = excluded.latest_event_hash,
    execution_mode       = excluded.execution_mode,
    is_terminal          = excluded.is_terminal,
    updated_at           = excluded.updated_at
"""

_SELECT_COLUMNS = """
    sequence_id, event_id, correlation_id, session_id, causation_id, state_version,
    idempotency_key, attempt, discriminator, event_type, execution_mode, agent_role,
    allocated_worker_image, payload, prev_hash, event_hash, recorded_at
"""


class CorrelationHead:
    """Current position of one task lineage. Row of the head projection."""

    __slots__ = (
        "correlation_id",
        "execution_mode",
        "is_terminal",
        "latest_event_type",
        "latest_sequence_id",
        "latest_state_version",
        "opened_at",
        "session_id",
        "updated_at",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        self.correlation_id = uuid.UUID(row["correlation_id"])
        self.session_id = uuid.UUID(row["session_id"]) if row["session_id"] else None
        self.latest_sequence_id: int = row["latest_sequence_id"]
        self.latest_state_version: int = row["latest_state_version"]
        self.latest_event_type = EventType(row["latest_event_type"])
        self.execution_mode: str = row["execution_mode"]
        self.is_terminal: bool = bool(row["is_terminal"])
        self.opened_at: datetime = from_iso(row["opened_at"])
        self.updated_at: datetime = from_iso(row["updated_at"])

    def age_seconds(self, *, now: datetime | None = None) -> float:
        return ((now or utc_now()) - self.updated_at).total_seconds()

    def __repr__(self) -> str:
        return (
            f"CorrelationHead({self.correlation_id} v{self.latest_state_version} "
            f"{self.latest_event_type.value}{' TERMINAL' if self.is_terminal else ''})"
        )


class LedgerStore:
    """The append-only event log."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- writes ------------------------------------------------------------

    async def append(self, event: LedgerEvent) -> LedgerEvent:
        """Persist one event and return its sealed form.

        On a duplicate idempotency key the *existing* event is returned rather
        than raising. Suppression is the normal, expected outcome of
        at-least-once delivery, and forcing every caller to catch an exception
        for the happy path would be noise.
        """
        key = event.idempotency_key

        async with self._db.transaction() as conn:
            existing = await self._find_by_key(conn, key)
            if existing is not None:
                log.debug(
                    "ledger.duplicate_suppressed",
                    idempotency_key=key[:16],
                    event_type=event.event_type.value,
                    correlation_id=str(event.correlation_id),
                )
                return existing

            version, prev_hash = await self._chain_tip(conn, event.correlation_id)
            sealed = event.sealed(
                sequence_id=-1,  # replaced below with the real rowid
                state_version=version + 1,
                prev_hash=prev_hash,
            )

            try:
                cursor = await conn.execute(_INSERT_SQL, self._insert_params(sealed, key))
            except sqlite3.IntegrityError as exc:
                # A concurrent writer in *another process* beat us to it. The
                # in-process lock cannot cover that case, so re-read and return
                # whatever landed.
                if (duplicate := await self._find_by_key(conn, key)) is not None:
                    return duplicate
                raise LedgerError(
                    "ledger append violated an integrity constraint",
                    correlation_id=str(event.correlation_id),
                    event_type=event.event_type.value,
                    detail=str(exc),
                ) from exc

            sequence_id = int(cursor.lastrowid or 0)
            sealed = sealed.model_copy(update={"sequence_id": sequence_id})
            await self._update_head(conn, sealed)

        log.debug(
            "ledger.appended",
            sequence_id=sealed.sequence_id,
            state_version=sealed.state_version,
            event_type=sealed.event_type.value,
            correlation_id=str(sealed.correlation_id),
        )
        return sealed

    async def append_many(self, events: Sequence[LedgerEvent]) -> list[LedgerEvent]:
        """Append a batch. Not atomic across the batch by design.

        Each event is chained individually so a mid-batch failure still leaves
        a valid prefix on disk — which is what recovery expects to find.
        """
        return [await self.append(e) for e in events]

    def _insert_params(self, event: LedgerEvent, key: str) -> tuple[Any, ...]:
        return (
            str(event.event_id),
            str(event.correlation_id),
            str(event.session_id) if event.session_id else None,
            str(event.causation_id) if event.causation_id else None,
            event.state_version,
            key,
            event.attempt,
            event.discriminator,
            event.event_type.value,
            event.execution_mode.value,
            event.agent_role,
            event.allocated_worker_image,
            dumps(event.payload),
            event.prev_hash,
            event.event_hash,
            to_iso(event.recorded_at),
        )

    async def _chain_tip(
        self, conn: sqlite3.Connection, correlation_id: uuid.UUID
    ) -> tuple[int, str]:
        """Highest ``state_version`` and its digest for a correlation.

        Returns ``(0, GENESIS_HASH)`` for a lineage's first event.
        """
        async with conn.execute(
            "SELECT state_version, event_hash FROM system_state_ledger "
            "WHERE correlation_id = ? ORDER BY state_version DESC LIMIT 1",
            (str(correlation_id),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row["state_version"]), str(row["event_hash"])

    async def _find_by_key(self, conn: sqlite3.Connection, key: str) -> LedgerEvent | None:
        async with conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM system_state_ledger WHERE idempotency_key = ?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_event(row) if row else None

    async def _update_head(self, conn: sqlite3.Connection, event: LedgerEvent) -> None:
        now = to_iso(event.recorded_at)
        await conn.execute(
            _HEAD_UPSERT_SQL,
            (
                str(event.correlation_id),
                str(event.session_id) if event.session_id else None,
                event.sequence_id,
                event.state_version,
                event.event_type.value,
                event.event_hash,
                event.execution_mode.value,
                1 if event.event_type in TERMINAL_EVENTS else 0,
                now,
                now,
            ),
        )

    # -- reads -------------------------------------------------------------

    async def read_correlation(
        self,
        correlation_id: CorrelationId | uuid.UUID,
        *,
        from_version: int = 0,
    ) -> list[LedgerEvent]:
        """Every event for a lineage in replay order.

        Ordered by ``state_version``, not ``sequence_id``: the global sequence
        can gap when a transaction rolls back, whereas the per-correlation
        version is dense and is what replay folds over.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_SELECT_COLUMNS} FROM system_state_ledger "
            "WHERE correlation_id = ? AND state_version > ? ORDER BY state_version ASC",
            (str(correlation_id), from_version),
        )
        return [_row_to_event(r) for r in rows]

    async def latest_event(
        self, correlation_id: CorrelationId | uuid.UUID
    ) -> LedgerEvent | None:
        row = await self._db.fetch_one(
            f"SELECT {_SELECT_COLUMNS} FROM system_state_ledger "
            "WHERE correlation_id = ? ORDER BY state_version DESC LIMIT 1",
            (str(correlation_id),),
        )
        return _row_to_event(row) if row else None

    async def head(self, correlation_id: CorrelationId | uuid.UUID) -> CorrelationHead | None:
        row = await self._db.fetch_one(
            "SELECT * FROM system_state_correlation_head WHERE correlation_id = ?",
            (str(correlation_id),),
        )
        return CorrelationHead(row) if row else None

    async def open_correlations(self, *, limit: int = 1000) -> list[CorrelationHead]:
        """Lineages that have not reached a terminal event.

        This is the recovery sweep's entry point. It reads the small head
        projection rather than scanning the ledger — see the ADR-0010 note in
        the schema for why the RFC's partial index does not work.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM system_state_correlation_head "
            "WHERE is_terminal = 0 ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        )
        return [CorrelationHead(r) for r in rows]

    async def events_since(
        self,
        since: datetime,
        *,
        event_types: Sequence[EventType] | None = None,
        limit: int = 10_000,
    ) -> list[LedgerEvent]:
        """Time-ranged scan. Used by the weekly reflection engine."""
        sql = f"SELECT {_SELECT_COLUMNS} FROM system_state_ledger WHERE recorded_at >= ?"
        params: list[Any] = [to_iso(since)]
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            sql += f" AND event_type IN ({placeholders})"
            params.extend(e.value for e in event_types)
        sql += " ORDER BY sequence_id ASC LIMIT ?"
        params.append(limit)
        return [_row_to_event(r) for r in await self._db.fetch_all(sql, params)]

    async def count(self) -> int:
        return int(await self._db.fetch_value("SELECT COUNT(*) FROM system_state_ledger") or 0)

    # -- integrity ---------------------------------------------------------

    async def verify_chain(
        self, correlation_id: CorrelationId | uuid.UUID
    ) -> tuple[bool, list[str]]:
        """Verify one lineage's hash chain.

        Returns ``(ok, problems)``. Checks three things per event: the stored
        digest recomputes, ``prev_hash`` matches the predecessor's digest, and
        versions are dense and monotonic. A gap means rows were deleted.
        """
        events = await self.read_correlation(correlation_id)
        problems: list[str] = []
        expected_prev = GENESIS_HASH
        expected_version = 1

        for event in events:
            where = f"v{event.state_version}/{event.event_type.value}"
            if not event.verify_digest():
                problems.append(f"{where}: digest mismatch — payload or metadata was altered")
            if event.prev_hash != expected_prev:
                problems.append(
                    f"{where}: broken chain link (expected prev {expected_prev[:12]}…, "
                    f"got {(event.prev_hash or '')[:12]}…)"
                )
            if event.state_version != expected_version:
                problems.append(
                    f"{where}: version gap — expected {expected_version}, "
                    f"got {event.state_version}"
                )
                expected_version = event.state_version
            expected_prev = event.event_hash or GENESIS_HASH
            expected_version += 1

        return (not problems), problems

    async def verify_all_chains(self) -> dict[str, list[str]]:
        """Verify every lineage. Returns correlation id -> problems, empty if clean."""
        rows = await self._db.fetch_all(
            "SELECT DISTINCT correlation_id FROM system_state_ledger"
        )
        report: dict[str, list[str]] = {}
        for row in rows:
            cid = row["correlation_id"]
            ok, problems = await self.verify_chain(uuid.UUID(cid))
            if not ok:
                report[cid] = problems
        return report

    # -- snapshots ---------------------------------------------------------

    async def save_snapshot(
        self,
        correlation_id: CorrelationId | uuid.UUID,
        state_version: int,
        projection: dict[str, Any],
    ) -> None:
        """Checkpoint a projection so replay can skip earlier events."""
        await self._db.execute(
            "INSERT OR REPLACE INTO system_state_snapshots "
            "(correlation_id, state_version, projection, created_at) VALUES (?, ?, ?, ?)",
            (str(correlation_id), state_version, dumps(projection), to_iso(utc_now())),
        )

    async def load_snapshot(
        self, correlation_id: CorrelationId | uuid.UUID
    ) -> tuple[int, dict[str, Any]] | None:
        row = await self._db.fetch_one(
            "SELECT state_version, projection FROM system_state_snapshots "
            "WHERE correlation_id = ? ORDER BY state_version DESC LIMIT 1",
            (str(correlation_id),),
        )
        if row is None:
            return None
        return int(row["state_version"]), loads(row["projection"], {})


def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
    return LedgerEvent(
        sequence_id=row["sequence_id"],
        event_id=uuid.UUID(row["event_id"]),
        correlation_id=uuid.UUID(row["correlation_id"]),
        session_id=uuid.UUID(row["session_id"]) if row["session_id"] else None,
        causation_id=uuid.UUID(row["causation_id"]) if row["causation_id"] else None,
        state_version=row["state_version"],
        attempt=row["attempt"],
        discriminator=row["discriminator"],
        event_type=EventType(row["event_type"]),
        execution_mode=row["execution_mode"],
        agent_role=row["agent_role"],
        allocated_worker_image=row["allocated_worker_image"],
        payload=loads(row["payload"], {}),
        prev_hash=row["prev_hash"],
        event_hash=row["event_hash"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]).astimezone(UTC),
    )
