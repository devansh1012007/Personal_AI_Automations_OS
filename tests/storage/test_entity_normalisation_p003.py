"""Stored normalisation on the entity row: the P-003 (T-003) closure.

Why this file exists
--------------------
ADR-0029 records four limitations of the column fold and closes none
of them: (a) the column is folded by the engine, the term by Python;
(b) U+0130 folds three ways; (c) the alias blob escapes non-ASCII so
a non-ASCII alias is invisible to the count proxy; (d) the exact and
alias rungs still compare with the engine's ``lower()``. ADR-0029's
prescription is a stored, Python-computed normalisation - a schema
change in both DDL files with a Python-executed backfill - and this
file proves that prescription landed in v6:

* the v5 to v6 migration adds the two columns, backfills the rows
  by folding in Python (never an SQL ``lower()`` backfill), and
  records version 6;
* re-running it changes nothing and errors on nothing;
* a crash mid-step - the columns present but a row never filled -
  is what the separate backfill guard exists to repair. One guard
  for both effects would be satisfied by the add, and the backfill
  would never run;
* a fresh database and a migrated one have the same shape;
* the write path stores the fold and the read paths measure
  against it, so SQLite and PostgreSQL return identical NON-EMPTY
  rowsets for non-ASCII (T-003): éclair alias, U+0130, alias blob.

The crash-mid-step construction is deliberate - and its limit is
stated so it is not taken for the thing it cannot be. A migration
runs in one transaction (the steps and their version row), so the
real runner cannot leave the columns added with the backfill
unrun behind. The partial state is therefore constructed by hand
in a tmp database: nullable columns written without values, the
way an interrupted build that added the columns without filling
them leaves behind.

The PostgreSQL half mirrors ``tests/storage/test_postgres_migrations.py``
and is skipped without ``PAA_TEST_POSTGRES_DSN`` - a missing server
is a missing server, not a pass. Without a DSN the server never saw
the migration, and the parity claim is never made from SQLite alone.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.memory.facts import FactRepository, trigram_similarity
from paa.storage.relational.database import Database, dumps, to_iso, utc_now
from paa.storage.relational.dialect import PostgresDialect, SqliteDialect, fold_case
from paa.storage.relational.errors import CheckViolation
from paa.storage.relational.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    description_for,
)

#: The server the gated tests run against, or ``None`` when there is none.
DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")

HAS_PSYCOPG = (
    importlib.util.find_spec("psycopg") is not None
    and importlib.util.find_spec("psycopg_pool") is not None
)

_TABLE = "hot_serving_entity_index"
_CANONICAL_NORM = "canonical_name_norm"
_ALIASES_NORM = "aliases_norm"
_STAMP = "2026-01-01T00:00:00+00:00"
_ENTITY = "00000000-0000-0000-0000-0000000000e1"

#: The two trigger names the v6 FTS sync restores. The old (v5) form
#: inserts the raw columns; the new form inserts the normalised ones.
_OLD_TRIGGERS: dict[str, str] = {
    "trg_entity_fts_insert": (
        "CREATE TRIGGER trg_entity_fts_insert "
        "AFTER INSERT ON hot_serving_entity_index BEGIN "
        "INSERT INTO hot_serving_entity_fts(rowid, canonical_name, aliases) "
        "VALUES (new.rowid, new.canonical_name, new.aliases); END"
    ),
    "trg_entity_fts_delete": (
        "CREATE TRIGGER trg_entity_fts_delete "
        "AFTER DELETE ON hot_serving_entity_index BEGIN "
        "INSERT INTO hot_serving_entity_fts("
        "hot_serving_entity_fts, rowid, canonical_name, aliases) "
        "VALUES ('delete', old.rowid, old.canonical_name, old.aliases); END"
    ),
    "trg_entity_fts_update": (
        "CREATE TRIGGER trg_entity_fts_update "
        "AFTER UPDATE ON hot_serving_entity_index BEGIN "
        "INSERT INTO hot_serving_entity_fts("
        "hot_serving_entity_fts, rowid, canonical_name, aliases) "
        "VALUES ('delete', old.rowid, old.canonical_name, old.aliases); "
        "INSERT INTO hot_serving_entity_fts(rowid, canonical_name, aliases) "
        "VALUES (new.rowid, new.canonical_name, new.aliases); END"
    ),
}


def _raw(path: Path) -> sqlite3.Connection:
    """A plain connection, for reaching the state the runner must repair."""
    return sqlite3.connect(path)


def _rewind_to_v5(path: Path) -> None:
    """Rewind the version log to version 5."""
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version > 5")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (5, _STAMP, description_for(5)),
        )
        conn.commit()
    finally:
        conn.close()


def _downgrade_to_v5(path: Path, *, with_rows: bool = True) -> None:
    """Turn a current (v6) database back into the version-5 shape.

    The inverse of v6: drop the two normalised columns, restore the
    three FTS triggers to the old (raw) form, and rewind the log.
    ``with_rows`` writes the entity rows the backfill acts on.
    The triggers are dropped before the columns: SQLite refuses to
    drop a column a trigger still references.
    """
    conn = _raw(path)
    try:
        for name in _OLD_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_ALIASES_NORM}")
        conn.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {_CANONICAL_NORM}")
        for _name, statement in _OLD_TRIGGERS.items():
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    _rewind_to_v5(path)
    if with_rows:
        _write_entity_raw(path, _ENTITY, "Widget Service", ["Gadget Assembly"])
        _write_entity_raw(path, f"{_ENTITY[:-1]}2", "Widget Services", ["éclair"])
        _write_entity_raw(path, f"{_ENTITY[:-1]}3", "İstanbul", [])


def _write_entity_raw(
    path: Path, entity_id: str, canonical_name: str, aliases: list[str]
) -> None:
    """Write one entity row behind the runtime's back, at v5 shape.

    Written while the database is still at version 5, so that the
    migration is what fills its normalised columns.
    """
    conn = _raw(path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO hot_serving_entity_index "
            "(id, class, canonical_name, aliases, created_at, updated_at) "
            "VALUES (?, 'thing', ?, ?, ?, ?)",
            (entity_id, canonical_name, dumps(aliases), _STAMP, _STAMP),
        )
        conn.commit()
    finally:
        conn.close()


async def _versions(db: Database) -> list[int]:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row["version"]) for row in rows]


async def _norm_rows(db: Database) -> list[tuple[str, Any, Any]]:
    rows = await db.fetch_all(
        f"SELECT id, {_CANONICAL_NORM}, {_ALIASES_NORM} FROM {_TABLE} ORDER BY id"
    )
    return [
        (str(row["id"]), row[_CANONICAL_NORM], row[_ALIASES_NORM]) for row in rows
    ]


async def _v5_database_with_rows(path: Path) -> None:
    """A tmp database at v5, holding entity rows that have no fold yet."""
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v5(path)


def test_schema_version_is_six() -> None:
    """The runner's version is derived from the migration list's last entry."""
    assert SCHEMA_VERSION == 6


async def test_a_fresh_database_has_the_normalised_columns(tmp_path: Path) -> None:
    """A database created outright already has the normalised columns."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        columns = [row[1] for row in await db.fetch_all(f"PRAGMA table_info({_TABLE})")]
        assert _CANONICAL_NORM in columns and _ALIASES_NORM in columns
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_a_version_5_database_is_migrated_and_backfilled(tmp_path: Path) -> None:
    """Pre-existing rows gain a Python-folded, non-escaped norm, on migration.

    The fold is ``fold_case`` - the seam's one fold - and the alias
    norm is the folded list as JSON text with ``ensure_ascii=False``,
    so ``éclair`` appears raw rather than as its escape.
    """
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        rows = await _norm_rows(db)
        gadget_norm = json.dumps(
            ["gadget assembly"], ensure_ascii=False, separators=(",", ":")
        )
        eclair_norm = json.dumps(
            ["éclair"], ensure_ascii=False, separators=(",", ":")
        )
        expected = [
            (_ENTITY, "widget service", gadget_norm),
            (f"{_ENTITY[:-1]}2", "widget services", eclair_norm),
            (f"{_ENTITY[:-1]}3", "i̇stanbul", "[]"),
        ]
        assert rows == expected
        # The raw blob stays escaped: the convergence mechanism is the
        # norm column, not a global dumps change.
        raw = await db.fetch_one(
            f"SELECT aliases FROM {_TABLE} WHERE id = ?", (f"{_ENTITY[:-1]}2",)
        )
        assert raw is not None
        assert str(raw["aliases"]) == '["\\u00e9clair"]'
    finally:
        await db.close()


async def test_running_the_migration_again_changes_nothing(tmp_path: Path) -> None:
    """Idempotency: once at v6 the runner has no steps left to take."""
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)

    db = Database(path)
    await db.connect()
    await db.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        rows = await _norm_rows(db)
        assert len(rows) == 3
        assert all(row[1] is not None and row[2] is not None for row in rows)
    finally:
        await db.close()


async def test_v6_step_guards_are_satisfied_on_a_v6_database(tmp_path: Path) -> None:
    """Guard-level idempotency, not just runner-level.

    The reconnect test would pass even if every step guard were
    broken. This test evaluates every v6 step's SQLite guards directly
    against a v6 database: each guard must return no row, which is
    exactly the runner's ``_still_needed == False`` condition.
    """
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)

    db = Database(path)
    await db.connect()
    await db.close()

    migration = next(m for m in MIGRATIONS if m.version == 6)
    conn = _raw(path)
    try:
        for step in migration.steps:
            for guarded in step.for_engine("sqlite").guarded:
                row = conn.execute(guarded.guard, guarded.guard_params).fetchone()
                assert row is None, (step.description, guarded.guard)
    finally:
        conn.close()


async def test_the_backfill_guard_repairs_a_crash_mid_step(tmp_path: Path) -> None:
    """The separate backfill guard fires when columns exist but rows were never filled.

    This is the state the guard-on-effect rule exists for: the None
    norm row, written while the columns existed but the migration
    never ran. If the backfill shared the add-column's guard, the
    add's "column exists" answer would satisfy it and the row would
    stay NULL forever.
    """
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)

    conn = _raw(path)
    try:
        # Nullable columns with no default, the way an interrupted
        # build that added the columns without filling them leaves
        # behind. One row is filled by hand, one is left NULL.
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_CANONICAL_NORM} TEXT")
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_ALIASES_NORM} TEXT")
        conn.execute(
            f"UPDATE {_TABLE} SET {_CANONICAL_NORM} = ?, {_ALIASES_NORM} = ? WHERE id = ?",
            ("widget service", '["gadget assembly"]', _ENTITY),
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        rows = await _norm_rows(db)
        assert all(row[1] is not None and row[2] is not None for row in rows)
    finally:
        await db.close()


async def test_an_assigned_norm_is_not_reset_by_a_repaired_hole(tmp_path: Path) -> None:
    """The backfill writes only never-assigned rows; a filled norm survives it."""
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)

    conn = _raw(path)
    try:
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_CANONICAL_NORM} TEXT")
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_ALIASES_NORM} TEXT")
        conn.execute(
            f"UPDATE {_TABLE} SET {_CANONICAL_NORM} = 'sentinel', "
            f"{_ALIASES_NORM} = '[]' WHERE id = ?",
            (_ENTITY,),
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    await db.connect()
    try:
        rows = await _norm_rows(db)
        by_id = {row[0]: row for row in rows}
        assert by_id[_ENTITY][1] == "sentinel"
        assert by_id[f"{_ENTITY[:-1]}2"][1] == "widget services"
    finally:
        await db.close()


async def test_a_migrated_database_shape_matches_a_fresh_one(tmp_path: Path) -> None:
    """The invariant: carried forward and created outright are the same table."""
    fresh = Database(tmp_path / "fresh.db")
    await fresh.connect()
    fresh_columns = await fresh.fetch_all(f"PRAGMA table_info({_TABLE})")
    fresh_checks = await fresh.fetch_all(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_TABLE,),
    )
    fresh_triggers = await fresh.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
    )
    await fresh.close()

    migrated_path = tmp_path / "migrated.db"
    db = Database(migrated_path)
    await db.connect()
    await db.close()
    _downgrade_to_v5(migrated_path, with_rows=False)

    db = Database(migrated_path)
    await db.connect()
    try:
        migrated_columns = await db.fetch_all(f"PRAGMA table_info({_TABLE})")
        migrated_checks = await db.fetch_all(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_TABLE,),
        )
        migrated_triggers = await db.fetch_all(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
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

        def trigger_tuples(rows: Any) -> list[tuple[str, str]]:
            return [
                (str(row["name"]), " ".join((row["sql"] or "").split()))
                for row in rows
            ]

        assert trigger_tuples(migrated_triggers) == trigger_tuples(fresh_triggers)
    finally:
        await db.close()


async def test_the_normalised_columns_carry_no_check(tmp_path: Path) -> None:
    """No CHECK is the contract: any folded text is valid here."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_TABLE,),
        )
        sql = str(rows[0]["sql"] or "")
        check_texts = [
            " ".join(sql[cursor : end + 1].split())
            for cursor, end in _check_ranges(sql)
        ]
        for check in check_texts:
            assert _CANONICAL_NORM not in check
            assert _ALIASES_NORM not in check
        # And arbitrary folded text is accepted, not refused.
        entity_id = str(uuid.uuid4())
        stamp = _STAMP
        await db.execute(
            f"INSERT INTO {_TABLE} (id, class, canonical_name, aliases, "
            f"{_CANONICAL_NORM}, {_ALIASES_NORM}, created_at, updated_at) "
            "VALUES (?, 'thing', ?, '[]', ?, ?, ?, ?)",
            (entity_id, "Arbitrary Text", "arbitrary text", "[]", stamp, stamp),
        )
        with pytest.raises(CheckViolation):
            # A fact-level CHECK is still enforced elsewhere; this is
            # the positive control that the helper itself works.
            await db.execute(
                "INSERT INTO hot_serving_active_facts "
                "(id, entity_id, predicate, object_value, "
                "created_at, last_queried_at, tier) "
                "VALUES (?, ?, 'predicate', 'value', ?, ?, 'T9')",
                (str(uuid.uuid4()), entity_id, stamp, stamp),
            )
    finally:
        await db.close()


async def test_the_fact_contract_is_not_widened(tmp_path: Path) -> None:
    """The norm columns are an entity-index concern; fact reads do not see them."""
    # The Fact projection is named in facts.py; the norm columns must
    # not appear in it. Read the source the way the lock does.
    from paa.memory.facts import _FACT_COLUMNS

    assert _CANONICAL_NORM not in _FACT_COLUMNS
    assert _ALIASES_NORM not in _FACT_COLUMNS


# ---------------------------------------------------------------------------
# T-003 SQLite halves (ungated): write + read, U+0130, alias blob
# ---------------------------------------------------------------------------


async def test_eclair_alias_resolves_identically_non_empty(tmp_path: Path) -> None:
    """T-003 éclair: seed alias 'éclair', search 'ÉCLAIR', identical NON-EMPTY.

    On the embedded substrate the assertion is that the one substrate
    answers with the alias itself, non-empty - never a vacuous empty
    equality. The PostgreSQL half asserts the same triple there.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity(
            "person", "Widget Service", aliases=["éclair"]
        )
        resolved = await repo.resolve_entity("ÉCLAIR", fuzzy=True)
        assert resolved is not None
        assert resolved.id == entity_id
        assert resolved.match == "alias"
        assert resolved.score == 1.0
        # The candidate set is non-empty through the same seam.
        folded = fold_case("éclair")
        grams = tuple(sorted({folded[i : i + 3] for i in range(len(folded) - 2)}))
        sql, params = db.dialect.entity_candidates_sql(
            mode="broad", term="ÉCLAIR", grams=grams, limit=20
        )
        candidates = await db.fetch_all(sql, params)
        assert {str(row["id"]) for row in candidates} == {entity_id}
    finally:
        await db.close()


async def test_istanbul_resolves_via_the_fold_identically(tmp_path: Path) -> None:
    """U+0130: 'İstanbul' answers fuzzy 0.625 on this substrate.

    The fold is the seam's Python fold, so the exact rung misses the
    two-character mapping on both substrates and the gate decides.
    Before the fix the two substrates disagreed about match kind and
    score; after it they agree here - and the PostgreSQL half asserts
    the agreement.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity("place", "İstanbul")
        resolved = await repo.resolve_entity("istanbul", fuzzy=True)
        assert resolved is not None
        assert resolved.id == entity_id
        assert resolved.match == "fuzzy"
        assert resolved.score == pytest.approx(0.625)
    finally:
        await db.close()


async def test_izmir_broad_candidate_is_visible_but_gate_rejects(tmp_path: Path) -> None:
    """U+0130 İZMİR: broad offers the row, the gate refuses it, both agree.

    Stored 'İZMİR' folds to 'i̇zmi̇r' - the broad query sees the
    shared gram 'zmi' - but the gate scores 'izmir' against 'İZMİR'
    below the floor, so the answer is no match. Before the fix
    SQLite offered nothing and PostgreSQL answered exact; after it
    both offer the row and both answer no match.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity("place", "İZMİR")
        assert await repo.resolve_entity("izmir", fuzzy=True) is None
        folded_izmir = fold_case("izmir")
        grams = tuple(
            sorted({folded_izmir[i : i + 3] for i in range(len(folded_izmir) - 2)})
        )
        sql, params = db.dialect.entity_candidates_sql(
            mode="broad", term="izmir", grams=grams, limit=20
        )
        candidates = await db.fetch_all(sql, params)
        assert {str(row["id"]) for row in candidates} == {entity_id}
    finally:
        await db.close()


async def test_alias_blob_eee_fuzzy_visible(tmp_path: Path) -> None:
    """Alias blob (c): alias 'ééé' is visible to the broad query after."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity(
            "project", "Widget Service", aliases=["ééé"]
        )
        assert trigram_similarity("éééx", "ééé") == pytest.approx(0.5)
        resolved = await repo.resolve_entity("éééx", fuzzy=True)
        assert resolved is not None
        assert resolved.id == entity_id
        assert resolved.match == "fuzzy"
        assert resolved.score == pytest.approx(0.5)
    finally:
        await db.close()


async def test_cafe_noir_exact_via_stored_fold(tmp_path: Path) -> None:
    """'CAFÉ NOIR' resolves exactly on this substrate after the fix.

    Before, the engine's ASCII-only lower left 'É' alone, so the
    exact rung missed and the answer was fuzzy. After, the stored
    fold is the gate's fold and the exact rung answers.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity("person", "CAFÉ NOIR")
        resolved = await repo.resolve_entity("café noir", fuzzy=True)
        assert resolved is not None
        assert resolved.id == entity_id
        assert resolved.match == "exact"
        assert resolved.score == 1.0
    finally:
        await db.close()


async def test_write_path_stores_the_fold_atomically(tmp_path: Path) -> None:
    """upsert writes the norm with the row, and a refresh rewrites both.

    The invariant under the two-writer race: read and write are one
    transaction, so the norm can never belong to a different name
    than the row it sits on.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        entity_id = await repo.upsert_entity(
            "person", "Éclair Service", aliases=["ÉCLAIR"]
        )
        row = await db.fetch_one(
            f"SELECT {_CANONICAL_NORM}, {_ALIASES_NORM} FROM {_TABLE} WHERE id = ?",
            (entity_id,),
        )
        assert row is not None
        assert row[_CANONICAL_NORM] == "éclair service"
        assert row[_ALIASES_NORM] == '["éclair"]'
        again = await repo.upsert_entity(
            "person", "éclair service", aliases=["New Alias"]
        )
        assert again == entity_id
        row = await db.fetch_one(
            f"SELECT canonical_name, {_CANONICAL_NORM}, {_ALIASES_NORM} "
            f"FROM {_TABLE} WHERE id = ?",
            (entity_id,),
        )
        assert row is not None
        assert row[_CANONICAL_NORM] == "éclair service"
        assert '"new alias"' in str(row[_ALIASES_NORM])
    finally:
        await db.close()


async def test_concurrent_upserts_agree_on_the_norm(tmp_path: Path) -> None:
    """Concurrency: two writers of one name agree on id and norm.

    The race the transaction exists to serialise: both lookups
    run before either write would duplicate the row. The write
    lock serialises them, so one refreshes the other's row.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        first, second = await asyncio.gather(
            repo.upsert_entity("person", "Concurrent Name", aliases=["Alias One"]),
            repo.upsert_entity("person", "concurrent name", aliases=["Alias Two"]),
        )
        assert first == second
        row = await db.fetch_one(
            f"SELECT {_CANONICAL_NORM}, {_ALIASES_NORM} FROM {_TABLE} WHERE id = ?",
            (first,),
        )
        assert row is not None
        assert row[_CANONICAL_NORM] == "concurrent name"
        assert "alias one" in str(row[_ALIASES_NORM])
        assert "alias two" in str(row[_ALIASES_NORM])
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Boundary, hostile, idempotency
# ---------------------------------------------------------------------------


async def test_empty_and_short_terms_resolve_to_no_match(tmp_path: Path) -> None:
    """Boundary: nothing to search for resolves to nothing."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        await repo.upsert_entity("person", "Widget Service")
        assert await repo.resolve_entity("", fuzzy=True) is None
        assert await repo.resolve_entity("   ", fuzzy=True) is None
        assert await repo.resolve_entity("ab", fuzzy=True) is None
    finally:
        await db.close()


async def test_hostile_terms_resolve_to_no_match(tmp_path: Path) -> None:
    """Hostile: NUL, FTS5 operators, and the length-changing fold.

    The fold is idempotent everywhere, including for the one code
    point whose mapping changes the length - so a doubled fold is
    indistinguishable from a single one.
    """
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        await repo.upsert_entity("person", "Widget Service")
        assert await repo.resolve_entity("a\x00b", fuzzy=True) is None
        for term in ('"', "*", "AND", "OR", "NEAR", "(", ")"):
            assert await repo.resolve_entity(term, fuzzy=True) is None, term
        assert fold_case(fold_case("İ")) == fold_case("İ")
        assert len(fold_case("İ")) == 2
    finally:
        await db.close()


async def test_oversized_term_is_refused_before_a_statement(tmp_path: Path) -> None:
    """A term past the gram ceiling resolves to no match, never raises."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        await repo.upsert_entity("person", "Widget Service")
        long_term = "a" * 500
        assert await repo.resolve_entity(long_term, fuzzy=True) is None
    finally:
        await db.close()


async def test_upsert_is_idempotent(tmp_path: Path) -> None:
    """Re-upserting the same name is one row, one norm."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        repo = FactRepository(db)
        first = await repo.upsert_entity("person", "Idempotent Name")
        second = await repo.upsert_entity("person", "Idempotent Name")
        assert first == second
        count = await db.fetch_value(f"SELECT COUNT(*) FROM {_TABLE}")
        assert count == 1
    finally:
        await db.close()


async def test_dialect_seam_returns_statement_with_params(tmp_path: Path) -> None:
    """The seam shape still holds: placeholder count equals params."""
    from paa.storage.relational.postgres import _qmark_to_pyformat

    def _count(sql: str) -> int:
        return _qmark_to_pyformat(sql).count("%s")

    for dialect in (SqliteDialect(), PostgresDialect()):
        for mode in ("phrase", "broad"):
            sql, params = dialect.entity_candidates_sql(
                mode=mode, term="Widget Service", grams=("wid", "idg", "dge"), limit=20
            )
            assert _count(sql) == len(params)


async def test_fts_phrase_finds_the_normalised_alias(tmp_path: Path) -> None:
    """FTS sync: after migration the phrase query finds the alias."""
    path = tmp_path / "paa.db"
    await _v5_database_with_rows(path)
    db = Database(path)
    await db.connect()
    try:
        # The Object[] row holds the éclair alias; phrase search for
        # the folded term finds it through the new FTS content.
        rows = await db.fetch_all(
            "SELECT rowid FROM hot_serving_entity_fts "
            "WHERE hot_serving_entity_fts MATCH ?",
            ('"éclair"',),
        )
        assert len(rows) >= 1
        # And resolve agrees (alias rung).
        repo = FactRepository(db)
        resolved = await repo.resolve_entity("ÉCLAIR", fuzzy=True)
        assert resolved is not None
        assert resolved.match == "alias"
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
# The PostgreSQL half (gated - no DSN here, so this skips honestly)
# ---------------------------------------------------------------------------


async def _pg_execute(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Run one statement against the database, outside the runtime."""
    import psycopg

    connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    async with connection:
        await connection.execute(sql, params)


async def _pg_downgrade_to_v5(dsn: str) -> None:
    """Turn a server database back into the version-5 shape."""
    await _pg_execute(dsn, f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_ALIASES_NORM}")
    await _pg_execute(dsn, f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_CANONICAL_NORM}")
    await _pg_execute(dsn, "DELETE FROM schema_migrations WHERE version > 5")
    await _pg_execute(
        dsn,
        "INSERT INTO schema_migrations(version, applied_at, description) "
        "VALUES (%s, now(), %s) ON CONFLICT (version) DO NOTHING",
        (5, description_for(5)),
    )


@pytest.mark.requires_postgres
@pytest.mark.skipif(
    not HAS_PSYCOPG or not DSN,
    reason="needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN",
)
async def test_postgres_version_5_is_migrated_and_backfilled(
    postgres_dsn: str,
) -> None:
    """A pre-normalisation server database is carried to v6, folded in Python.

    Mirrors ``test_postgres_migrations.py``: the DSN names a server, the
    fixture makes a database of its own on it. Without a DSN this test
    skips - a missing server is a missing server, not a pass. CI runs
    it; locally it is NOT-RUN and the parity claim is not made from
    SQLite alone.
    """
    from paa.storage.relational.postgres import PostgresDatabase

    entity_id = uuid.uuid4()
    other_id = uuid.uuid4()

    db = PostgresDatabase(postgres_dsn)
    await db.connect()
    await db.close()
    await _pg_downgrade_to_v5(postgres_dsn)

    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    await _pg_execute(
        postgres_dsn,
        "INSERT INTO hot_serving_entity_index "
        "(id, class, canonical_name, aliases, created_at, updated_at) "
        "VALUES (%s, 'thing', %s, %s, %s, %s)",
        (
            entity_id,
            "İstanbul",
            json.dumps([], separators=(",", ":")),
            stamp,
            stamp,
        ),
    )
    await _pg_execute(
        postgres_dsn,
        "INSERT INTO hot_serving_entity_index "
        "(id, class, canonical_name, aliases, created_at, updated_at) "
        "VALUES (%s, 'thing', %s, %s, %s, %s)",
        (
            other_id,
            "Widget Service",
            json.dumps(["éclair"], separators=(",", ":")),
            stamp,
            stamp,
        ),
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
            f"SELECT id, {_CANONICAL_NORM}, {_ALIASES_NORM} FROM {_TABLE} ORDER BY created_at"
        )
        assert [(str(row["id"]), row[_CANONICAL_NORM], row[_ALIASES_NORM]) for row in rows] == [
            (str(entity_id), "i̇stanbul", "[]"),
            (str(other_id), "widget service", '["éclair"]'),
        ]
        # T-003 identical-rowset, server side: éclair alias resolves.
        repo = FactRepository(db)
        resolved = await repo.resolve_entity("ÉCLAIR", fuzzy=True)
        assert resolved is not None
        assert resolved.id == str(other_id)
        assert resolved.match == "alias"
        # U+0130: İstanbul answers fuzzy identically on both substrates.
        resolved = await repo.resolve_entity("istanbul", fuzzy=True)
        assert resolved is not None
        assert resolved.id == str(entity_id)
        assert resolved.match == "fuzzy"
        assert resolved.score == pytest.approx(0.625)
    finally:
        await db.close()
        # Keep the imports the linter would otherwise call unused.
        _ = (to_iso, utc_now)
