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
:func:`_qmark_to_pyformat` for the one sharp edge — a literal ``?`` inside a
string constant). The connection pool comes from ``psycopg_pool``.

Row compatibility
-----------------
``sqlite3.Row`` supports *both* ``row["col"]`` and ``row[0]``. The whole
codebase relies on that duality — ``fetch_value`` reads ``row[0]`` while every
repository reads by name. psycopg's ``dict_row`` gives name access only, so this
module installs a :class:`_Row` factory that is a ``dict`` subclass also
answering positional integer keys, in column order.

JSON and timestamps
-------------------
PostgreSQL columns are ``JSONB`` and ``TIMESTAMPTZ`` (schema_postgres.sql), not
TEXT. The module-level :func:`dumps`/:func:`loads`/:func:`to_iso`/:func:`from_iso`
mirror the SQLite helpers' *signatures* but adapt to native types: ``dumps``
wraps a value so psycopg binds it as ``jsonb``; ``to_iso`` yields an aware
``datetime`` psycopg binds as ``timestamptz``. Reads come back already parsed
(``dict``/``list``/``datetime``), and ``loads``/``from_iso`` are tolerant of
both the native object and the SQLite string form so shared call sites work
either way.

``psycopg``/``psycopg_pool`` are imported lazily so this module stays importable
— and the package stays installable — without the ``postgres`` extra. Only
constructing or connecting a :class:`PostgresDatabase` requires them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self

import structlog

from paa.core.errors import StorageError
from paa.storage.relational.database import SCHEMA_VERSION, from_iso, to_iso, utc_now

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "PostgresDatabase",
    "dumps",
    "from_iso",
    "loads",
    "postgres_available",
    "to_iso",
    "utc_now",
]

log = structlog.get_logger(__name__)

_SUBSTRATE = "postgres"

# schema_postgres.sql lives beside this module; resolved lazily in _schema_ddl
# so importing the module never touches the filesystem.


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


# ---------------------------------------------------------------------------
# Serialisation helpers — mirror database.py's signatures, adapt to native types
# ---------------------------------------------------------------------------


def dumps(value: Any) -> Any:
    """Prepare ``value`` for binding into a ``JSONB`` column.

    Where the SQLite helper returns a compact JSON *string*, this wraps the
    value in psycopg's :class:`~psycopg.types.json.Jsonb` adapter so it binds as
    native ``jsonb``. ``None`` is passed through untouched so a nullable JSONB
    column receives SQL ``NULL`` rather than the JSON literal ``null``.
    """
    if value is None:
        return None
    _, jsonb, _ = _require_psycopg()
    return jsonb(value)


def loads(value: Any, default: Any = None) -> Any:
    """Inverse of :func:`dumps`, tolerant of both dialects.

    A ``JSONB`` read comes back already parsed (``dict``/``list``/scalar); a
    value that arrived as a SQLite-style JSON *string* is parsed here. Anything
    unparseable degrades to ``default`` with a warning, matching the SQLite
    helper's fail-soft contract for a malformed column.
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            log.warning("postgres.malformed_json_column", raw=value[:200])
            return default
    return value


class _Row(dict):
    """A mapping row that also answers positional integer access, in column order.

    Bridges the gap between psycopg (name access) and ``sqlite3.Row`` (name *and*
    index access). ``fetch_value`` reads ``row[0]``; repositories read by name;
    both must work against one row object.
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
        self._apply_schema = apply_schema
        self._pool: Any = None
        self._closed = False

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

        if self._apply_schema:
            await self._apply_schema_ddl()

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

    async def _apply_schema_ddl(self) -> None:
        current = await self._current_version()
        if current >= SCHEMA_VERSION:
            return
        ddl = _schema_ddl()
        async with self.transaction() as conn:
            await conn.execute(ddl)
            await conn.execute(
                "INSERT INTO schema_migrations(version, applied_at, description) "
                "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
                (SCHEMA_VERSION, utc_now(), "initial PAA v4.1 schema"),
            )
        log.info("postgres.schema_applied", version=SCHEMA_VERSION)

    async def _current_version(self) -> int:
        try:
            value = await self.fetch_value("SELECT MAX(version) FROM schema_migrations")
        except Exception:
            return 0  # table absent => fresh database
        return int(value) if value is not None else 0

    # -- access ------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_Cursor]:
        """All-or-nothing write transaction.

        Yields a cursor-like object whose ``execute``/``executemany`` accept the
        codebase's ``?`` placeholders. Commits on clean exit, rolls back on any
        exception. PostgreSQL enforces isolation at the connection level, so no
        application-side write lock is needed (contrast the SQLite backend).
        """
        pool = self._require_pool()
        async with pool.connection() as conn, conn.transaction():
            yield _Cursor(conn)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        pool = self._require_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_qmark_to_pyformat(sql), tuple(params))
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        pool = self._require_pool()
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_qmark_to_pyformat(sql), tuple(params))
            return await cur.fetchone()

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetch_one(sql, params)
        return row[0] if row is not None else None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a single write statement in its own transaction. Returns rowcount."""
        async with self.transaction() as conn:
            return await conn.execute(sql, tuple(params))

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
        async with pool.connection() as conn:
            await conn.execute("ANALYZE")

    async def integrity_check(self) -> bool:
        """PostgreSQL has no PRAGMA integrity_check; a successful ping stands in."""
        try:
            return (await self.fetch_value("SELECT 1")) == 1
        except Exception:  # pragma: no cover - needs a live server
            return False

    async def table_names(self) -> list[str]:
        rows = await self.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        return [r["tablename"] for r in rows]

    @property
    def dsn(self) -> str:
        return self._dsn


class _Cursor:
    """Thin adapter giving a psycopg connection the ``?``-placeholder execute API.

    The repositories call ``conn.execute(sql, params)`` and
    ``conn.executemany(sql, rows)`` with ``?`` placeholders inside a
    :meth:`PostgresDatabase.transaction` block. This wraps a live psycopg
    connection, translating placeholders and returning ``rowcount`` from
    ``execute`` to match the SQLite backend.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = await self._conn.execute(_qmark_to_pyformat(sql), tuple(params))
        return cur.rowcount

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        async with self._conn.cursor() as cur:
            await cur.executemany(_qmark_to_pyformat(sql), [tuple(r) for r in rows])


# ---------------------------------------------------------------------------


def _schema_ddl() -> str:
    from pathlib import Path

    schema_file: Path = Path(__file__).with_name("schema_postgres.sql")
    return schema_file.read_text(encoding="utf-8")


def _qmark_to_pyformat(sql: str) -> str:
    """Translate SQLite ``?`` placeholders to psycopg ``%s``.

    The codebase's SQL is written with ``?``; psycopg wants ``%s``. This walks
    the string once, leaving ``?`` characters that fall inside single- or
    double-quoted literals untouched, and doubling literal ``%`` to ``%%`` so
    psycopg's own parameter parser does not mistake them for a placeholder. It is
    a lexer, not a full SQL parser, but it correctly handles the only constructs
    this codebase's SQL contains: quoted strings and quoted identifiers.
    """
    out: list[str] = []
    quote: str | None = None
    for ch in sql:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)
