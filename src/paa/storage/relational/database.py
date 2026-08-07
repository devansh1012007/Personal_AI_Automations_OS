"""Async SQLite access layer.

Concurrency model
-----------------
SQLite in WAL mode permits one writer concurrent with many readers. That maps
to a dedicated write connection guarded by an :class:`asyncio.Lock`, plus a
pool of read-only connections. Attempting to share one connection for both
would serialise reads behind the curator's long write transactions.

All timestamps are stored as ISO-8601 UTC strings. They sort lexicographically,
which is what lets the ledger's ``ORDER BY recorded_at`` work without a
dedicated numeric column.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite
import structlog

from paa.core.errors import StorageError

__all__ = ["SCHEMA_VERSION", "Database", "from_iso", "to_iso", "utc_now"]

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1
_SCHEMA_FILE = Path(__file__).with_name("schema_sqlite.sql")

#: Read connections. Small: this is a single-user runtime, and each connection
#: costs a page cache.
_READ_POOL_SIZE = 4


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Serialise a timestamp for storage. Rejects naive datetimes."""
    if value.tzinfo is None:
        raise ValueError("refusing to store a naive datetime; attach a timezone")
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Database:
    """Owns every SQLite connection in the process.

    Usage::

        db = Database(path)
        await db.connect()
        try:
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO ...", (...))
        finally:
            await db.close()

    Or as an async context manager, which connects and closes for you.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = 10_000,
        read_pool_size: int = _READ_POOL_SIZE,
    ) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._read_pool_size = read_pool_size

        self._write_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._read_pool: asyncio.Queue[aiosqlite.Connection] | None = None
        self._all_reads: list[aiosqlite.Connection] = []
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open connections and apply the schema. Idempotent."""
        if self._write_conn is not None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._write_conn = await self._open(readonly=False)
        await self._apply_schema(self._write_conn)

        # Readers are opened only after the schema exists — a read-only
        # connection against a nonexistent file fails rather than creating it.
        self._read_pool = asyncio.Queue(maxsize=self._read_pool_size)
        for _ in range(self._read_pool_size):
            conn = await self._open(readonly=True)
            self._all_reads.append(conn)
            self._read_pool.put_nowait(conn)

        log.info(
            "database.connected",
            path=str(self._path),
            schema_version=SCHEMA_VERSION,
            read_pool=self._read_pool_size,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for conn in self._all_reads:
            with _suppress_close_errors():
                await conn.close()
        self._all_reads.clear()
        self._read_pool = None

        if self._write_conn is not None:
            with _suppress_close_errors():
                # Fold the WAL back into the main database so a copy of the
                # single .db file is a complete backup.
                await self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await self._write_conn.close()
            self._write_conn = None

        log.info("database.closed", path=str(self._path))

    async def _open(self, *, readonly: bool) -> aiosqlite.Connection:
        if readonly:
            uri = f"file:{self._path.as_posix()}?mode=ro"
            conn = await aiosqlite.connect(uri, uri=True, isolation_level=None)
        else:
            conn = await aiosqlite.connect(self._path, isolation_level=None)

        conn.row_factory = sqlite3.Row

        # journal_mode is a database-level property, so it is only meaningful
        # from the writer; the rest are per-connection.
        if not readonly:
            await conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL is the correct durability point under WAL: transactions are
            # still crash-safe against process death, and only lose the tail on
            # OS-level power loss — which the recovery sweep is built to repair.
            await conn.execute("PRAGMA synchronous=NORMAL")

        await conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        await conn.execute("PRAGMA foreign_keys=ON")
        # Negative value = KiB of page cache rather than a page count. 16 MB is
        # a deliberate ceiling for a memory-constrained host.
        await conn.execute("PRAGMA cache_size=-16000")
        await conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    async def _apply_schema(self, conn: aiosqlite.Connection) -> None:
        current = await self._current_version(conn)
        if current >= SCHEMA_VERSION:
            return

        ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
        try:
            await conn.executescript(ddl)
        except sqlite3.Error as exc:  # pragma: no cover - schema is tested
            raise StorageError(
                f"failed to apply schema: {exc}", substrate="sqlite", path=str(self._path)
            ) from exc

        await conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (SCHEMA_VERSION, to_iso(utc_now()), "initial PAA v4.1 schema"),
        )
        log.info("database.schema_applied", version=SCHEMA_VERSION)

    async def _current_version(self, conn: aiosqlite.Connection) -> int:
        try:
            async with conn.execute("SELECT MAX(version) FROM schema_migrations") as cur:
                row = await cur.fetchone()
        except sqlite3.OperationalError:
            return 0  # table absent => fresh database
        return int(row[0]) if row and row[0] is not None else 0

    # -- access ------------------------------------------------------------

    def _require_write(self) -> aiosqlite.Connection:
        if self._write_conn is None:
            raise StorageError("database is not connected", substrate="sqlite")
        return self._write_conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Exclusive write transaction.

        Commits on clean exit, rolls back on any exception. The lock makes
        SQLITE_BUSY impossible between coroutines in this process; the busy
        timeout still covers other processes touching the same file.
        """
        conn = self._require_write()
        async with self._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._read_pool is None:
            raise StorageError("database is not connected", substrate="sqlite")
        conn = await self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put_nowait(conn)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        async with self._reader() as conn, conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        async with self._reader() as conn, conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetch_one(sql, params)
        return row[0] if row is not None else None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a single write statement in its own transaction. Returns rowcount."""
        async with self.transaction() as conn:
            cur = await conn.execute(sql, tuple(params))
            return cur.rowcount

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        batch = [tuple(r) for r in rows]
        if not batch:
            return
        async with self.transaction() as conn:
            await conn.executemany(sql, batch)

    # -- maintenance -------------------------------------------------------

    async def vacuum(self) -> None:
        """Reclaim space. Runs outside a transaction, as SQLite requires."""
        conn = self._require_write()
        async with self._write_lock:
            await conn.execute("VACUUM")
        log.info("database.vacuumed")

    async def analyze(self) -> None:
        """Refresh the query planner's statistics."""
        async with self.transaction() as conn:
            await conn.execute("ANALYZE")

    async def integrity_check(self) -> bool:
        result = await self.fetch_value("PRAGMA integrity_check")
        return result == "ok"

    async def table_names(self) -> list[str]:
        rows = await self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r["name"] for r in rows]

    @property
    def path(self) -> Path:
        return self._path


class _suppress_close_errors:
    """Swallow teardown errors so one bad connection cannot block shutdown."""

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            log.warning("database.close_error", error=str(exc))
        return True


def dumps(value: Any) -> str:
    """JSON for storage columns. Compact; ordering does not matter here."""
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        log.warning("database.malformed_json_column", raw=value[:200])
        return default
