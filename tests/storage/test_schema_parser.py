"""The DDL lexer that reads the two ``.sql`` schema files.

The gap these tests close
-------------------------
The parser used to be a regular expression plus a parenthesis counter, and
it was on the **production** path: the migration runner reads the schema
file to learn which tables a database at the base version is expected to
hold, and a *phantom* table in that set makes the runner refuse a whole
database as incomplete. A phantom name is not a cosmetic error — it is a
database that cannot be opened, with a message that blames the operator's
backup rather than the reader.

Every input below was a defect in the old reader, one DDL edit away from
happening. The old reader is reproduced in the comments so the shape of
each failure is legible without a checkout of the previous revision.

The invariants
--------------
* A name that is not in the file is never reported. A string literal, a
  block comment and a nested expression are all places a reader can
  mistake text for code.
* A construct the reader does not understand is an **error**, not a
  table and not a column. The reader names the file and the construct.
* The reader is a *lexer*: it knows about literals, quoted identifiers,
  comments and trigger bodies, and it knows where a statement ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.storage.relational.schema_parser import (
    SchemaParseError,
    parse_tables,
    split_statements,
    split_top_level,
    strip_comments,
)

_RELATIONAL = Path(__file__).resolve().parents[2] / "src" / "paa" / "storage" / "relational"
_SQLITE_SQL = _RELATIONAL / "schema_sqlite.sql"
_POSTGRES_SQL = _RELATIONAL / "schema_postgres.sql"


def _names(sql: str) -> set[str]:
    return set(parse_tables(sql, source="probe.sql"))


def _columns(sql: str, table: str) -> set[str]:
    return set(parse_tables(sql, source="probe.sql")[table].columns)


# ---------------------------------------------------------------------------
# The adversarial inputs
# ---------------------------------------------------------------------------


def test_a_table_declared_inside_a_string_literal_is_not_a_table() -> None:
    """A literal is a value, not code.

    The old reader matched ``CREATE TABLE`` inside the literal and
    reported a table called ``fake`` — a name the database does not
    have, which the runner would then refuse the database over.
    """
    sql = "CREATE TABLE real (x TEXT, note TEXT DEFAULT 'CREATE TABLE fake (x)');"
    assert _names(sql) == {"real"}
    assert _columns(sql, "real") == {"x", "note"}


def test_a_table_declared_inside_a_block_comment_is_not_a_table() -> None:
    """A block comment is not code.

    The old reader did not strip block comments at all, so a commented
    out table joined the table set.
    """
    sql = "CREATE TABLE real (x TEXT);\n/* CREATE TABLE ghost (x) */\n"
    assert _names(sql) == {"real"}


def test_a_line_comment_inside_a_string_literal_does_not_eat_the_line() -> None:
    """The comment stripper must not run inside a literal.

    The old reader stripped ``--`` to the end of the line wherever it
    found one, so a literal holding ``--`` unbalanced the parentheses of
    the table the literal sat in, and the reader raised ``ValueError:
    unbalanced parentheses`` at connect time.
    """
    sql = "CREATE TABLE real (x TEXT, note TEXT DEFAULT 'x -- y');"
    assert _names(sql) == {"real"}
    assert _columns(sql, "real") == {"x", "note"}


def test_a_comment_inside_a_string_literal_does_not_hide_a_table() -> None:
    """The other direction: a literal must not swallow what follows it."""
    sql = "CREATE TABLE real (a TEXT DEFAULT 'x -- CREATE TABLE fake (y)');"
    assert _names(sql) == {"real"}


def test_a_constraint_written_without_a_space_is_not_a_column() -> None:
    """``UNIQUE(a, b)`` is a constraint.

    The old reader split the item on whitespace, so it read the name as
    ``unique(a,`` — a column the table does not have.
    """
    sql = "CREATE TABLE real (a TEXT, b TEXT, UNIQUE(a, b));"
    assert _columns(sql, "real") == {"a", "b"}


def test_a_comma_inside_a_string_literal_does_not_split_a_column() -> None:
    """``DEFAULT 'x,y'`` is one column definition.

    The old reader split the body on the comma inside the literal and
    reported a second column called ``y'``.
    """
    sql = "CREATE TABLE real (a TEXT DEFAULT 'x,y');"
    assert _columns(sql, "real") == {"a"}


def test_a_quoted_identifier_is_read_as_a_name() -> None:
    """A quoted identifier is a name, and the reader reports it."""
    sql = 'CREATE TABLE "real table" ("a col" TEXT);'
    tables = parse_tables(sql, source="probe.sql")
    assert set(tables) == {"real table"}
    assert set(tables["real table"].columns) == {"a col"}


def test_a_semicolon_inside_a_trigger_body_does_not_end_the_statement() -> None:
    """The trigger's body holds statements of its own.

    The ``;`` between them does not end the trigger: the trigger is
    ended by the ``;`` that follows its ``END``. A reader that split on
    every ``;`` would cut the trigger in half and read the remainder as
    a statement of its own.
    """
    sql = (
        "CREATE TABLE real (a TEXT);\n"
        "CREATE TRIGGER trg AFTER INSERT ON real BEGIN\n"
        "  UPDATE real SET a = NEW.rowid WHERE rowid = NEW.rowid;\n"
        "END;\n"
        "CREATE TABLE other (b TEXT);\n"
    )
    assert _names(sql) == {"real", "other"}
    statements = split_statements(sql, source="probe.sql")
    assert len(statements) == 3
    assert statements[1].startswith("CREATE TRIGGER trg")
    assert statements[1].rstrip().endswith("END;")


def test_a_case_expression_inside_a_trigger_body_does_not_close_it() -> None:
    """``CASE … END`` opens and closes a pair of its own."""
    sql = (
        "CREATE TABLE real (a TEXT);\n"
        "CREATE TRIGGER trg AFTER INSERT ON real BEGIN\n"
        "  UPDATE real SET a = CASE WHEN NEW.a = 1 THEN 2 ELSE 3 END;\n"
        "END;\n"
        "CREATE TABLE other (b TEXT);\n"
    )
    assert _names(sql) == {"real", "other"}
    assert len(split_statements(sql, source="probe.sql")) == 3


# ---------------------------------------------------------------------------
# Failing loudly
# ---------------------------------------------------------------------------


def test_a_body_item_the_reader_does_not_understand_is_an_error() -> None:
    """A construct it cannot read must not become a name."""
    with pytest.raises(SchemaParseError) as caught:
        parse_tables("CREATE TABLE real (123abc TEXT);", source="probe.sql")
    message = str(caught.value)
    assert "probe.sql" in message
    assert "real" in message


def test_an_unbalanced_table_is_an_error() -> None:
    with pytest.raises(SchemaParseError) as caught:
        parse_tables("CREATE TABLE real (a TEXT;", source="probe.sql")
    assert "probe.sql" in str(caught.value)


def test_a_table_declared_twice_is_an_error() -> None:
    with pytest.raises(SchemaParseError) as caught:
        parse_tables(
            "CREATE TABLE real (a TEXT);\nCREATE TABLE real (b TEXT);", source="probe.sql"
        )
    assert "probe.sql" in str(caught.value)


def test_an_unterminated_literal_is_an_error() -> None:
    with pytest.raises(SchemaParseError) as caught:
        parse_tables("CREATE TABLE real (a TEXT DEFAULT 'abc);", source="probe.sql")
    assert "probe.sql" in str(caught.value)


def test_a_table_without_a_body_is_an_error() -> None:
    with pytest.raises(SchemaParseError) as caught:
        parse_tables("CREATE TABLE real;", source="probe.sql")
    assert "probe.sql" in str(caught.value)


def test_the_error_names_the_file_it_came_from() -> None:
    """The reader has to say which file it could not read."""
    with pytest.raises(SchemaParseError) as caught:
        parse_tables("CREATE TABLE real (123abc TEXT);", source="schema_postgres.sql")
    assert "schema_postgres.sql" in str(caught.value)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_strip_comments_removes_both_forms_and_keeps_literals() -> None:
    sql = "SELECT 1 -- a line comment\n/* a block comment */ FROM t WHERE x = '-- not a comment'"
    stripped = strip_comments(sql, source="probe.sql")
    assert "a line comment" not in stripped
    assert "a block comment" not in stripped
    assert "'-- not a comment'" in stripped


def test_strip_comments_keeps_the_offsets_stable() -> None:
    """A blanked comment is spaces, so every offset still points at the
    same character of the original."""
    sql = "SELECT 1 -- comment\nFROM t"
    stripped = strip_comments(sql, source="probe.sql")
    assert len(stripped) == len(sql)
    assert stripped[9] == " "


def test_split_top_level_does_not_split_inside_a_literal_or_a_nesting() -> None:
    body = "a TEXT, b TEXT CHECK (x IN (1, 2)), c TEXT DEFAULT 'p,q'"
    assert split_top_level(body, source="probe.sql") == [
        "a TEXT",
        "b TEXT CHECK (x IN (1, 2))",
        "c TEXT DEFAULT 'p,q'",
    ]


def test_split_statements_splits_on_the_semicolons() -> None:
    sql = "CREATE TABLE a (x TEXT);\nCREATE TABLE b (y TEXT);\n"
    assert len(split_statements(sql, source="probe.sql")) == 2


# ---------------------------------------------------------------------------
# The real files
# ---------------------------------------------------------------------------


def test_the_sqlite_schema_file_reads_as_the_runtime_expects() -> None:
    tables = parse_tables(_SQLITE_SQL.read_text(encoding="utf-8"), source="schema_sqlite.sql")
    assert "system_state_ledger" in tables
    assert "cold_lake_signals" in tables
    assert len(tables) >= 20


def test_the_postgres_schema_file_reads_as_the_runtime_expects() -> None:
    tables = parse_tables(
        _POSTGRES_SQL.read_text(encoding="utf-8"), source="schema_postgres.sql"
    )
    assert "system_state_ledger" in tables
    assert "cold_lake_signals" in tables
    assert len(tables) >= 20


def test_the_version_log_statement_is_the_files_own_text() -> None:
    """The statement the runner applies is the one the file declares.

    A statement read from a comment-stripped copy of the file would
    create a table whose recorded SQL differs from the one a fresh
    database gets — the same table, described two ways.
    """
    text = _SQLITE_SQL.read_text(encoding="utf-8")
    tables = parse_tables(text, source="schema_sqlite.sql")
    statement = tables["schema_migrations"].statement
    assert statement in text
    assert statement.startswith("CREATE TABLE IF NOT EXISTS schema_migrations")
