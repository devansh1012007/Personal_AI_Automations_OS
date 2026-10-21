"""The dialect seam — where SQLite and PostgreSQL genuinely differ.

Two relational backends, one repository layer. The repository layer writes
SQL that both engines accept, with a handful of exceptions; this module is
where those exceptions live, so that no repository has to ask which engine it
is talking to.

Why a module and not ``if backend == "postgres"`` at the call sites: the
repository layer is where the business logic lives, and a branch there is a
branch that has to be re-tested for every new backend. Here, each divergence
is a small, named, unit-testable value — the same pattern
``storage/relational/like.py`` already uses for ``LIKE`` patterns.

What belongs here, and what does not:

* **Value coercion** (how a column's value arrives) — a timestamp column is
  TEXT on SQLite and ``TIMESTAMPTZ`` on PostgreSQL, so the same row hands
  back a ``str`` on one and a ``datetime`` on the other. The coercion is
  *value-level* and lives in the shared helpers (``from_iso``, ``loads``)
  rather than here, because a repository reads a column, it does not execute
  a query about it. The UUID coercion lives beside it, in
  ``paa.core.types`` (``as_uuid`` and ``as_text``), for the same reason:
  it is a *value* coercion, and both substrates' rows go through the same
  helper so they produce the same value. Neither belongs to the seam.
  The one exception is the **case fold** — also value-level, also
  portable, and here for the opposite reason: the fold exists *because*
  the two engines' folds differ, so the difference has to be resolved in
  one place, and this is the place where the two engines' idioms live.
  :func:`fold_case` is that one place.
* **SQL fragments** — a handful of expressions that have no portable
  spelling. Each is a short string, assembled once, and asserted in a test
  that needs no database.
* **Locking** — how a transaction serialises writers for one lineage. This
  is the one place the *semantics* differ rather than the spelling, and it
  is the reason the ledger needs a hook rather than a rewritten query.

**The FTS5 quoting lives here too.** ``MATCH`` is a *query language*, and
quoting a term for it is an engine difference, so the quoting is a seam
member's business rather than the repository's: :func:`_fts_phrase` is the
only place in the repository that quotes for FTS5, and a second quoting is
a second place for the injection barrier to be wrong.

**The case fold lives here too, and it is the one that had to.** The ways
the two engines handle case are all different: FTS5's ``trigram``
tokenizer folds the case of the term it is asked about (a Unicode *simple*
fold — measured: ``"Éclair"`` and ``"éclair"`` produce the same token set,
the ``"écl"`` trigram among them); SQLite's ``lower()`` folds ASCII only;
PostgreSQL's ``lower()`` is locale-aware; and PostgreSQL's ``LIKE`` — which
is what the server's phrase predicate is built from — is case-sensitive
outright (SQLite's ``LIKE`` folds ASCII case, and is case-sensitive only
outside ASCII).

A fold therefore cannot be left to the engine, because the engines' folds
are not the same fold: :func:`fold_case` does it in Python, once, and both
engines' phrase predicates are built from the text it produces. A
repository that folded the term itself would be carrying a per-engine
difference — "the server's ``LIKE`` needs a folded term, the embedded
one's does not" — and the seam exists so that no repository has to know
that.

SPEC DEVIATION (docs/adr/0025): the RFC assumed the two substrates were
interchangeable because their DDL was kept column-for-column identical. It
was not sufficient — the divergence is in the SQL dialect and in the value
types, and a repository that knows neither is the only way to keep them
interchangeable in practice. See ``docs/adr/0025-postgres-dialect-seam.md``.

**The seam's shape changed, and the change amends ADR-0025.**
:meth:`Dialect.entity_candidates_sql` used to return a statement and
*require* the caller to supply the parameters, and the requirement could
not hold for the *new* design — which takes one parameter per column as
well as the matcher expression, and the two engines take different
numbers of them. It now returns the statement **together with the
parameters that statement takes**, which is the only shape in which the
two engines' idioms cannot drift apart at the call site. (The old code
was not wrong: the old statement did take ``(query, limit)``, and the old
caller bound exactly that.) That amendment is recorded in **ADR-0029**
(``docs/adr/0029-entity-resolution-parity.md``).
"""

from __future__ import annotations

from typing import Any, Final, Literal, Protocol, runtime_checkable

from paa.storage.relational.like import like_count_expression, like_pattern, like_predicate

__all__ = [
    "Dialect",
    "PostgresDialect",
    "SqliteDialect",
    "fold_case",
]

#: The two columns an entity's text lives in: the canonical name, and the
#: alias blob (the JSON *text* of the alias array). Both are folded to
#: lower case by the statement — the *column* is folded by the engine and
#: the *term* is folded by :func:`fold_case`, and the two folds are not the
#: same fold: SQLite's ``lower()`` is ASCII-only and PostgreSQL's is
#: locale-aware. The divergence for non-ASCII text is a recorded
#: limitation rather than an accident (see ``docs/plans/w3-w5.md`` §1.6).
#:
#: The alias **blob** rather than the expanded aliases, and that is
#: deliberate: the blob is the same text on both engines, so the two
#: candidate queries are the same question asked the same way, and a row
#: whose alias column is corrupt cannot break one engine's fuzzy rung and
#: not the other's.
#:
#: **The blob is JSON text, and ``json.dumps`` escapes non-ASCII.** The
#: column holds ``["\u00e9\u00e9\u00e9"]`` for the alias ``"ééé"``, so a
#: non-ASCII alias is **invisible to the count proxy**: the proxy's
#: pattern is built from the folded *text*, and the text is not in the
#: escaped blob. The gate would score that alias 0.5 against the term
#: ``"éééx"`` — over the floor — and the row is not in the candidate set,
#: so the answer is "no match". The breakage is *symmetric*: the blob is
#: the same text on both substrates, so both answer "no match" and the
#: superset property fails on both rather than on one. Closing it needs a
#: stored normalised form (ADR-0029's limitation (c)).
_CANONICAL_COLUMN: Final[str] = "lower(e.canonical_name)"
_ALIASES_COLUMN: Final[str] = "lower(e.aliases)"


def fold_case(text: str) -> str:
    """The text, case-folded — the portable fold, in one place.

    **The fold cannot be left to the engine, because the engines do not
    fold the same way.** FTS5's ``trigram`` tokenizer folds the case of
    the term it is asked about, with a Unicode *simple* fold (measured:
    ``"Éclair"`` is matched as ``"éclair"``); SQLite's ``lower()`` folds
    ASCII only; PostgreSQL's ``lower()`` is locale-aware; and
    PostgreSQL's ``LIKE`` — the operator the server's phrase predicate is
    built from — is case-sensitive outright.

    That last one is the defect this function exists to close. The phrase
    predicate folds the *column* (``lower(e.canonical_name)``) and compares
    it with a bound pattern, and on the server the comparison is
    case-sensitive, so a pattern bound with the **raw** term matches no row
    there while FTS5 — which folds the term itself — matches it on SQLite:
    the two engines running different queries over the same corpus, and
    answering differently.

    So the fold happens here, in Python, once, and both engines' phrase
    predicates are built from the text it produces.

    **What the fold is not.** Python's ``str.lower()`` is Unicode-aware
    and full: it maps each character to its lowercase form, and for
    exactly one code point in Unicode — ``U+0130`` ``İ`` — that mapping
    *changes the length* (``"İ"`` → ``"i̇"``, two characters). FTS5's
    simple fold cannot do that, so the token set of the folded term and
    the token set of the raw term differ for that one character: the
    seam's phrase parameter is the folded text, and FTS5 matches a stored
    ``"i̇stanbul"`` and not a stored ``"İstanbul"`` — the opposite of
    what the tokenizer's own fold would have found. PostgreSQL's
    ``lower()`` gives a third answer for the same character (it maps
    ``İ`` to a single ``i``). Every other code point folds to the same
    token set, and the *fold itself* is idempotent everywhere.

    **And the column is folded by the engine, not by this.** The gate
    folds the row's text with Python; the candidate query folds it with
    the engine's ``lower()`` — ASCII-only on SQLite, locale-aware on
    PostgreSQL. So the *term* is folded the same way everywhere, and the
    *column* is not: for text outside ASCII the two substrates can still
    answer differently. That is the recorded limitation of
    ``docs/plans/w3-w5.md`` §1.6, not a new one, and it is the reason
    the fold is done here rather than by the engine. This is the same
    fold the repository's gate uses (:func:`paa.memory.facts._trigrams`),
    so the candidate query asks about the same *term* the gate scores.
    """
    return text.lower()


@runtime_checkable
class Dialect(Protocol):
    """The per-engine differences a repository is allowed to know about."""

    #: Short, stable name of the substrate — ``"sqlite"`` / ``"postgres"``.
    name: str

    # -- SQL fragments -----------------------------------------------------

    def scalar_max(self, expression: str, parameter: str) -> str:
        """The two-argument maximum, spelled for this engine.

        SQLite's ``MAX`` is scalar with two arguments; PostgreSQL's ``MAX``
        is strictly an aggregate, and the two-argument form is ``GREATEST``.
        """
        ...

    def alias_values(self, column: str) -> str:
        """Expand a JSON array column into rows, one element per row.

        SQLite has ``json_each``; PostgreSQL has
        ``jsonb_array_elements_text``. Both name their output column
        ``value``, so the caller's ``a.value`` reference works either way.
        """
        ...

    def entity_candidates_sql(
        self,
        *,
        mode: Literal["phrase", "broad"],
        term: str,
        grams: tuple[str, ...],
        limit: int,
    ) -> tuple[str, tuple[Any, ...]]:
        """The candidate-selection query for entity resolution — and its parameters.

        **The statement comes with the parameters it takes.** The two
        engines take different parameters for the same question — an FTS5
        expression on one, a set of ``LIKE`` patterns on the other — and
        the old contract ("the statement must take exactly the parameters
        the caller supplies, ``(query, limit)``") cannot hold for the
        *new* design, which takes one parameter per column as well as
        the matcher expression. The seam now returns the parameters with
        the statement, so the call site stays engine-agnostic and the two
        engines cannot drift apart there. (The old code was not wrong —
        the old statement did take ``(query, limit)``, and the old caller
        bound exactly that.)

        (This is a change to the seam's shape and it amends ADR-0025; it is
        recorded in **ADR-0029**
        (``docs/adr/0029-entity-resolution-parity.md``).)

        **The two modes are two questions, and each has one unambiguous
        parameter.**

        ``mode="phrase"`` — *is the term a substring of the row's text?*
        SQLite matches an FTS5 phrase; PostgreSQL matches the term as a
        literal pattern over the canonical name and the alias blob.

        **The phrase parameter is the case-folded term, on both engines.**
        The fold is done here, in Python, by :func:`fold_case`, and both
        engines' phrase predicates are built from the text it produces —
        so the two engines ask the *same* question. Handing the raw term
        to FTS5 and the raw term to ``LIKE`` asks two different
        questions: FTS5 folds the term and ``LIKE`` does not, so a term
        carrying an upper-case letter matches a phrase row on one substrate
        and none on the other, and the two substrates answer differently
        over the same corpus.

        ``mode="broad"`` — *does the row's text share at least one of the
        term's grams?*  That is the gate's own precondition — a pair the
        gate accepts shares at least one gram — so the candidate set is a
        superset of the gate's acceptance set on both substrates. SQLite
        matches the grams OR'd; PostgreSQL counts the same grams with the
        portable ``LIKE`` count and tests the count against zero.

        **The superset holds for the text the two engines fold the same
        way.** The gate folds the row's text with Python; the count folds
        it with the engine's ``lower()``, and those are not the same fold
        outside ASCII.

        **And the difference is one-directional, and the answers differ.**
        PostgreSQL's ``lower()`` is locale-aware and folds ``U+0130``
        ``İ`` to a single ``i``; SQLite's ``lower()`` is ASCII-only and
        leaves it alone, and FTS5's simple fold cannot follow Python's
        length-changing mapping either. So **PostgreSQL sees rows the
        other two substrates cannot see**, and the two answers are not the
        same answer — sometimes one substrate resolves and the other does
        not, and sometimes both resolve the *same* entity with a
        different match kind and a different score. Measured on this
        host, live PostgreSQL 16 and the embedded substrate, the same
        corpus in both, through the repository's own code:

        ```
        stored 'İstanbul'  term 'istanbul'  sqlite: ('İstanbul','fuzzy',0.625)
                                          postgres: ('İstanbul','exact',1.0)
        stored 'İZMİR'     term 'izmir'      sqlite: None
                                          postgres: ('İZMİR','exact',1.0)
        stored 'éåİ'       term 'éåi'        sqlite: None
                                          postgres: ('éåİ','exact',1.0)
        ```

        The broad candidate set diverges with it: the term ``"izmir"``
        offers the row ``"İZMİR"`` on PostgreSQL and offers nothing on
        SQLite. This is the recorded limitation of
        ``docs/plans/w3-w5.md`` §1.6, and it is measured — not assumed —
        by ``tests/storage/test_entity_parity.py``.

        (The gate can also accept a row that *neither* substrate offers:
        the term ``"İabx"`` against the row ``"İab"`` is accepted at
        0.6667, and every gram the two share involves the character whose
        Python fold changes the length. That case is a narrowing of the
        candidate set on both substrates — they agree — and it is the
        *other* direction of the same limitation.)

        **The order is a total order, and it is portable for ASCII text.**
        The primary key is the count proxy — the number of the term's
        grams in the row's text — and the secondary key is the unique
        ``e.id``. The engines' own rankings (``bm25``, ``similarity``) are
        not comparable, so a cut made on them is a cut that can change
        the answer; the count proxy is the same measure on both engines
        **for ASCII text**, and it is the measure the broad predicate
        uses, so the rows that survive the cut are the rows that share
        the most with the term. Outside ASCII the proxy is *not* the same
        measure on both engines — the column's fold is the engine's
        (SQLite's is ASCII-only, PostgreSQL's is locale-aware), so the
        counts can differ: measured, the row ``"CAFÉ NOIR"`` against the
        term ``"café"`` counts **1** on SQLite and **2** on PostgreSQL —
        and the cut is then not guaranteed to keep the same rows. That is
        the same limitation as above, and it is recorded with it.

        The proxy is the count the gate counts with, not the gate's score
        — see :func:`_count_proxy`.

        ``term`` is the text being resolved and ``grams`` is that text's
        trigrams — the same grams the gate counts, produced by the same
        function, so that one place decides what a gram is. The term is
        case-folded by this method, not by the caller and not by the
        engine.

        **The caller must bound the number of grams.** The count proxy is
        a sum of two ``CASE`` expressions per gram, and it sits in the
        statement's ``ORDER BY``, so SQLite's expression-depth limit is
        reached at about 500 grams (measured: 498 parse, 499 do not) — and
        the statement is then *refused* rather than answered, with an
        error that is not a ``QueryRejected`` and so does not fail closed.
        The repository refuses such a term before it gets here
        (``paa.memory.facts._MAX_TERM_GRAMS``), symmetrically on both
        substrates; a caller of this seam that does not is the one that
        has to know the limit.

        :raises ValueError: if ``grams`` is empty. The count proxy is a sum
            over the grams, and the sum of nothing cannot order a cut.
        """
        ...

    def arrival_seq(self) -> str:
        """The column that carries insertion order.

        The tiebreak of last resort for two rows written in the same clock
        tick. It is a column on both engines — SQLite's is filled from
        ``rowid`` by a trigger, PostgreSQL's is an identity column — so the
        ordering is a property of the *schema* rather than of the engine's
        internals.
        """
        ...

    # -- locking ----------------------------------------------------------

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """Serialise writers for one lineage, for the life of the transaction.

        The ledger assigns ``state_version`` by reading the current tip and
        writing ``tip + 1``. That read-then-write is only atomic if two
        writers for the same lineage cannot interleave — on SQLite it is,
        because the backend holds a process-wide write lock for the whole
        transaction. On a server substrate the lock has to be taken in the
        server, or the second writer collides with the first and the append
        fails where SQLite would have succeeded.

        Takes the correlation id so the lock is per-lineage: unrelated
        lineages must not block each other.
        """
        ...


class SqliteDialect:
    """The embedded engine's dialect."""

    name = "sqlite"

    def scalar_max(self, expression: str, parameter: str) -> str:
        return f"MAX({expression}, {parameter})"

    def alias_values(self, column: str) -> str:
        return f"json_each({column})"

    def entity_candidates_sql(
        self,
        *,
        mode: Literal["phrase", "broad"],
        term: str,
        grams: tuple[str, ...],
        limit: int,
    ) -> tuple[str, tuple[Any, ...]]:
        # FTS5's trigram matcher. The FTS5 table is a virtual table, so it
        # is joined by rowid — the entity index's rowid, which is the FTS
        # table's content rowid.
        #
        # The two modes differ in the one parameter the matcher takes: the
        # quoted phrase, or the quoted grams OR'd together. The order is
        # the same in both, because the count proxy counts the *term's*
        # grams either way — the gate's numerator does not depend on how
        # the candidate set was drawn.
        #
        # The count proxy is built first: it is what refuses a query with
        # no grams, and the match expression is built from the same grams.
        count, patterns = _count_proxy(grams)
        # The phrase is the *folded* term, so that both engines' phrase
        # predicates are built from the same text (see :func:`fold_case`).
        # The tokenizer folds the case of the term itself — a Unicode
        # simple fold — so the token set is the same one either way, with
        # one exception: ``U+0130`` ``İ``, whose Python lowercase mapping
        # changes the length and a simple fold cannot follow.
        match = _fts_phrase(fold_case(term)) if mode == "phrase" else _fts_broad(grams)
        sql = (
            "SELECT e.* FROM hot_serving_entity_fts f "  # noqa: S608
            "JOIN hot_serving_entity_index e ON e.rowid = f.rowid "
            "WHERE hot_serving_entity_fts MATCH ? "
            f"ORDER BY {count} DESC, e.id ASC LIMIT ?"
        )
        return sql, (match, *patterns, limit)

    def arrival_seq(self) -> str:
        return "arrival_seq"

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """No-op: the write lock is already held for the whole transaction.

        :meth:`paa.storage.relational.database.Database.transaction` takes a
        process-wide write lock before it yields, so two writers in one
        process cannot interleave. Two writers in *two* processes are
        serialised by SQLite's own file locking, which is what
        ``busy_timeout`` waits on.
        """
        return None


class PostgresDialect:
    """The server engine's dialect."""

    name = "postgres"

    def scalar_max(self, expression: str, parameter: str) -> str:
        return f"GREATEST({expression}, {parameter})"

    def alias_values(self, column: str) -> str:
        # The column holds JSON as text on both engines, so the server form
        # casts it to jsonb before expanding it.
        return f"jsonb_array_elements_text(({column})::jsonb)"

    def entity_candidates_sql(
        self,
        *,
        mode: Literal["phrase", "broad"],
        term: str,
        grams: tuple[str, ...],
        limit: int,
    ) -> tuple[str, tuple[Any, ...]]:
        count, patterns = _count_proxy(grams)
        if mode == "phrase":
            # The term as a literal, over the two columns the row's text
            # lives in. The pattern is built by ``like_pattern``, so a term
            # carrying a wildcard is searched for literally rather than
            # compiled into one.
            #
            # **The pattern is built from the folded term**, and that is the
            # whole point of the fold: ``LIKE`` is case-sensitive on both
            # engines while the *column* is folded by the engine, so a
            # pattern bound with the raw term matches no row here while
            # FTS5 — which folds the term itself — matches it on SQLite.
            # The two engines would then run different queries over the
            # same corpus and answer differently. Folded here, in Python,
            # both engines' phrase predicates are built from the same text
            # (see :func:`fold_case`).
            folded = fold_case(term)
            predicate = like_predicate(_CANONICAL_COLUMN, _ALIASES_COLUMN)
            predicate_params: tuple[Any, ...] = (like_pattern(folded), like_pattern(folded))
        else:
            # The same count the order uses, tested against zero: the
            # predicate is the gate's own precondition, expressed the way
            # the count is expressed. Its patterns are bound twice — once
            # for the predicate and once for the order — because the sum
            # appears in both.
            predicate = f"({count}) > 0"
            predicate_params = patterns
        sql = (
            "SELECT e.* FROM hot_serving_entity_index e "  # noqa: S608
            f"WHERE {predicate} "
            f"ORDER BY {count} DESC, e.id ASC LIMIT ?"
        )
        return sql, (*predicate_params, *patterns, limit)

    def arrival_seq(self) -> str:
        return "arrival_seq"

    async def lock_correlation(self, conn: Any, correlation_id: str) -> None:
        """Take a transaction-scoped advisory lock for this lineage.

        ``pg_advisory_xact_lock`` is held until the transaction ends — commit
        or rollback — so it cannot leak. The key is the correlation id
        hashed to a bigint: a hash collision over-serialises two lineages,
        which costs a little throughput and corrupts nothing.

        The lock is taken *before* the tip is read, so the read-then-write
        that assigns ``state_version`` is atomic per lineage.
        """
        # The cursor is closed: an advisory lock takes no result set, and an
        # unclosed cursor is a connection resource held for the life of the
        # transaction — one per ledger append.
        async with conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))", (correlation_id,)
        ):
            pass


# ---------------------------------------------------------------------------
# The pieces both engines share
# ---------------------------------------------------------------------------


def _count_proxy(grams: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """The count proxy — a portable count of the term's grams, as SQL, and
    its patterns.

    The proxy counts the term's grams present in the row's text: one
    ``CASE`` per (gram, column) pair, and one bound pattern per
    expression, in the order the expressions appear. It is a count of
    the *row's text as the query sees it* — the canonical name plus the
    alias **blob** — rather than an engine's relevance score, for three
    reasons: it is the same measure on both engines, it is the measure
    the broad predicate uses, and it is the measure the gate counts with
    — so the rows the cut keeps are the rows that share the most with
    the term.

    **"The same measure on both engines" is true for ASCII text, and only
    for ASCII text.** The column is folded by the *engine* — SQLite's
    ``lower()`` is ASCII-only, PostgreSQL's is locale-aware — and the two
    folds are different folds, so outside ASCII the counts differ:
    measured, the row ``"CAFÉ NOIR"`` against the term ``"café"`` counts
    **1** on SQLite and **2** on PostgreSQL. The count is therefore
    portable where the corpus is ASCII, and the divergence outside it is
    the recorded limitation of ADR-0029, not a property of the proxy.

    **The count is not the score, and it is not the gate's own
    numerator either.** The gate scores the canonical name and each
    alias *individually*, and its numerator is the count over *that*
    pair; the proxy counts over the row's text as a whole, so the two
    differ — measured for the row (canonical ``gamma``, aliases
    ``["alpha", "beta"]``) and the term ``"alpha beta"``: the proxy
    counts 5, while the gate's numerators are 3 (against ``"alpha"``)
    and 2 (against ``"beta"``). And the count is not the score: the
    gate's score is a Jaccard, the count divided by the *union* of the
    two gram sets, and the union's size varies with the row. Two rows
    can therefore hold the same count and score differently — measured
    against the term ``"widget service"``: ``"Widget Services"`` holds
    all twelve of the term's grams and scores 0.9231, ``"Widget Service
    Platform"`` holds the same twelve and scores 0.5714 — so a cut
    between them is decided by the id, not by the score. The proxy is
    the right thing to *order* by (it is the same measure everywhere the
    two engines fold alike), and it is not a promise about which row the
    gate would have scored highest.

    The two columns are the canonical name and the alias blob, both folded
    to lower case by the statement (see :data:`_CANONICAL_COLUMN`). The
    blob is **JSON text**, and ``json.dumps`` escapes non-ASCII, so a
    non-ASCII alias is invisible to the count — see
    :data:`_ALIASES_COLUMN`.

    :raises ValueError: if there are no grams. The proxy is a sum over the
        grams, and the sum of nothing cannot order a cut.
    """
    if not grams:
        raise ValueError("an entity candidate query needs at least one gram")
    expressions: list[str] = []
    patterns: list[str] = []
    for gram in grams:
        for column in (_CANONICAL_COLUMN, _ALIASES_COLUMN):
            expressions.append(like_count_expression(column))
            patterns.append(like_pattern(gram))
    return " + ".join(expressions), tuple(patterns)


def _fts_phrase(value: str) -> str:
    """Quote a term as an FTS5 phrase literal.

    FTS5's ``MATCH`` is a *query language*: an unquoted ``AND`` is an
    operator, a stray ``*`` is a prefix operator, and a term carrying a
    ``"`` closes the phrase and the rest of the term is parsed as more
    query. Doubling the embedded quotes makes the whole term one literal
    phrase, and inside a quoted phrase FTS5 treats ``*``, ``AND``, ``OR``,
    ``NEAR``, ``^``, ``-`` and ``:`` as ordinary characters.

    **This is the injection barrier, and it is the only place in the
    repository that quotes for FTS5.** A second quoting is a second place
    for the barrier to be wrong, so the quoting is a seam member's
    business and not the caller's.

    Measured, on both sides of the barrier: a term carrying ``"``, ``*``,
    ``AND``, ``OR``, ``NEAR``, ``(`` and ``)`` is quoted into a parameter
    that matches nothing and raises nothing; the same term *unquoted* is an
    FTS5 syntax error — ``fts5: syntax error near "-"`` — which the
    repository catches and fails closed on. The quoted form is the one that
    answers "no match" rather than "the query was refused".
    """
    return '"' + value.replace('"', '""') + '"'


def _fts_broad(grams: tuple[str, ...]) -> str:
    """The FTS5 expression for "shares at least one of these grams".

    Each gram is quoted on its own — through :func:`_fts_phrase`, so there
    is still exactly one place that quotes — and the phrases are OR'd
    together. Measured to be exactly "the documents sharing at least one of
    these grams", over every sample tried.
    """
    return " OR ".join(_fts_phrase(gram) for gram in grams)
