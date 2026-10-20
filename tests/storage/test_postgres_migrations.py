"""The PostgreSQL half of the schema migration machinery, against a live server.

Why this file exists
--------------------
``tests/storage/test_migrations.py`` proves the SQLite half. The PostgreSQL
half was — until this file — not executed by **any** test, and it is the
half with the most machinery in it:

* the six-statement ``arrival_seq`` step, which adds a column, numbers the
  rows that already exist, forbids nulls, drops any identity, attaches an
  identity, and then moves the sequence past the rows that are there so
  that the next insert does not collide with one of them;
* the JSONB-to-text conversion, which carries a ``'{}'::jsonb`` default
  across the type change and then restates it as a text default, because
  a catalog that still describes a text column's default in terms of
  ``jsonb`` describes a type the column no longer has;
* the ``span_parent_id`` retype, from a ``UUID`` column to a text one,
  because a span id is sixteen hex characters and not a UUID at all;
* the schema-scoped guards, which have to ask about *one* schema's catalog
  rather than the catalog as a whole;
* the whole migration transport — the pool, the transaction, and the DDL
  script applied as one statement inside one transaction.

None of that is visible from the SQLite side, and all of it is the kind of
thing that is wrong until it is run.

Pointing the suite at a server
------------------------------
The tests need a PostgreSQL server, named by ``PAA_TEST_POSTGRES_DSN``:

.. code-block:: bash

    docker run -d --name paa-test-pg \\
        -e POSTGRES_USER=paa -e POSTGRES_PASSWORD=paa \\
        -e POSTGRES_DB=postgres -p 127.0.0.1:55460:5432 postgres:16
    export PAA_TEST_POSTGRES_DSN="postgresql://paa:paa@127.0.0.1:55460/postgres"
    pytest tests/storage/test_postgres_migrations.py

Without that variable the module **skips**: the default suite has to stay
runnable on a laptop with no server, and a test that fails because there
is no server is a test that fails for the wrong reason.

The DSN names a *server*, not a database to write to. Every test here
creates a database of its own, named after the process, and drops it when
it is over — so a run can never touch the database the deployment is
using, whatever the DSN points at.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from paa.core.errors import StorageError
from paa.storage.relational.errors import SchemaVersionError
from paa.storage.relational.migrations import (
    ARRIVAL_SEQUENCES,
    JSONB_COLUMNS,
    SCHEMA_VERSION,
)
from paa.storage.relational.postgres import PostgresDatabase

#: The server the tests run against, or ``None`` when there is none.
#:
#: The environment variable is the house convention for a test-only switch
#: (``PAA_TEST_VERBOSE`` is the existing example): it names the server, and
#: the tests make their own database on it.
DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")

HAS_PSYCOPG = (
    importlib.util.find_spec("psycopg") is not None
    and importlib.util.find_spec("psycopg_pool") is not None
)

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.skipif(
        not HAS_PSYCOPG or not DSN,
        reason="needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN",
    ),
]

#: The trigram index on the entity table's aliases.
#:
#: It has to be dropped before the aliases column can be retyped and
#: recreated afterwards, and the reason is PostgreSQL's: an index
#: expression on a ``text`` column is stored as the column itself, so
#: retyping the column would leave the index pointing at a ``jsonb``
#: expression that ``gin_trgm_ops`` cannot accept. A version-1 database
#: was created with the column already ``jsonb``, so its index expression
#: really is a cast — and the migration retypes the column underneath it
#: without trouble.
#:
#: The definition has to be the one in ``schema_postgres.sql``. The shape
#: comparison below compares the migrated database's indexes with a fresh
#: one's, so a drift between this copy and the schema file is caught
#: rather than hidden.
TRGM_ALIASES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_hot_entity_trgm_aliases "
    "ON hot_serving_entity_index USING gin ((aliases::text) gin_trgm_ops)"
)

#: The timestamp the rows of the backfill test are written with.
#:
#: Explicit, and one second apart, because the backfill orders the rows
#: that already exist by their timestamp — and two rows written in the
#: same clock tick would be ordered by the id, which says nothing about
#: the order they arrived in.
_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


#: The server-backed fixtures live in ``tests/storage/conftest.py``: the
#: PostgreSQL contract suite needs the same throwaway database, and one
#: implementation of "a database of this test's own" is worth more than
#: two.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _columns(db: PostgresDatabase) -> dict[str, list[tuple[Any, ...]]]:
    """Every table's columns, in order.

    A list rather than a mapping, because the *order* of the columns is
    part of the shape.

    The comparison is over the ordered list rather than over
    ``ordinal_position`` itself, because that column is PostgreSQL's
    physical attribute number: a table that had a column dropped and
    another added keeps a gap in its numbering, while the columns
    themselves are in the same order. The gap is an artefact of the
    migration's own ``ALTER TABLE``, not a difference in what the table
    holds.
    """
    rows = await db.fetch_all(
        "SELECT table_name, column_name, data_type, column_default, is_nullable, "
        "  is_identity, identity_generation "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "ORDER BY table_name, ordinal_position"
    )
    out: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        out.setdefault(row["table_name"], []).append(
            (
                row["column_name"],
                row["data_type"],
                row["column_default"],
                row["is_nullable"],
                row["is_identity"],
                row["identity_generation"],
            )
        )
    return out


async def _constraints(db: PostgresDatabase) -> list[tuple[Any, ...]]:
    """Every table's constraints, as a sorted list.

    The primary key and the unique constraints are the ones a migration
    could drop without any column changing, so they are compared
    explicitly rather than inferred from the columns.

    PostgreSQL's own NOT NULL checks are left out. It creates one per
    NOT NULL column and names it after the table's *object id*
    (``cold_lake_signals_2200_27756_11_not_null``), so the name says
    which database the table was created in rather than anything about
    the constraint. The property they carry is compared instead, through
    ``is_nullable`` in :func:`_columns`.
    """
    rows = await db.fetch_all(
        "SELECT tc.table_name, tc.constraint_name, tc.constraint_type, "
        "  kcu.column_name, kcu.ordinal_position "
        "FROM information_schema.table_constraints tc "
        "LEFT JOIN information_schema.key_column_usage kcu "
        "  ON kcu.constraint_name = tc.constraint_name "
        "  AND kcu.table_schema = tc.table_schema "
        "WHERE tc.table_schema = current_schema() "
        "  AND tc.constraint_name NOT LIKE '%not_null' "
        "ORDER BY tc.table_name, tc.constraint_name, kcu.ordinal_position"
    )
    return [
        (
            row["table_name"],
            row["constraint_name"],
            row["constraint_type"],
            row["column_name"],
            row["ordinal_position"],
        )
        for row in rows
    ]


async def _indexes(db: PostgresDatabase) -> list[tuple[Any, ...]]:
    """Every index's definition, as a sorted list."""
    rows = await db.fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() ORDER BY indexname"
    )
    return [(row["indexname"], row["indexdef"]) for row in rows]


async def _versions(db: PostgresDatabase) -> list[int]:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row["version"]) for row in rows]


async def _execute(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Run one statement against the database, outside the runtime."""
    import psycopg

    connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    async with connection:
        await connection.execute(sql, params)


async def _fetch(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read rows from the database, outside the runtime."""
    import psycopg

    connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    async with connection, connection.cursor() as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def _downgrade_to_v1(dsn: str) -> None:
    """Turn a current database back into the version-1 shape.

    The inverse of versions 2, 3 and 4: drop the two ``arrival_seq``
    columns, turn the twenty ``JSONB`` columns back into ``JSONB`` with
    the defaults they had, and turn the span parent id back into a
    ``UUID``. The version log is rewound to version 1.

    The reversal starts from the *current* schema rather than from a
    frozen copy of the version-1 one, so there is no second copy of the
    schema to keep in step — the same discipline the SQLite tests use.

    The trigram index on the aliases column is dropped first and
    recreated last, because PostgreSQL folds an index expression on a
    ``text`` column down to the column itself: with the index in place,
    retyping the column to ``jsonb`` would leave the index pointing at an
    expression ``gin_trgm_ops`` cannot accept.
    """
    await _execute(dsn, "DROP INDEX IF EXISTS idx_hot_entity_trgm_aliases")
    for table in ARRIVAL_SEQUENCES:
        await _execute(dsn, f"ALTER TABLE {table} DROP COLUMN IF EXISTS arrival_seq")
    for table, column, default in JSONB_COLUMNS:
        # The default has to go first: a ``'{}'::jsonb`` default cannot be
        # carried across to a ``text`` column, and the reverse is true as
        # well.
        await _execute(dsn, f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
        await _execute(
            dsn,
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE JSONB "
            f"USING {column}::text::jsonb",
        )
        if default is not None:
            await _execute(
                dsn,
                f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT '{default}'::jsonb",
            )
    await _execute(
        dsn,
        "ALTER TABLE hot_serving_execution_runs "
        "ALTER COLUMN span_parent_id TYPE UUID USING span_parent_id::uuid",
    )
    await _execute(dsn, TRGM_ALIASES_INDEX)
    await _execute(dsn, "DELETE FROM schema_migrations WHERE version > 1")


# ---------------------------------------------------------------------------
# A fresh database
# ---------------------------------------------------------------------------


async def test_a_fresh_database_is_at_the_current_version(postgres: PostgresDatabase) -> None:
    """The server's schema file is applied, and every version is recorded."""
    assert await _versions(postgres) == list(range(1, SCHEMA_VERSION + 1))


async def test_connecting_twice_changes_nothing(postgres_dsn: str) -> None:
    """The second connect finds the version current and does no work."""
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    before = await _columns(db)
    await db.close()

    await db.connect()
    try:
        assert await _columns(db) == before
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


async def test_a_version_1_database_is_migrated_forward(postgres_dsn: str) -> None:
    """A version-1 server database is carried forward to the current version."""
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        columns = await _columns(db)
        assert "arrival_seq" in [entry[0] for entry in columns["cold_lake_signals"]]
        assert "arrival_seq" in [entry[0] for entry in columns["queue_messages"]]
    finally:
        await db.close()


async def test_the_reversal_really_produced_a_version_1_database(postgres_dsn: str) -> None:
    """The reversal is a fact the suite checks, not one it assumes.

    Every other test in this file builds its version-1 database by
    *reversing* the migration. If the reversal quietly stopped being a
    reversal — the JSONB re-typing loop skipped, the ``span_parent_id``
    retype skipped — then the database would still carry the current
    shape, every migration would find its work already done, and every
    test would still pass while the ``ALTER TABLE ... ALTER COLUMN TYPE``
    statements were executed by **nothing**. The suite would not be able
    to tell.

    So the reversal is looked at, before the migration runs: the JSON
    columns are ``jsonb`` again, and the parent span id is a ``uuid``.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    rows = await _fetch(
        postgres_dsn,
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema()",
    )
    types = {(row[0], row[1]): row[2] for row in rows}

    for table, column, _default in JSONB_COLUMNS:
        assert types[(table, column)] == "jsonb", (table, column)
    assert types[("hot_serving_execution_runs", "span_parent_id")] == "uuid"


async def test_the_migration_really_retypes_what_the_reversal_reversed(
    postgres_dsn: str,
) -> None:
    """The other half of the pair: the migration puts it back.

    The test above proves the reversal really reversed; this one proves
    the migration really converts. Together they say that the re-typing
    statements are executed by the suite, which is the thing the two of
    them exist to establish.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
        types = {(row["table_name"], row["column_name"]): row["data_type"] for row in rows}
        for table, column, _default in JSONB_COLUMNS:
            assert types[(table, column)] == "text", (table, column)
        assert types[("hot_serving_execution_runs", "span_parent_id")] == "text"
    finally:
        await db.close()


async def test_a_migrated_database_has_the_same_shape_as_a_fresh_one(
    postgres_dsn: str,
) -> None:
    """The invariant, on the server.

    A database carried forward and a database created outright must be
    the same database: the same columns in the same order, with the same
    types, the same defaults, the same nullability and the same identity;
    the same constraints, primary keys and unique constraints included;
    and the same indexes.
    """
    fresh = PostgresDatabase(postgres_dsn)
    await fresh.connect()
    fresh_columns = await _columns(fresh)
    fresh_constraints = await _constraints(fresh)
    fresh_indexes = await _indexes(fresh)
    await fresh.close()

    await _downgrade_to_v1(postgres_dsn)

    migrated = PostgresDatabase(postgres_dsn)
    await migrated.connect()
    try:
        assert await _columns(migrated) == fresh_columns
        assert await _constraints(migrated) == fresh_constraints
        assert await _indexes(migrated) == fresh_indexes
    finally:
        await migrated.close()


async def test_the_arrival_column_becomes_an_identity(postgres_dsn: str) -> None:
    """The six statements leave an identity column, and the guard agrees.

    The guard asks whether the column is an identity, is not nullable, and
    was generated *always* — that is the finished effect the six
    statements produce. A re-run therefore redoes all six rather than
    half of them, and each of the six is individually re-runnable.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT table_name, is_identity, is_nullable, identity_generation "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'arrival_seq' "
            "ORDER BY table_name"
        )
        assert [(r["table_name"], r["is_identity"]) for r in rows] == [
            ("cold_lake_signals", "YES"),
            ("queue_messages", "YES"),
        ]
        for row in rows:
            assert row["is_nullable"] == "NO"
            assert row["identity_generation"] == "ALWAYS"
    finally:
        await db.close()


async def test_the_arrival_column_numbers_the_rows_that_already_existed(
    postgres_dsn: str,
) -> None:
    """The backfill gives the rows that were there a total order.

    The sequence behind the identity column starts at one, so the rows
    that existed before the migration are numbered by hand — and the
    sequence is then moved past them, so that the next insert does not
    take a number one of them already has. Both halves of that are
    observable: the rows carry 1, 2 and 3 in the order they arrived, and
    the next row carries 4.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    # The rows are written while the database is still at version 1, so
    # that the migration is what numbers them.
    for index in range(3):
        stamp = _BASE_TIME + timedelta(seconds=index)
        await _execute(
            postgres_dsn,
            "INSERT INTO queue_messages (id, stream, payload, status, priority, "
            "attempts, max_attempts, visible_after, enqueued_at) "
            "VALUES (%s, 's', '{}', 'ready', 100, 0, 3, %s, %s)",
            (uuid.uuid4(), stamp, stamp),
        )

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT id, arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        assert [row["arrival_seq"] for row in rows] == [1, 2, 3]

        await db.execute(
            "INSERT INTO queue_messages (id, stream, payload, status, priority, "
            "attempts, max_attempts, visible_after, enqueued_at) "
            "VALUES (?, 's', '{}', 'ready', 100, 0, 3, ?, ?)",
            (uuid.uuid4(), _BASE_TIME, _BASE_TIME),
        )
        rows = await db.fetch_all(
            "SELECT arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        assert [row["arrival_seq"] for row in rows] == [1, 2, 3, 4]
    finally:
        await db.close()


async def test_the_json_columns_become_text_and_keep_their_defaults(
    postgres_dsn: str,
) -> None:
    """The retyped column is text, and its default is restated as text.

    The type conversion carries the old ``'{}'::jsonb`` default across as
    a literal of the old type, which casts to the right value but leaves
    the catalog describing the default in terms of a type the column no
    longer has. The second guarded block restates it, and the default a
    migrated database carries is the same one a fresh one does.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT table_name, column_name, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "ORDER BY table_name, column_name"
        )
        wanted = {(table, column): default for table, column, default in JSONB_COLUMNS}
        seen = 0
        for row in rows:
            key = (row["table_name"], row["column_name"])
            if key not in wanted:
                continue
            seen += 1
            assert row["data_type"] == "text", row
            default = wanted[key]
            if default is None:
                assert row["column_default"] is None, row
            else:
                assert row["column_default"] == f"'{default}'::text", row
        assert seen == len(JSONB_COLUMNS)
    finally:
        await db.close()


async def test_the_span_parent_id_becomes_text(postgres_dsn: str) -> None:
    """A span id is sixteen hex characters, and not a UUID."""
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "  AND table_name = 'hot_serving_execution_runs' "
            "  AND column_name = 'span_parent_id'"
        )
        assert [(r["column_name"], r["data_type"]) for r in rows] == [
            ("span_parent_id", "text")
        ]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


async def test_a_newer_database_is_refused(postgres_dsn: str) -> None:
    """An older build must not write to a newer schema."""
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(
        postgres_dsn,
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES (%s, now(), 'from the future')",
        (SCHEMA_VERSION + 1,),
    )

    db = PostgresDatabase(postgres_dsn)
    with pytest.raises(SchemaVersionError):
        await db.connect()
    await db.close()


async def test_the_refusal_names_both_versions(postgres_dsn: str) -> None:
    """A refusal that does not say what it refused is a puzzle.

    Both versions: the one the database is at, and the one this build
    understands. An operator reading the refusal has to be able to tell
    which side of the gap the database is on.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(
        postgres_dsn,
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES (%s, now(), 'from the future')",
        (SCHEMA_VERSION + 1,),
    )

    db = PostgresDatabase(postgres_dsn)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    message = str(caught.value)
    assert str(SCHEMA_VERSION + 1) in message
    assert str(SCHEMA_VERSION) in message


async def test_a_database_with_a_gap_in_its_version_log_is_refused(
    postgres_dsn: str,
) -> None:
    """A gap means the highest version does not describe the shape.

    A version recorded without the migration that produced it — or a log
    an operator edited — leaves the highest version claiming a shape the
    database does not have, and taking it at face value would skip the
    migration that would have repaired the database.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(postgres_dsn, "DELETE FROM schema_migrations WHERE version = 2")

    db = PostgresDatabase(postgres_dsn)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    assert "gap" in str(caught.value).lower()


async def test_a_database_with_all_the_tables_and_no_version_is_migrated(
    postgres_dsn: str,
) -> None:
    """A database whose version was never recorded is a real database.

    The old build applied the schema and recorded the version as two
    separate statements, so a crash in between left tables with no
    version row. Treating that as a brand-new database would stamp it
    with a version its shape does not have — and no later start would ever
    repair it, because the version it claims is the version that says it
    needs no repair.

    The refusal for an *incomplete* schema does not depend on the log
    being absent: a half-applied schema with an empty log is refused by
    name, and a whole one with an empty log is migrated. This is the
    second of those two.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(postgres_dsn, "DELETE FROM schema_migrations")

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_a_refusal_says_which_database_it_is_about(postgres_dsn: str) -> None:
    """A server holds several databases, and a refusal has to say which.

    The SQLite refusal carries the path of the file; the server's
    equivalent is the database's name, and it is the *name* only — the
    DSN carries a password, and a refusal is something an operator reads
    out loud.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(
        postgres_dsn,
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES (%s, now(), 'from the future')",
        (SCHEMA_VERSION + 1,),
    )

    db = PostgresDatabase(postgres_dsn)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    details = caught.value.details
    assert details.get("database")
    assert "password" not in str(details)


async def test_a_half_applied_schema_is_refused_by_name(postgres_dsn: str) -> None:
    """A schema that was applied half-way is refused, and named.

    The refusal comes out of the catalog, so it names the tables that are
    missing rather than reporting the first table a query happened to
    touch — and it happens before anything is written. The version log is
    emptied as well, because a database whose log still claims the
    current version is not the shape an interrupted schema application
    leaves behind.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()

    await _execute(postgres_dsn, "DROP TABLE cold_lake_signals CASCADE")
    await _execute(postgres_dsn, "DELETE FROM schema_migrations")

    db = PostgresDatabase(postgres_dsn)
    with pytest.raises(StorageError) as caught:
        await db.connect()
    await db.close()

    message = str(caught.value)
    assert "incomplete" in message.lower()
    assert "cold_lake_signals" in message
    assert "no such table" not in message.lower()


# ---------------------------------------------------------------------------
# Two starts at once
# ---------------------------------------------------------------------------


async def test_two_starts_at_once_do_not_fail_each_other(postgres_dsn: str) -> None:
    """Two processes may start at the same moment, and both must succeed.

    The migration takes an advisory lock for its whole duration, so the
    second process waits rather than re-running the first one's steps
    underneath it. Without the lock the two raced each other: the loser
    died with ``duplicate key value violates unique constraint
    "pg_extension_name_index"`` while installing ``pg_trgm``, or with
    ``column "arrival_seq" can only be updated to DEFAULT`` while adding
    the identity column the other had already added.

    Nothing was ever *corrupted* by the race — the surviving log was
    right — but the loser failed its start-up, and a start-up that fails
    because another process happened to start at the same moment is not
    something a deployment can live with.
    """
    first = PostgresDatabase(postgres_dsn)
    second = PostgresDatabase(postgres_dsn)
    await asyncio.gather(first.connect(), second.connect())
    try:
        assert await _versions(first) == list(range(1, SCHEMA_VERSION + 1))
        assert await _versions(second) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await first.close()
        await second.close()


async def test_two_migrations_at_once_do_not_fail_each_other(postgres_dsn: str) -> None:
    """The same, one version behind.

    The interesting case, because the two processes are doing the *same*
    work: the second waits for the first to commit, and then finds the
    work already done — every guard tests the effect of its step, so
    the re-run changes nothing and the version row is written with
    ``ON CONFLICT DO NOTHING``.
    """
    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _downgrade_to_v1(postgres_dsn)

    first = PostgresDatabase(postgres_dsn)
    second = PostgresDatabase(postgres_dsn)
    await asyncio.gather(first.connect(), second.connect())
    try:
        assert await _versions(first) == list(range(1, SCHEMA_VERSION + 1))
        columns = await _columns(first)
        assert "arrival_seq" in [entry[0] for entry in columns["cold_lake_signals"]]
    finally:
        await first.close()
        await second.close()



async def test_a_guard_is_not_satisfied_by_another_schema_s_table(
    postgres_dsn: str,
) -> None:
    """The guard asks about ONE schema's catalog, not the catalog as a whole.

    A guard that named only a table and a column would be satisfied by a
    same-named table in *any* schema the user can see — and
    ``information_schema`` spans every one of them. The step would then be
    skipped as "already applied", the version row would be written anyway,
    and the database would claim a shape it does not have. That is exactly
    the failure the whole module exists to prevent, and with a single
    schema on the server it cannot happen: the unscoped guard and the
    scoped guard find the same row.

    So the test builds the situation that distinguishes them: a database at
    the base version, and a *decoy* schema holding a table of the same name
    that already carries the finished effect the guard looks for. The
    migration must run against the real schema, and the decoy must be left
    exactly as it was.
    """
    # A database at the current version, then wound back to the base one,
    # so the migration has real work to do.
    warm = PostgresDatabase(postgres_dsn)
    await warm.connect()
    await warm.close()
    await _downgrade_to_v1(postgres_dsn)

    # The decoy carries the *finished* effect: the column exists, is
    # generated, and is not null. An unscoped guard would find it, and
    # would skip the step.
    await _execute(postgres_dsn, "CREATE SCHEMA IF NOT EXISTS decoy")
    await _execute(
        postgres_dsn,
        "CREATE TABLE IF NOT EXISTS decoy.queue_messages ("
        "id TEXT PRIMARY KEY, arrival_seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL)",
    )
    await _execute(
        postgres_dsn,
        "INSERT INTO decoy.queue_messages (id) VALUES ('decoy-row')",
    )

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        # The step ran, against the schema the runner resolved.
        columns = await _columns(db)
        assert "arrival_seq" in [
            name for name, *_ in columns.get("queue_messages", [])
        ], "the migration did not run against the resolved schema"
        # And the decoy was not touched: its table and its row are as they
        # were made.
        decoy_columns = await _fetch(
            postgres_dsn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'decoy' AND table_name = 'queue_messages'",
        )
        assert [row[0] for row in decoy_columns] == ["id", "arrival_seq"]
        decoy_rows = await _fetch(
            postgres_dsn, "SELECT id FROM decoy.queue_messages ORDER BY id"
        )
        assert [row[0] for row in decoy_rows] == ["decoy-row"]
    finally:
        await db.close()
