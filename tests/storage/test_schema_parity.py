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

_RELATIONAL = Path(__file__).resolve().parents[2] / "src" / "paa" / "storage" / "relational"
_SQLITE_SQL = _RELATIONAL / "schema_sqlite.sql"
_POSTGRES_SQL = _RELATIONAL / "schema_postgres.sql"

#: Line-leading keywords that mark a table *constraint*, not a column definition.
_CONSTRAINT_KEYWORDS = (
    "constraint",
    "primary",
    "unique",
    "check",
    "foreign",
    "exclude",
    "like",
)


def _strip_comments(sql: str) -> str:
    """Remove ``-- ...`` line comments so they cannot be mistaken for tokens."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _split_top_level(body: str) -> list[str]:
    """Split a table body on commas that are not nested inside parentheses.

    CHECK constraints and multi-column type modifiers contain their own commas
    inside ``(...)``; splitting naively would shred them. This tracks paren depth
    and only breaks at depth zero.
    """
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _parse_tables(sql: str) -> dict[str, set[str]]:
    """Map every ``CREATE TABLE`` to its set of column names.

    ``CREATE VIRTUAL TABLE`` (SQLite FTS5) is intentionally not matched — those
    have no PostgreSQL table equivalent and must not enter the comparison.
    """
    text = _strip_comments(sql)
    tables: dict[str, set[str]] = {}

    # Match `CREATE TABLE [IF NOT EXISTS] name (` and remember where the body
    # opens; the matching close paren is found by depth-counting from there.
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        open_idx = match.end() - 1  # index of the "("
        depth = 0
        close_idx = None
        for idx in range(open_idx, len(text)):
            if text[idx] == "(":
                depth += 1
            elif text[idx] == ")":
                depth -= 1
                if depth == 0:
                    close_idx = idx
                    break
        assert close_idx is not None, f"unbalanced parens in table {name!r}"
        body = text[open_idx + 1 : close_idx]

        columns: set[str] = set()
        for item in _split_top_level(body):
            if not item:
                continue
            first = item.split(None, 1)[0].strip('"').lower()
            if first in _CONSTRAINT_KEYWORDS:
                continue
            columns.add(first)
        tables[name] = columns
    return tables


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
