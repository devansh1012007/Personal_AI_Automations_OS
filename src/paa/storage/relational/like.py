"""LIKE patterns that match user text literally.

A ``LIKE`` pattern is SQL's one place where a *value* silently changes the
*meaning* of a query. ``%`` matches any run of characters and ``_`` matches
any one of them, so a keyword containing either is not searched for — it is
compiled into a wildcard, and the search quietly returns rows the caller
never asked about. Nothing raises; the only symptom is the wrong facts in the
agent's context, which is the hardest kind of bug to notice and the easiest
to cause.

Three facts make this a module rather than a helper copied into each caller.

**The escape character differs by backend.** SQLite's ``LIKE`` has no default
escape character; PostgreSQL's defaults to a backslash. The same escaped
pattern is therefore *wrong* on SQLite — the backslash is matched literally
and the ``_`` is still a wildcard — and *right* on PostgreSQL, unless the SQL
declares ``ESCAPE '\\'`` explicitly. This runtime speaks to both
(:mod:`paa.storage.relational.database` and
:mod:`paa.storage.relational.postgres`), so the clause is declared
everywhere. The character and the clause are derived from one constant here,
so the two halves of the contract cannot drift apart.

**A NUL byte truncates the pattern.** SQLite reaches the pattern through the
C string API, so ``%\\x00%`` is not "a literal NUL between two wildcards" —
the pattern *ends* at the NUL, leaving ``%``, and a bare ``%`` matches
*every row in the table*. The escape clause does not help: a NUL is not a
metacharacter, it is the end of the string. (SQLite truncates the searched
string the same way, which is why a stored value carrying a NUL matches a
prefix of itself.)

A NUL is never meaningful in a keyword, so it is dropped rather than
searched: the pattern then searches for the text without it, which is a
narrower and perfectly predictable search, and one that can never widen into
a wildcard — unless the term was *nothing but* NUL bytes, in which case
there is nothing left to search for and the caller is told rather than
handed a pattern of ``"%%"``, which matches every row in the table. It is
not the same text the caller wrote, so a caller who would rather be told
than be searched for refuses the NUL itself — see the policy paragraph
below.

**The policy is the caller's.** :func:`like_pattern` drops NUL bytes and
escapes the metacharacters, and returns a usable pattern for any text that
has something to search for; a term with nothing to search for is refused
(see below). A caller that would rather refuse the query outright — because
the text came from somewhere the caller is answering for, and "your query
was rejected" is more useful than "your query matched everything" — checks
for the NUL itself first and raises. :meth:`paa.memory.facts.FactRepository.
search_facts_by_text` is that caller: the query is a model-supplied tool
argument, so the refusal is reported back to the model as an error value
rather than swallowed. Every internal recall path uses the permissive form,
because those paths have to answer rather than refuse.

The whole decision, with the consequence at each call site spelled out, is
in ``docs/adr/0023-like-pattern-escaping.md`` (SPEC DEVIATION
(docs/adr/0023)).

The **SQL half** of a LIKE lives here as well, so that the escape character
and the clause are declared in one place: :func:`like_predicate` builds the
search predicate over a list of columns, and :func:`like_count_expression`
builds the per-gram count a caller orders candidate rows by. Neither of them
builds a pattern — a pattern is a value, and a value is bound by the caller
— so the pattern half of both is :func:`like_pattern`.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "JSONB_DEFAULT_PATTERN",
    "LIKE_ESCAPE",
    "LIKE_ESCAPE_SQL",
    "escape_like",
    "like_count_expression",
    "like_pattern",
    "like_predicate",
]

#: The character LIKE patterns are escaped with. One definition, because the
#: pattern half (:func:`like_pattern`) and the SQL half (:data:`LIKE_ESCAPE_SQL`)
#: have to agree: an escape the pattern uses and an escape the SQL does not
#: declare is a pattern that matches something other than what it says.
LIKE_ESCAPE: Final[str] = "\\"

#: The SQL fragment that declares the escape character to the engine. Derived
#: from :data:`LIKE_ESCAPE` so the two cannot disagree. A backslash inside a
#: single-quoted SQL literal is a backslash — not an escape — under the
#: standard-conforming string rules both SQLite and PostgreSQL apply by
#: default, so the fragment reads ``ESCAPE '\'`` on the wire. PostgreSQL's
#: ``standard_conforming_strings`` has defaulted to ``on`` since 9.1 and
#: :mod:`paa.storage.relational.postgres` does not turn it off; a deployment
#: that did would need the fragment written as ``ESCAPE '\\'`` instead.
LIKE_ESCAPE_SQL: Final[str] = f"ESCAPE '{LIKE_ESCAPE}'"

#: The pattern that matches a column default the catalog still describes in
#: terms of ``jsonb``.
#:
#: A *catalog* pattern rather than a search pattern: it is matched against
#: the way PostgreSQL renders a column's default in
#: ``information_schema.columns``, so what it looks for is the literal text
#: ``::jsonb`` inside that rendering. There is no user text in it, so
#: there is nothing to escape — but it is a LIKE pattern, and it lives here
#: with every other one rather than in the call site, so that "every LIKE
#: pattern in the relational layer has one home" stays true.
#:
#: It is used as a **bound parameter**, not written into the statement: a
#: pattern is a value, and a value belongs in the parameter list.
JSONB_DEFAULT_PATTERN: Final[str] = "%::jsonb%"


def escape_like(value: str) -> str:
    """Escape the LIKE metacharacters in ``value``.

    ``\\``, ``%`` and ``_`` are the three characters SQLite's ``LIKE``
    treats specially; the backslash is escaped first because it is the
    escape character itself, and escaping it after the others would double
    the backslashes this function just introduced.

    The result is only meaningful as a pattern *with* :data:`LIKE_ESCAPE`
    declared — see :data:`LIKE_ESCAPE_SQL` and :func:`like_predicate`.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def like_pattern(term: str) -> str:
    """The LIKE pattern that matches ``term`` literally, as a substring.

    NUL bytes are dropped: a NUL ends the pattern where SQLite reads it
    through the C string API, so it can never be searched for, and the rest
    of the term is still worth searching for. A term of nothing but NUL
    bytes therefore has nothing left to search for.

    :raises ValueError: if the term has no searchable content — it is empty,
        or was nothing but NUL bytes. A pattern built from nothing would be
        ``"%%"``, a bare wildcard that matches every row in the table, and
        there is no LIKE pattern that matches nothing *instead*, so the
        caller has to be told rather than handed a wildcard. A caller that
        would rather refuse a NUL than drop it checks for one itself and
        raises — see the module docstring for why the policy is the
        caller's to make.
    """
    searchable = term.replace("\x00", "")
    if not searchable:
        raise ValueError("a search term must not be empty")
    return f"%{escape_like(searchable)}%"


def like_predicate(*columns: str) -> str:
    """The SQL half of a literal LIKE search over ``columns``.

    Builds ``"a LIKE ? ESCAPE '\\' OR b LIKE ? ESCAPE '\\'"`` — one
    placeholder per column, in the order given, each with the escape
    character declared. The pattern half is :func:`like_pattern`; the two are
    meant to be used together, and the number of placeholders here is the
    number of parameters the caller binds.

    The column names are interpolated because a column list is a
    module-level constant at every call site, never caller input — the same
    rule the rest of the relational layer follows, and the same reason
    :data:`LIKE_ESCAPE_SQL` is a constant rather than something assembled
    from a value.

    :raises ValueError: if no column is given. The empty predicate would be
        an empty SQL fragment, which is a statement that matches nothing and
        looks like a bug at the call site rather than one.
    """
    if not columns:
        raise ValueError("a LIKE predicate needs at least one column")
    return " OR ".join(f"{column} LIKE ? {LIKE_ESCAPE_SQL}" for column in columns)


def like_count_expression(column: str) -> str:
    """The SQL half of "how many of a term's grams are in this column".

    Builds ``"(CASE WHEN column LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"`` — a
    1 when the column contains the bound pattern and a 0 when it does not,
    for use in an ``ORDER BY`` expression. A caller that wants to count a
    term's grams sums one of these per gram and binds one pattern per
    expression, in the order the expressions appear.

    The pattern half is :func:`like_pattern`, and it is *bound* rather than
    interpolated: a pattern is a value, and a value belongs in the parameter
    list. The caller is the only party that knows which grams the term has,
    and building the pattern here would put the escaping rule in two places
    — the rule that has to be in one place (see ``docs/adr/0023``).

    The escape character is declared on the comparison, so the fragment
    reads the same on both engines and a pattern that was escaped for SQLite
    is not silently unescaped by PostgreSQL's default.

    The column name is interpolated because a column name is a module-level
    constant at every call site, never caller input — the same rule
    :func:`like_predicate` follows, and the same reason
    :data:`LIKE_ESCAPE_SQL` is a constant rather than something assembled
    from a value.

    **The fragment is the same on both engines; one difference inside it is
    not.** SQLite's ``LIKE`` folds ASCII case and PostgreSQL's does not, so
    the count of a case-differing pair is 1 on one engine and 0 on the
    other.

    **Folding the column does not make the count portable.** A caller
    that needs the count to mean the same thing on both engines is tempted
    to fold the column itself — ``like_count_expression("lower(e.name)")``
    — and that fixes the ASCII ``LIKE``-case axis only. The two engines'
    ``lower()``\\ s are *different folds*: SQLite's is ASCII-only and
    PostgreSQL's is locale-aware, so the folded column still differs
    outside ASCII (measured: the row ``"CAFÉ NOIR"`` against the term
    ``"café"`` counts 1 on SQLite and 2 on PostgreSQL). The column's fold
    is the **engine's**, and that is the recorded limitation
    (ADR-0029's limitation (a)); the count is portable for ASCII text,
    which is where the corpus and the floor's calibration live. What the
    caller *can* do is fold the text in Python — the seam's
    :func:`paa.storage.relational.dialect.fold_case` — and bind that; a
    fold left to the engine is a fold the two engines do not agree on.

    :raises ValueError: if the column is empty. The expression would name
        no column at all, and a statement that cannot be parsed is a worse
        place to find that out than the call that built it.
    """
    if not column:
        raise ValueError("a LIKE count expression needs a column")
    return f"(CASE WHEN {column} LIKE ? {LIKE_ESCAPE_SQL} THEN 1 ELSE 0 END)"
