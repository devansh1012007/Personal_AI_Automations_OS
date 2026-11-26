"""The version-7 migration: ``ck_market_kind`` admits ``'pipeline'`` (C8.6).

The pattern ``tests/storage/test_tier_migration_p009.py`` pins for the
version-5 migration, restated for the version-7 one: a database at the
pre-migration shape is built behind the runtime's back (the downgrade
reverses the rebuild the migration performs), the runtime then migrates it
on connect, and the tests prove the *effect* — the kind vocabulary admits
``'pipeline'`` while still refusing a word outside it, the rows a v6
database already held survive, the re-run changes nothing, and the shape a
migrated database ends in is the shape a fresh one is born in (the
``_shape`` comparison in ``test_migrations.py`` covers every part of it).

The PostgreSQL half of the migration (drop + re-add the named CHECK) is
exercised by the server-gated contract suites; this file is the SQLite
half, which is the rebuild — the riskier dialect by far, because a rebuild
that dropped a constraint, an index or the STRICT flag would still *work*
while no longer being the same database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from paa.storage.relational.database import Database
from paa.storage.relational.errors import CheckViolation
from paa.storage.relational.migrations import SCHEMA_VERSION, description_for

_TABLE = "marketplace_packages"
_STAMP = "2026-01-01T00:00:00+00:00"

# The v6 shape of the kind CHECK — the vocabulary the migration widens.
_V6_KIND_CHECK = (
    "CONSTRAINT ck_market_kind CHECK (kind IN "
    "('skill','agent','playbook','config','bundle'))"
)
_V7_KIND_CHECK = (
    "CONSTRAINT ck_market_kind CHECK (kind IN "
    "('skill','agent','playbook','config','bundle','pipeline'))"
)


def _raw(path: Path) -> sqlite3.Connection:
    """A plain connection, for reaching the state the runner must repair.

    The point of these tests is to reach a pre-migration state the runtime
    never produces, so the setup has to bypass the runtime.
    """
    return sqlite3.connect(path, isolation_level=None)


def _rewind_to_v6(path: Path) -> None:
    """Rewind the version log to version 6.

    Contiguous from 1: a gap is refused, so a rewind that left a hole
    would leave the runner refusing the database rather than migrating it.
    """
    conn = _raw(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version > 6")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at, description) "
            "VALUES (?, ?, ?)",
            (6, _STAMP, description_for(6)),
        )
    finally:
        conn.close()


def _rebuild_without_pipeline(path: Path) -> None:
    """Reverse the migration: rebuild the table with the v6 kind CHECK.

    The inverse of version 7's SQLite half — the same rename/create/copy/
    drop dance, with the narrower vocabulary — so the database the tests
    migrate really is a v6 database, not a v7 database whose version row
    lies. The one declared index is recreated with it.
    """
    legacy = f"{_TABLE}_test_v6"
    ddl = (
        f"CREATE TABLE {_TABLE} ("
        "    id              TEXT PRIMARY KEY,"
        "    package_name    TEXT NOT NULL,"
        "    version         TEXT NOT NULL,"
        "    kind            TEXT NOT NULL,"
        "    publisher       TEXT NOT NULL,"
        "    publisher_key   TEXT,"
        "    description     TEXT NOT NULL DEFAULT '',"
        "    manifest        TEXT NOT NULL,"
        "    content_hash    TEXT NOT NULL,"
        "    signature       TEXT,"
        "    signature_state TEXT NOT NULL DEFAULT 'unverified',"
        "    source_registry TEXT,"
        "    installed_at    TEXT,"
        "    trust_score     REAL NOT NULL DEFAULT 0.0,"
        "    CONSTRAINT uq_marketplace_pkg UNIQUE (package_name, version),"
        f"    {_V6_KIND_CHECK},"
        "    CONSTRAINT ck_market_sig CHECK (signature_state IN"
        "      ('unverified','valid','invalid','untrusted_key')),"
        "    CONSTRAINT ck_market_manifest_json CHECK (json_valid(manifest))"
        ") STRICT"
    )
    index = (
        "CREATE INDEX IF NOT EXISTS idx_marketplace_installed"
        f"    ON {_TABLE}(installed_at DESC) WHERE installed_at IS NOT NULL"
    )
    conn = _raw(path)
    try:
        conn.execute(f"ALTER TABLE {_TABLE} RENAME TO {legacy}")
        conn.execute(ddl)
        conn.execute(
            f"INSERT INTO {_TABLE} (id, package_name, version, kind, publisher, "
            "publisher_key, description, manifest, content_hash, signature, "
            "signature_state, source_registry, installed_at, trust_score) "
            "SELECT id, package_name, version, kind, publisher, publisher_key, "
            "description, manifest, content_hash, signature, signature_state, "
            "source_registry, installed_at, trust_score "
            f"FROM {legacy}"
        )
        conn.execute(f"DROP TABLE {legacy}")
        conn.execute(index)
    finally:
        conn.close()


def _write_skill_row_raw(path: Path) -> None:
    """Write one v6-era package row behind the runtime's back.

    Written while the database is still at version 6, so the migration's
    copy step — not the runtime's insert path — is what carries it across
    the rebuild.
    """
    conn = _raw(path)
    try:
        conn.execute(
            f"INSERT INTO {_TABLE} (id, package_name, version, kind, publisher, "
            "manifest, content_hash) "
            "VALUES ('11111111-2222-3333-4444-555555555555', 'greeter', '1.0.0', "
            "'skill', 'fixture', '{}', 'a')"
        )
    finally:
        conn.close()


def _downgrade_to_v6(path: Path, *, with_rows: bool = True) -> None:
    """A current database turned back into the version-6 shape."""
    _rebuild_without_pipeline(path)
    _rewind_to_v6(path)
    if with_rows:
        _write_skill_row_raw(path)


async def _versions(db: Database) -> list[int]:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    return [int(row["version"]) for row in rows]


def _stored_table_sql(path: Path) -> str:
    conn = _raw(path)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_TABLE,),
        ).fetchall()
    finally:
        conn.close()
    return " ".join((rows[0][0] or "").split())


def test_schema_version_is_seven() -> None:
    """The migration list's last entry names this migration's version."""
    assert SCHEMA_VERSION == 7


async def test_a_fresh_database_admits_the_pipeline_kind(tmp_path: Path) -> None:
    """The .sql half: a database created outright accepts the kind."""
    db = Database(tmp_path / "paa.db")
    await db.connect()
    try:
        await db.execute(
            f"INSERT INTO {_TABLE} (id, package_name, version, kind, publisher, "
            "manifest, content_hash) "
            "VALUES ('11111111-2222-3333-4444-555555555555', 'demo.echo', '1.2.3', "
            "'pipeline', 'local_dir', '{}', 'a')"
        )
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
    finally:
        await db.close()


async def test_a_v6_database_is_migrated_and_keeps_its_rows(tmp_path: Path) -> None:
    """The migration half: the vocabulary widens and the rows survive."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v6(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        # The v6 row the downgrade wrote crossed the rebuild's copy step.
        row = await db.fetch_one(
            f"SELECT package_name, version, kind FROM {_TABLE} "
            "WHERE package_name = 'greeter'"
        )
        assert row is not None
        assert row["kind"] == "skill"
        # The widened vocabulary admits the new kind…
        await db.execute(
            f"INSERT INTO {_TABLE} (id, package_name, version, kind, publisher, "
            "manifest, content_hash) "
            "VALUES ('22222222-3333-4444-5555-666666666666', 'demo.echo', '1.2.3', "
            "'pipeline', 'local_dir', '{}', 'b')"
        )
    finally:
        await db.close()


async def test_a_kind_outside_the_vocabulary_is_still_refused(tmp_path: Path) -> None:
    """The CHECK is widened, not removed: a hostile kind is refused by the
    schema, not by the installer's goodwill."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v6(path)

    db = Database(path)
    await db.connect()
    try:
        with pytest.raises(CheckViolation):
            await db.execute(
                f"INSERT INTO {_TABLE} (id, package_name, version, kind, publisher, "
                "manifest, content_hash) "
                "VALUES ('33333333-4444-5555-6666-777777777777', 'evil', '0.9', "
                "'nonsense', 'fixture', '{}', 'c')"
            )
    finally:
        await db.close()


async def test_running_the_migration_again_changes_nothing(tmp_path: Path) -> None:
    """The guard asks the finished effect, so a re-run is a no-op."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v6(path)

    db = Database(path)
    await db.connect()
    await db.close()
    before = _stored_table_sql(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        assert _stored_table_sql(path) == before
        assert _V7_KIND_CHECK in before
    finally:
        await db.close()


async def test_a_migrated_database_shape_matches_a_fresh_one(tmp_path: Path) -> None:
    """The invariant, for this migration: a database carried forward and a
    database created outright must be the same database.

    The comparison is the one ``test_migrations.py`` keeps for the whole
    schema (columns, triggers, indexes, index lists, foreign keys, CHECK
    expressions, STRICT flags); here it is asked of a database whose
    marketplace table went through the rebuild specifically.
    """
    from tests.storage.test_migrations import _shape

    fresh = Database(tmp_path / "fresh.db")
    await fresh.connect()
    fresh_shape = await _shape(fresh)
    await fresh.close()

    path = tmp_path / "migrated.db"
    migrated = Database(path)
    await migrated.connect()
    await migrated.close()
    _downgrade_to_v6(path)

    migrated = Database(path)
    await migrated.connect()
    try:
        assert await _shape(migrated) == fresh_shape
    finally:
        await migrated.close()


async def test_the_reversal_really_produced_a_v6_database(tmp_path: Path) -> None:
    """The downgrade is a fact the suite checks, not one it assumes.

    If the reversal quietly stopped rebuilding the table, the migration's
    guard would see ``'pipeline'`` already stored, the step would be
    skipped, and every other test here would pass while the rebuild was
    executed by **nothing**. So the v6 state is looked at before the
    migration runs: the stored table names no ``pipeline``, and the
    version log stops at 6.
    """
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v6(path, with_rows=False)

    sql = _stored_table_sql(path)
    assert "pipeline" not in sql
    assert _V6_KIND_CHECK in sql

    conn = _raw(path)
    try:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        conn.close()
    assert versions[-1] == 6


async def test_the_migration_really_rebuilt_the_table_the_reversal_unbuilt(
    tmp_path: Path,
) -> None:
    """The other half of the pair: the migration puts the widened CHECK in."""
    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v6(path, with_rows=False)

    db = Database(path)
    await db.connect()
    try:
        assert _V7_KIND_CHECK in _stored_table_sql(path)
    finally:
        await db.close()


async def test_a_v6_database_at_version_one_is_carried_all_the_way_forward(
    tmp_path: Path,
) -> None:
    """The rebuild runs from every pre-7 version, not just the one before
    it: a database the version-1 reversal produced (test_migrations.py's
    own downgrade) still reaches v7 with the widened vocabulary."""
    from tests.storage.test_migrations import _downgrade_to_v1

    path = tmp_path / "paa.db"
    db = Database(path)
    await db.connect()
    await db.close()
    _downgrade_to_v1(path)

    db = Database(path)
    await db.connect()
    try:
        assert await _versions(db) == list(range(1, SCHEMA_VERSION + 1))
        assert "pipeline" in _stored_table_sql(path)
    finally:
        await db.close()
