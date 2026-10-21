"""Async PostgreSQL access layer — the RFC's original relational substrate.

SPEC DEVIATION REVERSAL (docs/adr/0019): ADR-0001 replaced PostgreSQL with
embedded SQLite because the target laptop had no Docker and ~3.5 GB of RAM.
The Docker deployment restores the server. This module presents the *same*
async surface as :class:`paa.storage.relational.database.Database`
(connect/close/transaction/fetch_all/fetch_one/fetch_value/execute/
execute_many) so the ledger, repositories and queue see one contract and the
composition root swaps the implementation per ``backend_relational``.

Why psycopg 3 async, not asyncpg
--------------------------------
psycopg 3's placeholder is ``%s`` and its type adaptation is closest to the
DB-API the rest of the codebase assumes. The repositories were written against
SQLite and use ``?`` placeholders; this backend translates ``?`` -> ``%s`` on
the way through so the *same* SQL string runs on both engines (see
:func:`_qmark_to_pyformat` for the two sharp edges — a literal ``?`` inside a
string constant, and a ``?`` or an apostrophe inside a comment). The
connection pool comes from ``psycopg_pool``.

Row compatibility
-----------------
``sqlite3.Row`` supports *both* ``row["col"]`` and ``row[0]``. The whole
codebase relies on that duality — ``fetch_value`` reads ``row[0]`` while every
repository reads by name. psycopg's ``dict_row`` gives name access only, so this
module installs a :class:`_Row` factory that is a ``dict`` subclass also
answering positional integer keys, in column order.

JSON and timestamps
-------------------
PostgreSQL columns are ``TIMESTAMPTZ`` and — for the tables whose content is a
*document* rather than a *value* — ``JSONB``. The ledger's ``payload`` and the
cold lake's ``raw_payload`` are deliberately ``TEXT`` in both dialects: the
ledger hashes its payload, and the cold lake scans its raw bytes for
hostile control characters, so both need the bytes they were given rather
than a re-serialised interpretation of them. ``JSONB`` normalises key order
and numeric literals, which would make the ledger's hash chain report false
tampering and would hide an escape sequence from the scanner.

The shared helpers in :mod:`paa.storage.relational.database` — ``from_iso``,
``loads`` — accept both shapes a column can arrive in: the ``str`` SQLite
stores and the ``datetime``/``dict`` PostgreSQL hands back. A repository
reads a column; it does not execute a query about it, so the coercion lives
with the column, not with the statement.

``psycopg``/``psycopg_pool`` are imported lazily so this module stays importable
— and the package stays installable — without the ``postgres`` extra. Only
constructing or connecting a :class:`PostgresDatabase` requires them.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, NoReturn, Self, cast

import structlog

from paa.core.errors import StorageError
from paa.storage.relational.connection import Cursor, Result, TransactionConnection
from paa.storage.relational.database import from_iso, to_iso, utc_now
from paa.storage.relational.dialect import PostgresDialect
from paa.storage.relational.errors import (
    CheckViolation,
    ForeignKeyViolation,
    MalformedDocument,
    NotNullViolation,
    ParameterCountMismatch,
    QueryRejected,
    SerializationFailure,
    UniqueViolation,
)
from paa.storage.relational.migrations import SCHEMA_VERSION, MigrationRunner

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "PostgresDatabase",
    "from_iso",
    "postgres_available",
    "to_iso",
    "utc_now",
]

log = structlog.get_logger(__name__)

_SUBSTRATE = "postgres"

#: The class id of the advisory lock that serialises schema migrations.
#:
#: Advisory locks are a pair of integers: a *class* and an *object*. The
#: class is this application's own, so the lock cannot collide with
#: another application's advisory lock on the same server; the object is
#: a hash of the database's name, so two databases on one server do not
#: serialise against each other.
#:
#: It is the same mechanism the dialect's ``lock_correlation`` uses, at
#: the scope of the whole migration rather than of one row.
_MIGRATION_LOCK_CLASS = 0x5041414D  # "PAAM"

# schema_postgres.sql lives beside this module; the migration runner reads it
# through paa.storage.relational.migrations, so importing this module never
# touches the filesystem.


def postgres_available() -> bool:
    """Whether the ``psycopg`` package is importable. Used to skip tests."""
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def _require_psycopg() -> tuple[Any, Any, Any]:
    """Import psycopg on demand, with an actionable failure."""
    try:
        import psycopg
        from psycopg.types.json import Jsonb
        from psycopg_pool import AsyncConnectionPool
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise StorageError(
            "the postgres backend requires the 'postgres' extra "
            "(pip install 'paa[postgres]')",
            substrate=_SUBSTRATE,
        ) from exc
    return psycopg, Jsonb, AsyncConnectionPool


#: PostgreSQL's SQLSTATEs, mapped to the neutral exception each means.
#:
#: The distinction between them is the point of the mapping. The ledger has
#: two unique constraints that mean different things: a duplicate
#: idempotency key is a suppressed retry, and a duplicate ``state_version``
#: is a concurrency collision. Collapsing both into one exception would turn
#: a benign retry into a hard failure.
_POSTGRES_CONSTRAINTS: dict[str, type[StorageError]] = {
    "23505": UniqueViolation,
    "23514": CheckViolation,
    "23503": ForeignKeyViolation,
    "23502": NotNullViolation,
}

#: SQLSTATEs that mean "retry this transaction", not "this statement is wrong".
#:
#: ``25P02`` is in this set for a particular reason: it is what the server
#: says when a statement is attempted inside a transaction that an earlier
#: failure has already aborted. It is not a statement about the statement
#: that was attempted — it is a statement about the transaction it was
#: attempted in — and the remedy is the same as for a serialization
#: failure: roll the transaction back and try again.
_POSTGRES_RETRYABLE: frozenset[str] = frozenset({"40001", "40P01", "25P02", "53300"})

#: SQLSTATEs that mean the statement could not be attempted at all.
#:
#: A missing table, a missing function, a missing column, a syntax error —
#: the engine refused the statement before it ran it.
_POSTGRES_UNDEFINED: frozenset[str] = frozenset(
    {
        "42P01",  # undefined_table
        "42883",  # undefined_function
        "42703",  # undefined_column
        "42601",  # syntax_error
    }
)


#: SQLSTATEs that mean a *value* the engine could not read.
#:
#: ``22P02 invalid_text_representation`` is here rather than in the
#: query-rejected set because it is the *data's* fault: the JSON columns are
#: text on both engines and the dialect casts them back to ``jsonb`` to expand
#: them, so a column holding something that is not JSON raises it. A row that
#: is corrupt is not the same problem as a query that is wrong, and the
#: vocabulary says so.
_POSTGRES_MALFORMED: frozenset[str] = frozenset(
    {
        "22P02",  # invalid_text_representation
    }
)


#: The markers psycopg's own wording for a placeholder/parameter mismatch.
#:
#: "the query has 2 placeholders but 1 parameters were passed". Both markers
#: have to be present, because psycopg's ``ProgrammingError`` also covers
#: "incomplete placeholder" and the cross-thread guard — neither of which is
#: a count disagreement.
_POSTGRES_PLACEHOLDER_COUNT_MARKERS: tuple[str, ...] = ("placeholders", "parameters")


def _is_programming_error(exc: BaseException) -> bool:
    """Whether the driver refused the statement as malformed.

    Matched by the DB-API class *name* rather than by import, because
    ``psycopg`` is an optional extra and cannot be imported at module scope.
    The name ``ProgrammingError`` is stable across psycopg versions, and
    the whole ``Exception`` MRO is searched so a subclass is still recognised.
    """
    return any(cls.__name__ == "ProgrammingError" for cls in type(exc).__mro__)


def _is_driver_error(exc: BaseException) -> bool:
    """Whether an exception came from the driver rather than from us.

    The distinction matters at the *transaction* boundary, which is the one
    place where the boundary is wider than the statement: the block it
    wraps contains the repository's own code, so an exception raised there
    may be a driver error, one of our own neutral errors, or a plain
    programming error in the repository — a ``TypeError``, say, or the
    ledger's own ``LedgerError``.

    Translating the last two would be wrong, and not only cosmetically: a
    caller that catches ``LedgerError`` to recognise a lost race would not
    catch a ``StorageError`` instead, so the two substrates would report
    the *same* domain failure with different exception types. And a
    programming error reported as an infrastructure fault sends the
    operator looking in the wrong place.

    A driver error is recognised by its SQLSTATE when it has one, and
    otherwise by the module it came from — ``psycopg`` or ``psycopg_pool``.
    The module is checked rather than the class name because the class
    names are not ours to choose (``ProgrammingError`` is a name a
    repository class could also carry) while the module of a driver error
    is, and a repository's own exceptions live under ``paa``.
    """
    if getattr(exc, "sqlstate", None) is not None:
        return True
    return type(exc).__module__.partition(".")[0] in {"psycopg", "psycopg_pool"}


def _is_placeholder_count_mismatch(exc: BaseException) -> bool:
    """Whether the driver refused the statement because the counts disagree.

    This is the one class of statement bug the ``?``-to-``%s`` translator can
    introduce, and it has to be classified as a rejected query: the only
    fail-closed handler in the repository layer catches
    :class:`~paa.storage.relational.errors.QueryRejected`, and a miscounted
    placeholder would otherwise take the whole ingestion batch down.
    """
    if not _is_programming_error(exc):
        return False
    message = str(exc).lower()
    return all(marker in message for marker in _POSTGRES_PLACEHOLDER_COUNT_MARKERS)


def _translate_postgres_error(exc: Exception) -> Exception:
    """Turn a psycopg error into a backend-neutral one.

    The SQLSTATE is the portable part of the driver's error: the exception
    *class* differs between psycopg versions, but the code does not.

    An error with no SQLSTATE — psycopg's own ``DataError`` for a NUL byte
    in a bound string, for instance — is wrapped rather than classified. It
    is not a driver error a repository should have to know about, and the
    original is the ``__cause__``.

    The translation is **idempotent**: an error that is already one of ours
    is returned unchanged. The transaction boundary catches everything the
    body raises — including the neutral errors the statement boundary has
    already produced — and without this a unique violation would be
    flattened into a vaguer storage error on its way out.
    """
    if isinstance(exc, StorageError):
        return exc
    code = getattr(exc, "sqlstate", None)
    if code is None:
        if _is_placeholder_count_mismatch(exc):
            return ParameterCountMismatch(
                f"query rejected: {exc}", substrate=_SUBSTRATE
            )
        return StorageError(f"postgres error: {exc}", substrate=_SUBSTRATE)
    kind = _POSTGRES_CONSTRAINTS.get(code)
    if kind is not None:
        return kind(f"constraint violated: {exc}", substrate=_SUBSTRATE, sqlstate=code)
    if code in _POSTGRES_RETRYABLE:
        return SerializationFailure(
            f"transaction could not proceed: {exc}", substrate=_SUBSTRATE, sqlstate=code
        )
    if code in _POSTGRES_MALFORMED:
        return MalformedDocument(
            f"malformed document: {exc}", substrate=_SUBSTRATE, sqlstate=code
        )
    if code in _POSTGRES_UNDEFINED:
        return QueryRejected(
            f"query rejected: {exc}", substrate=_SUBSTRATE, sqlstate=code
        )
    return StorageError(f"postgres error: {exc}", substrate=_SUBSTRATE, sqlstate=code)


def _raise_translated(exc: Exception) -> NoReturn:
    """Re-raise a driver error in the neutral vocabulary.

    A bare ``raise`` re-raises the exception currently being handled, so an
    error that is *already* neutral — because the statement boundary
    translated it — keeps its type and its traceback instead of being
    flattened into a vaguer storage error. The transaction boundary catches
    everything the body raises, so without this the body of a transaction
    could not tell a unique violation from any other failure.
    """
    translated = _translate_postgres_error(exc)
    if translated is exc:
        raise
    raise translated from exc


def _raise_driver_error(exc: Exception) -> NoReturn:
    """Re-raise, translating a *driver* error and nothing else.

    This is the boundary that wraps a block containing the repository's own
    code — a transaction body, a read that maps rows — so the exception it
    catches may be the driver's, one of ours, or a plain programming error
    in the repository.

    Only the driver's is translated. An exception that is already one of
    ours keeps its type (and its traceback), and an exception that is not
    ours at all is re-raised unchanged: a ``LedgerError`` that a caller
    catches to recognise a lost race must not arrive as a ``StorageError``,
    and a ``TypeError`` that is a bug in the repository must not be
    reported as an infrastructure fault. The two substrates would then
    disagree about the same failure, which is the one thing this
    vocabulary exists to prevent.
    """
    if isinstance(exc, StorageError) or not _is_driver_error(exc):
        raise
    translated = _translate_postgres_error(exc)
    if translated is exc:
        raise
    raise translated from exc


def _database_identity(dsn: str) -> str:
    """The database's name, for a refusal that says which database it is about.

    A server holds several databases, and a refusal that does not say
    which one it is about sends the operator to the wrong place. Only the
    *name* is taken: the DSN carries a password, and a refusal is
    something an operator reads out loud.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict

        return str(conninfo_to_dict(dsn).get("dbname") or "unknown")
    except Exception:  # pragma: no cover - a DSN psycopg itself refused
        return "unknown"


class _Row(dict[str, Any]):
    """A mapping row that also answers positional integer access, in column order.

    Bridges the gap between psycopg (name access) and ``sqlite3.Row`` (name *and*
    index access). ``fetch_value`` reads ``row[0]``; repositories read by name;
    both must work against one row object.

    The value type is ``Any`` on purpose: a column's value type is the
    *table's* business, not this row adapter's, and the repository layer
    coerces a column as it reads it (see ``from_iso`` and ``loads``). Naming
    the parameters keeps the row a *typed* mapping rather than an untyped
    one, without pretending to know what a given column holds.
    """

    __slots__ = ()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            try:
                return list(self.values())[key]
            except IndexError as exc:  # pragma: no cover - defensive
                raise IndexError(f"row has no column at index {key}") from exc
        return super().__getitem__(key)


def _row_factory(cursor: Any) -> Any:
    """psycopg row factory producing :class:`_Row` objects."""
    description = cursor.description
    names = [col.name for col in description] if description else []

    def make(values: Sequence[Any]) -> _Row:
        return _Row(zip(names, values, strict=False))

    return make


class PostgresDatabase:
    """Owns the process's PostgreSQL connection pool.

    API-compatible with :class:`paa.storage.relational.database.Database`. Unlike
    SQLite there is no single-writer constraint, so the SQLite backend's split
    into one write connection plus a read pool collapses into a single pool here:
    PostgreSQL's MVCC lets readers and the writer proceed concurrently without
    the application serialising them. ``transaction`` still exists and still
    means "all-or-nothing", it just no longer needs a process-wide lock.

    Usage mirrors :class:`Database`::

        db = PostgresDatabase(dsn)
        await db.connect()
        try:
            async with db.transaction() as tx:
                await tx.execute("INSERT INTO ... VALUES (?, ?)", (a, b))
        finally:
            await db.close()
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        apply_schema: bool = True,
    ) -> None:
        if not dsn:
            raise StorageError("postgres backend requires a DSN", substrate=_SUBSTRATE)
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._apply_schema_enabled = apply_schema
        self._pool: Any = None
        self._closed = False
        self._dialect = PostgresDialect()

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the pool and apply the schema. Idempotent."""
        if self._pool is not None:
            return
        # A database that was closed and is opened again is a database that
        # was never really closed — the flag would otherwise make the second
        # ``close()`` a no-op and leak the whole pool.
        self._closed = False
        _, _, async_pool = _require_psycopg()

        # open=False + explicit open() keeps construction side-effect-free and
        # gives a single place to translate a connection failure into a
        # StorageError rather than leaking psycopg's own exception type.
        self._pool = async_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            kwargs={"autocommit": True, "row_factory": _row_factory},
        )
        try:
            await self._pool.open(wait=True)
        except Exception as exc:  # pragma: no cover - needs a live server
            self._pool = None
            raise StorageError(
                f"could not connect to postgres: {exc}", substrate=_SUBSTRATE
            ) from exc

        if self._apply_schema_enabled:
            try:
                await self._apply_schema()
            except BaseException:
                # The connect failed, so the pool is this object's to close.
                # Leaving it open would leak it, and leaving ``_pool`` set
                # would make the *next* connect a no-op that reports success
                # on a database that was never opened.
                with contextlib.suppress(Exception):
                    await self._pool.close()
                self._pool = None
                raise

        log.info("postgres.connected", schema_version=SCHEMA_VERSION, pool=self._max_size)

    async def close(self) -> None:
        if self._closed or self._pool is None:
            return
        self._closed = True
        try:
            await self._pool.close()
        finally:
            self._pool = None
        log.info("postgres.closed")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise StorageError("database is not connected", substrate=_SUBSTRATE)
        return self._pool

    async def _apply_schema(self) -> None:
        """Bring the database up to the schema version this build expects.

        The rules are shared, and they live in
        :class:`paa.storage.relational.migrations.MigrationRunner`. There
        are six outcomes, and this method is only told about one of them:
        a fresh database, whose ``.sql`` file is applied whole or not at
        all. The other five are the runner's business — a database at the
        base version (migrated forward), an incomplete one (refused by
        name), a newer one (refused), one whose version log has a gap
        (refused), and one whose version log claims a shape the database
        does not have (refused).

        What this method supplies is the PostgreSQL half: the DDL script,
        applied in one transaction, and a way to read.
        """
        runner = MigrationRunner(
            engine="postgres",
            substrate=_SUBSTRATE,
            log_prefix="postgres",
            transport=_PostgresMigrationTransport(self),
            log=log,
            details={"database": _database_identity(self._dsn)},
        )
        await runner.run()

    # -- access ------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TransactionConnection]:
        """All-or-nothing write transaction.

        Yields a cursor-like object whose ``execute``/``executemany`` accept the
        codebase's ``?`` placeholders. Commits on clean exit, rolls back on any
        exception. PostgreSQL enforces isolation at the connection level, so no
        application-side write lock is needed (contrast the SQLite backend).

        The type is the *common* transaction connection rather than this
        module's own adapter, so that the two backends present the same
        contract to the repository layer — and so that the code that drives a
        transaction is type-checked against one shape rather than two.

        The whole block is inside the translation boundary, because the
        COMMIT happens in the transaction context manager's ``__aexit__`` —
        outside any statement — and a deferred constraint violation or a
        ``40001`` at COMMIT is exactly as real as one at the statement that
        provoked it.

        But the boundary is the *driver's* boundary and no wider: the block
        contains the repository's own code, and an exception raised there
        may be the repository's rather than the driver's. Those keep their
        own types — a ``LedgerError`` stays a ``LedgerError`` on this
        substrate as it does on SQLite, and a ``TypeError`` stays a
        ``TypeError`` rather than being reported as an infrastructure
        fault. See :func:`_raise_driver_error`.
        """
        pool = self._require_pool()
        try:
            async with pool.connection() as conn, conn.transaction():
                yield _Cursor(conn)
        except Exception as exc:
            _raise_driver_error(exc)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        pool = self._require_pool()
        try:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_qmark_to_pyformat(sql), tuple(params))
                return list(await cur.fetchall())
        except Exception as exc:
            # The read path is a substrate boundary too. A repository that
            # catches ``QueryRejected`` to fail closed has to be able to
            # catch it here too — a raw psycopg error escaping from a
            # *read* would take down the caller that was written to
            # survive it. The pool acquisition is inside the boundary for
            # the same reason: ``psycopg_pool.PoolTimeout`` is raised
            # before a cursor exists, and it has no SQLSTATE, so it only
            # becomes a neutral error here.
            #
            # The boundary is the driver's, and no wider: mapping a row is
            # the repository's own code, and a bug in it must not be
            # reported as a storage failure.
            _raise_driver_error(exc)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        pool = self._require_pool()
        try:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_qmark_to_pyformat(sql), tuple(params))
                return await cur.fetchone()
        except Exception as exc:
            _raise_driver_error(exc)

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

    async def analyze(self) -> None:
        """Refresh the planner's statistics. VACUUM cannot run in a transaction."""
        pool = self._require_pool()
        try:
            # A cursor, not ``conn.execute``: the statement-boundary form
            # hands back a cursor the caller owns, and this one had no owner
            # to close it.
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("ANALYZE")
        except Exception as exc:
            _raise_translated(exc)

    async def integrity_check(self) -> bool:
        """PostgreSQL has no PRAGMA integrity_check; a successful ping stands in."""
        try:
            value = await self.fetch_value("SELECT 1")
            # The ping answered if the server said 1. ``fetch_value`` hands
            # back whatever the driver returned — it is deliberately untyped,
            # because the column it read is the caller's to interpret — so
            # the comparison is *bound* to a bool here instead of being
            # returned as the driver's own ``Any``.
            answered: bool = value == 1
            return answered
        except Exception:  # pragma: no cover - needs a live server
            return False

    async def table_names(self) -> list[str]:
        """The tables of the schema an unqualified name resolves to.

        The schema is the one the connection's search path resolves to,
        which is the same one the migration runner resolves and the same
        one the guards ask about. Asking about ``public`` instead would
        answer about a schema that may hold nothing at all: under
        ``search_path = paa, public`` the runtime's tables are in
        ``paa``, and a list that said otherwise would be a lie about the
        database it was asked about.
        """
        rows = await self.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
            "ORDER BY tablename"
        )
        return [r["tablename"] for r in rows]

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def dialect(self) -> PostgresDialect:
        """The engine's dialect — the SQL fragments that have no portable spelling.

        A repository asks the dialect for the parts of a statement that
        differ between engines, rather than branching on which engine it is
        holding. See :mod:`paa.storage.relational.dialect`.
        """
        return self._dialect

    @property
    def path(self) -> Path:
        """The SQLite file path — *not* available on a server substrate.

        A server backend has no single file: a backup is ``pg_dump``, not a
        copy. Raising here rather than returning ``None`` is deliberate — a
        caller that reaches for ``db.path`` is about to do something the
        substrate cannot support, and it should be told plainly rather than
        handed a ``None`` that fails three lines later with a less useful
        message.
        """
        raise AttributeError(
            "the postgres backend has no single database file; use pg_dump for a backup"
        )


class _PostgresMigrationTransport:
    """The PostgreSQL half of what the shared migration runner needs.

    The runner's rules are engine-independent; what is left is how a
    statement is run outside a transaction, and how a DDL script is
    applied whole or not at all.

    The migration runs on a **dedicated** connection rather than on a
    pooled one, and the reason is the schema file. Its ``CREATE TABLE``
    statements are unqualified, so they land in whichever schema the
    connection's ``search_path`` resolves to — which is why the runner
    resolves the schema on the connection it applies the file on. A
    pooled connection cannot be that connection: ``psycopg_pool`` does
    not reset a connection's session state when it hands it back, so a
    ``SET search_path`` on any connection in the pool would move the
    resolution and the DDL together, into a schema nobody chose.

    The dedicated connection also holds the **migration lock** — a
    PostgreSQL advisory lock, taken for the whole migration. Two
    processes starting at once is not a hypothetical: the loser used to
    fail its start-up, because it re-ran a step the winner was running at
    the same time and the two raced each other onto an identity column.
    The lock makes the second process wait instead.
    """

    __slots__ = ("_conn", "_db")

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db
        self._conn: Any = None

    async def _connection(self) -> Any:
        """The connection the migration runs on, opened on first use.

        Opening it also takes the migration lock: the lock is
        session-scoped, so it is held until the connection is closed, and
        the connection is closed when the migration is over.
        """
        if self._conn is None:
            self._conn = await self._connect()
            await self._lock()
        return self._conn

    async def _connect(self) -> Any:
        import psycopg

        try:
            return await psycopg.AsyncConnection.connect(
                self._db.dsn, autocommit=True, row_factory=_row_factory
            )
        except Exception as exc:
            _raise_translated(exc)

    async def _lock(self) -> None:
        """Take the advisory lock that serialises migrations.

        Two integers rather than one: the first is this application's
        own class id, so the lock cannot collide with another
        application's, and the second is a hash of the database's name,
        so two databases on the same server do not serialise against
        each other.

        The lock is session-scoped and is released when the connection
        closes, which is what makes a refusal safe: a process that gives
        up does not leave the database locked.
        """
        conn = self._conn
        await conn.execute(
            "SELECT pg_advisory_lock(%s, hashtext(current_database()))",
            (_MIGRATION_LOCK_CLASS,),
        )

    async def read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run one statement and return its rows.

        A failure to read is translated at the substrate boundary, so the
        runner sees the neutral vocabulary: a missing table is a
        ``QueryRejected``, and every other failure is itself.

        The read runs outside a write transaction, on the migration's own
        connection. It is finished before any transaction is opened, so
        the two are never held at once and there is nothing to deadlock
        over; and the migration lock is held throughout, so a read cannot
        be stale in the way that matters — another process cannot be
        inside the migration at the same time.
        :meth:`paa.storage.relational.migrations.MigrationRunner._version_rows`
        spells the argument out in full.
        """
        conn = await self._connection()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_qmark_to_pyformat(sql), tuple(params))
                return list(await cur.fetchall())
        except Exception as exc:
            _raise_translated(exc)

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[TransactionConnection]:
        conn = await self._connection()
        try:
            async with conn.transaction():
                yield _Cursor(conn)
        except Exception as exc:
            _raise_translated(exc)

    async def apply_ddl(self, ddl: str) -> None:
        """Apply the DDL script in one transaction.

        PostgreSQL's DDL is transactional, so the whole file is one
        statement inside one transaction: either every table is created or
        none is, and a script that fails leaves the database exactly as it
        was.

        The extension is installed *before* the file's own statement, and
        inside a savepoint that can be rolled back. ``CREATE EXTENSION IF
        NOT EXISTS`` is not concurrency-safe: two sessions that both find
        the extension missing both try to create it, and the loser gets a
        unique violation on ``pg_extension_name_index`` — which, inside a
        transaction, would abort the whole schema application. The
        savepoint makes that violation survivable: the extension is
        simply already there.
        """
        conn = await self._connection()
        try:
            async with conn.transaction():
                await self._ensure_extension(conn)
                await conn.execute(_qmark_to_pyformat(ddl))
        except Exception as exc:
            _raise_translated(exc)

    async def _ensure_extension(self, conn: Any) -> None:
        """Install ``pg_trgm``, tolerating a concurrent installation."""
        await conn.execute("SAVEPOINT paa_extension")
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except Exception as exc:
            translated = _translate_postgres_error(exc)
            if not isinstance(translated, UniqueViolation):
                raise translated from exc
            # Another session installed it between our check and ours.
            await conn.execute("ROLLBACK TO SAVEPOINT paa_extension")
        await conn.execute("RELEASE SAVEPOINT paa_extension")

    async def aclose(self) -> None:
        """Close the migration's connection, which releases the lock."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.close()


class _Cursor:
    """The PostgreSQL side of the common transaction contract.

    The repositories call ``conn.execute(sql, params)`` and
    ``conn.executemany(sql, rows)`` with ``?`` placeholders inside a
    :meth:`PostgresDatabase.transaction` block, and expect the *same* shape
    the SQLite backend presents — a cursor they can fetch from, reachable
    both as ``async with conn.execute(...) as cur:`` and as
    ``cur = await conn.execute(...)``.

    psycopg's own ``execute`` is a coroutine returning a cursor, and its
    placeholders are ``%s``; this adapter supplies the missing half of the
    contract, translates the placeholders, and translates the driver's
    errors into the neutral ones so a repository never imports psycopg.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        return Result(self._execute(sql, params))

    async def _execute(self, sql: str, params: Sequence[Any]) -> Cursor:
        try:
            cur = await self._conn.execute(_qmark_to_pyformat(sql), tuple(params))
        except Exception as exc:
            _raise_translated(exc)
        return cast(Cursor, cur)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        try:
            async with self._conn.cursor() as cur:
                await cur.executemany(_qmark_to_pyformat(sql), [tuple(r) for r in rows])
        except Exception as exc:
            _raise_translated(exc)


# ---------------------------------------------------------------------------


def _qmark_to_pyformat(sql: str) -> str:
    """Translate SQLite ``?`` placeholders to psycopg's ``%s``.

    The codebase's SQL is written with ``?``; psycopg wants ``%s``. This walks
    the string once, leaving ``?`` characters that fall inside single- or
    double-quoted literals — or inside a comment — untouched, and doubling
    every literal ``%`` so psycopg's own parameter parser does not mistake
    them for a placeholder.

    The ``%`` and the ``?`` are treated differently on purpose. A ``?`` inside
    a quoted literal is part of a *string* on both engines, so it is not a
    placeholder anywhere. A ``%`` is not: psycopg's parser scans the whole
    query for placeholder markers, quoted literals included, so a ``%`` inside
    a literal has to be doubled whether or not it is inside quotes. Doubling
    it is what psycopg expects — it un-doubles the escape before sending the
    query, so the server sees the character that was written.

    Comments are skipped for the same reason a quoted literal is: a ``?`` in
    a comment is not a placeholder, and neither engine parses the comment.
    It matters more than it looks. An apostrophe inside a comment would
    otherwise be read as the start of a literal and every later ``?`` in the
    statement would go unconverted — which is a statement psycopg would
    refuse, because the placeholder count and the parameter count would
    disagree. The ``%`` inside a comment is still doubled, because psycopg
    scans the raw bytes and would otherwise count it as a placeholder.

    It is a lexer, not a full SQL parser, but it correctly handles the only
    constructs this codebase's SQL contains: quoted strings, quoted
    identifiers, and comments.

    Three constructs it does **not** handle are recorded here so that
    nobody "fixes" the wrong thing. None is reachable from this
    repository's SQL — no ``$$``, no ``E'``, no nested block comments —
    and each would make the placeholder count disagree with PostgreSQL's
    own:

    * a **nested** block comment (``/* a /* b */ c ? */``) — PostgreSQL
      nests them and the whole thing is a comment; this lexer ends the
      comment at the first ``*/`` and reads the ``?`` as a placeholder;
    * an **escape string** (``E'a\\' ? b'``) — PostgreSQL reads ``\\'`` as
      an escaped quote, so the ``?`` is inside the literal; this lexer
      does not treat a backslash as an escape, so it closes the literal
      early and reads the ``?`` as a placeholder;
    * a **dollar-quoted** literal (``$$a ? b$$``) — PostgreSQL reads the
      whole thing as a literal; this lexer reads the ``?`` as a
      placeholder.
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        ch = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""
        if quote is not None:
            # Inside a literal: a ``?`` is a character, but a ``%`` is still
            # a placeholder marker as far as psycopg's parser is concerned.
            if ch == "%":
                out.append("%%")
            else:
                out.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            index += 1
            continue
        if ch == "-" and nxt == "-":
            # A line comment runs to the end of the line. Neither engine
            # parses it, so neither does a ``?`` in it mean a placeholder.
            end = sql.find("\n", index)
            end = length if end == -1 else end
            out.append(_comment_body(sql[index:end]))
            index = end
            continue
        if ch == "/" and nxt == "*":
            # A block comment runs to the matching ``*/``. An unterminated
            # one runs to the end of the statement, which is what both
            # engines do with it too.
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(_comment_body(sql[index:end]))
            index = end
            continue
        if ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        index += 1
    return "".join(out)


def _comment_body(text: str) -> str:
    """A comment, copied verbatim except that every ``%`` is doubled.

    psycopg's own parser scans the whole query for placeholder markers,
    comments included, so a bare ``%`` in a comment would be read as one.
    Doubling it is what psycopg expects — it un-doubles the escape before
    sending the query — and it leaves the comment itself unchanged.
    """
    return text.replace("%", "%%")
