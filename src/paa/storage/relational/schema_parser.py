"""A lexer for the two ``.sql`` schema files.

Why this is a lexer and not a regular expression
------------------------------------------------
The runtime reads the schema files to learn which tables a database at the
base version is expected to hold, and the parity test reads them to learn
which columns each table has. Both answers have to be *right*, because a
wrong answer is not a cosmetic error: a phantom table makes the runner
refuse a whole database as incomplete, and a phantom column makes the
parity test demand a column the schema does not have.

The first version of this was a regular expression plus a parenthesis
counter, and it was wrong in ways that were one edit away from happening:

* ``DEFAULT 'CREATE TABLE fake (x)'`` — the text of a string literal was
  read as code, and the literal's words joined the table set;
* ``/* CREATE TABLE ghost (x) */`` — a block comment was not stripped at
  all, and its words joined the table set;
* ``DEFAULT 'x -- y'`` — the line-comment stripper ran inside the literal
  and ate the rest of the line, which unbalanced the parentheses of the
  table the literal sat in;
* ``UNIQUE(a, b)`` — a constraint written without a space was read as a
  column called ``unique(a,``;
* ``DEFAULT 'x,y'`` — the comma inside the literal split the column
  definition in two.

Every one of those was a *phantom* — a name that was not there, believed
to be there. The scanner below is a character-level reader that knows the
three things a SQL text can hide code in: a string literal, a quoted
identifier, and a comment.

Failing loudly
--------------
A name that is not a valid SQL identifier, or a body item the reader does
not recognise, raises :class:`SchemaParseError` naming the file and the
construct. The reader never guesses: a table it cannot read is a table
the runtime would otherwise refuse a whole database over, and a column it
cannot read is a column the parity test would demand.

A ``CREATE`` the reader does not recognise is refused for the same
reason, and it is the case that is easiest to get wrong: a reader that
skips every statement it does not understand turns ``CREATE TABEL foo``
into a *missing table* rather than into an error, and a missing table is
exactly what a migration guard is there to notice. The refusal names the
file, the line and the statement's first words.

The reader is not a SQL parser. It **reads** the tables — ``CREATE
TABLE`` and its ``TEMP`` / ``TEMPORARY`` / ``UNLOGGED`` spellings — and it
**recognises and knowingly skips** the other objects the two schema files
create: ``CREATE INDEX`` (and ``CREATE UNIQUE INDEX``), ``CREATE
TRIGGER`` (with a ``BEGIN … END`` body), ``CREATE EXTENSION``, and
``CREATE VIRTUAL TABLE``. Every other ``CREATE`` form is refused rather
than misread.

The virtual table is skipped *knowingly*, and
:func:`parse_virtual_tables` reports it separately: the engine creates
shadow tables beside it, and a caller that compares a database's catalog
against the schema has to know their names to tell them from a table
nobody declared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "SchemaParseError",
    "SchemaTable",
    "parse_tables",
    "parse_virtual_tables",
    "split_statements",
    "split_top_level",
    "strip_comments",
]


class SchemaParseError(ValueError):
    """A schema file says something this reader cannot interpret.

    A :class:`ValueError` because that is what a malformed source file
    is: a defect in the repository, found by reading a file — not a
    runtime failure of a substrate. The migration runner turns it into a
    :class:`~paa.core.errors.StorageError` when it meets one at connect
    time, so a caller that catches ``PaaError`` still catches it.
    """


@dataclass(frozen=True, slots=True)
class SchemaTable:
    """One table, as the schema file declares it.

    ``columns`` is the set of column names, lower-cased — the two
    engines spell a column the same way, and the comparison is between
    the two files rather than against SQL's own case rules.

    ``statement`` is the ``CREATE TABLE`` **as it was written** —
    comments, whitespace and all. That is deliberate: the statement is
    what the runtime applies to a database that has to have the version
    log, and a statement read from a comment-stripped copy of the file
    would create a table whose recorded SQL differs from the one a fresh
    database gets.
    """

    name: str
    columns: frozenset[str]
    statement: str


#: Line-leading keywords that mark a table *constraint*, not a column
#: definition.
#:
#: Matched on the bare (unquoted) first word of a body item, and in
#: upper case: ``UNIQUE(a, b)`` and ``unique (a, b)`` are both
#: constraints, and a *quoted* ``"unique"`` is a column whose name
#: happens to be a keyword.
_CONSTRAINT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE"}
)

#: A bare SQL identifier: a letter or underscore, then letters, digits
#: and underscores.
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: The words of a statement, and the semicolons between them.
#:
#: One pattern for both, because the reader has to know the order in
#: which they appear: a ``CREATE TRIGGER``'s body is delimited by words
#: (``BEGIN`` … ``END``) and its inner statements are separated by
#: semicolons that do **not** end the trigger.
_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|;")

#: The words that may sit between ``CREATE`` and the word that says what
#: is being created.
#:
#: ``TEMP`` / ``TEMPORARY`` / ``UNLOGGED`` qualify a table or a trigger,
#: and ``UNIQUE`` qualifies an index. They say nothing about *what* is
#: created, so the reader folds them away before it decides what the
#: statement is.
_CREATE_MODIFIERS: Final[frozenset[str]] = frozenset(
    {"TEMP", "TEMPORARY", "UNLOGGED", "UNIQUE"}
)

#: The modifiers that can qualify a **table**.
#:
#: Derived from :data:`_CREATE_MODIFIERS` rather than written out, so
#: the two cannot drift: a modifier that qualifies a table here and not
#: there would make the reader read a statement one way in one place and
#: another way in the other.
_TABLE_MODIFIERS: Final[frozenset[str]] = _CREATE_MODIFIERS - {"UNIQUE"}

#: The ``CREATE`` forms the reader knows and does **not** read.
#:
#: The key is the form's canonical name — the words that follow
#: ``CREATE``, in upper case, with the modifiers folded away. The value
#: says what the object is, because a form that is skipped *knowingly* is
#: a decision and a form that is skipped by accident is a defect.
#:
#: Every entry is a form one of the two schema files actually uses. The
#: set is closed on purpose: a ``CREATE`` outside it is refused rather
#: than skipped, because a skipped ``CREATE`` is a table the runtime
#: would never know about — and a table the runtime does not know about
#: is a database it refuses as incomplete, for a reason the operator
#: cannot see.
_KNOWN_NON_TABLE_FORMS: Final[dict[str, str]] = {
    "INDEX": "an index is not a table",
    "TRIGGER": "a trigger is not a table",
    "EXTENSION": "an extension is not a table",
    "VIRTUAL TABLE": (
        "a virtual table has no columns in the ordinary sense and no "
        "PostgreSQL equivalent, and the shadow tables the engine creates "
        "beside it are its own bookkeeping rather than the schema's"
    ),
}


def _blank(chars: list[str], start: int, end: int) -> None:
    """Blank a run of characters, keeping the newlines.

    The newlines are kept so that a blanked comment does not join two
    lines into one and an offset in the blanked text is still on the
    same *line* as the offset in the original.
    """
    for index in range(start, end):
        if chars[index] != "\n":
            chars[index] = " "


def _end_of_quoted(sql: str, start: int, quote: str, source: str) -> int:
    """Where the quoted run that starts at ``start`` ends.

    A doubled quote is an escaped quote, not the end of the run — the
    rule both engines follow. An unterminated run is a defect in the
    file, and is reported as one rather than being read to the end of
    the file.
    """
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise SchemaParseError(
        f"{source}: the quoted text that starts at offset {start} is never closed"
    )


def _scan(sql: str, source: str) -> tuple[str, str]:
    """The code mask and the comment-free text.

    Both have the same length as the input, so an offset in one is the
    offset it had in the other. That is what lets a pattern be matched
    against the *mask* — where a literal's and a comment's characters
    are spaces — and the original text be read at the same offset.

    The mask blanks the *contents* of string literals, quoted identifiers
    and comments — it is the text a regular expression may match against
    without ever matching a word that is really a value. The delimiters
    of a literal and of a quoted identifier stay visible, because they
    are where the value begins and ends. The comment-free text keeps the
    literals and drops the comments: it is the text a statement is read
    from.
    """
    length = len(sql)
    code = list(sql)
    plain = list(sql)
    index = 0
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if char == "'":
            end = _end_of_quoted(sql, index, "'", source)
            _blank(code, index + 1, end - 1)
            index = end
        elif char == '"':
            end = _end_of_quoted(sql, index, '"', source)
            _blank(code, index + 1, end - 1)
            index = end
        elif char == "-" and following == "-":
            end = sql.find("\n", index)
            end = length if end == -1 else end
            _blank(plain, index, end)
            _blank(code, index, end)
            index = end
        elif char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            _blank(plain, index, end)
            _blank(code, index, end)
            index = end
        else:
            index += 1
    return "".join(code), "".join(plain)


def strip_comments(sql: str, *, source: str = "<sql>") -> str:
    """The SQL with its comments removed.

    Both comment forms are removed — ``--`` to the end of the line, and
    ``/* … */`` — and neither is removed from inside a string literal: a
    literal is a value rather than code, so a ``--`` in it is part of
    the value.

    The comment's characters are replaced by spaces rather than deleted,
    so every offset in the result is the offset it had in the input.
    """
    _, plain = _scan(sql, source)
    return plain


def _skip_space(code: str, index: int) -> int:
    """The first index at or after ``index`` that is not whitespace."""
    while index < len(code) and code[index].isspace():
        index += 1
    return index


def _read_word(code: str, index: int) -> tuple[str, int]:
    """The word that starts at ``index``, and the index after it.

    A word is a bare identifier: letters, digits and underscores. A
    quoted identifier is *not* a word — it is read by
    :func:`_read_identifier`, which knows about the quotes.
    """
    match = _IDENTIFIER.match(code, index)
    if match is None:
        return "", index
    return match.group(0), match.end()


def _read_identifier(sql: str, code: str, index: int, source: str) -> tuple[str, int]:
    """The identifier that starts at ``index``, and the index after it.

    Either a bare identifier or a quoted one. A quoted identifier's
    content is unescaped (``""`` is one quote) and returned as written,
    so the name the reader reports is the name the schema declares.

    Anything else is an error: a name that is not an identifier is a
    name the reader is being asked to believe, and it will not.
    """
    index = _skip_space(code, index)
    if index < len(sql) and sql[index] == '"':
        end = _end_of_quoted(sql, index, '"', source)
        name = sql[index + 1 : end - 1].replace('""', '"')
        if not name:
            raise SchemaParseError(f"{source}: an empty quoted identifier")
        return name, end
    word, end = _read_word(code, index)
    if not word:
        raise SchemaParseError(
            f"{source}: expected an identifier at offset {index}, found "
            f"{sql[index:index + 20]!r}"
        )
    return word, end


def _statement_spans(sql: str, source: str) -> list[tuple[int, int]]:
    """Where each statement of a DDL script begins and ends.

    A ``;`` ends a statement — except inside a ``CREATE TRIGGER``'s body,
    where the semicolons separate the body's own statements and the
    trigger is ended by the ``;`` that follows its ``END``. A trigger
    body is opened by ``BEGIN`` and closed by ``END``, and a ``CASE …
    END`` inside it opens and closes a pair of its own, so the reader
    counts them.

    The offsets are into ``sql``, and the statement text is
    ``sql[start:end]`` — the terminating semicolon included, because that
    is the text the schema file contains.
    """
    code, _ = _scan(sql, source)
    spans: list[tuple[int, int]] = []
    start = 0
    create_seen = False
    awaiting_trigger = False
    in_body = False
    depth = 0
    for match in _TOKEN.finditer(code):
        word = match.group(0)
        if awaiting_trigger:
            # A ``CREATE TRIGGER`` was seen. Its body starts at ``BEGIN``;
            # a trigger without one (PostgreSQL's function triggers) is
            # ended by the next semicolon like any other statement.
            if word.upper() == "BEGIN":
                awaiting_trigger = False
                in_body = True
                depth = 1
            elif word == ";":
                awaiting_trigger = False
                spans.append((start, match.end()))
                start = match.end()
            continue
        if in_body:
            upper = word.upper()
            if upper in ("CASE", "BEGIN"):
                depth += 1
            elif upper == "END":
                depth -= 1
                if depth <= 0:
                    in_body = False
            continue
        if word == ";":
            spans.append((start, match.end()))
            start = match.end()
            continue
        upper = word.upper()
        if upper == "CREATE":
            create_seen = True
            continue
        if create_seen and upper in ("TEMP", "TEMPORARY"):
            # ``CREATE TEMP TRIGGER`` — the trigger keyword is still to come.
            continue
        if create_seen and upper == "TRIGGER":
            awaiting_trigger = True
        create_seen = False
    tail = sql[start:]
    if tail.strip():
        spans.append((start, len(sql)))
    # The statement starts at its first *code* character: the comments
    # and blank lines that precede it are the file's prose, not part of
    # the statement, and the statement the runtime applies is the one
    # the file declares.
    return [(_skip_space(code, start), end) for start, end in spans]


def split_statements(sql: str, *, source: str = "<sql>") -> list[str]:
    """The statements of a DDL script.

    A ``;`` inside a ``CREATE TRIGGER``'s body does not end the
    statement: the body's own statements are separated by ``;`` and the
    trigger is ended by the one that follows its ``END``.
    """
    return [sql[start:end] for start, end in _statement_spans(sql, source)]


def _split_spans(code: str, body: str) -> list[tuple[int, int]]:
    """The spans of a table body's items, as offsets into ``body``.

    A comma inside a string literal is a character and a comma inside
    parentheses belongs to a nested expression, so neither ends an item:
    ``CHECK (a IN (1, 2))`` and ``DEFAULT 'a,b'`` are each one item,
    and splitting them would shred the definition.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for index, char in enumerate(code):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            spans.append((start, index))
            start = index + 1
    if body[start:].strip():
        spans.append((start, len(body)))
    return spans


def split_top_level(body: str, *, source: str = "<sql>") -> list[str]:
    """Split a table body on the commas that are not nested.

    A ``CHECK`` constraint and a multi-column type modifier contain
    their own commas inside ``(...)``, and a string literal may contain
    one at all; splitting naively would shred them.
    """
    code, _ = _scan(body, source)
    return [body[start:end].strip() for start, end in _split_spans(code, body)]


def _columns_of(
    body: str,
    body_code: str,
    table: str,
    source: str,
    line: int,
) -> frozenset[str]:
    """The column names of one table's body.

    A body item is either a column definition or a table constraint. A
    constraint is recognised by its first *bare* word — ``PRIMARY``,
    ``UNIQUE``, ``CHECK``, ``FOREIGN``, ``EXCLUDE``, ``LIKE``,
    ``CONSTRAINT`` — and is not a column, however it is spelled or
    spaced. Anything else is a column definition, and its name is the
    first identifier of the item.

    An item whose first word is neither a constraint keyword nor an
    identifier is an error: it is a construct the reader does not
    understand, and understanding it wrongly would put a name in the
    column set that the table does not have.
    """
    columns: set[str] = set()
    for start, end in _split_spans(body_code, body):
        item = body[start:end].strip()
        if not item:
            continue
        # Re-align the span onto the stripped item, so the text and its
        # mask are sliced the same way and their offsets agree.
        lead = len(body[start:end]) - len(body[start:end].lstrip())
        item_start = start + lead
        item_text = body[item_start:end]
        item_code = body_code[item_start:end]
        index = _skip_space(item_code, 0)
        quoted = index < len(item_code) and item_code[index] == '"'
        if quoted:
            name, _ = _read_identifier(item_text, item_code, index, source)
        else:
            word, _after = _read_word(item_code, index)
            if not word:
                raise SchemaParseError(
                    f"{source}: line {line}: the body of table {table!r} holds an "
                    f"item this reader does not understand: {item!r}"
                )
            if word.upper() in _CONSTRAINT_KEYWORDS:
                continue
            name = word
        columns.add(name.lower())
    return frozenset(columns)


def _table_from_statement(
    statement: str,
    statement_code: str,
    source: str,
    line: int,
) -> SchemaTable:
    """The table one ``CREATE TABLE`` statement declares.

    The statement is already known to be a table statement; this reads
    the name, the body and the closing parenthesis.
    """
    index = _skip_space(statement_code, 0)
    word, index = _read_word(statement_code, index)
    if word.upper() != "CREATE":
        raise SchemaParseError(
            f"{source}: line {line}: expected a table statement, found {word!r}"
        )
    index = _skip_space(statement_code, index)
    word, index = _read_word(statement_code, index)
    if word.upper() in _TABLE_MODIFIERS:
        # ``CREATE TEMP TABLE`` and friends: the ``TABLE`` keyword is
        # still to come.
        index = _skip_space(statement_code, index)
        word, index = _read_word(statement_code, index)
    if word.upper() != "TABLE":
        raise SchemaParseError(
            f"{source}: line {line}: expected TABLE, found {word!r}"
        )
    index = _skip_space(statement_code, index)
    following, _after = _read_word(statement_code, index)
    if following.upper() == "IF":
        # ``IF NOT EXISTS``
        index = _skip_if_not_exists(statement_code, index, source, line)
    name, index = _read_identifier(statement, statement_code, index, source)
    index = _skip_space(statement_code, index)
    if index >= len(statement_code) or statement_code[index] != "(":
        raise SchemaParseError(
            f"{source}: line {line}: expected the body of table {name!r} to "
            f"start with '('"
        )
    open_index = index
    depth = 0
    close_index = -1
    for position in range(open_index, len(statement_code)):
        char = statement_code[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_index = position
                break
    if close_index == -1:
        raise SchemaParseError(
            f"{source}: line {line}: unbalanced parentheses in the definition "
            f"of table {name!r}"
        )
    body = statement[open_index + 1 : close_index]
    body_code = statement_code[open_index + 1 : close_index]
    return SchemaTable(
        name=name,
        columns=_columns_of(body, body_code, name, source, line),
        statement=statement.strip(),
    )


def _skip_if_not_exists(
    statement_code: str, index: int, source: str, line: int
) -> int:
    """Where the object's name starts, past an optional ``IF NOT EXISTS``.

    ``CREATE TABLE IF NOT EXISTS`` and ``CREATE VIRTUAL TABLE IF NOT
    EXISTS`` are the spellings the two schema files use, and the clause
    says nothing about the name — so it is stepped over rather than read
    as one.

    A clause that is half-written is an error: a reader that stepped
    over ``IF`` alone would read ``NOT`` as the name of the table.
    """
    following, after = _read_word(statement_code, _skip_space(statement_code, index))
    if following.upper() != "IF":
        return index
    not_exists, after_not = _read_word(statement_code, _skip_space(statement_code, after))
    if not_exists.upper() != "NOT":
        raise SchemaParseError(
            f"{source}: line {line}: expected IF NOT EXISTS, found "
            f"{following!r} {not_exists!r}"
        )
    exists, after_exists = _read_word(statement_code, _skip_space(statement_code, after_not))
    if exists.upper() != "EXISTS":
        raise SchemaParseError(
            f"{source}: line {line}: expected IF NOT EXISTS, found "
            f"{following!r} {not_exists!r} {exists!r}"
        )
    return after_exists


def _create_form(statement_code: str, index: int, source: str, line: int) -> tuple[str, int]:
    """What a ``CREATE`` statement creates, and where its name starts.

    The first half is ``"TABLE"`` for a table this reader reads, the
    canonical name of a known non-table form for one it skips knowingly,
    and a :class:`SchemaParseError` for a form it does not know. The
    second half is the offset at which the object's **name** begins — the
    name always follows the form's own keyword, so a caller that wants
    the name does not have to know the form's spelling.

    The form is decided from the words that follow ``CREATE``, with the
    modifiers folded away: ``CREATE UNIQUE INDEX`` and ``CREATE INDEX``
    are the same form, and ``CREATE TEMP TABLE`` is a table.

    A form the reader does not know is an **error**, not a skip. A
    skipped ``CREATE`` is a table the runtime would never know about,
    and a database missing a table is one the runtime refuses as
    incomplete — so a typo in the schema file would present as a
    database that is broken for a reason nobody can see.
    """
    spelled: list[str] = []
    words: list[str] = []
    after_each: list[int] = []
    cursor = index
    while len(words) < 4:
        word, after = _read_word(statement_code, _skip_space(statement_code, cursor))
        if not word:
            break
        if "(" in statement_code[cursor:after]:
            # The statement has reached its body: the words after that
            # belong to the object, not to the form.
            break
        spelled.append(word)
        words.append(word.upper())
        after_each.append(after)
        cursor = after
    # The head is the statement's first words **as they were written**:
    # the refusal quotes the file, and a quote that has been shouted
    # back at the reader is not the file's text.
    head = " ".join(spelled) if spelled else ""

    def refuse() -> SchemaParseError:
        return SchemaParseError(
            f"{source}: line {line}: a CREATE statement this reader does not "
            f"understand: {head!r}. The reader knows CREATE TABLE, CREATE "
            "INDEX, CREATE TRIGGER, CREATE EXTENSION and CREATE VIRTUAL "
            "TABLE, and refuses the rest rather than misreading it"
        )

    if not words or words[0] != "CREATE":
        raise refuse()
    rest = words[1:]
    # The offset of the form's own keyword: one past ``CREATE``, plus one
    # more when a modifier sits between them.
    at = 1
    if rest and rest[0] in _CREATE_MODIFIERS:
        rest = rest[1:]
        at = 2
    if not rest:
        raise refuse()
    form = rest[0]
    if form == "TABLE":
        return "TABLE", after_each[at]
    if form == "VIRTUAL":
        # ``CREATE VIRTUAL TABLE`` — and only that: a ``VIRTUAL`` that
        # is not followed by ``TABLE`` is a form the reader does not
        # know.
        if len(rest) > 1 and rest[1] == "TABLE":
            return "VIRTUAL TABLE", after_each[at + 1]
        raise refuse()
    if form in _KNOWN_NON_TABLE_FORMS:
        return form, after_each[at]
    raise refuse()


def parse_virtual_tables(sql: str, *, source: str = "<sql>") -> frozenset[str]:
    """The virtual tables the schema file declares.

    A ``CREATE VIRTUAL TABLE`` (SQLite's FTS5) *is* a table the file
    declares, and the reader knows it — but it is not a table the
    parity comparison can talk about: it has no PostgreSQL equivalent,
    and the shadow tables the engine creates beside it are the engine's
    own bookkeeping rather than the schema's.

    So it is reported separately rather than in the table set. A caller
    that compares the *catalog* against the schema needs the name: the
    engine creates ``<name>_data``, ``<name>_idx`` and the rest beside
    the virtual table, and a table the schema never declared is not
    evidence of anything.
    """
    code, _ = _scan(sql, source)
    names: set[str] = set()
    for start, end in _statement_spans(sql, source):
        statement = sql[start:end]
        statement_code = code[start:end]
        line = sql.count("\n", 0, start) + 1
        index = _skip_space(statement_code, 0)
        word, _after = _read_word(statement_code, index)
        if word.upper() != "CREATE":
            continue
        form, name_at = _create_form(statement_code, index, source, line)
        if form != "VIRTUAL TABLE":
            continue
        name_at = _skip_if_not_exists(statement_code, name_at, source, line)
        name, _ = _read_identifier(
            statement, statement_code, _skip_space(statement_code, name_at), source
        )
        names.add(name)
    return frozenset(names)


def parse_tables(sql: str, *, source: str = "<sql>") -> dict[str, SchemaTable]:
    """Every table the schema file declares.

    The reader reads ``CREATE TABLE`` — and the ``TEMP`` / ``TEMPORARY``
    / ``UNLOGGED`` spellings of it — and recognises, and knowingly skips,
    the other objects the two schema files create: an index, a trigger,
    an extension, and the FTS5 virtual table, which has no PostgreSQL
    equivalent and whose shadow tables are the engine's own bookkeeping
    rather than the schema's.

    A ``CREATE`` form outside that set is **refused**. The reader used
    to skip anything it did not recognise, which turned ``CREATE TABEL
    foo`` into a missing table rather than into an error — and a missing
    table is exactly what a migration guard exists to notice, so the
    typo would present as a database that is broken for a reason nobody
    can see.

    A statement that is not a ``CREATE`` at all — an ``ALTER``, an
    ``INSERT``, a pragma — is skipped, and is not the reader's business:
    it creates no object, so there is nothing for it to misread.

    The statements are read one at a time, so a ``;`` inside a trigger's
    body does not end a statement, and the table set is the set of
    tables the file *declares* rather than the set of words that happen
    to look like a table declaration.
    """
    code, _ = _scan(sql, source)
    tables: dict[str, SchemaTable] = {}
    for start, end in _statement_spans(sql, source):
        statement = sql[start:end]
        statement_code = code[start:end]
        line = sql.count("\n", 0, start) + 1
        index = _skip_space(statement_code, 0)
        word, _after = _read_word(statement_code, index)
        if word.upper() != "CREATE":
            continue
        if _create_form(statement_code, index, source, line)[0] != "TABLE":
            # A known non-table form: an index, a trigger, an extension,
            # a virtual table. It is skipped *knowingly* — the reader
            # knows what it is and why it is not a table.
            continue
        table = _table_from_statement(statement, statement_code, source, line)
        if table.name in tables:
            raise SchemaParseError(
                f"{source}: line {line}: table {table.name!r} is declared twice"
            )
        tables[table.name] = table
    return tables
