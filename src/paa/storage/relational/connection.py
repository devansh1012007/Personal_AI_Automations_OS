"""The connection contract both relational backends present inside a transaction.

A repository that writes inside a transaction gets an object it can call
``execute`` on, with ``?`` placeholders, and gets back a cursor it can fetch
from. That is all it needs, and it is the same on both engines — which is
the only way the repository layer can be written once and run on either.

The two engines do not offer that shape natively, and the difference is
small enough to hide:

* ``aiosqlite`` returns a ``Result`` that is *both* awaitable and an async
  context manager, and yields an ``aiosqlite.Cursor``.
* ``psycopg`` returns a coroutine that yields an ``AsyncCursor``, with ``%s``
  placeholders and no ``?`` translation.

:class:`Result` below is the shape both backends present, so a repository
never has to know which one it is holding. The call sites already use the
``async with conn.execute(...) as cur:`` form, because that is what
``aiosqlite`` taught them; the contract adopts that form rather than
inventing a new one.

Error translation happens here too, at the boundary. The drivers report
failures in their own vocabulary — ``sqlite3.IntegrityError`` and
``psycopg.errors.UniqueViolation`` — and a repository that catches either
would have to import the driver. ``src/paa/ledger/store.py`` is imported on
every laptop install, where psycopg is not installed at all, so the
translation has to happen *before* the exception reaches a repository. See
:mod:`paa.storage.relational.errors` for why the distinction between the
kinds of constraint violation matters.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any, Final, Protocol, cast, runtime_checkable

from paa.core.errors import StorageError
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

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable, Sequence

__all__ = [
    "Cursor",
    "Result",
    "SqliteTransactionConnection",
    "TransactionConnection",
    "translate_sqlite_error",
]


@runtime_checkable
class Cursor(Protocol):
    """What a repository may ask of a cursor."""

    @property
    def rowcount(self) -> int:
        """Rows affected by the statement, or -1 when the driver cannot say."""
        ...

    async def fetchone(self) -> Any:
        """The next row, or ``None`` when there are no more."""
        ...

    async def fetchall(self) -> list[Any]:
        """Every remaining row."""
        ...

    async def fetchmany(self, size: int) -> list[Any]:
        """Up to ``size`` remaining rows."""
        ...


class Result:
    """A cursor that is both awaitable and an async context manager.

    Two shapes in one object is unusual, but it is the shape every call site
    in the repository layer already uses, and adopting it means the SQLite
    path keeps working unchanged while the PostgreSQL path is made to match.

    The context-manager form closes the cursor on exit, which is what the
    ``async with`` call sites rely on. The await form does not — a cursor
    obtained that way is the caller's to close — so the ``async with`` form
    is the one to reach for.
    """

    __slots__ = ("_coro", "_cursor")

    def __init__(self, coro: Coroutine[Any, Any, Cursor]) -> None:
        self._coro = coro
        self._cursor: Cursor | None = None

    def __await__(self) -> Any:
        return self._coro.__await__()

    async def __aenter__(self) -> Cursor:
        self._cursor = await self._coro
        return self._cursor

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        cursor, self._cursor = self._cursor, None
        if cursor is not None:
            close = getattr(cursor, "close", None)
            if close is not None:
                await close()


@runtime_checkable
class TransactionConnection(Protocol):
    """What a repository may ask of the connection it is handed."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Run one statement with ``?`` placeholders and return its cursor."""
        ...

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run one statement for every row of ``rows``."""
        ...


#: SQLite's constraint codes, mapped to the neutral exception each means.
#:
#: ``SQLITE_CONSTRAINT_PRIMARYKEY`` is separate from
#: ``SQLITE_CONSTRAINT_UNIQUE`` even though both are a uniqueness failure —
#: SQLite reports a non-INTEGER PRIMARY KEY violation under its own code, so
#: a translation that only knew about ``UNIQUE`` would let a primary-key
#: collision escape as an unclassified error.
_SQLITE_CONSTRAINTS: dict[str, type[StorageError]] = {
    "SQLITE_CONSTRAINT_UNIQUE": UniqueViolation,
    "SQLITE_CONSTRAINT_PRIMARYKEY": UniqueViolation,
    "SQLITE_CONSTRAINT_CHECK": CheckViolation,
    "SQLITE_CONSTRAINT_FOREIGNKEY": ForeignKeyViolation,
    "SQLITE_CONSTRAINT_NOTNULL": NotNullViolation,
}


#: The messages SQLite uses for a statement it could not even begin to answer.
#:
#: SQLite reports all of these as ``OperationalError``, so they are told apart
#: by their text. The list is the *actual* wording the shipped SQLite emits,
#: which is not the wording one would guess: a malformed ``MATCH`` expression
#: is reported as "fts5: syntax error near ..." or "unterminated string",
#: never as "malformed match expression" — a phrase this list used to carry,
#: and which therefore never matched anything. A missing *view* is reported
#: as "no such table", so there is no "no such view" wording to match either.
_QUERY_REJECTED_MESSAGES: tuple[str, ...] = (
    "no such table",
    "no such function",
    "no such column",
    "no such index",
    "no such trigger",
    "unterminated string",
    "unknown special query",
    "fts5: syntax error",
    "syntax error",
)


#: SQLite's wording for a JSON document it cannot read.
#:
#: It is listed apart from the rest because it is the one rejected-query
#: wording that is a *data* fault rather than a query fault: the JSON
#: columns are text on both substrates, and the server dialect casts them
#: back to ``jsonb`` to expand them, so a row holding something that is
#: not JSON fails on the server as ``22P02 invalid_text_representation``.
#: Without this, the same corrupt row would be a named class on the server
#: and an anonymous generic error on the embedded substrate — and the
#: repository layer's one fail-closed handler, which catches the
#: rejected-query class, would not fire on the embedded one.
#:
#: The schema's ``CHECK (json_valid(...))`` constraints mean a row cannot
#: be *written* corrupt on the embedded substrate, so this is a defence
#: against a row that arrived by another route rather than a live path.
_SQLITE_MALFORMED_JSON_MESSAGE: Final[str] = "malformed json"


#: The exact wordings SQLite uses for contention.
#:
#: Matched as whole phrases rather than as a bare ``locked`` or ``busy``
#: substring, because those words turn up in perfectly ordinary schema
#: errors: "no such table: locked_users" is a *missing table*, and reading it
#: as contention would turn the fail-closed case the repository layer
#: handles into a batch-killing one.
#:
#: The list is every wording the shipped ``libsqlite3`` carries, not the
#: handful that are easy to remember. ``strings`` on the library finds six:
#:
#: * ``database is locked``
#: * ``database table is locked`` (and ``database table is locked: %s``)
#: * ``database schema is locked: %s``
#: * ``database %s is locked`` — the *named* form, "database main is locked"
#:   and "database temp is locked"
#: * ``database is deadlocked``
#: * ``database is busy``
#:
#: The named form is why the match is a *shape* rather than a list of
#: literals: the name is interpolated by SQLite, so the wording is not
#: known in advance and only its shape is.
_SQLITE_CONTENTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"database(?:\s+\w+)?\s+is\s+(?:locked|deadlocked|busy)"
)


#: The wording SQLite uses when the statement and the bindings disagree.
#:
#: ``sqlite3.ProgrammingError`` covers several things that are not rejected
#: queries — "you can only execute one statement at a time", the cross-thread
#: guard — so the classification is scoped to the one wording that means the
#: placeholder count and the binding count disagree.
_SQLITE_BINDING_COUNT_MESSAGE = "number of bindings"


def translate_sqlite_error(exc: BaseException) -> Exception:
    """Turn a SQLite driver error into a backend-neutral one.

    Returns the exception to raise rather than raising it, so the caller
    keeps the ``raise ... from exc`` chain that shows the original.

    An error this function cannot classify is still wrapped: the contract
    this module exists to keep is that a repository never has to import
    ``sqlite3`` to know what went wrong, and a raw driver error escaping
    breaks that contract just as surely as a misclassified one. The
    original is the ``__cause__``, so nothing is lost.

    The translation is **idempotent**: an error that is already one of ours
    is returned unchanged. On the server side the guard is load-bearing —
    the transaction boundary there catches everything the body raises,
    including the neutral errors the statement boundary has already
    produced, and without it a unique violation would be flattened into a
    vaguer storage error on its way out. On this side the boundaries only
    ever see the driver's own errors, so the guard is here for symmetry
    and for a caller that hands this function something else.
    """
    if isinstance(exc, StorageError):
        return exc
    name = getattr(exc, "sqlite_errorname", "") or ""
    kind = _SQLITE_CONSTRAINTS.get(name)
    if kind is not None:
        return kind(f"constraint violated: {exc}", substrate="sqlite", sqlite_code=name)
    if isinstance(exc, sqlite3.ProgrammingError) and (
        _SQLITE_BINDING_COUNT_MESSAGE in str(exc).lower()
    ):
        # "Incorrect number of bindings supplied" is the one class of bug the
        # placeholder translator can introduce, and SQLite reports it as a
        # ProgrammingError with no error code — so it would fall through to
        # the generic case and reach the caller as an unclassified storage
        # error. The only consumer of the rejected-query class is written to
        # fail closed, and a miscounted placeholder must not be the thing
        # that takes the ingestion batch down.
        return ParameterCountMismatch(f"query rejected: {exc}", substrate="sqlite")
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        # The rejected-query classification is evaluated FIRST, and the
        # contention match is on whole phrases rather than on a bare
        # "locked"/"busy" substring. A schema error whose text happens to
        # contain a contention word — "no such table: locked_users" — is a
        # missing table, and reading it as contention would turn the
        # fail-closed case the repository layer handles (resolve nothing
        # rather than take the ingestion batch down) into a batch-killing
        # one.
        if any(fragment in message for fragment in _QUERY_REJECTED_MESSAGES):
            return QueryRejected(f"query rejected: {exc}", substrate="sqlite")
        if _SQLITE_MALFORMED_JSON_MESSAGE in message:
            # The same *data* fault the server substrate reports as
            # ``22P02``, named the same way here. It is a member of the
            # rejected-query family, so the repository layer's fail-closed
            # handler catches it on both substrates.
            return MalformedDocument(
                f"malformed document: {exc}", substrate="sqlite", sqlite_code=name or None
            )
        # "database is locked" and "database is busy" are contention, not a
        # bad statement: the same work, re-run, may well succeed. That is a
        # different thing from a constraint violation, which is a statement
        # about the data and will fail identically however often it is
        # retried — so it gets its own type.
        if _SQLITE_CONTENTION_PATTERN.search(message):
            return SerializationFailure(
                f"write contention: {exc}", substrate="sqlite"
            )
    return StorageError(
        f"sqlite error: {exc}", substrate="sqlite", sqlite_code=name or None
    )


class SqliteTransactionConnection:
    """The SQLite side of the common transaction contract.

    Wraps the ``aiosqlite`` connection and translates the driver's errors
    into the neutral ones, so that the repository layer never has to import
    ``sqlite3`` to know what went wrong.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        return Result(self._execute(sql, params))

    async def _execute(self, sql: str, params: Sequence[Any]) -> Cursor:
        try:
            return cast(Cursor, await self._conn.execute(sql, tuple(params)))
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        try:
            await self._conn.executemany(sql, [tuple(row) for row in rows])
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
