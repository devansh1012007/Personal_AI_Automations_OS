"""The dialect seam: the candidate query, the parameters it takes, and its order.

Entity resolution is the one place the two substrates have to give the same
answer, and it is built on three pieces: the *candidate set* (which rows the
database hands to the gate), the *gate* (which of them the Python trigram
similarity accepts), and the *cut* (how many of them survive the ``LIMIT``).

This file covers the seam's half of that: the statement, the parameters that
statement takes, and the order the cut is made in. The gate's half is
``paa.memory.facts``'s, and the two engines' answers are compared in
``tests/storage/test_entity_parity.py``.

**These tests need no database.** The seam's members are values a backend
cannot spell portably (ADR-0025), and a value is asserted by looking at it.
The one exception is the FTS5 quoting, which is an *engine* property — a
string assertion cannot prove that a bound parameter is inert to a query
language — so that one is checked against an in-memory SQLite, which needs
no server, no configuration and no schema file.

**What the old shape could not express.** The seam used to return a statement
and *require* the caller to supply the parameters, and the requirement could
not hold for both engines: the SQLite form took an FTS5 expression and the
PostgreSQL form took a term, plus the count proxy's patterns on both. So
both implementations ignored *both* of their arguments, and the caller bound
the parameters the implementation ignored — a statement and its parameters
that were free to disagree, with nothing checking. The seam now returns the
parameters with the statement, and the first test below counts one against
the other.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Final, Literal

import pytest

from paa.storage.relational.dialect import (
    Dialect,
    PostgresDialect,
    SqliteDialect,
    fold_case,
)
from paa.storage.relational.like import like_pattern
from paa.storage.relational.postgres import _qmark_to_pyformat

#: The term the tests resolve, and the grams of that term.
#:
#: The grams are an *input* to the seam — the seam is told which grams the
#: term has, so that one place decides what a gram is — so the tests state
#: them rather than deriving them from the gate they are meant to agree with.
_TERM: Final[str] = "Widget Service"
_GRAMS: Final[tuple[str, ...]] = ("wid", "idg", "dge")

#: The count proxy's half-expression, written out rather than imported from
#: the module that builds it: the test has to be able to fail when the seam
#: stops using the helper, and it cannot do that if it *is* the helper.
_COUNT_CASE: Final[str] = (
    "(CASE WHEN lower(e.canonical_name) LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END)"
)

#: The two columns the count proxy reads, and the two it reads them from.
_COLUMNS: Final[tuple[str, str]] = ("lower(e.canonical_name)", "lower(e.aliases)")

#: The modes the seam takes, as literals — the seam's own type, so the
#: tests cannot pass a mode it does not know.
_MODES: Final[tuple[Literal["phrase", "broad"], ...]] = ("phrase", "broad")

#: The dialects, as the protocol they are used through.
_DIALECTS: Final[tuple[Dialect, ...]] = (SqliteDialect(), PostgresDialect())

#: A term carrying every character FTS5's query language gives a meaning to.
#:
#: The double quote closes a phrase, the ``*`` is a prefix operator, ``AND``
#: / ``OR`` / ``NEAR`` are the operators, and ``^`` / ``-`` / ``:`` are the
#: remaining ones. Unquoted, this term is not a term at all — it is a query.
_HOSTILE: Final[str] = 'alpha" OR "beta* AND NEAR(gamma) ^ - : delta'


def _placeholder_count(sql: str) -> int:
    """The number of placeholders the statement carries.

    Counted with the *production* translator rather than with a count of
    ``?``: the translator is what the server substrate's parser will see,
    and it skips a ``?`` inside a quoted literal — so a statement whose
    literal carries a question mark must not be counted as carrying a
    parameter.
    """
    return _qmark_to_pyformat(sql).count("%s")


def _order_of(sql: str) -> str:
    """The ``ORDER BY`` expression, without its ``DESC`` and its ``LIMIT``."""
    tail = sql.split("ORDER BY", 1)[1]
    return tail.split(" DESC", 1)[0].strip()


def _predicate_of(sql: str) -> str:
    """The ``WHERE`` expression."""
    return sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0].strip()


class TestTheFold:
    """The fold is a seam member, so it is asserted on its own.

    ADR-0025 asks of every seam member that it be "a value a backend
    cannot spell portably, and each is asserted in a test that needs no
    database" — and the fold is the seam member with the most weight on
    it: the term is folded here, in Python, because the engines' folds are
    not the same fold. A test that only asserted the *use* of the fold
    would not notice the fold changing under it.
    """

    def test_the_fold_is_a_lower_case_mapping(self) -> None:
        """The fold lower-cases, and it folds the whole text.

        The engines' folds are all different — FTS5's tokenizer folds the
        term it is asked about, SQLite's ``lower()`` folds ASCII only,
        PostgreSQL's is locale-aware — so the fold that both engines' phrase
        predicates are built from has to be *one* fold, decided in one
        place.
        """
        assert fold_case("Widget Service") == "widget service"
        assert fold_case("WIDGET SERVICE") == "widget service"
        assert fold_case("Éclair") == "éclair"
        assert fold_case("") == ""

    def test_the_fold_is_idempotent(self) -> None:
        """Folding twice is folding once.

        The candidate query folds the term and the gate folds the row's
        text, so a fold that were not idempotent would make the two
        incomparable — the query would ask about a text the gate never
        scores.
        """
        for text in ("Widget Service", "Éclair", "İstanbul", "café", "ACME 42", ""):
            once = fold_case(text)
            assert fold_case(once) == once, f"the fold is not idempotent for {text!r}"

    def test_the_fold_changes_the_length_for_exactly_one_code_point(self) -> None:
        """``U+0130`` is the one character whose lowercase mapping is longer.

        Python's ``str.lower()`` is a *full* mapping, and for exactly one
        code point in Unicode — ``U+0130`` ``İ`` — it maps one character
        to two (``"i̇"``, ``i`` and a combining dot above). FTS5's
        simple fold cannot follow that, and PostgreSQL's ``lower()`` gives
        a third answer (a single ``i``), so this one character is where
        the three engines part company. It is recorded as ADR-0029's
        limitation (b).
        """
        assert len("İ") == 1
        assert len(fold_case("İ")) == 2
        assert fold_case("İ") == "i̇"
        # And the fold of the folded text is the folded text — the
        # idempotence holds even for the one character that changes the
        # length.
        assert fold_case(fold_case("İ")) == fold_case("İ")


class TestTheSeamShape:
    """The statement and the parameters that statement takes.

    The old contract was "the statement must take exactly the parameters
    the caller supplies" — and neither implementation used the parameters
    it was given, so the statement and the parameters could disagree about
    their own shape and nothing would notice. The seam now returns both, so
    the two can be counted against each other.
    """

    @pytest.mark.parametrize("dialect", _DIALECTS, ids=lambda dialect: dialect.name)
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_statement_comes_with_the_parameters_it_takes(
        self, dialect: Dialect, mode: Literal["phrase", "broad"]
    ) -> None:
        sql, params = dialect.entity_candidates_sql(
            mode=mode, term=_TERM, grams=_GRAMS, limit=20
        )
        assert isinstance(sql, str)
        assert sql.strip()
        assert isinstance(params, tuple)
        assert _placeholder_count(sql) == len(params), (
            f"{dialect.name}/{mode}: the statement takes "
            f"{_placeholder_count(sql)} parameters and the seam returned {len(params)}"
        )

    @pytest.mark.parametrize("dialect", _DIALECTS, ids=lambda dialect: dialect.name)
    def test_a_query_without_grams_is_refused(
        self, dialect: Dialect
    ) -> None:
        """The count proxy is a sum over the grams, and the sum of nothing
        cannot order a cut.

        The repository never asks for it — the ladder refuses a term
        shorter than a trigram — so this is the seam refusing to build a
        statement it cannot honour.
        """
        with pytest.raises(ValueError, match="at least one gram"):
            dialect.entity_candidates_sql(
                mode="broad", term=_TERM, grams=(), limit=20
            )


class TestTheOrder:
    """The cut is made on a total order, and the same one on both engines.

    ``AGENTS.md`` requires a total order for a ``LIMIT`` cut, and the two
    engines' own rankings (``bm25``, ``similarity``) are not comparable —
    a cut made on them is a cut that can change the answer. The order here
    is the gate's own numerator (the count proxy) and the unique key.
    """

    @pytest.mark.parametrize("dialect", _DIALECTS, ids=lambda dialect: dialect.name)
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_order_is_the_count_proxy_then_the_unique_key(
        self, dialect: Dialect, mode: Literal["phrase", "broad"]
    ) -> None:
        sql, _ = dialect.entity_candidates_sql(
            mode=mode, term=_TERM, grams=_GRAMS, limit=20
        )
        tail = sql.split("ORDER BY", 1)[1]
        assert tail.endswith("DESC, e.id ASC LIMIT ?")

        # One case per (gram, column) pair, and the columns are the two the
        # row's text lives in.
        assert tail.count("CASE WHEN") == len(_GRAMS) * len(_COLUMNS)
        for column in _COLUMNS:
            assert tail.count(column) == len(_GRAMS)

    def test_both_engines_order_by_the_same_count_proxy(self) -> None:
        """The order is portable only if the proxy is the same expression.

        Two spellings of the same idea would cut two different sets, which
        is the defect the portable order exists to remove.
        """
        sqlite_sql, _ = SqliteDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        postgres_sql, _ = PostgresDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        assert _order_of(sqlite_sql) == _order_of(postgres_sql)
        assert _order_of(sqlite_sql).count(_COUNT_CASE) == len(_GRAMS)

    def test_the_count_proxy_is_built_by_the_like_helper(self) -> None:
        """The pattern half of the proxy comes from one place.

        A ``LIKE`` pattern is a value, and a value is bound rather than
        interpolated — so the proxy's SQL half is the helper's and its
        pattern half is the caller's, and the two cannot drift apart.
        """
        _, params = SqliteDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        # The first parameter is the matcher's and the last is the limit;
        # the ones between are the proxy's.
        assert list(params[1:-1]) == [
            like_pattern(gram) for gram in _GRAMS for _ in _COLUMNS
        ]

    def test_a_gram_carrying_a_metacharacter_is_a_literal_not_a_wildcard(self) -> None:
        """The proxy's patterns come from ``like_pattern``, metacharacters and all.

        The grams above carry no ``LIKE`` metacharacter, so a proxy that
        built its patterns with an f-string — ``f"%{gram}%"`` — would
        produce the *same* parameters and pass every other test here. It
        would also be a parity break: a ``LIKE`` metacharacter in a gram
        is a wildcard on one engine and a literal on the other, so the two
        substrates would be asked different questions and would draw
        different candidate sets.

        The grams below are the ones that expose it: ``"50%"`` (a
        wildcard that matches any run of characters), ``"a_b"`` (``_``
        matches any *one* character) and ``"a\\b"`` (the escape character
        itself).
        """
        grams = ("50%", "a_b", "a\\b")
        expected = [like_pattern(gram) for gram in grams for _ in _COLUMNS]

        _, sqlite_params = SqliteDialect().entity_candidates_sql(
            mode="broad", term="50% a_b a\\b", grams=grams, limit=20
        )
        # The first parameter is the matcher's and the last is the limit;
        # the ones between are the proxy's.
        assert list(sqlite_params[1:-1]) == expected

        _, postgres_params = PostgresDialect().entity_candidates_sql(
            mode="broad", term="50% a_b a\\b", grams=grams, limit=20
        )
        # The server's statement carries the proxy twice — once in the
        # predicate and once in the order — and the limit is last.
        assert list(postgres_params[:-1]) == expected * 2

        # And the metacharacter is escaped, not compiled in.
        assert "%50\\%%" in expected
        assert "%a\\_b%" in expected
        assert "%a\\\\b%" in expected


class TestTheSqliteMatcher:
    """SQLite's half: FTS5's trigram matcher, and its quoting."""

    def test_the_phrase_parameter_is_the_quoted_phrase(self) -> None:
        sql, params = SqliteDialect().entity_candidates_sql(
            mode="phrase", term=_TERM, grams=_GRAMS, limit=20
        )
        assert "MATCH ?" in sql
        # The seam folds the term in Python, once, and both engines' phrase
        # predicates are built from the folded text — a fold left to the
        # engine is a fold the two engines do not agree on. Folding here is
        # idempotent for FTS5, which folds the term itself.
        assert params[0] == '"' + fold_case(_TERM) + '"'
        assert params[0] == '"widget service"'

    def test_the_broad_parameter_is_the_quoted_grams_or_ed(self) -> None:
        sql, params = SqliteDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        assert "MATCH ?" in sql
        assert params[0] == '"wid" OR "idg" OR "dge"'

    def test_the_two_modes_differ_only_in_the_match_parameter(self) -> None:
        """The order is the same in both modes.

        The count proxy counts the *term's* grams either way — the gate's
        numerator does not depend on how the candidate set was drawn.
        """
        phrase_sql, phrase_params = SqliteDialect().entity_candidates_sql(
            mode="phrase", term=_TERM, grams=_GRAMS, limit=20
        )
        broad_sql, broad_params = SqliteDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        assert phrase_sql == broad_sql
        assert phrase_params[1:] == broad_params[1:]
        assert phrase_params[0] != broad_params[0]


class TestThePostgresPredicate:
    """PostgreSQL's half: the same question, asked with portable SQL."""

    def test_the_phrase_predicate_is_the_term_over_the_two_columns(self) -> None:
        sql, params = PostgresDialect().entity_candidates_sql(
            mode="phrase", term=_TERM, grams=_GRAMS, limit=20
        )
        assert _predicate_of(sql) == (
            "lower(e.canonical_name) LIKE ? ESCAPE '\\' "
            "OR lower(e.aliases) LIKE ? ESCAPE '\\'"
        )
        assert list(params)[:2] == [
            like_pattern(fold_case(_TERM)),
            like_pattern(fold_case(_TERM)),
        ]

    def test_the_broad_predicate_is_the_gram_count_tested_against_zero(self) -> None:
        """The predicate is the gate's own precondition.

        A pair the gate accepts shares at least one gram, so "shares at
        least one of the term's grams" is exactly the gate's precondition
        — and it is expressed with the same count the order uses, so the
        candidate set and the ranking are the same measure.
        """
        sql, _params = PostgresDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        predicate = _predicate_of(sql)
        assert predicate.endswith("> 0")
        assert predicate.count("CASE WHEN") == len(_GRAMS) * len(_COLUMNS)
        assert predicate == f"({_order_of(sql)}) > 0"

    def test_the_broad_parameters_are_one_pattern_per_gram_and_column(self) -> None:
        """The predicate's patterns and the order's patterns, then the limit.

        The count appears twice — once in the predicate and once in the
        order — so its patterns are bound twice, and the limit is last.
        """
        _, params = PostgresDialect().entity_candidates_sql(
            mode="broad", term=_TERM, grams=_GRAMS, limit=20
        )
        patterns = [like_pattern(gram) for gram in _GRAMS for _ in _COLUMNS]
        assert list(params) == patterns + patterns + [20]
        assert params[-1] == 20


class TestTheFts5Quoting:
    """The quoting is the injection barrier, and it is load-bearing.

    Everything the ingestion path resolves is untrusted text, and FTS5's
    ``MATCH`` is a query language: an unquoted ``AND`` is an operator, a
    stray ``*`` is a prefix operator, and a double quote closes the phrase
    and hands the rest of the term to the parser. The seam quotes the term,
    so the term is one literal phrase and the operators inside it are
    ordinary characters.
    """

    def test_a_hostile_term_is_quoted_into_one_inert_phrase(self) -> None:
        _, params = SqliteDialect().entity_candidates_sql(
            mode="phrase", term=_HOSTILE, grams=_GRAMS, limit=20
        )
        quoted = str(params[0])
        assert quoted.startswith('"') and quoted.endswith('"')
        # Every quote inside the phrase is doubled, so the phrase cannot be
        # closed early and the rest of the term cannot become query.
        assert quoted.count('"') == 2 + 2 * fold_case(_HOSTILE).count('"')
        # And the term is inside the quotes, not beside them: the operators
        # are characters of a phrase rather than operators of a query.
        assert quoted == '"' + fold_case(_HOSTILE).replace('"', '""') + '"'

    def test_the_quoting_is_load_bearing(self) -> None:
        """The engine's own verdict on the *unquoted* term.

        Without this, the quoting could be deleted on the grounds that it
        "looked fine" — and the term would become a query error rather
        than a term that matches nothing.
        """
        with (
            closing(_sqlite_with_entities()) as conn,
            pytest.raises(sqlite3.OperationalError, match="fts5: syntax error"),
        ):
            conn.execute(
                "SELECT rowid FROM hot_serving_entity_fts "
                "WHERE hot_serving_entity_fts MATCH ?",
                (_HOSTILE,),
            ).fetchall()

    def test_a_hostile_term_matches_nothing_and_raises_nothing(self) -> None:
        """The whole statement, run against a real SQLite.

        The parameter is inert, so the statement runs, returns nothing, and
        raises nothing — which is what "the term resolves to no match"
        means. The positive control is there so that "nothing" cannot be
        the answer for a reason that has nothing to do with the term: the
        same statement, with a term that does occur, finds the row.
        """
        with closing(_sqlite_with_entities()) as conn:
            dialect = SqliteDialect()

            hostile_sql, hostile_params = dialect.entity_candidates_sql(
                mode="phrase", term=_HOSTILE, grams=_GRAMS, limit=20
            )
            assert conn.execute(hostile_sql, hostile_params).fetchall() == []

            good_sql, good_params = dialect.entity_candidates_sql(
                mode="phrase", term=_TERM, grams=_GRAMS, limit=20
            )
            matched = conn.execute(good_sql, good_params).fetchall()
            assert [row[2] for row in matched] == [_TERM]


def _sqlite_with_entities() -> sqlite3.Connection:
    """An in-memory SQLite carrying the entity index and its FTS5 shadow.

    The two tables the seam's statement names, and nothing else: the
    statement reads ``id``, ``canonical_name`` and ``aliases``, and the
    FTS5 table is the trigram-tokenised shadow the schema keeps in step by
    trigger. No server, no configuration, no schema file — the quoting is
    an engine property and an engine is the only thing that can decide it.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE hot_serving_entity_index ("
        "id TEXT PRIMARY KEY, class TEXT, canonical_name TEXT, aliases TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE hot_serving_entity_fts "
        "USING fts5(canonical_name, aliases, content='', tokenize='trigram')"
    )
    conn.execute(
        "CREATE TRIGGER trg_entity_fts_insert AFTER INSERT ON hot_serving_entity_index BEGIN"
        " INSERT INTO hot_serving_entity_fts(rowid, canonical_name, aliases)"
        " VALUES (new.rowid, new.canonical_name, new.aliases); END"
    )
    conn.execute(
        "INSERT INTO hot_serving_entity_index VALUES (?,?,?,?)",
        ("11111111-1111-1111-1111-111111111111", "project", _TERM, "[]"),
    )
    return conn
