"""The P-009 tier DDL and the v5 migration that carries it to existing rows.

Why this file exists
--------------------
ADR-0037 adds ``tier`` and ``tier_updated_at`` to ``hot_serving_active_facts``
alone, in both schemas, with a v5 migration that backfills every pre-existing
row to **T2**. A column added only to the ``.sql`` files reaches only
databases that do not yet exist; a column added only to ``migrations.py``
leaves a fresh database without it and the runner's incomplete-database
refusal names its own freshly-stamped database as broken. Both halves must
land, in one commit, and this file proves both:

* the v4 → v5 migration adds the columns, backfills the rows, and records
  version 5;
* re-running it changes nothing and errors on nothing;
* a crash mid-step — the column present but the backfill never run — is what
  the separate backfill guard exists to repair. One guard for both effects
  would be satisfied by the add, and the backfill would never run;
* a fresh database and a migrated one have the same shape, or the schema
  two ways diverges: the named CHECK and the column order are compared
  between the two;
* a tier outside ``T0``-``T4`` is refused by the schema, in both shapes.

The crash-mid-step construction is deliberate — and its limit is stated so
it is not taken for the thing it cannot be. A migration runs in one
transaction (the steps and their version row), so the real runner cannot
leave the column added with the backfill unrun behind. The partial state is
therefore constructed by hand in a tmp database: a nullable ``tier`` column
written without the default, the way an interrupted build that added the
column without filling it leaves behind.

The PostgreSQL half mirrors ``tests/storage/test_postgres_migrations.py``
and is skipped without ``PAA_TEST_POSTGRES_DSN`` — a missing server is a
missing server, not a pass. Without a DSN the server never saw the migration.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.storage.relational.database import Database
from paa.storage.relational.errors import CheckViolation
from paa.storage.relational.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    description_for,
)

# ---------------------------------------------------------------------------
# The PostgreSQL gate (mirroring test_postgres_migrations.py, applied only
# to the gated half at the bottom of this file)
# ---------------------------------------------------------------------------

#: The server the gated tests run against, or ``None`` when there is none.
DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")

HAS_PSYCOPG = (
    importlib.util.find_spec("psycopg") is not None
    and importlib.util.find_spec("psycopg_pool") is not None
)


# ---------------------------------------------------------------------------
# Helpers (the downgrade reverses v5 only, like test_migrations' reversals
# reverse only what they name)
# ---------------------------------------------------------------------------

_TABLE = "hot_serving_active_facts"
_TIER = "tier"
_TIER_UPDATED_AT = "tier_updated_at"
_STAMP = "2026-01-01T00:00:00+00:00"
_ENTITY = "00000000-0000-0000-0000-0000000000e1"
_FACT = "00000000-0000-0000-0000-0000000000f1"


def _raw(path: Path) -> sqlite3.Connection:
    """A plain connection, for reaching the state the runner must repair.

    The point of these tests is to reach a pre-migration state the runtime
    never produces, so the setup has to bypass the runtime.
    """
    return sqlite3.connect(path)


def _rewind_to_v4(path: Path) -> None:
    """Rewind the version log to version 4.

    Contiguous from 1: a gap is refused, so a rewind that left a hole
    would leave the runner refusing the database rather than migrating it.
    """
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version > 4")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (4, _STAMP, description_for(4)),
        )
        conn.commit()
    finally:
        conn.close()


def _downgrade_to_v4(path: Path, *, with_rows: bool = True) -> None:
    """Turn a current database back into the version-4 shape.

    The inverse of v5: drop the two tier columns and rewind the version log.
    ``tier``'s CHECK is a column-level named constraint, so it drops with the
    column. ``with_rows`` writes the fact rows the backfill acts on; it is
    False for a test about the shape of an empty table.
    """
    conn = _raw(path)
    try:
        conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_TIER_UPDATED_AT}")
        conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_TIER}")
        conn.commit()
    finally:
        conn.close()
    _rewind_to_v4(path)
    if with_rows:
        _write_fact_raw(path, _FACT)
        _write_fact_raw(path, f"{_FACT[:-1]}2")


def _write_fact_raw(path: Path, fact_id: str) -> None:
    """Write an entity and a fact row behind the runtime's back.

    Written while the database is still at version 4, so that the migration
    is what assigns its tier.
    """
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO hot_serving_entity_index "
            "(id, class, canonical_name, created_at, updated_at) "
            "VALUES (?, 'thing', ?, ?, ?)",
            (_ENTITY, f"entity-{fact_id[-1]}", _STAMP, _STAMP),
        )
        conn.execute(
            f"INSERT INTO {_TABLE} (id, entity_id, predicate, object_value, "
            "created_at, last_queried_at) VALUES (?, ?, 'predicate', 'value', ?, ?)",
            (fact_id, _ENTITY, _STAMP, _STAMP),
        )
        conn.commit()
    finally:
        conn.close()


async def _versions(db: Database) -> list[int]:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row["version"]) for row in rows]


async def _tier_rows(db: Database) -> list[tuple[str, Any, Any]]:
    rows = await db.fetch_all(
        f"SELECT id, {_TIER}, {_TIER_UPDATED_AT} FROM {_TABLE} ORDER BY id"
    )
    return [(row["id"], row[_TIER], row[_TIER_UPDATED_AT]) for row in rows]


async def _v4_database_with_rows(path: Path) -> None:
    """A tmp database at v4, holding two fact rows that have no tier yet."""
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v4(path)


# ---------------------------------------------------------------------------
# The version-5 migration over fact rows
# ---------------------------------------------------------------------------


def test_schema_version_is_five() -> None:
    """The runner's version is derived from the migration list's last entry."""
    assert SCHEMA_VERSION == 5


async def test_a_fresh_database_has_the_tier_columns(tmp_path: Path) -> None:
    """A database created outright already has the tier columns (the .sql half).

    New rows default to T2, and a backfilled row's tier_updated_at is NULL by
    construction — it is the mark of "never moved".
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        columns = [row[1] for row in await db.fetch_all(f"PRAGMA table_info({_TABLE})")]
        assert _TIER in columns and _TIER_UPDATED_AT in columns
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_a_version_4_database_is_migrated_and_backfilled(tmp_path: Path) -> None:
    """Pre-existing rows gain a tier, deterministically T2, on migration.

    The rows are written at v4, so the migration is what assigns their tier.
    """
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        rows = await _tier_rows(db)
        assert rows == [(_FACT, "T2", None), (f"{_FACT[:-1]}2", "T2", None)]
    finally:
        await db.close()


async def test_a_tier_outside_the_ladder_is_refused(tmp_path: Path) -> None:
    """The CHECK is a property of the migrated table, not of fresh installs alone."""
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    db = Database(path)
    await db.connect()
    try:
        with pytest.raises(CheckViolation) as caught:
            await db.execute(
                f"INSERT INTO {_TABLE} (id, entity_id, predicate, object_value, "
                "created_at, last_queried_at, tier) "
                "VALUES (?, ?, 'predicate', 'value', ?, ?, 'T9')",
                (f"{_FACT[:-1]}9", _ENTITY, _STAMP, _STAMP),
            )
        assert "ck_fact_tier" in str(caught.value)
    finally:
        await db.close()


async def test_running_the_migration_again_changes_nothing(tmp_path: Path) -> None:
    """Idempotency: once at v5 the runner has no steps left to take."""
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    db = Database(path)
    await db.connect()
    await db.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        assert await _tier_rows(db) == [(_FACT, "T2", None), (f"{_FACT[:-1]}2", "T2", None)]
    finally:
        await db.close()


async def test_v5_step_guards_are_satisfied_on_a_v5_database(tmp_path: Path) -> None:
    """R-W2-106: guard-level idempotency, not just runner-level.

    ``test_running_the_migration_again_changes_nothing`` reconnects at v5,
    where the runner has no pending steps — it would pass even if every
    step guard were broken (e.g. always-true). This test evaluates every
    v5 step's guards directly against a v5 database: each guard must
    return no row, which is exactly the runner's ``_still_needed == False``
    condition, so re-running the steps would execute nothing. The guards
    are read from the step API (``MIGRATIONS``) and evaluated with a raw
    connection — the same tmp-database construction the other tests use —
    rather than through the runner's transport-bound ``_run_step``.
    """
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    db = Database(path)
    await db.connect()
    await db.close()

    migration = next(m for m in MIGRATIONS if m.version == 5)
    conn = _raw(path)
    try:
        for step in migration.steps:
            for guarded in step.for_engine("sqlite").guarded:
                row = conn.execute(guarded.guard, guarded.guard_params).fetchone()
                assert row is None, (step.description, guarded.guard)
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _tier_rows(db) == [(_FACT, "T2", None), (f"{_FACT[:-1]}2", "T2", None)]
    finally:
        await db.close()


async def test_the_backfill_guard_repairs_a_crash_mid_step(tmp_path: Path) -> None:
    """The separate backfill guard fires when the column exists but was never filled.

    This is the state the guard-on-effect rule exists for: the None tier row,
    written while the tier column existed but the migration never ran. If the
    backfill shared the add-column's guard, the add's "column exists" answer
    would satisfy it and the row would stay NULL forever.
    """
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    conn = _raw(path)
    try:
        # A nullable tier with no default, the way an interrupted build that
        # added the column without filling it leaves behind.
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_TIER} TEXT")
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        assert await _tier_rows(db) == [(_FACT, "T2", None), (f"{_FACT[:-1]}2", "T2", None)]
    finally:
        await db.close()


async def test_an_assigned_tier_is_not_reset_by_a_repaired_hole(tmp_path: Path) -> None:
    """The backfill writes only never-assigned rows; a moved tier survives it.

    A reset would read as data loss: a fact already moved to T0 would be set
    back to the default by a hole in the migration that a re-run repairs.
    """
    path = tmp_path / "paa.db"
    await _v4_database_with_rows(path)

    conn = _raw(path)
    try:
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_TIER} TEXT")
        conn.execute(f"UPDATE {_TABLE} SET {_TIER} = 'T0' WHERE id = ?", (_FACT,))
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _tier_rows(db) == [(_FACT, "T0", None), (f"{_FACT[:-1]}2", "T2", None)]
    finally:
        await db.close()


async def test_a_migrated_database_shape_matches_a_fresh_one(tmp_path: Path) -> None:
    """The invariant: carried forward and created outright are the same table.

    The named CHECK and the column order are compared, because a different
    position in the stored CREATE statement would make the two shapes differ
    under fields the schema parser otherwise refuses to look at. The check
    expression list is read out of the stored statement text the same way
    ``tests/storage/test_migrations.py`` does it.
    """
    fresh = Database(tmp_path / "fresh.db")
    await fresh.connect()
    fresh_columns = await fresh.fetch_all(f"PRAGMA table_info({_TABLE})")
    fresh_checks = await fresh.fetch_all(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_TABLE,),
    )
    await fresh.close()

    migrated_path = tmp_path / "migrated.db"
    db = Database(migrated_path)
    await db.connect()
    await db.close()
    _downgrade_to_v4(migrated_path, with_rows=False)

    db = Database(migrated_path)
    await db.connect()
    try:
        migrated_columns = await db.fetch_all(f"PRAGMA table_info({_TABLE})")
        migrated_checks = await db.fetch_all(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_TABLE,),
        )

        def columns(rows: Any) -> list[tuple[str, str, str, int, int]]:
            return [
                (
                    row["name"],
                    row["type"],
                    row["dflt_value"] or "",
                    int(row["notnull"]),
                    int(row["pk"]),
                )
                for row in rows
            ]

        assert columns(migrated_columns) == columns(fresh_columns)

        def checks(rows: Any) -> list[str]:
            sql = rows[0]["sql"]
            return [" ".join(sql[cursor : end + 1].split()) for cursor, end in _check_ranges(sql)]

        assert checks(migrated_checks) == checks(fresh_checks)
    finally:
        await db.close()


def _check_ranges(sql: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` of every ``CHECK (...)`` in a CREATE statement."""
    found: list[tuple[int, int]] = []
    index = 0
    while True:
        index = sql.find("CHECK", index)
        if index == -1:
            return found
        cursor = index + len("CHECK")
        while cursor < len(sql) and sql[cursor] in " \t\n":
            cursor += 1
        if cursor >= len(sql) or sql[cursor] != "(":
            index += len("CHECK")
            continue
        depth = 0
        for position in range(cursor, len(sql)):
            if sql[position] == "(":
                depth += 1
            elif sql[position] == ")":
                depth -= 1
                if depth == 0:
                    found.append((cursor, position))
                    index = position
                    break


# ---------------------------------------------------------------------------
# The PostgreSQL half (gated — no DSN here, so this skips honestly)
# ---------------------------------------------------------------------------

async def _pg_execute(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Run one statement against the database, outside the runtime."""
    import psycopg

    connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    async with connection:
        await connection.execute(sql, params)


async def _pg_downgrade_to_v4(dsn: str) -> None:
    """Turn a server database back into the version-4 shape."""
    await _pg_execute(
        dsn, f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS ck_fact_tier"
    )
    await _pg_execute(dsn, f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_TIER_UPDATED_AT}")
    await _pg_execute(dsn, f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_TIER}")
    await _pg_execute(dsn, "DELETE FROM schema_migrations WHERE version > 4")
    await _pg_execute(
        dsn,
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES (%s, now(), %s) ON CONFLICT (version) DO NOTHING",
        (4, description_for(4)),
    )


@pytest.mark.requires_postgres
@pytest.mark.skipif(
    not HAS_PSYCOPG or not DSN,
    reason="needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN",
)
async def test_postgres_version_4_is_migrated_and_backfilled(
    postgres_dsn: str,
) -> None:
    """A pre-tier server database is carried to v5 with every row at T2."""
    from paa.storage.relational.postgres import PostgresDatabase

    entity_id = uuid.uuid4()
    fact_id = uuid.uuid4()

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _pg_downgrade_to_v4(postgres_dsn)

    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    await _pg_execute(
        postgres_dsn,
        "INSERT INTO hot_serving_entity_index (id, class, canonical_name, created_at, updated_at) "
        "VALUES (%s, 'thing', %s, %s, %s)",
        (entity_id, f"entity-{fact_id}", stamp, stamp),
    )
    await _pg_execute(
        postgres_dsn,
        f"INSERT INTO {_TABLE} (id, entity_id, predicate, object_value, "
        "created_at, last_queried_at) VALUES (%s, %s, 'predicate', 'value', %s, %s)",
        (fact_id, entity_id, stamp, stamp),
    )

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        assert [int(row["version"]) for row in rows] == list(
            range(1, SCHEMA_VERSION + 1)
        )
        rows = await db.fetch_all(
            f"SELECT id, {_TIER}, {_TIER_UPDATED_AT} FROM {_TABLE} ORDER BY created_at"
        )
        assert [(str(row["id"]), row[_TIER], row[_TIER_UPDATED_AT]) for row in rows] == [
            (str(fact_id), "T2", None)
        ]
    finally:
        await db.close()
