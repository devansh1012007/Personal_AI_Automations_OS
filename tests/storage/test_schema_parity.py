"""The lockstep guard for the two relational dialects.

``schema_sqlite.sql`` (ADR-0001, the laptop default) and
``schema_postgres.sql`` (ADR-0019, the Docker deployment) must stay
table-for-table and column-for-column identical. Only *types* may differ — the
PostgreSQL file deliberately uses UUID/JSONB/TIMESTAMPTZ/SMALLINT where SQLite
had to emulate them with TEXT/INTEGER — but a table or column present in one and
absent in the other is a bug that would silently corrupt data on whichever
backend the missing definition belongs to.

This test parses both files with a small DDL lexer (no database required, so it
runs on the laptop CI that has no PostgreSQL) and asserts the table sets and,
per table, the column sets match.

The lexer lives in :mod:`paa.storage.relational.schema_parser`, not here: the
runtime reads the same files to learn which tables a database at the base
version is expected to hold, and two copies of a DDL lexer is one copy too
many — the two could disagree about what a table in the file *is*, and the
runtime would then refuse a database the parity test calls whole. It is a
lexer rather than a regular expression because a *phantom* name is not a
cosmetic error: a table invented inside a string literal or a block comment
is a table the runtime refuses a whole database over.

Two documented, intentional asymmetries are encoded as allow-lists:

* ``hot_serving_entity_fts`` is a SQLite FTS5 *virtual* table. PostgreSQL
  expresses the same trigram search as a ``pg_trgm`` GIN index on the base
  table, so it has no table equivalent. Virtual tables are excluded from the
  comparison by construction (the parser only reads ``CREATE TABLE``).
* Nothing else. Any other divergence fails the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from paa.storage.relational.schema_parser import parse_tables, strip_comments

_RELATIONAL = Path(__file__).resolve().parents[2] / "src" / "paa" / "storage" / "relational"
_SQLITE_SQL = _RELATIONAL / "schema_sqlite.sql"
_POSTGRES_SQL = _RELATIONAL / "schema_postgres.sql"


def _parse_tables(sql: str) -> dict[str, set[str]]:
    """Map every ``CREATE TABLE`` to its set of column names."""
    return {name: set(table.columns) for name, table in parse_tables(sql).items()}


@pytest.fixture(scope="module")
def sqlite_tables() -> dict[str, set[str]]:
    return _parse_tables(_SQLITE_SQL.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def postgres_tables() -> dict[str, set[str]]:
    return _parse_tables(_POSTGRES_SQL.read_text(encoding="utf-8"))


def test_both_schema_files_exist() -> None:
    assert _SQLITE_SQL.is_file()
    assert _POSTGRES_SQL.is_file()


def test_parser_finds_the_core_tables(sqlite_tables: dict[str, set[str]]) -> None:
    """Guard against the parser silently matching nothing and passing vacuously."""
    assert "system_state_ledger" in sqlite_tables
    assert "cold_lake_signals" in sqlite_tables
    assert len(sqlite_tables) >= 20
    # The ledger's columns are the ones the whole system depends on.
    assert {"event_id", "correlation_id", "payload", "event_hash"} <= (
        sqlite_tables["system_state_ledger"]
    )


def test_table_sets_match(
    sqlite_tables: dict[str, set[str]], postgres_tables: dict[str, set[str]]
) -> None:
    """Every real table exists in both dialects (FTS5 virtual table excluded)."""
    only_sqlite = set(sqlite_tables) - set(postgres_tables)
    only_postgres = set(postgres_tables) - set(sqlite_tables)
    assert not only_sqlite, f"tables in SQLite but not PostgreSQL: {sorted(only_sqlite)}"
    assert not only_postgres, f"tables in PostgreSQL but not SQLite: {sorted(only_postgres)}"


def test_virtual_fts_table_is_sqlite_only(sqlite_tables: dict[str, set[str]]) -> None:
    """The FTS5 shadow table is a virtual table, so the parser must not see it.

    Its trigram role is filled in PostgreSQL by a pg_trgm GIN index. If this ever
    starts appearing as a real table, the parity comparison would wrongly demand
    a PostgreSQL twin for it.
    """
    assert "hot_serving_entity_fts" not in sqlite_tables


def test_columns_match_per_table(
    sqlite_tables: dict[str, set[str]], postgres_tables: dict[str, set[str]]
) -> None:
    """Column names agree table-by-table; only types are allowed to differ."""
    mismatches: dict[str, dict[str, list[str]]] = {}
    for table in sorted(set(sqlite_tables) & set(postgres_tables)):
        sq = sqlite_tables[table]
        pg = postgres_tables[table]
        if sq != pg:
            mismatches[table] = {
                "only_sqlite": sorted(sq - pg),
                "only_postgres": sorted(pg - sq),
            }
    assert not mismatches, f"column drift between dialects: {mismatches}"


def test_the_recorded_worker_image_matches_the_executed_one() -> None:
    """The image the ledger *records* is the image the sandbox *runs*.

    The image name lives in **four** places: the backend's default (what
    ``docker run`` is given), the ledger event's default (what is written to
    the row), and this pair of schemas (what the column defaults to). The
    parity test above proves the two schemas agree with each other; it says
    nothing about the two Python constants, so those could drift — and until
    Phase 0 they *had*: the string named an image nobody had ever built.

    This test is the missing link, and it is the only thing that makes the
    claim "the recorded image and the executed image cannot drift apart"
    true rather than aspirational.

    The ledger event's default is included because it is the one the *Python*
    code writes when it builds the event without naming an image — and an
    event that carries a different string from the one the schema defaults to
    would put two different images in the same column depending on which
    code path wrote the row.
    """
    from paa.ledger.events import LedgerEvent
    from paa.sandbox.docker_backend import DEFAULT_IMAGE

    event_default = LedgerEvent.model_fields["allocated_worker_image"].default
    assert event_default == DEFAULT_IMAGE, (
        f"the ledger event records {event_default!r} but the sandbox runs "
        f"{DEFAULT_IMAGE!r}"
    )

    pattern = re.compile(
        r"allocated_worker_image\s+TEXT\s+NOT NULL\s+DEFAULT\s+'([^']+)'",
        re.IGNORECASE,
    )
    for path in (_SQLITE_SQL, _POSTGRES_SQL):
        sql = strip_comments(path.read_text(encoding="utf-8"))
        found = pattern.search(sql)
        assert found is not None, f"{path.name} has no allocated_worker_image default"
        assert found.group(1) == DEFAULT_IMAGE, (
            f"{path.name} records {found.group(1)!r} but the sandbox runs "
            f"{DEFAULT_IMAGE!r}"
        )
