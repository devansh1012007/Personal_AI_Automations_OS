"""The shared LIKE-pattern helper: the invariants it exists to hold.

A ``LIKE`` pattern is the one place where a *value* changes the *meaning* of
a query, so the guarantee it offers — "this pattern matches exactly this
text" — is worth stating as a property rather than as three examples.

The example-based tests in ``tests/coder/test_runtime_tools.py`` cover the
consequences at the tool and repository level. These cover the mechanism, and
they cover it over arbitrary text, because that is how the NUL hole was
found: the examples covered ``%``, ``_`` and ``\\``, and the character that
actually broke the pattern was one none of them mentioned.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.storage.relational.like import (
    LIKE_ESCAPE,
    LIKE_ESCAPE_SQL,
    escape_like,
    like_count_expression,
    like_pattern,
    like_predicate,
)


def _has_content(text: str) -> bool:
    """Whether a term has anything to search for once its NULs are gone.

    The helper refuses such a term rather than handing back a pattern of
    ``"%%"``, so a strategy that produces one would make the property tests
    below fail for a reason that has nothing to do with the property they
    are about. The refusal itself is covered by its own test.
    """
    return bool(text.replace("\x00", ""))


def _unescape_like(pattern: str) -> str:
    """The inverse of the LIKE escaping, for the round-trip property.

    A backslash escapes whatever follows it; a trailing backslash stands for
    itself. Written here rather than imported because the production module
    has no use for the inverse — only the test does.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == LIKE_ESCAPE and index + 1 < len(pattern):
            out.append(pattern[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


class TestEscapingIsTotal:
    @given(text=st.text(min_size=1, max_size=24))
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_escaping_is_lossless_and_leaves_no_wildcard(self, text: str) -> None:
        escaped = escape_like(text)

        assert _unescape_like(escaped) == text, "escaping must be reversible"

        # Every ``%`` and ``_`` in the pattern must sit behind an odd number
        # of backslashes — an escape — and an even number means LIKE would
        # read it as a wildcard.
        run = 0
        for char in escaped:
            if char == LIKE_ESCAPE:
                run += 1
                continue
            assert not (char in "%_" and run % 2 == 0), (
                f"unescaped wildcard {char!r} survived in {escaped!r}"
            )
            run = 0

    @given(
        text=st.text(min_size=1, max_size=24).filter(_has_content),
        haystack=st.text(max_size=40),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_pattern_never_matches_a_string_that_does_not_contain_the_text(
        self, text: str, haystack: str
    ) -> None:
        """The real engine's own verdict, not a re-implementation of LIKE.

        Checked against SQLite directly — the engine the relational layer
        actually runs on — so the property is the database's, not this
        repository's reading of what ``ESCAPE`` means.
        """
        pattern = like_pattern(text)
        with closing(sqlite3.connect(":memory:")) as conn:
            (matched,) = conn.execute(
                f"SELECT ? LIKE ? {LIKE_ESCAPE_SQL}", (haystack, pattern)
            ).fetchone()

        # A match implies the searched-for text is really in there. The
        # pattern searches for the text *without* its NUL bytes — that is
        # what the drop means — so that is what the containment check is
        # against. SQLite's LIKE is case-insensitive for ASCII, so the check
        # folds case too.
        searched = text.replace("\x00", "")
        assert not matched or searched.lower() in haystack.lower()

    @given(
        prefix=st.text(max_size=12),
        suffix=st.text(max_size=12),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_a_nul_is_dropped_rather_than_truncating_the_pattern(
        self, prefix: str, suffix: str
    ) -> None:
        """A NUL truncates the pattern, so it is removed rather than searched.

        Found by a property test rather than by inspection: SQLite reaches
        the LIKE pattern through the C string API, so ``%\\x00%`` is ``%`` and
        matches *every row in the table* — the exact widening the metacharacter
        escaping exists to prevent, arrived at through a different character.

        The NUL is placed rather than drawn from the strategy: a strategy
        that *might* produce one makes this test a coin flip, and a test
        that only sometimes exercises the rule is a test that sometimes
        misses it.
        """
        text = f"{prefix}\x00{suffix}"
        searchable = text.replace("\x00", "")
        if not searchable:
            # Nothing but the NUL: there is no pattern to build, and the
            # helper says so rather than handing back a wildcard.
            with pytest.raises(ValueError, match="empty"):
                like_pattern(text)
            return

        pattern = like_pattern(text)

        assert "\x00" not in pattern
        # The rest of the text survives, so the search is for what the caller
        # meant rather than for a prefix of it. The body is the pattern
        # without its wrapping wildcards — sliced, not stripped: a stripped
        # "%" would also take the tail of an escaped "%%".
        assert _unescape_like(pattern[1:-1]) == text.replace("\x00", "")

    def test_a_term_with_nothing_to_search_for_is_refused(self) -> None:
        """The helper never hands back a pattern that matches every row.

        Dropping the NUL bytes out of a term that was *nothing but* NUL
        bytes leaves nothing, and a pattern built from nothing is ``"%%"`` —
        a bare wildcard. There is no LIKE pattern that matches nothing
        instead, so the caller has to be told.
        """
        with pytest.raises(ValueError, match="empty"):
            like_pattern("\x00")
        with pytest.raises(ValueError, match="empty"):
            like_pattern("")

    def test_an_undropped_nul_ends_the_pattern_where_the_engine_says_it_does(self) -> None:
        """The engine's verdict on the pattern the drop prevents.

        SQLite reads the pattern through the C string API, so a NUL *ends* it:
        ``%\\x00%`` is ``%`` and nothing else, and a bare ``%`` matches every
        row. The escape clause does not change this — the NUL is not a
        metacharacter, it is the end of the string.

        With the NUL dropped the pattern carries the text, and the text is
        what decides the match.
        """
        with closing(sqlite3.connect(":memory:")) as conn:
            (widened,) = conn.execute(
                f"SELECT ? LIKE ? {LIKE_ESCAPE_SQL}", ("anything", "%\x00%")
            ).fetchone()
            (dropped,) = conn.execute(
                f"SELECT ? LIKE ? {LIKE_ESCAPE_SQL}", ("anything", like_pattern("a\x00b"))
            ).fetchone()

        assert widened, "an undropped NUL ends the pattern at a bare wildcard"
        assert not dropped


class TestTheTwoHalvesAgree:
    def test_the_escape_clause_declares_the_escape_character(self) -> None:
        assert f"ESCAPE '{LIKE_ESCAPE}'" == LIKE_ESCAPE_SQL

    def test_the_predicate_declares_the_clause_on_every_column(self) -> None:
        predicate = like_predicate("f.predicate", "f.object_value")

        assert predicate == (
            f"f.predicate LIKE ? {LIKE_ESCAPE_SQL} OR f.object_value LIKE ? {LIKE_ESCAPE_SQL}"
        )

    def test_the_predicate_binds_one_parameter_per_column(self) -> None:
        """The placeholder count is the parameter count.

        A mismatch here is a runtime error on every call, and the arithmetic
        is the kind of thing that silently drifts when a column is added.
        """
        assert like_predicate("a").count("?") == 1
        assert like_predicate("a", "b", "c").count("?") == 3

    def test_a_predicate_with_no_column_is_refused(self) -> None:
        """An empty predicate is an empty SQL fragment.

        That would be a statement matching nothing, which reads as a bug at
        the call site rather than as one, so it is refused here instead.
        """
        with pytest.raises(ValueError, match="column"):
            like_predicate()

    def test_the_clause_survives_the_postgres_placeholder_translation(self) -> None:
        """The SQL half has to survive the rewrite to psycopg's dialect.

        The runtime speaks to two engines, and the Postgres one rewrites
        ``?`` to ``%s`` while doubling literal ``%``. The escape clause lives
        inside a quoted literal, so it must come through untouched — a
        rewritten clause would leave the pattern escaped against an engine
        that was never told about the escape character.
        """
        from paa.storage.relational.postgres import _qmark_to_pyformat

        sql = f"SELECT 1 WHERE {like_predicate('a', 'b')} AND x = ?"

        translated = _qmark_to_pyformat(sql)

        # Two placeholders from the LIKE predicate, one from the trailing
        # comparison.
        assert translated.count("%s") == 3
        assert LIKE_ESCAPE_SQL in translated
        assert translated.count(LIKE_ESCAPE_SQL) == 2

    def test_a_percent_inside_a_literal_is_still_escaped(self) -> None:
        """psycopg's parser does not stop at quotes.

        A ``%`` is a placeholder marker to psycopg whether it is inside a
        string literal or not, so a query that contains one — a ``LIKE``
        pattern written into the statement, say — has to double it. The
        error psycopg raises otherwise is about the *placeholder*, which
        says nothing about what was actually wrong.
        """
        from paa.storage.relational.postgres import _qmark_to_pyformat

        translated = _qmark_to_pyformat(
            "SELECT 1 FROM t WHERE column_default LIKE '%::jsonb%' AND x = ?"
        )

        # The ``%`` of the LIKE pattern is doubled, so psycopg un-doubles it
        # and the server sees the pattern that was written.
        assert "'%%::jsonb%%'" in translated
        # The placeholder is still a placeholder.
        assert translated.count("%s") == 1

    def test_a_question_mark_inside_a_literal_is_not_a_placeholder(self) -> None:
        """A ``?`` inside a string is a character on both engines."""
        from paa.storage.relational.postgres import _qmark_to_pyformat

        translated = _qmark_to_pyformat("SELECT 1 FROM t WHERE name = 'a?b' AND x = ?")

        assert translated.count("%s") == 1
        assert "'a?b'" in translated


class TestPatternShape:
    def test_the_pattern_is_a_substring_match(self) -> None:
        assert like_pattern("alpha") == "%alpha%"

    def test_a_term_with_no_content_is_refused_rather_than_matching_everything(
        self,
    ) -> None:
        """The empty pattern would be "%%" — a bare wildcard.

        The one shape this module must never produce.
        """
        with pytest.raises(ValueError, match="empty"):
            like_pattern("")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("%", "%\\%%"),
            ("_", "%\\_%"),
            ("\\", "%\\\\%"),
            ("a%b", "%a\\%b%"),
            ("a_b", "%a\\_b%"),
        ],
    )
    def test_the_metacharacters_are_escaped(self, text: str, expected: str) -> None:
        assert like_pattern(text) == expected


class TestTheCountExpression:
    """The per-gram count a candidate query orders its rows by.

    The count is the numerator of the trigram similarity the Python gate
    computes, expressed in SQL so that SQLite and PostgreSQL can order
    the same way. The expression is therefore a *number* the engine
    produces, and these tests are the engine's verdict rather than a
    re-implementation of it.
    """

    def test_the_expression_is_the_documented_fragment(self) -> None:
        """The exact shape, because the caller sums these.

        One expression per gram, summed; the shape is what tells the
        caller that the sum is a count and the placeholders are the
        patterns to bind.
        """
        assert like_count_expression("e.canonical_name") == (
            f"(CASE WHEN e.canonical_name LIKE ? {LIKE_ESCAPE_SQL} THEN 1 ELSE 0 END)"
        )

    def test_the_expression_binds_one_parameter(self) -> None:
        """The placeholder count is the pattern count.

        One expression per gram means one pattern per expression, and a
        mismatch here is a runtime error on every call.
        """
        assert like_count_expression("name").count("?") == 1

    def test_an_expression_with_no_column_is_refused(self) -> None:
        """The expression would name no column at all.

        A statement that cannot be parsed is a worse place to find that
        out than the call that built it.
        """
        with pytest.raises(ValueError, match="column"):
            like_count_expression("")

    def test_the_expression_counts_the_grams_a_row_contains(self) -> None:
        """The count is the engine's verdict, not a re-implementation of it.

        Checked against SQLite directly — the engine the relational layer
        actually runs on — so the property is the database's, not this
        repository's reading of what a ``CASE WHEN ... LIKE`` means. A row
        carrying one of the two grams counts 1; a row carrying neither
        counts 0.
        """
        grams = ("alp", "zep")
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, name TEXT)")
            conn.executemany(
                "INSERT INTO candidates (name) VALUES (?)", [("alpha",), ("zulu",)]
            )
            score = " + ".join(like_count_expression("name") for _ in grams)
            rows = conn.execute(
                f"SELECT id, {score} AS grams_matched FROM candidates ORDER BY id",
                [like_pattern(gram) for gram in grams],
            ).fetchall()

        assert rows == [(1, 1), (2, 0)]

    def test_the_expression_agrees_with_the_pattern_on_a_metacharacter(self) -> None:
        """The escape clause is *inside* the expression, not beside it.

        A gram carrying a ``%`` is matched literally because the pattern
        was escaped by :func:`like_pattern` **and** the expression declares
        the escape character. With either half missing the count is wrong:
        without the clause the escaped backslash is a literal character no
        row carries, and without the escaping the ``%`` is a wildcard that
        matches a row it should not.
        """
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, name TEXT)")
            conn.executemany(
                "INSERT INTO candidates (name) VALUES (?)", [("a%b",), ("axb",)]
            )
            rows = conn.execute(
                f"SELECT id, {like_count_expression('name')} AS matched "
                "FROM candidates ORDER BY id",
                (like_pattern("a%b"),),
            ).fetchall()

        assert rows == [(1, 1), (2, 0)]

    def test_the_expression_survives_the_postgres_placeholder_translation(self) -> None:
        """The SQL half has to reach psycopg's dialect intact.

        The runtime speaks to two engines, and the Postgres one rewrites
        ``?`` to ``%s``. The count is built by summing one expression per
        gram, so every one of them carries a placeholder that has to be
        translated, and the escape clause has to come through untouched.
        """
        from paa.storage.relational.postgres import _qmark_to_pyformat

        sql = f"SELECT 1 ORDER BY {like_count_expression('a')} + {like_count_expression('b')}"

        translated = _qmark_to_pyformat(sql)

        assert translated.count("%s") == 2
        assert translated.count(LIKE_ESCAPE_SQL) == 2
