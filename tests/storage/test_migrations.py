"""The schema migration machinery — the part that carries an existing database.

The gap these tests close
-------------------------
Until schema version 2, ``schema_sqlite.sql`` and ``schema_postgres.sql``
were applied whenever the stored version was lower than the code's, and
because every statement in them is ``CREATE ... IF NOT EXISTS``, applying
them to an existing database changed *nothing*. A schema change therefore
only ever reached a database that did not yet exist. The first change this
repository attempted — the ``arrival_seq`` columns and JSON stored as text —
found that gap, and :mod:`paa.storage.relational.migrations` is what closes
it.

The invariant every test here defends
-------------------------------------
**A migrated database has the same shape as a freshly created one.** Not
"close enough" — the same tables, the same columns, in the same order, with
the same types, the same defaults, the same primary keys, the same unique
constraints, the same strictness, the same triggers, the same foreign keys.
A database that was carried forward and a database that was created outright
are the same database, and the only difference between them is how they came
to be.

The PostgreSQL half of that claim is proved against a live server, in
``tests/storage/test_postgres_migrations.py``. This file is the SQLite half,
and it is the one that runs on a laptop with no server.

The v1 shape
------------
Version 1 is the schema this repository was born with: the ``.sql`` files as
they were at the first commit. The tests reconstruct it by *reversing* the
migration — dropping what version 2 added — rather than by keeping a frozen
copy of the old ``.sql`` file, so there is no second copy of the schema to
keep in step.

The reversal is a **checked fact**, not an assumption: a test opens the
reversed database and looks at it *before* the migration runs, and asserts
that the column version 2 adds is not there. Without that check the reversal
could quietly stop being a reversal — a dropped line in the helper, a
statement that no longer has the effect it had — and every test that
followed would still pass while exercising nothing: the migration's
``ALTER TABLE ... ADD COLUMN`` would never be executed by any test, and the
suite could not tell.

The reversal undoes version 2 only, and that is a deliberate limit rather
than an oversight. Version 3 changes no shape on SQLite at all — its
columns were always text — and version 4 changes the *contents* of one
column rather than the shape of the database, so neither of them has a
SQLite shape to reverse. The test that covers version 4 writes the old form
of a trace id into a version-1-shaped database and reads back the new one,
which is the only way that migration can be exercised.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from paa.core.errors import StorageError
from paa.storage.relational.database import Database
from paa.storage.relational.errors import SchemaVersionError
from paa.storage.relational.migrations import (
    ARRIVAL_SEQUENCES,
    ARRIVAL_TRIGGERS,
    BASE_SCHEMA_VERSION,
    JSONB_COLUMN_COUNT,
    JSONB_COLUMNS,
    MIGRATIONS,
    SCHEMA_PLACEHOLDER,
    SCHEMA_VERSION,
    Migration,
    description_for,
    pending,
    schema_ddl,
)
from paa.storage.relational.schema_parser import parse_tables

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _columns(db: Database) -> dict[str, list[tuple[str, str, str, int, int]]]:
    """Every table's columns, in order.

    A list, not a mapping: the *order* of the columns is part of the shape,
    and a mapping would quietly forget it.

    The primary-key flag is part of the tuple. ``PRAGMA table_info``
    reports it in a column called ``pk``, and a column that stopped being
    the primary key is a different shape from one that never was — so a
    comparison that ignored the flag would pass over a migration that
    dropped the key.
    """
    out: dict[str, list[tuple[str, str, str, int, int]]] = {}
    for table in await db.table_names():
        rows = await db.fetch_all(f"PRAGMA table_info({table})")
        out[table] = [
            (r["name"], r["type"], r["dflt_value"] or "", int(r["notnull"]), int(r["pk"]))
            for r in rows
        ]
    return out


def _extract_check_constraints(sql: str) -> list[str]:
    """The ``CHECK`` expressions in a table's DDL.

    SQLite keeps a table's constraints in the text of its ``CREATE TABLE``
    statement and offers no pragma for them, so they are read out of the
    text. The text itself cannot be compared — a column added by
    ``ALTER TABLE`` lands in a different place in the statement than one
    declared in it — but the constraints can.
    """
    found: list[str] = []
    marker = "CHECK"
    index = 0
    while True:
        index = sql.find(marker, index)
        if index == -1:
            return found
        cursor = index + len(marker)
        while cursor < len(sql) and sql[cursor] in " \t\n":
            cursor += 1
        if cursor >= len(sql) or sql[cursor] != "(":
            index += len(marker)
            continue
        depth = 0
        end = cursor
        for position in range(cursor, len(sql)):
            if sql[position] == "(":
                depth += 1
            elif sql[position] == ")":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        found.append(" ".join(sql[cursor : end + 1].split()))
        index = end


async def _index_sql(db: Database) -> dict[str, str]:
    """Every *declared* index's SQL, as ``{name: sql}`` with whitespace collapsed.

    The indexes SQLite creates for a ``PRIMARY KEY`` or a ``UNIQUE``
    constraint have no SQL of their own — they are ``sqlite_autoindex_*``
    rows with a null ``sql`` — so this reads only the declared ones.
    :func:`_index_lists` is what covers the constraint-backed ones.
    """
    rows = await db.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
        "ORDER BY name"
    )
    return {r["name"]: " ".join((r["sql"] or "").split()) for r in rows}


async def _index_lists(db: Database) -> dict[str, list[tuple[str, int, str, int]]]:
    """Every table's indexes, as ``{table: [(name, unique, origin, partial)]}``.

    Read from ``PRAGMA index_list`` rather than from ``sqlite_master``,
    because the constraint-backed indexes are exactly the ones
    ``sqlite_master`` cannot describe: a primary key or a unique
    constraint produces an ``sqlite_autoindex_*`` row with no SQL, and a
    comparison built on ``sqlite_master`` alone would not notice a
    migration that dropped either of them.

    ``origin`` says where the index came from: ``c`` for a declared
    ``CREATE INDEX``, ``pk`` for a primary key, ``u`` for a unique
    constraint.
    """
    out: dict[str, list[tuple[str, int, str, int]]] = {}
    for table in await db.table_names():
        rows = await db.fetch_all(f"PRAGMA index_list({table})")
        out[table] = [
            (r["name"], int(r["unique"]), r["origin"], int(r["partial"])) for r in rows
        ]
    return out


async def _strict_flags(db: Database) -> dict[str, int]:
    """Every table's ``STRICT`` flag, from ``PRAGMA table_list``.

    Strictness is part of the shape: a table that lost it would accept a
    value the strict table refuses, so the shape would differ even though
    what is *stored* would not.
    """
    rows = await db.fetch_all("PRAGMA table_list")
    return {r["name"]: int(r["strict"]) for r in rows}


async def _foreign_keys(db: Database) -> dict[str, list[tuple[Any, ...]]]:
    """Every table's foreign keys, as ``{table: [(from, to, on_delete), ...]}``."""
    out: dict[str, list[tuple[Any, ...]]] = {}
    for table in await db.table_names():
        rows = await db.fetch_all(f"PRAGMA foreign_key_list({table})")
        out[table] = sorted(
            (r["from"], r["table"], r["to"], r["on_delete"], r["on_update"])
            for r in rows
        )
    return out


async def _check_constraints(db: Database) -> dict[str, list[str]]:
    """Every table's ``CHECK`` expressions, in order."""
    rows = await db.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return {r["name"]: _extract_check_constraints(r["sql"] or "") for r in rows}


async def _triggers(db: Database) -> dict[str, str]:
    """Every trigger's SQL, as ``{name: sql}`` with the whitespace collapsed.

    SQLite stores the statement text as it was written, so the schema file's
    multi-line trigger and the migration's single-line one are the same
    trigger written differently. Comparing the collapsed form compares the
    statement rather than the formatting.
    """
    rows = await db.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    )
    return {r["name"]: " ".join((r["sql"] or "").split()) for r in rows}


async def _versions(db: Database) -> list[int]:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    return [int(r["version"]) for r in rows]


def _raw(path: Path) -> sqlite3.Connection:
    """A plain connection, for reaching behind the runtime.

    The point of these tests is to reach the state the runtime's own
    migration is supposed to repair, so the setup has to bypass the runtime.
    """
    return sqlite3.connect(path)


def _tables(path: Path) -> list[str]:
    """The names of the tables a database file holds."""
    conn = _raw(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _downgrade_to_v1(path: Path) -> None:
    """Turn a current database back into the version-1 shape.

    The inverse of version 2's migration: drop the two ``arrival_seq``
    columns and the two triggers that fill them, and rewind the version
    marker. Version 3 is a PostgreSQL-only change (JSONB to text), so
    SQLite needs nothing else.
    """
    conn = _raw(path)
    try:
        for table, trigger in ARRIVAL_TRIGGERS.items():
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN arrival_seq")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version > ?", (BASE_SCHEMA_VERSION,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (BASE_SCHEMA_VERSION, "2026-01-01T00:00:00+00:00", description_for(1)),
        )
        conn.commit()
    finally:
        conn.close()


def _enqueue_raw(path: Path, message_id: str) -> None:
    """Write a queue message behind the runtime's back."""
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT INTO queue_messages (id, stream, payload, status, priority, "
            "attempts, max_attempts, visible_after, enqueued_at) "
            "VALUES (?, 's', '{}', 'ready', 100, 0, 3, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (message_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _write_run_raw(path: Path, trace_id: str) -> None:
    """Write an execution-run row behind the runtime's back.

    The trace id is written verbatim, which is the point: the migration
    that repairs version 4 exists because SQLite stores a trace id as the
    text it was given, and a caller could give it the unhyphenated form.
    """
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT INTO hot_serving_execution_runs (trace_id, correlation_id, "
            "agent_role, modality, permission_mode, started_at) "
            "VALUES (?, '00000000-0000-0000-0000-000000000001', 'planner', "
            "'STANDARD', 'ASK', '2026-01-01T00:00:00+00:00')",
            (trace_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _record_future_version(path: Path, version: int) -> None:
    """Write a version row from the future, behind the runtime's back."""
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (version, "2026-01-01T00:00:00+00:00", "from the future"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
async def future_version_path(tmp_path: Path) -> AsyncIterator[Path]:
    """The path of a database whose version log records a version from the future.

    Every refusal test needs the same database, and building it four times
    over is four chances to spell the setup differently — and one chance to
    forget that the version row has to be *added* rather than written in
    place of the log.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _record_future_version(path, SCHEMA_VERSION + 1)
    yield path


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------


def test_the_migration_versions_are_contiguous() -> None:
    """A gap in the version list is a version nothing can reach."""
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(BASE_SCHEMA_VERSION + 1, SCHEMA_VERSION + 1))
    assert versions[-1] == SCHEMA_VERSION


def test_every_version_has_a_description() -> None:
    for version in range(1, SCHEMA_VERSION + 1):
        assert description_for(version)


def test_a_version_beyond_the_list_has_no_description() -> None:
    with pytest.raises(KeyError):
        description_for(SCHEMA_VERSION + 1)


def test_every_step_names_what_it_is_about() -> None:
    """A step that does not say which table and column it touches is opaque."""
    for migration in MIGRATIONS:
        for step in migration.steps:
            assert step.table
            assert step.column
            assert step.description


def test_every_statement_is_guarded() -> None:
    """An unguarded statement is a statement that fails on the second run."""
    for migration in MIGRATIONS:
        for step in migration.steps:
            for engine in (step.sqlite, step.postgres):
                for guarded in engine.guarded:
                    assert guarded.guard


def test_every_guard_s_placeholder_count_matches_its_parameters() -> None:
    """A guard whose placeholders and parameters disagree is a silent skip.

    A guard that matches no row says "the work is not needed", so the
    step is skipped — and the version row is written anyway, which is a
    database claiming a shape it does not have. The count is asserted
    here rather than left to be checked by eye, because a mis-counted
    guard raises nothing at all.

    The schema is named in the guard's *statement* rather than appended
    to its parameters, so there is no "the schema goes last" rule that a
    guard could get wrong: a schema is an identifier, and an identifier
    cannot be a parameter.
    """
    for migration in MIGRATIONS:
        for step in migration.steps:
            for engine in (step.sqlite, step.postgres):
                for guarded in engine.guarded:
                    assert guarded.guard.count("?") == len(guarded.guard_params), guarded


def test_every_schema_scoped_guard_names_its_schema() -> None:
    """A guard that reads the catalog says which schema it means.

    The catalog spans every schema the user can see, so a guard that does
    not name one is satisfied by another schema's table — and the
    migration it guards is then skipped while the version row is written
    anyway.
    """
    for migration in MIGRATIONS:
        for step in migration.steps:
            for engine in (step.sqlite, step.postgres):
                for guarded in engine.guarded:
                    if guarded.schema_scoped:
                        assert SCHEMA_PLACEHOLDER in guarded.guard, guarded
                    else:
                        assert SCHEMA_PLACEHOLDER not in guarded.guard, guarded


def test_no_step_is_empty_on_both_engines() -> None:
    """A step that does nothing anywhere is a version row that lies.

    The two halves of a step are allowed to differ — most of them do,
    because the dialects differ — but a step that is empty on *both* sides
    has no effect at all, and the version row written after it would say
    the database reached a shape it never reached.
    """
    for migration in MIGRATIONS:
        for step in migration.steps:
            assert step.sqlite or step.postgres


def test_a_step_names_its_table_and_column_in_its_own_sql() -> None:
    """The declared names and the SQL cannot disagree.

    ``table`` and ``column`` are interpolated into the statements, so a
    renamed column cannot leave the SQL pointing at the old one — but only
    if the interpolation really happens, and that is a property of the
    code that has to be asserted rather than assumed.

    The check is over each engine's half of the step rather than
    statement by statement, because one statement in a step can be about
    something other than the column — dropping the trigger that fills it,
    say — and demanding the column's name of that statement would be
    demanding the wrong thing.
    """
    for migration in MIGRATIONS:
        for step in migration.steps:
            for engine in (step.sqlite, step.postgres):
                sql = " ".join(
                    statement
                    for guarded in engine.guarded
                    for statement in guarded.statements
                )
                if not sql:
                    continue
                assert step.table in sql, step
                assert step.column in sql, step


def test_pending_returns_the_versions_above_the_current_one() -> None:
    assert [m.version for m in pending(0)] == [m.version for m in MIGRATIONS]
    assert [m.version for m in pending(SCHEMA_VERSION)] == []
    assert [m.version for m in pending(SCHEMA_VERSION - 1)] == [SCHEMA_VERSION]


# ---------------------------------------------------------------------------
# A fresh database
# ---------------------------------------------------------------------------


async def test_a_fresh_database_is_at_the_current_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_connecting_twice_changes_nothing(tmp_path: Path) -> None:
    """The second connect finds the version current and does no work."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    before = await _columns(db)
    await db.close()

    await db.connect()
    try:
        assert await _columns(db) == before
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_the_connection_enforces_foreign_keys_after_the_schema_is_applied(
    tmp_path: Path,
) -> None:
    """The pragma is set on the connection, not by the schema file.

    ``PRAGMA foreign_keys = ON`` is a **no-op inside a transaction**, and
    the schema file is applied inside one — so the line in the file does
    nothing at all, and the file says so. The connection sets it when it
    is opened, before the schema is applied, which is what makes the
    database's foreign keys real.

    Both connections are checked: the one the schema was applied on, and
    one of the readers.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        async with db.transaction() as conn, conn.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
            assert row is not None
            assert int(row[0]) == 1

        assert int(await db.fetch_value("PRAGMA foreign_keys")) == 1
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


async def test_a_version_1_database_is_migrated_forward(tmp_path: Path) -> None:
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        columns = await _columns(db)
        assert "arrival_seq" in [name for name, *_ in columns["cold_lake_signals"]]
        assert "arrival_seq" in [name for name, *_ in columns["queue_messages"]]
    finally:
        await db.close()


async def test_the_reversal_really_produced_a_version_1_database(tmp_path: Path) -> None:
    """The reversal is a fact the suite checks, not one it assumes.

    Every other test in this file builds its version-1 database by
    *reversing* the migration. If the reversal quietly stopped being a
    reversal — a dropped statement in the helper, a column that was never
    dropped — then the database would still carry the column version 2
    adds, the migration's guard would see the work already done, and every
    test would still pass while the ``ALTER TABLE ... ADD COLUMN`` was
    executed by **nothing**. The suite would not be able to tell.

    So the reversal is looked at, before the migration runs: the column
    version 2 adds is not there, and the version log records the base
    version and nothing else.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)

    conn = _raw(path)
    try:
        for table in ARRIVAL_SEQUENCES:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert "arrival_seq" not in columns, table
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
    finally:
        conn.close()

    assert versions == [BASE_SCHEMA_VERSION]


async def test_the_migration_really_adds_the_column_the_reversal_removed(
    tmp_path: Path,
) -> None:
    """The other half of the pair: the migration puts it back.

    The test above proves the reversal removed the column; this one proves
    the migration adds it. Together they say that the ``ALTER TABLE ...
    ADD COLUMN`` in the migration is executed by the suite, which is the
    thing the two of them exist to establish.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)

    db = Database(path)
    await db.connect()
    try:
        columns = await _columns(db)
        for table in ARRIVAL_SEQUENCES:
            assert "arrival_seq" in [name for name, *_ in columns[table]]
    finally:
        await db.close()


async def test_a_migrated_database_has_the_same_shape_as_a_fresh_one(
    tmp_path: Path,
) -> None:
    """The invariant. A database carried forward and a database created
    outright must be the same database.

    The comparison covers the columns (in order, with their types,
    defaults, nullability and primary-key flags), the declared indexes,
    the constraint-backed indexes that ``sqlite_master`` cannot describe,
    the foreign keys, the ``CHECK`` expressions, the triggers, and each
    table's ``STRICT`` flag. A migration that dropped a primary key, or a
    unique constraint, or a table's strictness, fails here.

    Two things SQLite cannot report are therefore not compared: a
    column's ``COLLATE`` (neither schema file declares one, and
    ``PRAGMA table_info`` does not report it), and the *name* of a
    foreign key (SQLite does not name them — the server's names are
    compared in ``tests/storage/test_postgres_migrations.py``).
    """
    fresh = Database(tmp_path / "fresh.db")
    await fresh.connect()
    fresh_columns = await _columns(fresh)
    fresh_triggers = await _triggers(fresh)
    fresh_indexes = await _index_sql(fresh)
    fresh_index_lists = await _index_lists(fresh)
    fresh_foreign_keys = await _foreign_keys(fresh)
    fresh_checks = await _check_constraints(fresh)
    fresh_strict = await _strict_flags(fresh)
    await fresh.close()

    migrated_path = tmp_path / "migrated.db"
    migrated = Database(migrated_path)
    await migrated.connect()
    await migrated.close()
    _downgrade_to_v1(migrated_path)

    migrated = Database(migrated_path)
    await migrated.connect()
    try:
        assert await _columns(migrated) == fresh_columns
        assert await _triggers(migrated) == fresh_triggers
        assert await _index_sql(migrated) == fresh_indexes
        assert await _index_lists(migrated) == fresh_index_lists
        assert await _foreign_keys(migrated) == fresh_foreign_keys
        assert await _check_constraints(migrated) == fresh_checks
        assert await _strict_flags(migrated) == fresh_strict
    finally:
        await migrated.close()


async def test_the_arrival_column_is_the_last_column_of_its_table(db: Database) -> None:
    """The migration appends the column, so the schema has to declare it last.

    ``ALTER TABLE ... ADD COLUMN`` puts the new column at the end of the
    table, so a migrated table's column order matches a fresh one's only
    because ``arrival_seq`` is declared last in the schema file. A column
    added to the file above it would leave the two orders different — and
    the fresh-versus-migrated comparison would then fail, which is what
    this test is here to explain.
    """
    columns = await _columns(db)
    for table, sequence in ARRIVAL_SEQUENCES.items():
        assert columns[table][-1][0] == sequence.column


async def test_a_migrated_database_fills_arrival_seq_in_order(tmp_path: Path) -> None:
    """The column the migration adds is the column the queue orders by."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _enqueue_raw(path, "m1")
    _enqueue_raw(path, "m2")

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT id, arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        assert [(r["id"], r["arrival_seq"]) for r in rows] == [("m1", 1), ("m2", 2)]
    finally:
        await db.close()


async def test_no_two_live_rows_share_an_arrival_order(tmp_path: Path) -> None:
    """A deleted row's order must not come back on a *live* row.

    A ``rowid`` is reused once the row that held it is deleted, so the
    arrival order a deleted row carried can be handed to the row that
    replaces it. That is fine — the order was freed with the row. What
    would not be fine is two live rows carrying the same order, because the
    ``LIMIT`` cut over that ordering would no longer be reproducible.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        for message_id in ("m1", "m2", "m3"):
            await db.execute(
                "INSERT INTO queue_messages (id, stream, payload, status, priority, "
                "attempts, max_attempts, visible_after, enqueued_at) "
                "VALUES (?, 's', '{}', 'ready', 100, 0, 3, "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (message_id,),
            )
        await db.execute("DELETE FROM queue_messages WHERE id = ?", ("m3",))
        await db.execute(
            "INSERT INTO queue_messages (id, stream, payload, status, priority, "
            "attempts, max_attempts, visible_after, enqueued_at) "
            "VALUES (?, 's', '{}', 'ready', 100, 0, 3, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            ("m4",),
        )
        rows = await db.fetch_all(
            "SELECT id, arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        orders = [r["arrival_seq"] for r in rows]
        assert len(set(orders)) == len(orders)
        # and the order still reflects the order the rows were written in
        assert [r["id"] for r in rows] == ["m1", "m2", "m4"]
    finally:
        await db.close()


async def test_the_migration_backfills_the_rows_that_already_existed(
    tmp_path: Path,
) -> None:
    """Rows written before the column existed still get a total order."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _enqueue_raw(path, "old0")
    _enqueue_raw(path, "old1")
    _enqueue_raw(path, "old2")

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT id, arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        assert [r["arrival_seq"] for r in rows] == [1, 2, 3]
    finally:
        await db.close()


async def test_a_trace_id_written_without_hyphens_is_reformatted(
    tmp_path: Path,
) -> None:
    """Version 4's SQLite half is exercised by this test and by no other.

    The step's guard looks for a trace id of 32 characters, so nothing
    else in the suite reaches it: the migration test reverses version 2
    only, and the rows the other tests write carry no trace id at all.
    Without this test the statement that reformats the id would never be
    executed by anything.

    The id that comes out is the canonical hyphenated form — the same form
    PostgreSQL's ``UUID`` column produces on its own, which is the whole
    reason the step exists.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _write_run_raw(path, "0123456789abcdef0123456789abcdef")

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all("SELECT trace_id FROM hot_serving_execution_runs")
        assert [r["trace_id"] for r in rows] == ["01234567-89ab-cdef-0123-456789abcdef"]
    finally:
        await db.close()


async def test_a_hyphenated_trace_id_is_left_alone(tmp_path: Path) -> None:
    """The reformatting touches only the ids that need it.

    A step that rewrote every row on every run would be a step that
    changed a correct value, and the guard is what stops it.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _write_run_raw(path, "01234567-89ab-cdef-0123-456789abcdef")

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all("SELECT trace_id FROM hot_serving_execution_runs")
        assert [r["trace_id"] for r in rows] == ["01234567-89ab-cdef-0123-456789abcdef"]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


async def test_a_newer_database_is_refused(future_version_path: Path) -> None:
    """An older build must not write to a newer schema."""
    db = Database(future_version_path)
    with pytest.raises(SchemaVersionError):
        await db.connect()
    await db.close()


async def test_a_refused_database_is_left_alone(future_version_path: Path) -> None:
    """The refusal happens before anything is written."""
    db = Database(future_version_path)
    with pytest.raises(SchemaVersionError):
        await db.connect()
    await db.close()

    conn = _raw(future_version_path)
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(version,) for version in range(1, SCHEMA_VERSION + 2)]


async def test_the_refusal_names_the_versions(future_version_path: Path) -> None:
    """A refusal that does not say what it refused is a puzzle."""
    db = Database(future_version_path)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    message = str(caught.value)
    assert str(SCHEMA_VERSION + 1) in message
    assert str(SCHEMA_VERSION) in message
    await db.close()


async def test_a_refused_database_leaves_nothing_open(future_version_path: Path) -> None:
    """The refusal happens before anything is written, and nothing leaks."""
    db = Database(future_version_path)
    with pytest.raises(SchemaVersionError):
        await db.connect()
    # The write connection was closed on the way out, so the next connect
    # tries again rather than reporting success on a database that was
    # never opened.
    assert db._write_conn is None
    with pytest.raises(SchemaVersionError):
        await db.connect()
    await db.close()


# ---------------------------------------------------------------------------
# An incomplete schema
# ---------------------------------------------------------------------------


async def test_a_half_applied_schema_is_refused_by_name(tmp_path: Path) -> None:
    """A schema that was applied half-way is refused, and named.

    The refusal has to come out of the *catalog*, not out of a query that
    happened to touch a missing table: a query against a table that is not
    there reports "no such table", which says nothing about the database
    and tells the operator to go and look at a query. The refusal instead
    names the tables that are missing, which says what happened and what
    to do about it.

    It also has to happen before anything is written. A refusal that
    migrated the database would leave it with a version log describing a
    shape it does not have — and the version it claims is the version that
    says it needs no repair.
    """
    path = tmp_path / "paa.db"
    conn = _raw(path)
    try:
        conn.execute("CREATE TABLE system_state_ledger (sequence_id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    # The *class* is part of the contract: every way the runner can refuse
    # a database raises ``SchemaVersionError``, so a caller that catches
    # that one class catches all of them. A bare ``StorageError`` here
    # would be a different promise, and the assertion would not notice.
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    message = str(caught.value)
    assert "incomplete" in message.lower()
    assert "cold_lake_signals" in message
    # and it is a refusal, not a missing table discovered the hard way
    assert "no such table" not in message.lower()

    # the refusal left the database exactly as it found it
    assert _tables(path) == ["system_state_ledger"]


async def test_a_half_applied_schema_with_an_empty_version_log_is_refused(
    tmp_path: Path,
) -> None:
    """The refusal does not depend on the version log being *absent*.

    A build that applied the schema and was interrupted before it wrote
    the version down leaves the log **empty** rather than missing, and
    that is the case the refusal exists for: the tables say one thing and
    the log says nothing. An empty log must not read as "new database",
    because stamping a half-applied database with the current version
    would be a claim its shape does not support.
    """
    path = tmp_path / "paa.db"
    conn = _raw(path)
    try:
        conn.execute("CREATE TABLE system_state_ledger (sequence_id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    message = str(caught.value)
    assert "incomplete" in message.lower()
    assert "cold_lake_signals" in message
    assert "no such table" not in message.lower()
    assert _tables(path) == ["schema_migrations", "system_state_ledger"]


class _BrokenSchemaDatabase(Database):
    """A database whose schema application fails on its last statement.

    The seam the production code uses is ``_apply_ddl`` — the method that
    runs a DDL script inside a transaction of its own — and this subclass
    reaches it with a deliberately broken script. The test therefore
    exercises the real path, rather than a re-implementation of it that
    would prove nothing about the code that runs.
    """
    async def _apply_schema(self) -> None:
        broken = schema_ddl("sqlite") + "\nINSERT INTO paa_no_such_table (x) VALUES (1);\n"
        await self._apply_ddl(broken)


async def test_the_schema_application_is_atomic(tmp_path: Path) -> None:
    """A schema that fails half-way leaves *none* of the schema behind.

    SQLite's ``executescript`` runs a script statement by statement in
    autocommit, so a script that fails half-way leaves the tables it had
    already created behind. That state is not a database at any version:
    it is exactly the shape an interrupted schema application leaves, and
    the runner refuses it by name rather than migrating it. So the
    application has to be atomic, and the only way to be sure of that is
    to fail it deliberately and look.

    Half of a schema would also be worse than none: the runner would find
    *some* runtime tables and refuse the database as incomplete, which
    reads as "your database is damaged" rather than "the schema was never
    applied".
    """
    path = tmp_path / "paa.db"
    db = _BrokenSchemaDatabase(path)
    with pytest.raises(StorageError):
        await db.connect()
    await db.close()

    assert _tables(path) == []

    # and a later, successful connect produces the whole schema
    db = Database(path)
    await db.connect()
    try:
        tables = await db.table_names()
        assert "cold_lake_signals" in tables
        assert "system_state_ledger" in tables
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# The shape of the list
# ---------------------------------------------------------------------------


def _migration(version: int) -> Migration:
    """The migration that reaches ``version``."""
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise AssertionError(f"no migration reaches version {version}")


def test_the_arrival_seq_steps_touch_the_two_ordered_tables() -> None:
    """The version that adds the ordering columns names the tables it orders."""
    steps = {step.table for step in _migration(2).steps}
    assert steps == {"cold_lake_signals", "queue_messages"}


def test_the_json_steps_cover_every_jsonb_column() -> None:
    """Every column that was JSONB in version 1 is converted."""
    converted = {(step.table, step.column) for step in _migration(3).steps}
    assert len(converted) == JSONB_COLUMN_COUNT
    assert ("system_state_ledger", "payload") in converted
    assert ("queue_messages", "payload") in converted


def test_every_jsonb_column_still_exists_in_the_schema() -> None:
    """The list version 3 converts names columns the schema still has.

    The list mirrors schema version 1 — a historical fact — but every
    column it names is still a column today, and a column that has been
    renamed or dropped would otherwise leave the migration converting
    something that is not there.
    """
    assert len(JSONB_COLUMNS) == JSONB_COLUMN_COUNT
    tables = {
        name: table.columns for name, table in parse_tables(schema_ddl("sqlite")).items()
    }
    for table, column, _default in JSONB_COLUMNS:
        assert table in tables, table
        assert column in tables[table], (table, column)


def test_the_json_steps_do_nothing_on_sqlite() -> None:
    """SQLite's columns were always text; the server's are what changed."""
    for step in _migration(3).steps:
        assert not step.sqlite
        assert step.postgres


def test_a_column_with_no_default_gets_no_default_statement() -> None:
    """A guard that can never fire is a statement that can never run.

    Nine of the columns version 1 declared as ``JSONB`` had no default at
    all, so there is no default to restate and nothing for a second
    guarded block to do. Emitting one anyway would put a statement in the
    list that no state can ever make necessary.
    """
    without_default = [entry for entry in JSONB_COLUMNS if entry[2] is None]
    assert without_default
    steps = {(step.table, step.column): step for step in _migration(3).steps}
    for table, column, default in JSONB_COLUMNS:
        blocks = steps[(table, column)].postgres.guarded
        if default is None:
            assert len(blocks) == 1, (table, column)
        else:
            assert len(blocks) == 2, (table, column)


def test_the_arrival_seq_steps_are_not_sqlite_only() -> None:
    """Both engines order the same two tables, by their own mechanisms."""
    for step in _migration(2).steps:
        assert step.sqlite
        assert step.postgres


def test_the_identifier_steps_fix_one_substrate_each() -> None:
    """The trace id is fixed on SQLite and the span id on PostgreSQL.

    Each substrate has one identifier whose stored form was wrong, and the
    two are not the same one: PostgreSQL normalises a trace id by itself
    and refuses a span id, while SQLite stores both as the text it was
    given.
    """
    trace, span = _migration(4).steps
    assert trace.column == "trace_id" and trace.sqlite and not trace.postgres
    assert span.column == "span_parent_id" and span.postgres and not span.sqlite


# ---------------------------------------------------------------------------
# The version log cannot lie about the shape
# ---------------------------------------------------------------------------


async def test_a_database_with_tables_but_no_version_is_migrated(
    tmp_path: Path,
) -> None:
    """A database whose version was never recorded is a real database.

    The old build applied the schema and recorded the version as two
    separate statements, so a crash in between left tables with no version
    row. Treating that as a brand-new database would stamp it with a
    version its shape does not have — and no later start would ever repair
    it, because the version it claims is the version that says it needs no
    repair.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        columns = await _columns(db)
        assert "arrival_seq" in [name for name, *_ in columns["cold_lake_signals"]]
    finally:
        await db.close()


async def test_a_database_with_a_gap_in_its_version_log_is_refused(
    tmp_path: Path,
) -> None:
    """A gap means the highest version does not describe the shape."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version = 2")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    with pytest.raises(SchemaVersionError):
        await db.connect()
    await db.close()


async def test_a_version_log_that_claims_a_shape_the_database_does_not_have_is_refused(
    tmp_path: Path,
) -> None:
    """The log is a claim, and the catalog is the evidence behind it.

    A database whose log records the current version but which is missing
    a table the runtime needs is not a database at that version. Believing
    the log would leave the database claiming a shape it does not have —
    and the version it claims is the version that says it needs no
    repair, so nothing would ever repair it.

    The refusal is a ``SchemaVersionError`` like the other two: a caller
    that catches that class catches every way the runner can refuse a
    database.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    conn = _raw(path)
    try:
        conn.execute("DROP TABLE queue_messages")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    with pytest.raises(SchemaVersionError) as caught:
        await db.connect()
    await db.close()

    message = str(caught.value)
    assert "queue_messages" in message
    assert str(SCHEMA_VERSION) in message


async def test_a_partially_applied_step_is_completed(tmp_path: Path) -> None:
    """A column that exists but was never backfilled is its own state.

    The guard on a step's *first* statement would be satisfied by the
    column's mere existence, and the backfill would never run — leaving
    every pre-existing row with an arrival order of zero, which is the
    non-total order the ``LIMIT`` cut depends on.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _enqueue_raw(path, "old0")
    _enqueue_raw(path, "old1")

    # The column exists — the first statement of the step ran — but the
    # backfill did not.
    conn = _raw(path)
    try:
        conn.execute("ALTER TABLE queue_messages ADD COLUMN arrival_seq INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT id, arrival_seq FROM queue_messages ORDER BY arrival_seq"
        )
        assert [r["arrival_seq"] for r in rows] == [1, 2]
    finally:
        await db.close()


async def test_a_failed_migration_does_not_leak_a_driver_error(
    tmp_path: Path,
) -> None:
    """A failure at the substrate boundary is a ``StorageError``.

    The driver error is the *cause*, not the message: a caller that
    catches ``PaaError`` at the edge of the runtime must not have to know
    which engine it is talking to.

    The failure is provoked by a trigger that refuses the backfill's
    update. Every table is still there, so the migration really runs and
    the statement that fails is one of its own — which is the point: a
    database that was merely *missing* a table would now be refused
    earlier, by the catalog check, and would never reach a statement.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)
    _enqueue_raw(path, "m1")
    conn = _raw(path)
    try:
        conn.execute(
            "CREATE TRIGGER trg_refuse_arrival BEFORE UPDATE ON queue_messages "
            "BEGIN SELECT RAISE(ABORT, 'this table may not be updated'); END"
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    with pytest.raises(StorageError):
        await db.connect()
    await db.close()


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------


async def test_the_sqlite_runner_says_that_it_migrated(tmp_path: Path) -> None:
    """An operator sees the migration on both substrates.

    The PostgreSQL runner logs ``postgres.schema_migrated``; the SQLite one
    logs ``database.schema_migrated``. A silence on one of them reads as
    "nothing happened", which is not what happened — and the two log
    events are the only place an operator learns that a migration ran at
    all.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)

    with structlog.testing.capture_logs() as captured:
        db = Database(path)
        await db.connect()
        await db.close()

    assert "database.schema_migrated" in {entry["event"] for entry in captured}


async def test_a_too_old_sqlite_is_refused_by_name(tmp_path: Path) -> None:
    """The floor is checked at connect time, and the refusal says why.

    The ledger's append uses ``RETURNING``, which arrived in SQLite
    3.35.0. On an older library every append fails with a syntax error
    deep inside the ledger, reported as a substrate error with no hint
    that the *host's* library is the problem — and the operator is left
    looking at the ledger rather than at the host.

    So the version is checked once, when the connection is opened, and
    the refusal names the version and the requirement.
    """
    from paa.storage.relational import database as database_module

    original = database_module.MINIMUM_SQLITE_VERSION
    database_module.MINIMUM_SQLITE_VERSION = (999, 0, 0)
    try:
        db = Database(tmp_path / "paa.db")
        with pytest.raises(StorageError) as caught:
            await db.connect()
        message = str(caught.value)
        assert "999.0.0" in message
        assert "RETURNING" in message
    finally:
        database_module.MINIMUM_SQLITE_VERSION = original
