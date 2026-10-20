"""The runtime's SQL against the schema it runs on.

The gap this test closes
------------------------
There is no DDL in Python: ``schema_sqlite.sql`` and
``schema_postgres.sql`` are the only description of the tables, and every
statement the runtime issues is written by hand against them. Nothing
checked that the two stayed in step.

So a repository statement that named a column the schema does not have
was not a build failure — it was a failure at *runtime*, on the first
write that used it, on whichever substrate reached that line first. And
the two dialects made it worse rather than better: the SQLite file and
the PostgreSQL file are kept in step by
``tests/storage/test_schema_parity.py``, so a column that is missing is
missing from *both* of them, and the parity test passes.

What this test does
-------------------
It reads the runtime's own SQL — every ``INSERT INTO`` / ``UPDATE`` /
``DELETE FROM`` / ``SELECT … FROM`` in ``src/paa`` — and asserts that
the tables and columns it names exist in the parsed schema.

Why it is in its own file
--------------------------
``tests/storage/test_schema_parity.py`` answers "do the two dialect
files agree with *each other*". This one answers "does the *runtime*
agree with the schema". Different subject, different fixture (the source
tree rather than the two ``.sql`` files) and a different failure mode, so
putting it in the parity file would give that file two subjects and blur
the one it already has.

What it does **not** do
-----------------------
It does not guess. A statement whose table name cannot be resolved
statically — because the name is interpolated at runtime, or because the
name is a CTE, a catalog table or a table-valued function — is **skipped**
with a reason, and every skip is reported by
``test_every_skip_has_a_reason``. A test that guesses is a test that cries
wolf, and a test that cries wolf is a test the suite learns to ignore.

The one thing it will not do is skip a name that *is* resolvable and
simply is not in the schema: that is the failure this file exists for.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from paa.storage.relational.schema_parser import parse_tables, parse_virtual_tables

_SRC: Final = Path(__file__).resolve().parents[2] / "src" / "paa"
_RELATIONAL: Final = _SRC / "storage" / "relational"

#: A bare SQL identifier.
_IDENT: Final = r"[A-Za-z_][A-Za-z0-9_]*"

#: The marker an interpolation that cannot be resolved is replaced with.
#:
#: It has no identifier characters in it, so a name next to it is a name
#: in its own right — and a name that *ends* where the marker begins is
#: a name the runtime built at runtime, which the schema cannot be
#: checked against.
_MARKER: Final = "@@@"

#: The tables the runtime names that are **not** runtime tables.
#:
#: Each one is named with what it is, because a name on this list is a
#: decision and a name that is merely absent from the schema is a
#: finding.
_NOT_RUNTIME_TABLES: Final[dict[str, str]] = {
    "sqlite_master": "SQLite's catalog",
    "sqlite_schema": "SQLite's catalog",
    "sqlite_temp_master": "SQLite's catalog",
    "sqlite_temp_schema": "SQLite's catalog",
    "pg_tables": "PostgreSQL's catalog",
    "pg_class": "PostgreSQL's catalog",
    "pg_namespace": "PostgreSQL's catalog",
    "information_schema": "the SQL standard's catalog",
    "pragma_table_info": "a pragma table-valued function",
    "json_each": "a table-valued function",
    "json_tree": "a table-valued function",
}


@dataclass(frozen=True)
class Statement:
    """One SQL string found in the runtime's source.

    ``text`` is the statement as it will be issued, with every
    interpolation that could be resolved statically substituted and every
    one that could not replaced by :data:`_MARKER`.

    ``resolved`` says whether *everything* in the statement was
    resolved. A statement with an unresolved hole is still worth reading
    — the table it names may be a literal — but the names the hole
    hides are not checked.
    """

    path: Path
    line: int
    text: str
    resolved: bool


# ---------------------------------------------------------------------------
# Reading the source
# ---------------------------------------------------------------------------


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """The module-level string constants, by name.

    A repository writes its column lists as module constants and
    interpolates them into the statement — ``f"INSERT INTO t ({_COLUMNS})
    …"`` — so a reader that cannot resolve them sees a statement whose
    columns are a hole, and checks no columns at all.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = [node.target]
        if value is None or not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _docstrings(tree: ast.Module) -> set[int]:
    """The ``id()`` of every docstring in the module.

    A docstring is prose, and prose contains "UPDATE" and "SELECT" and
    "DELETE FROM" — so a reader that treated a docstring as a statement
    would check the words in it against the schema.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _text_of(node: ast.expr, constants: dict[str, str]) -> tuple[str, bool]:
    """The statement's text, and whether everything in it was resolved.

    An interpolation that names a module-level string constant is
    **substituted** — that is how the repository writes its column
    lists. An interpolation that does not resolve is replaced by
    :data:`_MARKER`, so the statement's shape is still visible and the
    names the hole hides are simply not checked.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if not isinstance(node, ast.JoinedStr):
        return "", True
    parts: list[str] = []
    resolved = True
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        if isinstance(value, ast.FormattedValue):
            replacement = _resolve(value.value, constants)
            if replacement is None:
                parts.append(_MARKER)
                resolved = False
            else:
                parts.append(replacement)
            continue
        # A nested f-string, or something the reader does not know: the
        # statement is not checked rather than misread.
        return "", False
    return "".join(parts), resolved


def _resolve(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The text of an interpolated expression, when it is knowable.

    Only a name bound to a module-level string constant is knowable.
    Everything else — a local variable, a call, an attribute — is a
    value the reader cannot see, and the statement it hides is not
    checked.
    """
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        text, resolved = _text_of(node, constants)
        return text if resolved else None
    return None


def _inside_fstrings(tree: ast.Module) -> set[int]:
    """The ``id()`` of every node that is *part* of an f-string.

    An f-string's literal parts are ordinary string constants in the
    tree, and a reader that visited them as statements would read half
    a statement: the tail of a query, without the ``WITH`` clause that
    gives its tables their meaning.
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for value in node.values:
            for child in ast.walk(value):
                inside.add(id(child))
    return inside


def _statements() -> Iterator[Statement]:
    """Every SQL string in the runtime's source."""
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _module_constants(tree)
        docstrings = _docstrings(tree)
        parts = _inside_fstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            if id(node) in docstrings or id(node) in parts:
                continue
            text, resolved = _text_of(node, constants)
            if not text:
                continue
            if not _looks_like_sql(text):
                continue
            yield Statement(
                path=path,
                line=getattr(node, "lineno", 0),
                text=text,
                resolved=resolved,
            )


def _looks_like_sql(text: str) -> bool:
    """Whether the text is a statement rather than a sentence.

    The keywords are matched **case-sensitively**, because the
    repository's SQL is upper case and its prose is not: an error
    message that says "the row may not be UPDATED" is not a statement,
    and treating it as one would check the word "the" against the
    schema.
    """
    return bool(
        re.search(r"\bINSERT\s+INTO\s+", text)
        or re.search(r"\bUPDATE\s+" + _IDENT + r"\s+SET\b", text)
        or re.search(r"\bDELETE\s+FROM\s+", text)
        or (re.search(r"\bSELECT\b", text) and re.search(r"\bFROM\b", text))
    )


# ---------------------------------------------------------------------------
# Reading a statement
# ---------------------------------------------------------------------------


def _code(text: str) -> str:
    """The text with the *contents* of its literals blanked.

    The delimiters stay — they are where a value begins and ends — and
    the contents do not, because a word inside a literal is a value
    rather than code. A comma inside a literal does not separate two
    columns, and a parenthesis inside one does not open a nesting.
    """
    chars = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("'", '"'):
            end = index + 1
            while end < length:
                if text[end] == char:
                    if end + 1 < length and text[end + 1] == char:
                        end += 2
                        continue
                    break
                end += 1
            for position in range(index + 1, min(end, length)):
                chars[position] = " "
            index = end + 1
            continue
        index += 1
    return "".join(chars)


def _depths(code: str) -> list[int]:
    """The nesting depth at every offset of the masked text."""
    depths = [0] * (len(code) + 1)
    depth = 0
    for index, char in enumerate(code):
        depths[index] = depth
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            depths[index] = depth
    depths[len(code)] = depth
    return depths


def _split_items(
    code: str, depths: list[int], start: int, end: int
) -> list[tuple[int, int]]:
    """The spans of a list's items, split on the commas at the list's own depth.

    A comma inside a nested expression belongs to that expression, and a
    comma inside a literal is a character — so neither separates two
    items. The list's own depth is the one the caller starts at: the
    depth *inside* the parentheses of an ``INSERT``'s column list, and
    the depth of the statement for a select list or an assignment list.
    """
    spans: list[tuple[int, int]] = []
    item_start = start
    for index in range(start, end):
        if code[index] == "," and depths[index] == depths[start]:
            spans.append((item_start, index))
            item_start = index + 1
    if code[item_start:end].strip():
        spans.append((item_start, end))
    return spans


def _balanced(code: str, depths: list[int], open_at: int) -> int:
    """The offset just past the parenthesis that opens at ``open_at``."""
    depth = depths[open_at]
    for index in range(open_at + 1, len(code)):
        if code[index] == ")" and depths[index] == depth:
            return index
    return len(code)


#: The clauses that end a ``FROM`` clause.
_CLAUSE_ENDERS: Final = (
    "WHERE",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "RETURNING",
)


def _clause_end(code: str, depths: list[int], start: int) -> int:
    """Where the clause that starts at ``start`` ends."""
    depth = depths[start]
    index = start
    while index < len(code):
        if depths[index] != depth:
            index += 1
            continue
        if code[index] == ";":
            return index
        if code[index].isalpha() or code[index] == "_":
            end = index
            while end < len(code) and (code[end].isalnum() or code[end] == "_"):
                end += 1
            word = code[index:end]
            if word.upper() in _CLAUSE_ENDERS:
                return index
            index = end
            continue
        index += 1
    return len(code)


def _cte_names(code: str) -> set[str]:
    """The names a ``WITH`` clause introduces.

    A name a statement introduces is not a table: it is a result the
    statement computed. A ``FROM`` that names one is a statement reading
    its own output, and the columns it holds are the statement's, not
    the schema's.
    """
    names: set[str] = set()
    for match in re.finditer(r"\b(" + _IDENT + r")\s*(?:\([^)]*\))?\s+AS\s*\(", code):
        names.add(match.group(1))
    return names


def _name_at(code: str, offset: int) -> tuple[str, int] | None:
    """The identifier that starts at ``offset``, and where it ends."""
    while offset < len(code) and code[offset].isspace():
        offset += 1
    match = re.match(_IDENT, code[offset:])
    if match is None:
        return None
    return match.group(0), offset + len(match.group(0))


def _sources(code: str, depths: list[int], start: int, end: int) -> list[str]:
    """The tables a clause reads, by name.

    Only the names: the clause's own first table is named by the
    ``FROM`` that introduces it, and the rest are the joins and the
    subqueries inside it.
    """
    found: list[str] = []
    for match in re.finditer(r"\b(?:FROM|JOIN)\s+(" + _IDENT + r")", code[start:end]):
        found.append(match.group(1))
    return found


@dataclass(frozen=True)
class TableUse:
    """One table a statement names.

    ``after`` is the offset just past the name, so the caller can ask
    what comes next — and whether what comes next is the marker that
    stands in for a name the runtime built.
    """

    name: str
    after: int
    kind: str


def _tables_in(statement: Statement) -> list[TableUse]:
    """The tables a statement names."""
    code = _code(statement.text)
    ctes = _cte_names(code)
    found: dict[str, TableUse] = {}
    for match in re.finditer(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+", code):
        keyword = match.group(1)
        head = _name_at(code, match.end())
        if head is None:
            continue
        name, name_at = head
        if keyword == "UPDATE" and not re.match(r"\s+SET\b", code[name_at:]):
            # ``UPDATE <table> SET`` — the ``SET`` is what makes it a
            # statement rather than a sentence.
            continue
        if name in found:
            continue
        found[name] = TableUse(name=name, after=name_at, kind=keyword)
    return [use for use in found.values() if use.name not in ctes]


def _is_unresolvable(statement: Statement, use: TableUse) -> bool:
    """Whether the name is one the runtime built at runtime.

    A name that ends where the marker begins is the front of a name the
    runtime completed: the file says ``f"… FROM hot_serving_{table}"``
    and the reader cannot see what ``table`` is.
    """
    return statement.text[use.after :].startswith(_MARKER)


def _columns_in(statement: Statement, table: str) -> list[tuple[str, str]]:
    """The columns a statement names for ``table``.

    Three places name a column mechanically: an ``INSERT``'s column
    list, an assignment list (an ``UPDATE``'s ``SET``, and the ``DO
    UPDATE SET`` of an upsert), and a select list whose query reads
    exactly one table. A ``WHERE`` clause is not one of them — it names
    columns of every table it joins, and a reader that cannot resolve a
    join's aliases would check them against the wrong table.
    """
    code = _code(statement.text)
    depths = _depths(code)
    ctes = _cte_names(code)
    found: list[tuple[str, str]] = []

    for match in re.finditer(r"\bINSERT\s+INTO\s+" + re.escape(table) + r"\b", code):
        after = match.end()
        while after < len(code) and code[after].isspace():
            after += 1
        if after >= len(code) or code[after] != "(":
            continue
        end = _balanced(code, depths, after)
        for start, stop in _split_items(code, depths, after + 1, end):
            name = code[start:stop].strip()
            if re.fullmatch(_IDENT, name):
                found.append((name, "the insert column list"))

    for match in re.finditer(r"\bUPDATE\s+" + re.escape(table) + r"\s+SET\b", code):
        found.extend(_set_columns(code, depths, match.end()))

    if _inserts_into(code) == table:
        # An upsert's ``DO UPDATE SET`` names columns of the table it
        # upserts into — and of no other table in the statement.
        for match in re.finditer(r"\bDO\s+UPDATE\s+SET\b", code):
            found.extend(_set_columns(code, depths, match.end()))

    found.extend(_select_columns(code, depths, ctes, table))
    return found


def _inserts_into(code: str) -> str | None:
    """The table an ``INSERT`` writes to, when the statement has one."""
    match = re.search(r"\bINSERT\s+INTO\s+(" + _IDENT + r")", code)
    return match.group(1) if match is not None else None


def _set_columns(code: str, depths: list[int], start: int) -> list[tuple[str, str]]:
    """The columns an assignment list names."""
    depth = depths[start]
    end = start
    index = start
    while index < len(code):
        if depths[index] != depth:
            index += 1
            continue
        if code[index].isalpha() or code[index] == "_":
            word_end = index
            while word_end < len(code) and (code[word_end].isalnum() or code[word_end] == "_"):
                word_end += 1
            if code[index:word_end].upper() in ("WHERE", "RETURNING", "LIMIT", "ORDER", "GROUP"):
                break
            index = word_end
            continue
        if code[index] == ";":
            break
        index += 1
    end = index
    found: list[tuple[str, str]] = []
    for item_start, item_stop in _split_items(code, depths, start, end):
        item = code[item_start:item_stop]
        equals = item.find("=")
        if equals == -1:
            continue
        name = item[:equals].strip()
        if re.fullmatch(_IDENT, name):
            found.append((name, "the assignment list"))
    return found


def _select_columns(
    code: str, depths: list[int], ctes: set[str], table: str
) -> list[tuple[str, str]]:
    """The columns a select list names, for the queries that name one table.

    A select list is only read when the query it belongs to reads
    exactly one table: a query that joins two has a column that belongs
    to neither without a qualifier, and a query that reads a CTE has
    columns the schema never declared.
    """
    found: list[tuple[str, str]] = []
    for select_match in re.finditer(r"\bSELECT\b", code):
        start = select_match.end()
        depth = depths[start]
        from_match = re.compile(r"\bFROM\b").search(code, start)
        if from_match is None or depths[from_match.start()] != depth:
            continue
        list_end = from_match.start()
        clause_end = _clause_end(code, depths, from_match.end())
        # The table the ``FROM`` names is the first source; the joins
        # and the subqueries in the clause are the rest.
        named: list[str] = []
        head = _name_at(code, from_match.end())
        if head is not None:
            named.append(head[0])
        named.extend(_sources(code, depths, from_match.end(), clause_end))
        sources = [
            name
            for name in named
            if name not in ctes and name not in _NOT_RUNTIME_TABLES
        ]
        if sources != [table]:
            continue
        for item_start, item_stop in _split_items(code, depths, start, list_end):
            item = code[item_start:item_stop].strip()
            item = re.sub(r"^\s*(DISTINCT|ALL)\s+", "", item, flags=re.IGNORECASE)
            name = re.match(r"(" + _IDENT + r")\s+AS\s+" + _IDENT + r"$", item)
            if name is None:
                name = re.match(r"(" + _IDENT + r")$", item)
            if name is not None:
                found.append((name.group(1), "the select list"))
    return found


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def _schema() -> dict[str, set[str]]:
    """Every table the SQLite schema file declares, and its columns.

    The PostgreSQL file declares the same tables and the same columns —
    ``tests/storage/test_schema_parity.py`` fails the build if that
    stops being true — so one file answers the question for both.
    """
    text = (_RELATIONAL / "schema_sqlite.sql").read_text(encoding="utf-8")
    return {name: set(table.columns) for name, table in parse_tables(text).items()}


def _virtual_tables() -> set[str]:
    """The virtual tables the SQLite schema file declares."""
    text = (_RELATIONAL / "schema_sqlite.sql").read_text(encoding="utf-8")
    return set(parse_virtual_tables(text))


# ---------------------------------------------------------------------------
# The findings
# ---------------------------------------------------------------------------


def _unknown_tables(
    statements: list[Statement], schema: dict[str, set[str]], virtual: set[str]
) -> list[str]:
    """The tables the statements name that the schema does not declare."""
    unknown: list[str] = []
    for statement in statements:
        for use in _tables_in(statement):
            if use.name in schema or use.name in virtual:
                continue
            if use.name in _NOT_RUNTIME_TABLES:
                continue
            if _is_unresolvable(statement, use):
                continue
            unknown.append(
                f"{statement.path.relative_to(_SRC.parent.parent)}:{statement.line}: "
                f"the table {use.name!r} is not in the schema "
                f"(after {use.kind!r})\n    {statement.text.strip()[:160]}"
            )
    return unknown


def _skipped_tables(
    statements: list[Statement], schema: dict[str, set[str]], virtual: set[str]
) -> list[tuple[str, str]]:
    """The tables the scan skipped, and why it skipped them.

    Every skip carries a reason, and the reason is one of three kinds:
    the name is a catalog the runtime *reads* rather than a table it
    owns; the name is a virtual table the schema declares in a form the
    reader does not report; or the name is one the runtime built at
    runtime and the reader cannot see.
    """
    skipped: list[tuple[str, str]] = []
    for statement in statements:
        for use in _tables_in(statement):
            if use.name in schema:
                continue
            if use.name in virtual:
                skipped.append((use.name, "a virtual table the schema declares"))
            elif use.name in _NOT_RUNTIME_TABLES:
                skipped.append((use.name, _NOT_RUNTIME_TABLES[use.name]))
            elif _is_unresolvable(statement, use):
                skipped.append((use.name, "the runtime built the rest of the name at runtime"))
    return skipped


def _unknown_columns(statements: list[Statement], schema: dict[str, set[str]]) -> list[str]:
    """The columns the statements name that the table does not have."""
    unknown: list[str] = []
    for statement in statements:
        for use in _tables_in(statement):
            if use.name not in schema:
                continue
            for column, where in _columns_in(statement, use.name):
                if column.lower() in schema[use.name]:
                    continue
                unknown.append(
                    f"{statement.path.relative_to(_SRC.parent.parent)}:{statement.line}: "
                    f"the table {use.name!r} has no column {column!r} "
                    f"(named in {where})\n    {statement.text.strip()[:160]}"
                )
    return unknown


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_every_table_the_runtime_names_is_in_the_schema() -> None:
    """A statement that names a table the schema does not have is a runtime failure.

    The failure is not a syntax error: the statement is issued, the
    engine answers "no such table", and the error arrives as a
    substrate error that blames the query rather than the schema. And
    the parity test cannot see it, because the column is missing from
    *both* dialect files.
    """
    unknown = _unknown_tables(list(_statements()), _schema(), _virtual_tables())
    assert not unknown, "\n".join(unknown)


def test_every_column_the_runtime_names_is_in_the_schema() -> None:
    """A statement that names a column the schema does not have is a runtime failure.

    The same argument as the table above, and the same blindness in the
    parity test: a column that is missing is missing from both dialect
    files, so the two files agree with each other and with nothing.
    """
    unknown = _unknown_columns(list(_statements()), _schema())
    assert not unknown, "\n".join(unknown)


def test_the_scan_would_notice_a_table_the_schema_lacks() -> None:
    """The scan has to be able to fail.

    A reader that cannot report a missing table is a reader that will
    never report one, and the test above would pass forever. So the
    reader is handed a statement that names a table the schema does
    not have, and it has to say so.
    """
    statement = Statement(
        path=_SRC / "probe.py",
        line=1,
        text="SELECT id FROM no_such_table",
        resolved=True,
    )
    findings = _unknown_tables([statement], _schema(), _virtual_tables())
    assert findings, "the scan did not notice a table the schema does not have"


def test_the_scan_would_notice_a_column_the_schema_lacks() -> None:
    """The same for a column, in the three places a column is named."""
    schema = _schema()
    for text in (
        "INSERT INTO cold_lake_signals (id, no_such_column) VALUES (?, ?)",
        "UPDATE cold_lake_signals SET no_such_column = ? WHERE id = ?",
        "SELECT no_such_column FROM cold_lake_signals",
    ):
        statement = Statement(path=_SRC / "probe.py", line=1, text=text, resolved=True)
        findings = _unknown_columns([statement], schema)
        assert findings, text


def test_a_table_the_runtime_builds_is_skipped() -> None:
    """A name the runtime completes at runtime cannot be checked.

    The reader cannot see what the runtime put there, so it says so and
    moves on rather than reporting a table that does not exist. The
    marker is what stands in for the missing half, and the name next to
    it is the front of a name the runtime finished.
    """
    statement = Statement(
        path=_SRC / "probe.py",
        line=1,
        text=f"SELECT id FROM hot_serving_{_MARKER}",
        resolved=False,
    )
    uses = _tables_in(statement)
    assert [use.name for use in uses] == ["hot_serving_"]
    assert _is_unresolvable(statement, uses[0])
    assert not _unknown_tables([statement], _schema(), _virtual_tables())


def test_every_skip_has_a_reason() -> None:
    """A skip without a reason is a hole in the guard.

    The scan skips what it cannot read — a catalog, a virtual table, a
    name the runtime built at runtime — and each of those is a decision
    somebody has to be able to see. So every skip carries a reason, and
    the skips themselves are not empty: a scan that skipped nothing
    would have no skip path to test.
    """
    skipped = _skipped_tables(list(_statements()), _schema(), _virtual_tables())
    assert skipped, "nothing was skipped, so the skip path is untested"
    for name, reason in skipped:
        assert reason, name


def test_the_scan_actually_reads_the_runtime() -> None:
    """The scan must not be vacuous.

    A reader that matched nothing would pass both tests above, and
    would keep passing them after the schema changed. So the scan is
    looked at: it has to have found statements, in several files, and
    it has to have found tables the schema declares.
    """
    statements = list(_statements())
    assert len(statements) > 50, len(statements)
    files = {statement.path for statement in statements}
    assert len(files) >= 8, sorted(str(path) for path in files)

    tables: set[str] = set()
    for statement in statements:
        tables.update(use.name for use in _tables_in(statement))
    schema = _schema()
    assert len(tables & set(schema)) >= 15, sorted(tables)


def test_the_scan_skips_what_it_cannot_resolve() -> None:
    """What the scan skips, it skips for a stated reason.

    A name the runtime builds at runtime is not checked — the reader
    cannot see it — and the marker that stands in for it is what makes
    that visible. So the marker has to be there, and a statement that
    carries it has to be *reported* as unresolved rather than quietly
    checked.
    """
    statements = list(_statements())
    unresolved = [statement for statement in statements if not statement.resolved]
    assert unresolved, "no statement was left unresolved, so the skip path is untested"
    for statement in unresolved:
        assert _MARKER in statement.text
