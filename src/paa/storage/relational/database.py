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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite
import structlog

from paa.core.errors import StorageError
from paa.storage.relational.connection import (
    SqliteTransactionConnection,
    TransactionConnection,
    translate_sqlite_error,
)
from paa.storage.relational.dialect import SqliteDialect
from paa.storage.relational.migrations import SCHEMA_VERSION, MigrationRunner

__all__ = ["SCHEMA_VERSION", "Database", "from_iso", "to_iso", "utc_now"]

log = structlog.get_logger(__name__)

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


def from_iso(value: str | datetime) -> datetime:
    """Parse a stored timestamp.

    Accepts both shapes the same column has on the two substrates. SQLite
    stores a timestamp as TEXT and hands back the string it was given; a
    Postgres ``TIMESTAMPTZ`` column hands back a native ``datetime``. A
    repository that reads the same row through either backend must not have
    to know which substrate produced it, and :func:`datetime.fromisoformat`
    raises ``TypeError`` on a ``datetime`` — so the coercion is widened here
    rather than repeated at every read site.
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


#: The oldest SQLite this substrate supports.
#:
#: ``RETURNING`` arrived in SQLite 3.35.0 (2021-03-12), and the ledger's
#: append uses it to read back the rowid it just wrote. Without the clause
#: the append would have to select the row back by its key, which is a
#: second statement and a second race; with it, the write and the read are
#: one statement.
#:
#: So the floor is a *requirement*, not a preference: on an older library
#: every ledger append fails with a syntax error, and the failure arrives
#: as a substrate error rather than as anything that names the cause. It is
#: checked once, at connect time, and the refusal says what is wrong.
MINIMUM_SQLITE_VERSION: tuple[int, int, int] = (3, 35, 0)


def _require_supported_sqlite() -> None:
    """Refuse a SQLite too old for the statements this substrate issues.

    Checked at connect time rather than at the first failing statement:
    a syntax error in the middle of a ledger append is a substrate error
    with no hint that the library is the problem, and the operator is left
    looking at the ledger rather than at the host.
    """
    found = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:3])
    if found >= MINIMUM_SQLITE_VERSION:
        return
    raise StorageError(
        f"this SQLite is {sqlite3.sqlite_version}, and the substrate needs "
        f"{'.'.join(str(part) for part in MINIMUM_SQLITE_VERSION)} or newer: "
        "the ledger's append uses RETURNING, which arrived in 3.35.0",
        substrate="sqlite",
    )


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
        self._dialect = SqliteDialect()

    @property
    def dialect(self) -> SqliteDialect:
        """The engine's dialect — the SQL fragments that have no portable spelling.

        A repository asks the dialect for the parts of a statement that
        differ between engines, rather than branching on which engine it is
        holding. See :mod:`paa.storage.relational.dialect`.
        """
        return self._dialect

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
        _require_supported_sqlite()

        # A database that was closed and is opened again is a database that
        # was never really closed — the flag would otherwise make the second
        # ``close()`` a no-op and leak every connection this one opened.
        self._closed = False

        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._write_conn = await self._open(readonly=False)
        try:
            await self._apply_schema()
        except BaseException:
            # The connect failed, so the write connection is this object's
            # to close. Leaving it open would leak it, and leaving
            # ``_write_conn`` set would make the *next* connect a no-op
            # that reports success on a database that was never opened.
            with _suppress_close_errors():
                await self._write_conn.close()
            self._write_conn = None
            raise

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
                await self._pragma(self._write_conn, "PRAGMA wal_checkpoint(TRUNCATE)")
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
            await self._pragma(conn, "PRAGMA journal_mode=WAL")
            # NORMAL is the correct durability point under WAL: transactions are
            # still crash-safe against process death, and only lose the tail on
            # OS-level power loss — which the recovery sweep is built to repair.
            await self._pragma(conn, "PRAGMA synchronous=NORMAL")

        await self._pragma(conn, f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        await self._pragma(conn, "PRAGMA foreign_keys=ON")
        # Negative value = KiB of page cache rather than a page count. 16 MB is
        # a deliberate ceiling for a memory-constrained host.
        await self._pragma(conn, "PRAGMA cache_size=-16000")
        await self._pragma(conn, "PRAGMA temp_store=MEMORY")
        return conn

    async def _pragma(self, conn: aiosqlite.Connection, statement: str) -> None:
        """Run a PRAGMA, translating a failure.

        The PRAGMAs are the substrate's own statements rather than the
        repository layer's, but they run on the same connection and fail the
        same way — "database is locked" on the WAL pragma, an I/O error on
        the cache pragma — and a driver exception escaping from the connect
        path breaks the contract just as surely as one escaping from a read.
        """
        try:
            await conn.execute(statement)
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    async def _apply_schema(self) -> None:
        """Bring the database up to the schema version this build expects.

        The rules are shared, and they live in
        :class:`paa.storage.relational.migrations.MigrationRunner`. There
        are six outcomes, and this method is only told about one of them:
        a fresh database, whose ``.sql`` file is applied whole or not at
        all. The other five are the runner's business — a database at the
        base version (migrated forward), an incomplete one (refused by
        name), a newer one (refused), one whose version log has a gap
        (refused), and one whose log claims a shape the database does not
        have (refused).

        What this method supplies is the SQLite half of what the runner
        needs: the DDL script, applied whole or not at all, and a way to
        read a statement's rows before the read pool exists.
        """
        runner = MigrationRunner(
            engine="sqlite",
            substrate="sqlite",
            log_prefix="database",
            transport=_SqliteMigrationTransport(
                self._require_write,
                self.transaction,
                self._apply_ddl,
            ),
            log=log,
            details={"path": str(self._path)},
        )
        await runner.run()

    async def _apply_ddl(self, ddl: str) -> None:
        """Apply a DDL script — all of it, or none of it.

        ``executescript`` runs the file statement by statement in
        autocommit, so a script that fails half-way leaves behind the
        tables it had already created. That state is not a database at any
        version: it is the shape an interrupted schema application leaves,
        and the runner refuses it by name rather than migrating it.

        The script therefore carries its own transaction. It has to be
        *inside* the script: ``executescript`` commits whatever is pending
        before it runs, so a ``BEGIN`` around the call is committed out
        from under it and the ``COMMIT`` after it fails with "cannot
        commit - no transaction is active".

        A failure inside the script leaves its transaction open, and the
        rollback has to come before anything else touches the connection.
        It is issued as a plain statement rather than through
        ``executescript`` again, because that would commit the pending
        transaction first — which is the very thing being undone.
        """
        conn = self._require_write()
        async with self._write_lock:
            try:
                await conn.executescript(f"BEGIN IMMEDIATE;\n{ddl}\nCOMMIT;")
            except sqlite3.Error as exc:
                with suppress(sqlite3.Error):
                    await conn.execute("ROLLBACK")
                raise StorageError(
                    f"failed to apply schema: {exc}",
                    substrate="sqlite",
                    path=str(self._path),
                ) from exc

    # -- access ------------------------------------------------------------

    def _require_write(self) -> aiosqlite.Connection:
        if self._write_conn is None:
            raise StorageError("database is not connected", substrate="sqlite")
        return self._write_conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TransactionConnection]:
        """Exclusive write transaction.

        Commits on clean exit, rolls back on any exception. The lock makes
        SQLITE_BUSY impossible between coroutines in this process; the busy
        timeout still covers other processes touching the same file.

        Yields the common transaction connection rather than the raw
        ``aiosqlite`` one, so that a repository written against this
        backend runs unchanged against the PostgreSQL one — including the
        error vocabulary, which is translated at this boundary rather than
        in the repository.

        The transaction-control statements are substrate statements rather
        than repository ones, but they run on the same connection and fail
        the same way — a cross-process "database is locked" after the busy
        timeout, a deferred foreign key at COMMIT — so they are translated
        here too. A raw ``sqlite3`` error escaping from this boundary would
        break the contract just as surely as one escaping from a read.
        """
        async with self._write_lock:
            # The connection is taken *under* the lock, not before it. A
            # writer that arrives while another one is replacing a
            # connection that failed mid-transaction would otherwise be
            # told the database is not connected — a fault in the recovery
            # path reporting itself as a fault in the caller.
            conn = self._require_write()
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                # The transaction could not be started. Contention and a
                # bad statement have to stay distinguishable: contention is
                # worth retrying, a bad statement is not.
                raise translate_sqlite_error(exc) from exc
            try:
                yield SqliteTransactionConnection(conn)
            except BaseException:
                # The rollback must never replace the failure that caused
                # it. SQLite reports "cannot rollback - no transaction is
                # active" when the engine has already undone the
                # transaction itself, which is a normal outcome rather
                # than a fault, and raising it would hide the error the
                # caller needs to see.
                await self._rollback(conn)
                raise
            else:
                await self._commit(conn)

    async def _rollback(self, conn: aiosqlite.Connection) -> None:
        """Undo the transaction, never masking the failure that caused it.

        A rollback that fails is not the interesting failure — the
        statement that failed is — so the failure is logged and swallowed
        rather than raised over the error the caller needs to see.

        It does leave the transaction's fate unknown: SQLite reports
        "cannot rollback - no transaction is active" when the engine has
        already undone the transaction itself (which is a clean state,
        not a fault), but a rollback that fails on a lock may leave the
        transaction open. A connection handed back in that state refuses
        the next BEGIN with "cannot start a transaction within a
        transaction", so the write connection is replaced rather than
        reused.
        """
        try:
            await conn.execute("ROLLBACK")
        except Exception as exc:
            # Every exception, not just the driver's: a closed connection
            # makes aiosqlite raise ``ValueError``, which is not a
            # ``sqlite3.Error`` and would replace the failure the caller
            # needs to see — the one thing this method exists to prevent.
            log.warning(
                "database.rollback_failed", error=str(translate_sqlite_error(exc))
            )
            await self._replace_write_connection(conn)

    async def _replace_write_connection(self, stale: aiosqlite.Connection) -> None:
        """Put the write connection back into a known state.

        A connection that may still hold an open transaction is not one
        the next writer can use, and the write lock is about to be
        released — so the connection is closed and a fresh one opened in
        its place. The cost is one connection, and it is paid only after
        a fault; the alternative is a substrate that refuses every
        subsequent write with a message about a transaction nobody is
        holding.
        """
        if self._write_conn is not stale:
            return
        # The replacement is opened *before* the stale connection is
        # dropped. Dropping it first would leave a window in which there
        # is no write connection at all, and a writer arriving in that
        # window would be told the database is not connected — a fault in
        # the recovery path reporting itself as a fault in the caller.
        try:
            replacement = await self._open(readonly=False)
        except BaseException as exc:
            # The replacement could not be opened either. Leaving
            # ``_write_conn`` unset makes the next call say so plainly
            # rather than reach for a connection that may be in an unknown
            # state — and the failure is not raised, because the error
            # this is cleaning up after is the one the caller needs to
            # see.
            self._write_conn = None
            log.error("database.write_connection_replacement_failed", error=str(exc))
            return
        with _suppress_close_errors():
            await stale.close()
        self._write_conn = replacement

    async def _commit(self, conn: aiosqlite.Connection) -> None:
        """Commit, and leave the connection in a known state if that fails.

        A failed COMMIT leaves the transaction's fate ambiguous: SQLite
        keeps the transaction open for most errors and rolls it back for a
        few. Either way the connection must not be handed back with a
        transaction still open, so the undo is attempted before the failure
        is reported — and the failure is reported, because a caller that
        believes it committed when it did not is worse off than one that
        knows it did not.
        """
        try:
            await conn.execute("COMMIT")
        except sqlite3.Error as exc:
            translated = translate_sqlite_error(exc)
            await self._rollback(conn)
            raise translated from exc

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
        async with self._reader() as conn:
            try:
                async with conn.execute(sql, tuple(params)) as cur:
                    return list(await cur.fetchall())
            except sqlite3.Error as exc:
                # The read path is a substrate boundary too. A repository that
                # catches ``QueryRejected`` to fail closed has to be able to
                # catch it here too — a raw ``sqlite3.OperationalError``
                # escaping from a *read* would take down the caller that was
                # written to survive it.
                raise translate_sqlite_error(exc) from exc

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        async with self._reader() as conn:
            try:
                async with conn.execute(sql, tuple(params)) as cur:
                    return await cur.fetchone()
            except sqlite3.Error as exc:
                raise translate_sqlite_error(exc) from exc

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetch_one(sql, params)
        return row[0] if row is not None else None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a single write statement in its own transaction. Returns rowcount."""
        async with self.transaction() as conn, conn.execute(sql, tuple(params)) as cur:
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
            try:
                await conn.execute("VACUUM")
            except sqlite3.Error as exc:
                raise translate_sqlite_error(exc) from exc
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


class _SqliteMigrationTransport:
    """The SQLite half of what the shared migration runner needs.

    The runner's rules are engine-independent; what is left is how a
    statement is run outside a transaction, and how a DDL script is
    applied whole or not at all. The three callables are handed in rather
    than reached for, so this class holds no reference to the database it
    serves and cannot outlive it.

    The write connection is asked for on each read rather than captured
    once: a failed rollback replaces the write connection rather than
    handing back one that may still hold an open transaction, and a
    captured connection would then be a closed one.
    """

    __slots__ = ("_apply_ddl", "_require_write", "_transaction")

    def __init__(
        self,
        require_write: Callable[[], aiosqlite.Connection],
        transaction: Callable[[], AbstractAsyncContextManager[TransactionConnection]],
        apply_ddl: Callable[[str], Awaitable[None]],
    ) -> None:
        self._require_write = require_write
        self._transaction = transaction
        self._apply_ddl = apply_ddl

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run one statement and return its rows.

        The write connection, not the read pool: the schema is applied
        before the pool exists, and the pool is opened only once the
        schema is there.
        """
        conn = self._require_write()
        async with SqliteTransactionConnection(conn).execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        return self._transaction()

    async def apply_ddl(self, ddl: str) -> None:
        await self._apply_ddl(ddl)

    async def aclose(self) -> None:
        """Nothing to release.

        SQLite needs no migration lock: every write this backend opens is
        an exclusive one (``BEGIN IMMEDIATE``), and the busy timeout
        covers the contention with another process. The one thing a lock
        would add is the refusal to start while another process is
        migrating — which SQLite already does, by waiting.
        """
        return None


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


def loads(value: Any, default: Any = None) -> Any:
    """Read a JSON column.

    The same column has two shapes. A SQLite TEXT column holds the JSON
    text and is parsed here; a Postgres ``JSONB`` column comes back already
    parsed, and re-parsing a ``dict`` raises ``TypeError`` — which
    :class:`json.JSONDecodeError` does not catch, so the original
    fail-soft contract would not apply. Anything that is not text is
    therefore returned unchanged.
    """
    if value is None:
        return default
    if not isinstance(value, (str, bytes, bytearray)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("database.malformed_json_column", raw=str(value)[:200])
        return default
