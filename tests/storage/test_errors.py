"""The error vocabulary — the contract between a repository and a substrate.

The contract
------------
:mod:`paa.storage.relational.errors` says that a repository catches a
*concept* rather than a driver's exception, and that the translation happens
at the substrate boundary, once. Two things make that claim testable:

* every failure a statement can produce is a
  :class:`~paa.core.errors.StorageError`, so a caller that catches
  ``PaaError`` catches a storage fault whichever engine produced it; and
* the *kind* of failure is preserved, because the kinds mean different
  things: a constraint violation is a statement about the data and will
  fail identically however often it is retried, while a serialization
  failure is contention and may well succeed next time.

Before this file, none of that was tested. The vocabulary, the two
translators and the SQLSTATE table were all untested, and the SQLite
classifier's message list named two errors the shipped SQLite never emits
— so the one place in the repository layer written to *fail closed* on a
bad query could not have caught it.

The tests below run without a server: the translators are pure functions of
a driver's exception, and the SQLSTATE table is a table of strings.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from paa.core.errors import StorageError
from paa.storage.graph.sqlite_backend import SqliteGraphStore
from paa.storage.relational.connection import translate_sqlite_error
from paa.storage.relational.database import Database
from paa.storage.relational.errors import (
    CheckViolation,
    ConstraintViolation,
    ForeignKeyViolation,
    MalformedDocument,
    NotNullViolation,
    ParameterCountMismatch,
    QueryRejected,
    SerializationFailure,
    UniqueViolation,
)
from paa.storage.relational.postgres import _qmark_to_pyformat, _translate_postgres_error


class _FakePostgresError(Exception):
    """A driver error carrying a SQLSTATE and nothing else.

    psycopg's exception *class* differs between versions; its SQLSTATE does
    not. So the translation is tested against the portable part.
    """

    def __init__(self, sqlstate: str | None, message: str = "boom") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class ProgrammingError(Exception):
    """The driver's own ``ProgrammingError``, under its DB-API name.

    psycopg is an optional extra, so the translator cannot import the class
    to recognise it — it matches the name, which the DB-API fixes. The
    fake therefore has to carry the real name, and it has to carry no
    SQLSTATE, because a statement the driver refuses to send never has one.
    """

    sqlstate: str | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.sqlstate = None


# ---------------------------------------------------------------------------
# The SQLite classifier
# ---------------------------------------------------------------------------


class TestSqliteTranslation:
    def test_a_unique_violation_is_a_unique_violation(self) -> None:
        exc = sqlite3.IntegrityError("UNIQUE constraint failed: t.c")
        exc.sqlite_errorname = "SQLITE_CONSTRAINT_UNIQUE"  # type: ignore[attr-defined]
        assert isinstance(translate_sqlite_error(exc), UniqueViolation)

    def test_a_primary_key_violation_is_a_unique_violation(self) -> None:
        """SQLite reports a TEXT primary key under its own code."""
        exc = sqlite3.IntegrityError("UNIQUE constraint failed: t.id")
        exc.sqlite_errorname = "SQLITE_CONSTRAINT_PRIMARYKEY"  # type: ignore[attr-defined]
        assert isinstance(translate_sqlite_error(exc), UniqueViolation)

    def test_a_check_violation_is_a_check_violation(self) -> None:
        exc = sqlite3.IntegrityError("CHECK constraint failed: ck_x")
        exc.sqlite_errorname = "SQLITE_CONSTRAINT_CHECK"  # type: ignore[attr-defined]
        assert isinstance(translate_sqlite_error(exc), CheckViolation)

    def test_a_foreign_key_violation_is_a_foreign_key_violation(self) -> None:
        exc = sqlite3.IntegrityError("FOREIGN KEY constraint failed")
        exc.sqlite_errorname = "SQLITE_CONSTRAINT_FOREIGNKEY"  # type: ignore[attr-defined]
        assert isinstance(translate_sqlite_error(exc), ForeignKeyViolation)

    def test_a_not_null_violation_is_a_not_null_violation(self) -> None:
        exc = sqlite3.IntegrityError("NOT NULL constraint failed: t.c")
        exc.sqlite_errorname = "SQLITE_CONSTRAINT_NOTNULL"  # type: ignore[attr-defined]
        assert isinstance(translate_sqlite_error(exc), NotNullViolation)

    def test_contention_is_a_serialization_failure(self) -> None:
        """A locked database is contention, not a statement about the data.

        The wordings are every phrase the shipped ``libsqlite3`` emits, so a
        schema error whose text happens to contain "locked" is not mistaken
        for one — and a real contention is not missed, because a missed
        contention is a retryable failure reported as a generic error, and
        the repository layer only retries a serialization failure.
        """
        for message in (
            "database is locked",
            "database table is locked: hot_serving_ledger",
            "database schema is locked: main",
            "database is busy",
            # The *named* form: SQLite interpolates the name, so the wording
            # is not known in advance and only its shape is.
            "database main is locked",
            "database temp is locked",
            "database is deadlocked",
        ):
            assert isinstance(
                translate_sqlite_error(sqlite3.OperationalError(message)),
                SerializationFailure,
            ), message

    @pytest.mark.parametrize(
        "message",
        [
            "no such table: locked_users",
            "no such table: busy_flag",
            "no such table: unlocked_ledger",
        ],
    )
    def test_a_schema_error_is_not_contention(self, message: str) -> None:
        """A contention word inside a table name is not contention.

        The only consumer of the rejected-query class is written to *fail
        closed* — resolve nothing rather than take the ingestion batch
        down. Contention is retryable and is not caught there, so reading
        a missing table as contention would be exactly the batch-killing
        failure the fail-closed path exists to prevent.
        """
        translated = translate_sqlite_error(sqlite3.OperationalError(message))
        assert isinstance(translated, QueryRejected), message
        assert not isinstance(translated, SerializationFailure), message

    def test_a_placeholder_count_mismatch_is_a_rejected_query(self) -> None:
        """The one class of bug the placeholder translator can introduce.

        SQLite reports it as a ``ProgrammingError`` with no error code, so
        it used to fall through to the generic storage error — and the
        fail-closed path in the repository layer catches the rejected-query
        class, not the generic one.
        """
        exc = sqlite3.ProgrammingError(
            "Incorrect number of bindings supplied. "
            "The current statement uses 1, and there are 3 supplied."
        )
        translated = translate_sqlite_error(exc)
        assert isinstance(translated, ParameterCountMismatch)
        assert isinstance(translated, QueryRejected)

    def test_other_programming_errors_are_not_rejected_queries(self) -> None:
        """``sqlite3.ProgrammingError`` covers more than a count mismatch.

        "you can only execute one statement at a time" and the
        cross-thread guard are not a rejected query, so the
        classification has to be scoped to the wording that means the
        counts disagree.
        """
        for message in (
            "You can only execute one statement at a time.",
            "Objects created in a thread can only be used in that same thread.",
        ):
            translated = translate_sqlite_error(sqlite3.ProgrammingError(message))
            assert isinstance(translated, StorageError), message
            assert not isinstance(translated, QueryRejected), message

    @pytest.mark.parametrize(
        "message",
        [
            "no such table: t",
            "no such function: f",
            "no such column: c",
            "no such index: i",
            "unterminated string",
            "unknown special query: FTS5",
            "fts5: syntax error near \"x\"",
        ],
    )
    def test_a_statement_the_engine_cannot_answer_is_a_rejected_query(
        self, message: str
    ) -> None:
        assert isinstance(
            translate_sqlite_error(sqlite3.OperationalError(message)), QueryRejected
        )

    def test_the_fts5_errors_the_shipped_sqlite_actually_emits(self) -> None:
        """The two errors this classifier used to name, and SQLite never emits.

        A malformed ``MATCH`` expression is reported as "unterminated
        string" or "unknown special query". A classifier that looked for
        "malformed match expression" would never have caught either — and
        the catch it was written for would have stayed dead.
        """
        for message in ("unterminated string", "unknown special query: FTS5"):
            translated = translate_sqlite_error(sqlite3.OperationalError(message))
            assert isinstance(translated, QueryRejected), message

    @pytest.mark.parametrize(
        "message",
        ["malformed match expression", "no such view: v"],
    )
    def test_the_dead_wordings_are_not_carried(self, message: str) -> None:
        """Two wordings the classifier used to claim, and SQLite never emits.

        A malformed ``MATCH`` expression is reported as "fts5: syntax
        error" or "unterminated string", and a missing view is reported
        as "no such table" — both verified against the shipped SQLite.
        Carrying the dead wordings would suggest the classifier knows
        them, and a test that asserted them would lock them in.

        The pair is deliberately a *negative*: the wording the engine
        really uses for a missing view is "no such table", and that one
        **is** a rejected query — see the test above. Asserting the
        wording the engine cannot produce would put a dead string back
        into the vocabulary in the name of removing it.
        """
        translated = translate_sqlite_error(sqlite3.OperationalError(message))
        assert not isinstance(translated, QueryRejected), message

    def test_the_wording_the_engine_actually_uses_is_rejected(self) -> None:
        """The counterpart of the dead wording: the live one is a rejection.

        SQLite reports a missing view as "no such table", so the real
        wording has to be in the rejected-query set — otherwise a missing
        view would be a generic storage error while a missing table is a
        rejected query, which is not a distinction SQLite makes.
        """
        translated = translate_sqlite_error(sqlite3.OperationalError("no such table: v"))
        assert isinstance(translated, QueryRejected)

    def test_a_corrupt_json_row_is_the_data_s_fault_on_both_substrates(self) -> None:
        """The same corrupt row is the same *class* on both substrates.

        The JSON columns are text on both engines, and the server dialect
        casts them back to ``jsonb`` to expand them — so a row holding
        something that is not JSON fails on the server as ``22P02
        invalid_text_representation``. The embedded substrate's JSON
        functions say "malformed JSON" instead.

        Both are the *data's* fault, so both get the class that says so,
        and both are members of the rejected-query family so the
        repository layer's one fail-closed handler catches them. Before
        this, the same row was a named class on the server and an
        anonymous generic error here — and the fail-closed handler did
        not fire on this substrate.
        """
        translated = translate_sqlite_error(sqlite3.OperationalError("malformed JSON"))
        assert isinstance(translated, MalformedDocument), translated
        assert isinstance(translated, QueryRejected), translated

    def test_an_unclassified_error_is_still_a_storage_error(self) -> None:
        """The contract is that a repository never sees a driver exception.

        An error this function cannot classify is wrapped rather than
        passed through: a raw ``sqlite3.OperationalError`` escaping from a
        read would break the contract just as surely as a misclassified
        one, and the original is kept as the cause.
        """
        translated = translate_sqlite_error(sqlite3.OperationalError("something else"))
        assert isinstance(translated, StorageError)
        assert not isinstance(translated, ConstraintViolation)

    def test_the_translation_is_idempotent(self) -> None:
        """A wider boundary must not flatten a specific failure.

        The transaction control path catches everything the statement
        boundary already translated. If the translation were not
        idempotent, a unique violation would reach the caller as a vaguer
        storage error.
        """
        original = UniqueViolation("constraint violated: x", substrate="sqlite")
        assert translate_sqlite_error(original) is original

    def test_every_sqlite_error_is_a_storage_error(self) -> None:
        """The one invariant the vocabulary rests on."""
        for exc in (
            sqlite3.IntegrityError("x"),
            sqlite3.OperationalError("x"),
            sqlite3.DatabaseError("x"),
            sqlite3.InterfaceError("x"),
        ):
            assert isinstance(translate_sqlite_error(exc), StorageError)


# ---------------------------------------------------------------------------
# The PostgreSQL classifier
# ---------------------------------------------------------------------------


class TestPostgresTranslation:
    def test_a_unique_violation_is_a_unique_violation(self) -> None:
        assert isinstance(
            _translate_postgres_error(_FakePostgresError("23505")), UniqueViolation
        )

    def test_a_check_violation_is_a_check_violation(self) -> None:
        assert isinstance(
            _translate_postgres_error(_FakePostgresError("23514")), CheckViolation
        )

    def test_a_foreign_key_violation_is_a_foreign_key_violation(self) -> None:
        assert isinstance(
            _translate_postgres_error(_FakePostgresError("23503")), ForeignKeyViolation
        )

    def test_a_not_null_violation_is_a_not_null_violation(self) -> None:
        assert isinstance(
            _translate_postgres_error(_FakePostgresError("23502")), NotNullViolation
        )

    @pytest.mark.parametrize("sqlstate", ["40001", "40P01", "25P02", "53300"])
    def test_a_retryable_failure_is_a_serialization_failure(self, sqlstate: str) -> None:
        """``25P02`` is the aborted-transaction state.

        It is what the server says when a statement is attempted inside a
        transaction an earlier failure has already aborted. It is a
        statement about the transaction, not the statement, and the remedy
        is the same: roll back and try again.
        """
        assert isinstance(
            _translate_postgres_error(_FakePostgresError(sqlstate)),
            SerializationFailure,
        )

    @pytest.mark.parametrize(
        "sqlstate", ["42P01", "42883", "42703", "42601"]
    )
    def test_a_statement_the_engine_cannot_answer_is_a_rejected_query(
        self, sqlstate: str
    ) -> None:
        assert isinstance(
            _translate_postgres_error(_FakePostgresError(sqlstate)), QueryRejected
        )

    def test_a_corrupt_document_is_its_own_class(self) -> None:
        """``22P02`` is the data's fault, and the vocabulary says so.

        The JSON columns are text on both engines and the server dialect
        casts them back to ``jsonb`` to expand them, so a row holding
        something that is not JSON raises it. That is a corrupt *row*, not
        a broken query, and the two must not be the same class.

        It is still a rejected query — a subclass of one — because the
        repository path written to fail closed on a bad query has to catch
        it: entity resolution must degrade to "no match" rather than take
        the ingestion batch down, and a corrupt row must not be the thing
        that stops it.
        """
        translated = _translate_postgres_error(_FakePostgresError("22P02"))
        assert isinstance(translated, MalformedDocument)
        assert isinstance(translated, QueryRejected)
        # and it is distinguishable from an ordinary rejected query
        assert not isinstance(
            _translate_postgres_error(_FakePostgresError("42P01")), MalformedDocument
        )

    def test_a_placeholder_count_mismatch_is_a_rejected_query(self) -> None:
        """psycopg's own wording for the bug the translator can introduce.

        The driver's ``ProgrammingError`` carries no SQLSTATE, so it used
        to fall through to the generic storage error — and the fail-closed
        path in the repository layer catches the rejected-query class.
        """
        exc = ProgrammingError("the query has 2 placeholders but 1 parameters were passed")
        translated = _translate_postgres_error(exc)
        assert isinstance(translated, ParameterCountMismatch)
        assert isinstance(translated, QueryRejected)

    def test_other_programming_errors_are_not_count_mismatches(self) -> None:
        """The driver's ``ProgrammingError`` covers more than the count.

        "incomplete placeholder" and "only '%s' ... are allowed" are
        malformed statements rather than a disagreement about how many
        parameters there are, and neither is the cross-thread guard.
        """
        for message in (
            "incomplete placeholder: '%'",
            "only '%s', '%b', '%t' are allowed as placeholders, got '%x'",
            "the object was created in a different thread",
        ):
            translated = _translate_postgres_error(ProgrammingError(message))
            assert isinstance(translated, StorageError), message
            assert not isinstance(translated, ParameterCountMismatch), message

    def test_an_error_with_no_sqlstate_is_still_a_storage_error(self) -> None:
        """psycopg raises ``DataError`` for a NUL in a bound string.

        It carries no SQLSTATE, so it cannot be classified — but it must
        not reach a repository as a driver exception.
        """
        translated = _translate_postgres_error(_FakePostgresError(None))
        assert isinstance(translated, StorageError)

    def test_the_translation_is_idempotent(self) -> None:
        """A wider boundary must not flatten a specific failure.

        The transaction boundary catches everything the statement
        boundary already translated, so the translation has to leave an
        error that is already neutral exactly as it found it.
        """
        original = UniqueViolation("constraint violated: x", substrate="postgres")
        assert _translate_postgres_error(original) is original

    def test_an_unknown_sqlstate_is_still_a_storage_error(self) -> None:
        translated = _translate_postgres_error(_FakePostgresError("99999"))
        assert isinstance(translated, StorageError)
        assert not isinstance(translated, ConstraintViolation)

    def test_every_postgres_error_is_a_storage_error(self) -> None:
        for sqlstate in ("23505", "23514", "23503", "23502", "40001", "42P01", "22P02"):
            assert isinstance(
                _translate_postgres_error(_FakePostgresError(sqlstate)), StorageError
            )


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


class TestTheReadPathIsABoundary:
    """A read is a substrate boundary too.

    The repository layer has one place written to *fail closed* when a query
    is rejected — entity resolution, which must answer "no match" rather
    than take down the ingestion batch. That catch was dead code: a read
    raised the driver's own exception, which it cannot catch.
    """

    async def test_a_real_duplicate_is_a_unique_violation(self, tmp_path: Any) -> None:
        """The constraint table is exercised by a *real* violation.

        The classifier reads ``sqlite_errorname`` off the driver's
        exception, and the unit tests set that attribute by hand on a
        fabricated error. If the attribute were ever absent — a different
        SQLite build, a different Python — every constraint would fall
        through to the generic storage error, and the ledger's
        ``except UniqueViolation`` branch would silently stop firing: the
        duplicate would be reported as a generic failure instead of being
        suppressed, and every redelivery would look like a fault.

        So the classification is proved against a violation SQLite itself
        raises.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            # A table of the test's own, so the assertion is about the
            # classifier and not about the ledger's schema.
            await db.execute("CREATE TABLE dupes (id TEXT PRIMARY KEY, name TEXT UNIQUE)")
            await db.execute("INSERT INTO dupes (id, name) VALUES ('a', 'x')")
            with pytest.raises(UniqueViolation):
                await db.execute("INSERT INTO dupes (id, name) VALUES ('b', 'x')")
            with pytest.raises(UniqueViolation):
                await db.execute("INSERT INTO dupes (id, name) VALUES ('a', 'y')")
        finally:
            await db.close()

    async def test_a_missing_table_on_a_read_is_a_rejected_query(
        self, tmp_path: Any
    ) -> None:
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(QueryRejected):
                await db.fetch_all("SELECT * FROM no_such_table")
        finally:
            await db.close()

    async def test_a_missing_column_on_a_read_is_a_rejected_query(
        self, tmp_path: Any
    ) -> None:
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(QueryRejected):
                await db.fetch_one("SELECT no_such_column FROM system_state_ledger")
        finally:
            await db.close()

    async def test_a_missing_function_on_a_read_is_a_rejected_query(
        self, tmp_path: Any
    ) -> None:
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(QueryRejected):
                await db.fetch_value("SELECT no_such_function(1)")
        finally:
            await db.close()

    async def test_a_read_never_raises_a_driver_exception(self, tmp_path: Any) -> None:
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            for statement in (
                "SELECT * FROM no_such_table",
                "SELECT no_such_column FROM system_state_ledger",
                "SELECT no_such_function(1)",
            ):
                with pytest.raises(StorageError):
                    await db.fetch_all(statement)
        finally:
            await db.close()

    async def test_a_write_never_raises_a_driver_exception(self, tmp_path: Any) -> None:
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(StorageError):
                await db.execute("INSERT INTO no_such_table (x) VALUES (?)", (1,))
        finally:
            await db.close()

    async def test_a_placeholder_count_mismatch_on_a_read_is_a_rejected_query(
        self, tmp_path: Any
    ) -> None:
        """The failure the placeholder translator can introduce, for real.

        A statement with one placeholder and three parameters is not a
        statement this engine can run, and the fail-closed path in the
        repository layer has to be able to catch it.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(QueryRejected):
                await db.fetch_all(
                    "SELECT * FROM system_state_ledger WHERE event_id = ?", (1, 2, 3)
                )
        finally:
            await db.close()

    async def test_a_placeholder_count_mismatch_on_a_write_is_a_rejected_query(
        self, tmp_path: Any
    ) -> None:
        """The same, through the transaction boundary rather than a read.

        The write path rolls the transaction back before the error leaves,
        so the failure must survive the rollback rather than be replaced
        by it.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(ParameterCountMismatch):
                await db.execute(
                    "INSERT INTO system_state_ledger (event_id) VALUES (?)", (1, 2, 3)
                )
        finally:
            await db.close()

    async def test_a_rolled_back_transaction_leaves_the_connection_usable(
        self, tmp_path: Any
    ) -> None:
        """A failure inside a transaction must not poison the connection.

        The rollback that undoes the failed transaction is itself a
        statement that can fail, and it must never replace the error the
        caller needs to see — nor leave the connection inside a
        transaction the next caller would inherit.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            with pytest.raises(StorageError):
                await db.execute("INSERT INTO no_such_table (x) VALUES (?)", (1,))
            # The next transaction must begin cleanly.
            await db.execute(
                "INSERT INTO hot_serving_entity_index "
                "(id, class, canonical_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("e1", "concept", "E1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        finally:
            await db.close()


class TestTheTransactionControlPath:
    """The transaction-control statements are a substrate boundary too.

    ``BEGIN IMMEDIATE``, ``ROLLBACK`` and ``COMMIT`` are the substrate's
    own statements rather than the repository layer's, but they run on the
    same connection and fail the same way — a cross-process "database is
    locked" after the busy timeout, a deferred constraint at COMMIT — and
    a raw ``sqlite3`` error escaping from here would break the contract
    just as surely as one escaping from a read.
    """

    async def test_a_locked_begin_is_contention(self, tmp_path: Any) -> None:
        """The remedy is the same as for any contention: try again.

        It has to be distinguishable from a statement about the data,
        which will fail identically however often it is retried.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            conn = db._write_conn
            assert conn is not None
            original = conn.execute

            async def _locked(sql: str, *args: Any, **kwargs: Any) -> Any:
                if sql == "BEGIN IMMEDIATE":
                    raise sqlite3.OperationalError("database is locked")
                return await original(sql, *args, **kwargs)

            conn.execute = _locked  # type: ignore[method-assign]
            with pytest.raises(SerializationFailure):
                async with db.transaction():
                    pass
        finally:
            await db.close()

    async def test_a_failing_rollback_does_not_mask_the_original_error(
        self, tmp_path: Any
    ) -> None:
        """The statement that failed is the interesting failure.

        A rollback that fails must not replace it — and the connection
        must not be handed back holding an open transaction, or the next
        writer would be refused with "cannot start a transaction within a
        transaction".
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            conn = db._write_conn
            assert conn is not None
            original = conn.execute

            async def _rollback_fails(sql: str, *args: Any, **kwargs: Any) -> Any:
                if sql == "ROLLBACK":
                    raise sqlite3.OperationalError("database is locked")
                return await original(sql, *args, **kwargs)

            conn.execute = _rollback_fails  # type: ignore[method-assign]
            with pytest.raises(QueryRejected):
                await db.execute("INSERT INTO no_such_table (x) VALUES (?)", (1,))

            # And the connection is handed back in a usable state: the
            # failed rollback may have left the transaction open, and a
            # connection holding one refuses the next BEGIN.
            conn.execute = original  # type: ignore[method-assign]
            await db.execute(
                "INSERT INTO hot_serving_entity_index "
                "(id, class, canonical_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("e1", "concept", "E1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            rows = await db.fetch_all("SELECT id FROM hot_serving_entity_index")
            assert [r["id"] for r in rows] == ["e1"]
        finally:
            await db.close()

    async def test_a_failing_commit_is_translated_and_leaves_the_connection_usable(
        self, tmp_path: Any
    ) -> None:
        """A commit-time failure is as real as a statement-time one.

        A deferred foreign key is only checked at COMMIT, and a caller
        that believes it committed when it did not is worse off than one
        that knows it did not. The write must be undone and the
        connection left usable.
        """
        db = Database(tmp_path / "paa.db")
        await db.connect()
        try:
            conn = db._write_conn
            assert conn is not None
            original = conn.execute

            async def _commit_fails(sql: str, *args: Any, **kwargs: Any) -> Any:
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("cannot commit - no transaction is active")
                return await original(sql, *args, **kwargs)

            conn.execute = _commit_fails  # type: ignore[method-assign]
            with pytest.raises(StorageError):
                async with db.transaction() as tx:
                    await tx.execute(
                        "INSERT INTO hot_serving_entity_index "
                        "(id, class, canonical_name, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            "e1",
                            "concept",
                            "E1",
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )

            # The connection is usable again, and the aborted write is gone.
            conn.execute = original  # type: ignore[method-assign]
            await db.execute(
                "INSERT INTO hot_serving_entity_index "
                "(id, class, canonical_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("e2", "concept", "E2", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
            rows = await db.fetch_all("SELECT id FROM hot_serving_entity_index")
            assert [r["id"] for r in rows] == ["e2"]
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# The placeholder translator
# ---------------------------------------------------------------------------


class TestThePlaceholderTranslator:
    """The ``?``-to-``%s`` translator.

    The repository layer's SQL is written with ``?``; psycopg wants ``%s``.
    The translator is a lexer, and the two constructs it has to get right
    are the ones where a ``?`` is *not* a placeholder: inside a quoted
    literal, and inside a comment. Both engines parse neither, so neither
    may be treated as a placeholder — and a translator that got it wrong
    would produce a statement whose placeholder count disagrees with the
    parameter count, which psycopg refuses.
    """

    def test_a_placeholder_becomes_a_pyformat_placeholder(self) -> None:
        assert _qmark_to_pyformat("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_a_question_mark_in_a_literal_is_not_a_placeholder(self) -> None:
        """A ``?`` inside a string is a character on both engines."""
        assert _qmark_to_pyformat("SELECT 'what?' FROM t WHERE a = ?") == (
            "SELECT 'what?' FROM t WHERE a = %s"
        )

    def test_a_percent_is_doubled(self) -> None:
        """psycopg scans the raw bytes, quoted literals included.

        A bare ``%`` would be read as a placeholder marker, so it is
        doubled — which is what psycopg expects, and which it un-doubles
        before sending the query.
        """
        assert _qmark_to_pyformat("SELECT '100%' FROM t WHERE a = ?") == (
            "SELECT '100%%' FROM t WHERE a = %s"
        )

    def test_a_question_mark_in_a_line_comment_is_not_a_placeholder(self) -> None:
        """A comment is not a statement, so a ``?`` in it is not a placeholder."""
        assert _qmark_to_pyformat("SELECT 1 FROM t -- what?\nWHERE b = ?") == (
            "SELECT 1 FROM t -- what?\nWHERE b = %s"
        )

    def test_a_question_mark_in_a_block_comment_is_not_a_placeholder(self) -> None:
        assert _qmark_to_pyformat("SELECT 1 /* what? */ FROM t WHERE b = ?") == (
            "SELECT 1 /* what? */ FROM t WHERE b = %s"
        )

    def test_an_apostrophe_in_a_comment_does_not_swallow_the_statement(self) -> None:
        """The trap the comment handling exists to close.

        An apostrophe inside a comment used to be read as the start of a
        quoted literal, and the literal never closed — so every later
        ``?`` in the statement went unconverted, and psycopg refused the
        query because the placeholder count and the parameter count
        disagreed.
        """
        assert _qmark_to_pyformat("SELECT 1 FROM t -- it's fine\nWHERE b = ?") == (
            "SELECT 1 FROM t -- it's fine\nWHERE b = %s"
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 FROM t -- what? it's fine\nWHERE b = ?",
            "SELECT 1 /* what? */ FROM t WHERE b = ?",
            "SELECT 'what?' FROM t WHERE b = ?",
            "SELECT 1 FROM t WHERE b = ?",
            # The percent sign is the load-bearing half of the translator:
            # psycopg scans the raw bytes and collapses ``%%`` to ``%``,
            # so a literal ``%`` has to be doubled on the way out or the
            # server receives something the caller never wrote.
            "SELECT '100%' FROM t WHERE b = ?",
            "SELECT 1 FROM t -- 50% done\nWHERE b = ?",
        ],
    )
    def test_the_placeholder_count_agrees_with_the_driver(self, sql: str) -> None:
        """Checked against psycopg's own parser, when psycopg is installed.

        psycopg counts the placeholders in the *raw* bytes — comments and
        quoted literals included — so a translator that disagreed with it
        about the count would produce a statement the driver refuses.

        The count is read as the number of *parts* psycopg returns, which
        is one more than the number of placeholders: the query, then one
        part per placeholder. psycopg's private helper has changed shape
        across versions — it returned a list of bytes before 3.3 and a list
        of part objects after — so the arithmetic is pinned here rather
        than inferred from whichever shape is installed.
        """
        queries = pytest.importorskip("psycopg._queries")
        translated = _qmark_to_pyformat(sql)
        parts = queries._split_query(translated.encode())
        assert len(parts) == 2, (sql, translated)


# ---------------------------------------------------------------------------
# The boundary's output shape
# ---------------------------------------------------------------------------


class _UuidValuedBackend:
    """A backend whose rows carry a ``uuid.UUID`` where SQLite carries text.

    The server substrate stores the entity index's id in a ``UUID`` column,
    so the row a graph read receives carries a ``uuid.UUID`` — a shape the
    embedded substrate never produces. The two shapes have to produce the
    same model, and only the UUID-valued one exercises the coercion.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetch_all(self, sql: str, params: Any = ()) -> list[Any]:
        return [self._row]

    async def fetch_one(self, sql: str, params: Any = ()) -> Any:
        return self._row

    async def fetch_value(self, sql: str, params: Any = ()) -> Any:
        return None

    async def execute(self, sql: str, params: Any = ()) -> int:
        return 0

    async def execute_many(self, sql: str, rows: Any) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def table_names(self) -> list[str]:
        return ["hot_serving_entity_index", "hot_serving_relationships"]


class TestTheGraphNeighbourRead:
    """A substrate boundary must produce a model, not a validation error.

    The graph store's one-hop neighbour query joins the entity index and
    reads its id. On the server substrate that id is a ``UUID`` column,
    so it arrives as a :class:`uuid.UUID` — and ``GraphEntity.id`` is a
    pydantic field declared as text. Handing the raw value over raises a
    ``pydantic.ValidationError``, which is not in the vocabulary at all
    and which no repository can catch.

    The graph store has no in-tree caller and the default graph backend
    is kuzu, so this is a public-API defect rather than a crash on the
    default deployment path. It is the same defect the identifier
    coercion exists to prevent, and it was the one read site the coercion
    sweep missed.
    """

    async def test_a_neighbour_id_from_a_uuid_column_arrives_as_text(self) -> None:
        entity_id = uuid.uuid4()
        row: dict[str, Any] = {
            "id": "edge-1",
            "from_entity_id": "e1",
            "to_entity_id": str(entity_id),
            "rel_type": "DEPENDS_ON",
            "weight": 0.5,
            "confidence_decay": 0.004,
            "evidence_count": 1,
            "contradiction_score": 0.0,
            "source_memory_id": None,
            "origin_signal_id": None,
            "created_by_agent": "memory_creator",
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
            "valid_to": None,
            "n_id": entity_id,
            "n_class": "concept",
            "n_name": "Beta",
        }
        store = SqliteGraphStore(_UuidValuedBackend(row))  # type: ignore[arg-type]

        neighbours = await store.neighbors("e1")

        assert [n.entity.id for n in neighbours] == [str(entity_id)]
        assert isinstance(neighbours[0].entity.id, str)
