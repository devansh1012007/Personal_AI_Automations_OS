"""Cross-engine parity for entity resolution — the same corpus, the same answer.

Why this file exists
--------------------
Entity resolution is the one place the two relational substrates have to give
the *same* answer. A fact written on SQLite and read on PostgreSQL must
resolve to the same entity, or the same term means two different things
depending on which substrate served it — and the runtime cannot tell the
difference, because both answers arrive as an
:class:`~paa.memory.facts.Entity`.

The SQLite-only suite cannot see that class of defect. It can see one
substrate's answer; it can never see that the other substrate's answer is
different. This file puts the **same corpus** into both substrates and
compares what comes back.

The corpus is the same *content* in both, written in **opposite orders**. That
is deliberate: an answer that depends on the physical order of the rows is an
answer that depends on which substrate served the request, and the only way
to see it is to give the two substrates the same rows in a different order.

What it proves
--------------
1. **The same answer.** For every term, the two substrates return the same
   ``(entity id, match kind, score)`` — *all three*, because a suite that
   compares only the id misses the case where the two engines pick the same
   entity and disagree about how confident they are.
2. **The candidate set is a superset of the gate's acceptance set**, on both
   substrates: every row the gate would accept is a row the gate was given.
3. **The tie-break is deterministic.** Two entities with identical scores —
   constructed below, not found by accident — produce the same winner on both
   substrates and in any insertion order.
4. **The hostile terms behave alike.** A NUL, and FTS5's query language.
5. **The short-word case.** A term whose words are shorter than three
   characters finds the row the gate accepts outright, on both substrates —
   the candidate query is the gate's *own precondition*, so it does not
   depend on any engine's gram model (``pg_trgm`` pads every word, short
   ones included: ``similarity('a', 'a')`` is **1.0** there too).

The corpus
----------
Fourteen rows, chosen for the shapes that have broken this path before:
names with internal spaces, a name with a **double** space, punctuation
(``A. Lovelace``), digits (``Agent 47``), mixed case (``McDonald
Consulting``), an acronym in capitals (``ACME 42``), non-ASCII (``café``), a
name and an alias that differ only in case, an alias that is an exact match
for a term, two names that score *identically* against a third term, and the
pair the gate's own docstring names (``aba`` and ``abba``).

Two properties of the corpus are worth stating, because they are properties
of the *production* code and not of the test:

* **No two canonical names are equal case-insensitively.** The schema's
  ``UNIQUE`` on ``canonical_name`` is case-*sensitive* (SQLite's BINARY
  collation, PostgreSQL's default), so the table can hold ``café`` and
  ``Café`` as two rows — and the exact rung compares them with ``lower()``, so
  it sees them as *one* key and answers with whichever row its scan reaches
  first. That is a defect, and it is recorded in
  :class:`TestTheRepairedDivergences` rather than papered over here: the corpus
  used by the parity tests holds no such pair, so the parity assertions are
  about the resolution and not about the ambiguity.
* **The name and the alias that differ only in case are unreachable through
  the alias rung.** The exact rung folds the case, so a term that matches
  such an alias matches the name too, and the exact rung answers first. The
  pair is in the corpus because the *exact* rung is where the two engines'
  ``lower()`` differ — SQLite's is ASCII-only and PostgreSQL's is
  locale-aware — and because the non-ASCII pair is the shape that difference
  shows up in.

Pointing the suite at a server
------------------------------
The tests need a PostgreSQL server, named by ``PAA_TEST_POSTGRES_DSN``:

.. code-block:: bash

    docker run -d --name paa-test-pg \\
        -e POSTGRES_USER=paa -e POSTGRES_PASSWORD=paa \\
        -e POSTGRES_DB=postgres -p 127.0.0.1:55461:5432 postgres:16
    export PAA_TEST_POSTGRES_DSN="postgresql://paa:paa@127.0.0.1:55461/postgres"
    pytest tests/storage/test_entity_parity.py

Without that variable the module **skips**: the default suite has to stay
runnable on a laptop with no server.

The DSN names a *server*, not a database to write to. Every test here creates
a database of its own and drops it when it is over — so a run can never touch
the database the deployment is using, whatever the DSN points at.

Where the SQLite half of each property lives
--------------------------------------------
Each property below has a counterpart in the ordinary suite, which needs no
server and therefore actually runs on every machine:

* *the gate's model is the index's model* —
  ``tests/memory/test_facts.py::TestTheGateModel``
* *a NUL term never reaches the substrate* —
  ``tests/memory/test_facts.py::TestEntities::test_a_nul_term_never_reaches_the_substrate``
* *the candidate query's shape and its order* —
  ``tests/storage/test_dialect.py``
* *the two substrates keep the same error vocabulary* —
  ``tests/storage/test_postgres_contract.py``

The parity file is the one that can see the two substrates *disagree*.

What the suite found
--------------------
:class:`TestTheRepairedDivergences` records three divergences the suite
found, each of them a measurement rather than a contract — **each one
fails when the defect is fixed**, and each one names the place the fix
belongs:

* the exact rung has no total order, so two names that differ only in case
  are one key to the rung and the answer is whichever row the engine's scan
  reaches first;
* the PostgreSQL phrase predicate folds the *column* but not the *term*, so
  a term carrying an upper-case letter matches no phrase row there while
  FTS5 folds the case on SQLite;
* the two together make the two substrates answer **differently** for the
  same term and the same corpus.

:class:`TestTheRecordedDivergence` records the one divergence that is
**not** repaired: the case fold of the *column* is the engine's, and the
two engines' folds are not the same fold outside ASCII. That test
**passes while the divergence exists** — it is a measurement of what the
two substrates do, and it is what says the limitation is still open.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import pytest

from paa.memory.facts import Entity, FactRepository, _trigrams, trigram_similarity
from paa.storage.relational.database import Database, dumps, to_iso
from paa.storage.relational.postgres import PostgresDatabase

#: The server the tests run against, or ``None`` when there is none.
#:
#: The environment variable is the house convention for a test-only switch
#: (``PAA_TEST_VERBOSE`` is the existing example): it names the *server*, and
#: the tests make their own database on it.
DSN = os.environ.get("PAA_TEST_POSTGRES_DSN")

#: The guard for the heavy dependency. ``psycopg`` is never imported at
#: module scope: a minimal install must be able to import this module (and
#: skip) without the extra.
HAS_PSYCOPG = (
    importlib.util.find_spec("psycopg") is not None
    and importlib.util.find_spec("psycopg_pool") is not None
)

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.skipif(
        not HAS_PSYCOPG or not DSN,
        reason="needs the 'postgres' extra and PAA_TEST_POSTGRES_DSN",
    ),
]

#: The floor the gate applies, and the floor the brute force applies. The
#: two are the same number, and the tests pass it explicitly so that a change
#: to the repository's default cannot silently change what "accepted" means.
_FLOOR: Final[float] = 0.34

#: The candidate limit the tests ask for. The corpus is smaller than this, so
#: the cut is not exercised by the parity assertions; the cut itself is
#: exercised by
#: :meth:`TestTheCandidateSet.test_the_cut_is_made_on_the_same_order_on_both_substrates`,
#: which asks for one row.
_CANDIDATE_LIMIT: Final[int] = 20

#: The timestamp every row of the corpus is written with. One stamp for every
#: row, so that a row's position in a table cannot be a function of when it
#: was written.
_STAMP: Final[str] = to_iso(datetime(2026, 1, 1, tzinfo=UTC))


@dataclass(frozen=True, slots=True)
class _Row:
    """One row of ``hot_serving_entity_index``: the fixture's unit of corpus.

    The id is fixed rather than generated, for two reasons. The comparison
    is over *ids*, so the two substrates must agree on them or there is
    nothing to compare; and the tie-break is decided by the id, so a
    generated id would make the expected winner unknowable.
    """

    id: str
    canonical_name: str
    aliases: tuple[str, ...] = ()


#: The corpus. The same rows, in the same content, in both substrates.
_CORPUS: Final[tuple[_Row, ...]] = (
    # Internal spaces, an alias that is an exact match for a term, and an
    # alias that differs from the name only in case.
    _Row(
        "00000000-0000-4000-8000-000000000001",
        "Widget Service",
        ("Gadget Assembly", "widget service"),
    ),
    # Punctuation.
    _Row("00000000-0000-4000-8000-000000000002", "A. Lovelace", ("Ada Lovelace",)),
    # Digits.
    _Row("00000000-0000-4000-8000-000000000003", "Agent 47", ("Agent Forty Seven",)),
    # A double space. The repository collapses whitespace runs when it
    # *writes* a name, so this row is written directly: the point of the
    # shape is what the two engines do with the text the index holds.
    _Row("00000000-0000-4000-8000-000000000004", "Double  Space"),
    # Non-ASCII, with an alias that differs from the name only in case.
    _Row("00000000-0000-4000-8000-000000000005", "café", ("Café",)),
    # Mixed case.
    _Row("00000000-0000-4000-8000-000000000006", "McDonald Consulting", ("Mcdonald",)),
    # The tie pair. Both score exactly 0.5 against the term "silva", and both
    # hold every one of that term's grams, so their count proxies are equal
    # and the winner is decided by the id alone.
    _Row("00000000-0000-4000-8000-000000000007", "Silva Jr"),
    _Row("00000000-0000-4000-8000-000000000008", "Silva Sr"),
    # Short words: the gate compares two texts shorter than a trigram as
    # *text*, so the pair is accepted outright and the candidate query has
    # to be able to return the row.
    _Row("00000000-0000-4000-8000-000000000009", "a b"),
    _Row("00000000-0000-4000-8000-00000000000a", "a b c"),
    # Shares grams with "Widget Service" without being it.
    _Row("00000000-0000-4000-8000-00000000000b", "Widget Gadget"),
    # An acronym, in capitals. The case fold is where the two engines'
    # ``lower()`` differ, so a row that is *entirely* capitals is the shape
    # that notices: the candidate query has to count the term's grams in the
    # *folded* text, and the exact rung has to fold the name the same way on
    # both substrates. The alias carries no gram of the term, so the count
    # is the canonical name's alone.
    _Row("00000000-0000-4000-8000-00000000000c", "ACME 42", ("Forty Two",)),
    # The pair the gate's own docstring names: two texts that share no gram
    # of their own, and whose only shared grams are the *padding* grams a
    # padded model would invent. Under the raw model they share nothing, so
    # the gate refuses the pair; under a padded model the gate would accept
    # it on grams no candidate query over the index can see.
    _Row("00000000-0000-4000-8000-00000000000d", "aba"),
    _Row("00000000-0000-4000-8000-00000000000e", "abba"),
)

#: The term that ties the pair above, and the id the tie-break must pick.
#:
#: The candidates arrive in the order ``(count proxy DESC, e.id ASC)`` and the
#: gate's ``>=`` takes the *last* maximum, so among two rows with the same
#: score and the same count the winner is the one with the **greater** id.
_TIE_TERM: Final[str] = "silva"
_TIE_WINNER: Final[str] = "00000000-0000-4000-8000-000000000008"
_TIE_LOSER: Final[str] = "00000000-0000-4000-8000-000000000007"

#: The terms the parity assertions run over.
#:
#: Each one is there for a reason, and the reason is in the comment: the
#: ladder's three rungs, the case the gate's model changed for, and the
#: shapes that used to be answered differently.
_TERMS: Final[tuple[str, ...]] = (
    # The exact rung.
    "Widget Service",
    "widget service",
    "A. Lovelace",
    "a. lovelace",
    "Agent 47",
    "agent 47",
    "McDonald Consulting",
    "mcdonald consulting",
    # Non-ASCII, and the case-differing name/alias pair.
    "café",
    "Café",
    # The alias rung.
    "Gadget Assembly",
    "gadget assembly",
    "Ada Lovelace",
    "Mcdonald",
    "forty two",
    # The fuzzy rung: a typo, a prefix, a whitespace run, the tie, and the
    # short-word term.
    "A. Lovelce",
    "a. lovelce",
    "Agent 4",
    "agent 4",
    "acme 4",
    "Double  Space",
    "double space",
    "Silva",
    "silva",
    "a b d",
    "aba",
    # A term shorter than a trigram: the ladder refuses it before the gate
    # is asked.
    "ab",
)

#: The terms that reach a candidate query.
#:
#: Every term of :data:`_TERMS` except the one that is shorter than a
#: trigram: the ladder refuses that one before the seam is asked, and the
#: seam refuses it in turn — there is no gram to count and no phrase to
#: match, so there is no query to make.
_QUERY_TERMS: Final[tuple[str, ...]] = tuple(term for term in _TERMS if len(term) >= 3)

#: The terms carrying a NUL byte.
#:
#: SQLite truncates a string at a NUL when it reads it through the C string
#: API, so a NUL inside a ``LIKE`` pattern turns the pattern into a bare
#: ``%`` that matches every row; psycopg refuses the *bound parameter*
#: outright, before the statement is sent, with a ``DataError`` that carries
#: no SQLSTATE and so no classification a caller could catch. The refusal
#: in ``resolve_entity``, before any statement, is what makes the two agree.
_NUL_TERMS: Final[tuple[str, ...]] = (
    "\x00",
    "a\x00b",
    "Wid\x00get Service",
)

#: The terms carrying FTS5's query language.
#:
#: ``MATCH`` is a *query language*: an unquoted ``AND`` is an operator, a
#: stray ``*`` is a prefix operator, and a double quote closes the phrase and
#: hands the rest of the term to the parser. The seam quotes the term, so
#: these are ordinary characters — and the two substrates must agree that
#: they are.
_OPERATOR_TERMS: Final[tuple[str, ...]] = (
    '"',
    "*",
    "AND",
    "OR",
    "NEAR",
    "(",
    ")",
    'alpha" OR "beta* AND NEAR(gamma) ^ - : delta',
)

#: A corpus with two names that differ only in case.
#:
#: The schema's ``UNIQUE`` on ``canonical_name`` is case-sensitive, so the
#: table can hold both; the exact rung compares with ``lower()``, so it sees
#: them as one key and answers with whichever row its scan reaches first.
#: The two substrates hold these two rows in opposite orders, so they answer
#: differently — and the answer is a function of the physical order.
_CASE_VARIANT_CORPUS: Final[tuple[_Row, ...]] = (
    _Row("00000000-0000-4000-8000-000000000011", "café"),
    _Row("00000000-0000-4000-8000-000000000012", "Café"),
)

#: A corpus with a long name that *contains* a term, and a near-miss the gate
#: accepts and the phrase query cannot see.
#:
#: The long name contains "Widget Service" as a substring, so the phrase
#: query finds it — and the gate scores it 0.2727, below the floor, so the
#: answer is "no match". The near-miss shares eleven of the term's twelve
#: grams without containing the term, so only the *broad* query can see it,
#: and the gate scores it 0.9167.
_PHRASE_CORPUS: Final[tuple[_Row, ...]] = (
    _Row("00000000-0000-4000-8000-000000000013", "Widget Service Of The International Department"),
    _Row("00000000-0000-4000-8000-000000000014", "Widget Servic"),
)

#: A corpus whose names carry the one code point the three engines fold
#: three ways.
#:
#: ``U+0130`` ``İ`` is that code point: Python's ``str.lower()`` maps it to
#: *two* characters (``"i̇"``), FTS5's simple fold leaves it alone, and
#: PostgreSQL's locale-aware ``lower()`` maps it to a single ``i``. The
#: *term* is folded in Python by the seam, so the term is the same on both
#: substrates — but the *column* the candidate query reads is folded by the
#: engine, and that is where the two substrates part company: PostgreSQL
#: sees rows SQLite cannot see at all.
_FOLD_CORPUS: Final[tuple[_Row, ...]] = (
    _Row("00000000-0000-4000-8000-000000000021", "İstanbul"),
    _Row("00000000-0000-4000-8000-000000000022", "İZMİR"),
    _Row("00000000-0000-4000-8000-000000000023", "éåİ"),
)


@dataclass(frozen=True, slots=True)
class _Substrates:
    """The two substrates, holding the same corpus in opposite orders."""

    sqlite: FactRepository
    postgres: FactRepository
    sqlite_db: Database
    postgres_db: PostgresDatabase
    rows: tuple[_Row, ...]


async def _seed(db: Database | PostgresDatabase, rows: Sequence[_Row]) -> None:
    """Write the corpus into one substrate, verbatim.

    The rows are written directly rather than through
    :meth:`~paa.memory.facts.FactRepository.upsert_entity`, for three
    reasons: the two substrates have to agree on the *ids* (the comparison is
    over ids), the tie-break is decided by the id (a generated id would make
    the expected winner unknowable), and the repository collapses whitespace
    runs when it writes a name — so the double-space row could not be
    written at all through it.

    Every value is written the same way on both substrates: the id and the
    timestamps as text, which each engine casts to its own type, and the
    aliases as the same JSON text.
    """
    for row in rows:
        await db.execute(
            "INSERT INTO hot_serving_entity_index (id, class, canonical_name, aliases,"
            " importance_weight, confidence_rating, attributes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row.id,
                "person",
                row.canonical_name,
                dumps(list(row.aliases)),
                0.5,
                1.0,
                "{}",
                _STAMP,
                _STAMP,
            ),
        )


async def _substrates(
    db: Database, postgres: PostgresDatabase, rows: Sequence[_Row]
) -> _Substrates:
    """The corpus in both substrates, written in opposite orders.

    The order is the point: the two substrates hold the same rows, and the
    row that answers a term that matches more than one row is decided by
    which row the engine's scan reaches first. Writing them in opposite
    orders turns "the answer depends on the physical order" from an
    invisible property into a visible failure.
    """
    await _seed(db, rows)
    await _seed(postgres, tuple(reversed(rows)))
    return _Substrates(
        sqlite=FactRepository(db),
        postgres=FactRepository(postgres),
        sqlite_db=db,
        postgres_db=postgres,
        rows=tuple(rows),
    )


@pytest.fixture
async def substrates(db: Database, postgres: PostgresDatabase) -> _Substrates:
    """The main corpus, in both substrates, in opposite orders."""
    return await _substrates(db, postgres, _CORPUS)


@pytest.fixture
async def case_variant_substrates(db: Database, postgres: PostgresDatabase) -> _Substrates:
    """Two names that differ only in case, in opposite orders."""
    return await _substrates(db, postgres, _CASE_VARIANT_CORPUS)


@pytest.fixture
async def phrase_substrates(db: Database, postgres: PostgresDatabase) -> _Substrates:
    """A long name that contains a term, and a near-miss the broad query sees."""
    return await _substrates(db, postgres, _PHRASE_CORPUS)


@pytest.fixture
async def fold_substrates(db: Database, postgres: PostgresDatabase) -> _Substrates:
    """The corpus the three engines fold three ways, in both substrates."""
    return await _substrates(db, postgres, _FOLD_CORPUS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _triple(entity: Entity | None) -> tuple[str, str, float] | None:
    """The answer, as the three things two substrates have to agree about.

    The id says *which* entity, the match kind says *how* it was found
    (``exact``, ``alias`` or ``fuzzy``), and the score says *how confident*
    the answer is. A suite that compares only the id cannot see two engines
    agreeing about the entity and disagreeing about the confidence.
    """
    if entity is None:
        return None
    return (entity.id, entity.match, entity.score)


async def _resolve(repo: FactRepository, term: str) -> Entity | None:
    """Resolve a term with the floor and the cut the tests state."""
    return await repo.resolve_entity(
        term,
        fuzzy=True,
        fuzzy_floor=_FLOOR,
        candidate_limit=_CANDIDATE_LIMIT,
    )


async def _candidates(
    db: Database | PostgresDatabase, term: str, mode: Literal["phrase", "broad"]
) -> set[str]:
    """The ids one candidate query returns, on one substrate.

    The statement and the parameters it takes both come from the dialect
    seam, so this runs the *engine's own* candidate query — the same one the
    repository runs — rather than a paraphrase of it.
    """
    grams = tuple(sorted(_trigrams(term)))
    sql, params = db.dialect.entity_candidates_sql(
        mode=mode, term=term, grams=grams, limit=_CANDIDATE_LIMIT
    )
    rows = await db.fetch_all(sql, params)
    return {str(row["id"]) for row in rows}


def _gate_score(row: _Row, term: str) -> float:
    """The gate's score for one row: the best of its name's and its aliases'.

    The gate's own rule, over the row's canonical name and each of its
    aliases *individually* — an alias is a name the entity answers to,
    and the JSON blob the column holds is a different text.
    """
    return max(
        [
            trigram_similarity(term, row.canonical_name),
            *(trigram_similarity(term, alias) for alias in row.aliases),
        ]
    )


async def _effective_candidates(
    db: Database | PostgresDatabase, term: str, rows: Sequence[_Row]
) -> set[str]:
    """The candidates the *fuzzy rung* is given.

    The repository runs the **phrase** query first and falls back to the
    **broad** one when the phrase rows do not produce a candidate that
    clears the floor — not merely when the phrase query found nothing.
    The two policies are different, and the difference was a defect: the
    phrase query asks about *substrings* and the gate asks about *gram
    overlap*, so the phrase query is not a superset of the gate's
    acceptance set, and a fallback that ran only on an empty result never
    showed the gate a row it would have accepted.

    **So the oracle follows the implementation, not the older policy.** The
    phrase rows are *scored* with the gate, and the broad set is the
    fallback when none of them clears the floor. An oracle that modelled
    the old policy measured a set the repository never builds — and the
    set it measured was the one the suite called "the candidates the gate
    was given".

    The rows are the corpus's own: the score is taken over the row as the
    corpus holds it, so the oracle does not depend on which rows the
    candidate query happened to return.
    """
    collapsed = " ".join(term.split())
    phrase = await _candidates(db, term, "phrase")
    if any(_gate_score(row, collapsed) >= _FLOOR for row in rows if row.id in phrase):
        return phrase
    return await _candidates(db, term, "broad")


def _acceptance_set(rows: Sequence[_Row], term: str) -> set[str]:
    """Every row the gate would accept for this term, computed by brute force.

    The gate's own function, over every row of the corpus and every alias of
    it — so the answer does not depend on which rows the candidate query
    happened to return. A pair the gate accepts shares at least one gram, so
    the broad query's predicate ("shares at least one of the term's grams")
    is the gate's own precondition, and the candidate set must contain every
    row this set names.
    """
    collapsed = " ".join(term.split())
    accepted: set[str] = set()
    for row in rows:
        score = max(
            [
                trigram_similarity(collapsed, row.canonical_name),
                *(trigram_similarity(collapsed, alias) for alias in row.aliases),
            ]
        )
        if score >= _FLOOR:
            accepted.add(row.id)
    return accepted


def _term_id(term: str) -> str:
    """A readable id for a term, for the test names.

    A term carrying a NUL or a quote is unreadable in a test name, and an
    unreadable test name is a test nobody can find again.
    """
    return repr(term)


# ---------------------------------------------------------------------------
# 1. The same answer
# ---------------------------------------------------------------------------


class TestTheSameAnswer:
    """The same corpus and the same term give the same answer on both substrates.

    All three parts of the answer are compared — the id, the match kind and
    the score — and all three are reported when they differ, so one failure
    says everything that went wrong rather than only the first thing.
    """

    @pytest.mark.parametrize("term", _TERMS, ids=_term_id)
    async def test_the_same_term_gives_the_same_answer_on_both_substrates(
        self, substrates: _Substrates, term: str
    ) -> None:
        """The answer is the same on both substrates, in all three parts.

        Where the answer is ``None`` on both sides — the term shorter than
        a trigram — the two still agree, and the corpus is checked
        separately to be there, so "both said None" is never the whole
        answer.
        """
        sqlite_answer = _triple(await _resolve(substrates.sqlite, term))
        postgres_answer = _triple(await _resolve(substrates.postgres, term))

        differences: list[str] = []
        if sqlite_answer is None and postgres_answer is None:
            differences = []
        elif sqlite_answer is None or postgres_answer is None:
            differences.append(
                f"one substrate resolved the term and the other did not: "
                f"sqlite={sqlite_answer} postgres={postgres_answer}"
            )
        else:
            sqlite_id, sqlite_match, sqlite_score = sqlite_answer
            postgres_id, postgres_match, postgres_score = postgres_answer
            if sqlite_id != postgres_id:
                differences.append(f"id: sqlite={sqlite_id} postgres={postgres_id}")
            if sqlite_match != postgres_match:
                differences.append(f"match: sqlite={sqlite_match} postgres={postgres_match}")
            if sqlite_score != postgres_score:
                differences.append(f"score: sqlite={sqlite_score} postgres={postgres_score}")

        assert not differences, (
            f"the two substrates disagree about {term!r}: " + "; ".join(differences)
        )

    async def test_the_corpus_really_is_loaded_into_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The positive control for everything above.

        A parity suite in which both substrates are empty agrees about
        everything, so the corpus is counted on both sides before anything
        else is asserted.
        """
        for db in (substrates.sqlite_db, substrates.postgres_db):
            count = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_entity_index")
            assert count == len(_CORPUS), f"{db.dialect.name} holds {count} rows"

    async def test_the_ladder_reaches_every_rung_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The corpus exercises the exact rung, the alias rung and the fuzzy one.

        A parity suite over a corpus that only ever hits one rung proves
        very little, so the three rungs are each reached and each is
        reported: ``exact`` for a name, ``alias`` for an alias, ``fuzzy``
        for a near-miss.
        """
        kinds: dict[str, set[str]] = {"sqlite": set(), "postgres": set()}
        for term in ("Widget Service", "Gadget Assembly", "A. Lovelce"):
            sqlite_answer = await _resolve(substrates.sqlite, term)
            postgres_answer = await _resolve(substrates.postgres, term)
            assert sqlite_answer is not None and postgres_answer is not None
            kinds["sqlite"].add(sqlite_answer.match)
            kinds["postgres"].add(postgres_answer.match)

        assert kinds["sqlite"] == {"exact", "alias", "fuzzy"}
        assert kinds["postgres"] == {"exact", "alias", "fuzzy"}


# ---------------------------------------------------------------------------
# 2. The candidate set
# ---------------------------------------------------------------------------


class TestTheCandidateSet:
    """The candidate set is a superset of the gate's acceptance set.

    The gate decides; the database only decides which rows the gate gets to
    look at. That division is only sound if the gate is *shown* every row it
    would accept — a candidate query that narrows below the gate's
    acceptance set answers "no match" for a term the gate accepts, and the
    two substrates then answer differently.
    """

    @pytest.mark.parametrize("term", _QUERY_TERMS, ids=_term_id)
    async def test_the_broad_candidate_set_holds_every_accepted_entity(
        self, substrates: _Substrates, term: str
    ) -> None:
        """On both substrates, the broad query returns every accepted row.

        The acceptance set is computed by brute force over every row and
        every alias with the gate's own function, so it does not depend on
        which rows the query returned.
        """
        accepted = _acceptance_set(substrates.rows, term)
        for db in (substrates.sqlite_db, substrates.postgres_db):
            candidates = await _candidates(db, term, "broad")
            missing = accepted - candidates
            assert not missing, (
                f"{db.dialect.name} did not offer the gate {sorted(missing)} for {term!r}; "
                f"candidates={sorted(candidates)} accepted={sorted(accepted)}"
            )

    @pytest.mark.parametrize("term", _QUERY_TERMS, ids=_term_id)
    async def test_the_two_substrates_return_the_same_broad_candidate_set(
        self, substrates: _Substrates, term: str
    ) -> None:
        """The same question, the same rows, on both substrates.

        The broad query is the gate's own precondition expressed in SQL, and
        the count proxy it orders by is the same expression on both engines,
        so the two candidate sets are the same set.
        """
        sqlite_candidates = await _candidates(substrates.sqlite_db, term, "broad")
        postgres_candidates = await _candidates(substrates.postgres_db, term, "broad")
        assert sqlite_candidates == postgres_candidates, (
            f"the two substrates drew different candidate sets for {term!r}: "
            f"sqlite={sorted(sqlite_candidates)} postgres={sorted(postgres_candidates)}"
        )

    @pytest.mark.parametrize("term", _QUERY_TERMS, ids=_term_id)
    async def test_the_candidates_the_gate_was_given_hold_every_accepted_entity(
        self, substrates: _Substrates, term: str
    ) -> None:
        """The *effective* candidate set — the one the fuzzy rung is given.

        The repository runs the phrase query first and falls back to the
        broad one when the phrase rows do not produce a candidate that
        clears the floor, so the set the gate is given is one of the two.
        For this corpus that set holds every entity the gate accepts, on
        both substrates.

        This is a property of *this corpus and these terms*, not a guarantee
        of the design: the phrase query asks "is the term a substring of the
        row's text", which is a stronger question than the gate's, so a row
        the gate accepts can fail it. The measurement is in
        :class:`TestTheRepairedDivergences`.
        """
        accepted = _acceptance_set(substrates.rows, term)
        for db in (substrates.sqlite_db, substrates.postgres_db):
            candidates = await _effective_candidates(db, term, substrates.rows)
            missing = accepted - candidates
            assert not missing, (
                f"{db.dialect.name} did not show the gate {sorted(missing)} for {term!r}; "
                f"candidates={sorted(candidates)} accepted={sorted(accepted)}"
            )

    async def test_the_effective_candidate_set_is_the_broad_one_when_the_phrase_misses(
        self, phrase_substrates: _Substrates
    ) -> None:
        """The oracle follows the implementation's fallback policy.

        The corpus holds a name the term spells out that the gate scores
        **below** the floor, and a name only the broad query can see that
        the gate scores **above** it. So the effective candidate set is the
        **broad** one, and the row the gate accepts is in it.

        An oracle that modelled the older policy — the broad set only when
        the phrase query found *nothing* — would answer with the phrase row
        alone, and the assertion would fail: the row the gate would have
        accepted would not be in the set the gate was given. That is the
        whole reason the oracle is checked against the implementation
        rather than against an older reading of it.
        """
        accepted = _acceptance_set(phrase_substrates.rows, "Widget Service")
        assert accepted == {"00000000-0000-4000-8000-000000000014"}, (
            f"the corpus is not the one this test is about: {sorted(accepted)}"
        )
        for db in (phrase_substrates.sqlite_db, phrase_substrates.postgres_db):
            candidates = await _effective_candidates(
                db, "Widget Service", phrase_substrates.rows
            )
            missing = accepted - candidates
            assert not missing, (
                f"{db.dialect.name} did not show the gate {sorted(missing)}: "
                f"candidates={sorted(candidates)} accepted={sorted(accepted)}"
            )

    async def test_the_cut_is_made_on_the_same_order_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """A cut on the portable order keeps the two substrates together.

        The candidate query is ordered by the count proxy — the gate's own
        numerator — and then by the unique id, so the rows that survive the
        cut are the same rows on both substrates. A cut on an engine's own
        relevance (``bm25``, ``similarity``) would not be: those are not
        comparable, and a cut that differs is a cut that can change the
        answer.

        The limit here is **one**, and the candidate set is larger than one,
        so the cut really cuts — and the answer it produces is not the
        answer the uncut query produces.
        """
        limit = 1
        for term in ("silva", "a b d"):
            answers = []
            for repo in (substrates.sqlite, substrates.postgres):
                answer = await repo.resolve_entity(
                    term, fuzzy=True, fuzzy_floor=_FLOOR, candidate_limit=limit
                )
                answers.append(_triple(answer))
            assert answers[0] == answers[1], (
                f"the cut chose differently on the two substrates for {term!r}: {answers}"
            )

        # And the cut really cut: the uncut query answers with the other
        # one of the tied pair, because the tie-break takes the *last*
        # maximum of the rows it was given and the cut left it one row.
        uncut = await _resolve(substrates.sqlite, _TIE_TERM)
        cut = await substrates.sqlite.resolve_entity(
            _TIE_TERM, fuzzy=True, fuzzy_floor=_FLOOR, candidate_limit=limit
        )
        assert uncut is not None and cut is not None
        assert uncut.id == _TIE_WINNER
        assert cut.id == _TIE_LOSER


# ---------------------------------------------------------------------------
# 3. The tie-break
# ---------------------------------------------------------------------------


class TestTheTieBreak:
    """Two entities with identical scores produce the same winner, everywhere.

    The tie is constructed, not found: "Silva Jr" and "Silva Sr" score exactly
    0.5 against the term "silva", and both hold every one of that term's
    grams, so their count proxies are equal and the winner is decided by the
    id alone. A tie that was *accidental* would be a tie whose cause nobody
    knows, and a fix to the cause would not be a fix to the test.
    """

    async def test_the_tied_pair_really_does_tie(
        self, substrates: _Substrates
    ) -> None:
        """The positive control: the two scores are equal, and above the floor.

        Without this, "the winner is the greater id" could be true because
        the other row was not accepted at all.
        """
        scores = {
            row.id: max(
                [
                    trigram_similarity(_TIE_TERM, row.canonical_name),
                    *(trigram_similarity(_TIE_TERM, alias) for alias in row.aliases),
                ]
            )
            for row in substrates.rows
            if row.id in {_TIE_WINNER, _TIE_LOSER}
        }
        assert scores[_TIE_WINNER] == scores[_TIE_LOSER]
        assert scores[_TIE_WINNER] >= _FLOOR

    async def test_the_same_winner_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The winner is the same on both substrates — and it is the greater id.

        The two substrates hold the tied rows in *opposite* orders, so an
        answer that followed the physical order would differ between them.
        The rule the implementation states is that the candidates arrive in
        the order ``(count proxy DESC, e.id ASC)`` and the gate's ``>=``
        takes the last maximum — so among two rows with the same score and
        the same count, the greater id wins.
        """
        sqlite_answer = await _resolve(substrates.sqlite, _TIE_TERM)
        postgres_answer = await _resolve(substrates.postgres, _TIE_TERM)

        assert sqlite_answer is not None and postgres_answer is not None
        assert sqlite_answer.id == postgres_answer.id
        assert sqlite_answer.id == _TIE_WINNER
        assert sqlite_answer.match == "fuzzy"
        assert sqlite_answer.score == pytest.approx(0.5)

    async def test_the_winner_does_not_depend_on_the_insertion_order(
        self, substrates: _Substrates
    ) -> None:
        """The same rows, written the other way round, give the same winner.

        The corpus is deleted and written again in the opposite order — so
        the rows are in a different physical order, and the FTS5 index is
        rebuilt from them by the schema's triggers. The answer must not
        move.
        """
        before = await _resolve(substrates.sqlite, _TIE_TERM)
        assert before is not None

        await substrates.sqlite_db.execute("DELETE FROM hot_serving_entity_index")
        await _seed(substrates.sqlite_db, tuple(reversed(substrates.rows)))

        after = await _resolve(substrates.sqlite, _TIE_TERM)
        assert after is not None
        assert after.id == before.id == _TIE_WINNER
        assert after.score == before.score


# ---------------------------------------------------------------------------
# 4. The hostile terms
# ---------------------------------------------------------------------------


class TestTheHostileTerms:
    """A term the engines would answer differently is refused the same way.

    The two engines refuse a NUL in *different places*: SQLite truncates a
    string at a NUL when it reads it through the C string API, so a NUL
    inside a ``LIKE`` pattern degrades it to a bare ``%`` that matches every
    row, while psycopg refuses the bound parameter outright with a
    ``DataError`` that carries no SQLSTATE. The refusal in
    ``resolve_entity``, before any statement is issued, is what makes the
    two substrates behave alike — and this is what proves the refusal is
    *there*, rather than in the driver.
    """

    async def test_a_nul_term_resolves_to_no_match_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The refusal is the repository's, so it is the same on both."""
        for term in _NUL_TERMS:
            assert await _resolve(substrates.sqlite, term) is None, term
            assert await _resolve(substrates.postgres, term) is None, term

    async def test_a_nul_term_never_reaches_either_substrate(
        self,
        substrates: _Substrates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No statement is issued for a term carrying a NUL.

        The outcome alone cannot prove where the refusal happened: the
        embedded substrate would degrade to "no match" whether the
        repository refused the term or the engine rejected the statement. So
        the substrates are watched.

        The positive control is what keeps the test honest — it shows the
        spy records the statements that *are* issued, so an empty list
        afterwards means "refused before asking" and not "the spy is broken".
        """
        issued: dict[str, list[str]] = {"sqlite": [], "postgres": []}

        def _watch(name: str, db: Database | PostgresDatabase) -> None:
            """Record every statement one substrate is asked to run."""
            real_fetch_one = db.fetch_one
            real_fetch_all = db.fetch_all

            async def _fetch_one(sql: str, params: Any = ()) -> Any:
                issued[name].append(sql)
                return await real_fetch_one(sql, params)

            async def _fetch_all(sql: str, params: Any = ()) -> Any:
                issued[name].append(sql)
                return await real_fetch_all(sql, params)

            monkeypatch.setattr(db, "fetch_one", _fetch_one)
            monkeypatch.setattr(db, "fetch_all", _fetch_all)

        _watch("sqlite", substrates.sqlite_db)
        _watch("postgres", substrates.postgres_db)

        for term in _NUL_TERMS:
            assert await _resolve(substrates.sqlite, term) is None
            assert await _resolve(substrates.postgres, term) is None

        assert issued["sqlite"] == [], f"the refused term reached SQLite: {issued['sqlite']}"
        assert issued["postgres"] == [], (
            f"the refused term reached PostgreSQL: {issued['postgres']}"
        )

        # The positive control: the spy is live.
        assert await _resolve(substrates.sqlite, "Widget Service") is not None
        assert await _resolve(substrates.postgres, "Widget Service") is not None
        assert issued["sqlite"] and issued["postgres"]

    async def test_an_operator_term_resolves_to_no_match_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """FTS5's operators are characters, not query syntax.

        The terms below are a *query* if they reach the matcher unquoted: an
        unquoted ``AND`` is an operator, a stray ``*`` is a prefix operator,
        and a double quote closes the phrase and hands the rest of the term
        to the parser. The seam quotes the term, so on both substrates they
        are ordinary characters — and neither substrate answers with a row
        or raises.
        """
        for term in _OPERATOR_TERMS:
            sqlite_answer = await _resolve(substrates.sqlite, term)
            postgres_answer = await _resolve(substrates.postgres, term)
            assert sqlite_answer is None, f"sqlite matched {term!r} with {sqlite_answer}"
            assert postgres_answer is None, f"postgres matched {term!r} with {postgres_answer}"

    async def test_an_operator_term_is_inert_in_the_candidate_query(
        self, substrates: _Substrates
    ) -> None:
        """The candidate query itself runs, and raises nothing.

        The repository catches a rejected query and fails closed, so a term
        that reached the matcher unquoted would still answer "no match" —
        the refusal would be indistinguishable from the answer. The
        difference is visible only here: the *statement* runs.

        The terms that reach a query are the ones with a gram in them. The
        shorter ones never get that far: a term with no grams is refused by
        the ladder, and the seam refuses it in turn.
        """
        for term in _OPERATOR_TERMS:
            if not _trigrams(term):
                continue
            for db in (substrates.sqlite_db, substrates.postgres_db):
                for mode in ("phrase", "broad"):
                    candidates = await _candidates(db, term, mode)
                    assert isinstance(candidates, set)

    async def test_an_operator_term_with_no_grams_is_refused_before_a_query(
        self, substrates: _Substrates
    ) -> None:
        """The one-character operators are refused by the ladder, on both.

        ``"``, ``*``, ``(`` and ``)`` are one character, and ``OR`` is two:
        a term shorter than a trigram has no grams, so there is no query to
        make and the seam refuses to build one. The refusal is the same on
        both substrates, and it is the same refusal the ladder makes.
        """
        for term in ('"', "*", "OR", "(", ")"):
            assert await _resolve(substrates.sqlite, term) is None, term
            assert await _resolve(substrates.postgres, term) is None, term
            for db in (substrates.sqlite_db, substrates.postgres_db):
                with pytest.raises(ValueError, match="at least one gram"):
                    await _candidates(db, term, "broad")

    async def test_the_operators_are_not_the_only_terms_that_resolve_to_nothing(
        self, substrates: _Substrates
    ) -> None:
        """The positive control: the corpus still resolves.

        A test that asserts "no match" for everything passes on an empty
        corpus, so the corpus is asked a question it can answer.
        """
        answer = await _resolve(substrates.sqlite, "Widget Service")
        assert answer is not None and answer.canonical_name == "Widget Service"


# ---------------------------------------------------------------------------
# 5. The short-word case
# ---------------------------------------------------------------------------


class TestTheShortWord:
    """A term whose words are shorter than three characters behaves the same.

    The gate compares two texts shorter than a trigram as *text* rather
    than as gram sets, so ``"a b"`` against ``"a b"`` scores **1.0** — the
    pair is accepted outright, and the candidate query has to be able to
    return the row that holds it.

    ``pg_trgm`` is **not** the reason this case is pinned: it pads *every*
    word, so ``show_trgm('a')`` is ``{'  a', ' a '}`` and
    ``similarity('a', 'a')`` is **1.0** there too. The case is pinned
    because the candidate query is the gate's *own precondition* — "shares
    at least one of the term's grams" — and it has to be the same question
    on both substrates, whatever any engine's gram model happens to be.
    """

    async def test_the_gate_accepts_the_short_word_term_outright(
        self, substrates: _Substrates
    ) -> None:
        """The positive control: the gate scores "a b" against "a b" at 1.0.

        This is the half of the property that does not involve a database:
        the gate accepts the pair, so the candidate query has to be able to
        return it.
        """
        assert trigram_similarity("a b", "a b") == 1.0
        assert trigram_similarity("a b", "a b c") == pytest.approx(1 / 3)

    async def test_the_short_word_row_is_a_candidate_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The candidate query returns the row the gate accepts outright.

        The row named "a b" and the term "a b": the gate scores the pair
        1.0, so the pair is accepted — and the candidate query has to be
        able to see the row. It sees it because the query *is* the gate's
        own precondition ("shares at least one of the term's grams"),
        expressed the same way on both substrates; it does not depend on
        any engine's gram model.

        Both modes, on both substrates: the phrase query because the row
        contains the term, and the broad query because the row shares the
        term's one gram.
        """
        for db in (substrates.sqlite_db, substrates.postgres_db):
            for mode in ("phrase", "broad"):
                candidates = await _candidates(db, "a b", mode)
                assert "00000000-0000-4000-8000-000000000009" in candidates, (
                    f"{db.dialect.name}/{mode} did not return the short-word row "
                    f"for 'a b': {sorted(candidates)}"
                )

    async def test_the_broad_query_sees_both_short_word_rows(
        self, substrates: _Substrates
    ) -> None:
        """The broad query sees the short-word rows, on both substrates.

        "a b c" and "a b d" share the gram "a b" with the two short-word
        rows and nothing else, so the broad query is the only one that can
        return them — and it returns the same rows on both substrates.
        """
        for term in ("a b c", "a b d"):
            for db in (substrates.sqlite_db, substrates.postgres_db):
                candidates = await _candidates(db, term, "broad")
                assert "00000000-0000-4000-8000-000000000009" in candidates, (
                    f"{db.dialect.name} did not return the short-word row for {term!r}: "
                    f"{sorted(candidates)}"
                )
                assert "00000000-0000-4000-8000-00000000000a" in candidates, (
                    f"{db.dialect.name} did not return the 'a b c' row for {term!r}: "
                    f"{sorted(candidates)}"
                )

    async def test_a_short_word_term_resolves_the_same_on_both_substrates(
        self, substrates: _Substrates
    ) -> None:
        """The answer is the same, for a term the fuzzy rung has to answer.

        "a b d" matches no name and no alias exactly, so it reaches the
        fuzzy rung — and the gate accepts "a b c" for it at 0.5, on the one
        shared gram and the two grams around it.
        """
        sqlite_answer = _triple(await _resolve(substrates.sqlite, "a b d"))
        postgres_answer = _triple(await _resolve(substrates.postgres, "a b d"))
        assert sqlite_answer == postgres_answer
        assert sqlite_answer is not None
        assert sqlite_answer[1] == "fuzzy"
        assert sqlite_answer[2] == pytest.approx(0.5)

    async def test_a_term_shorter_than_a_trigram_resolves_to_nothing(
        self, substrates: _Substrates
    ) -> None:
        """The ladder refuses it, on both substrates.

        A term shorter than three characters has no grams at all, so the
        gate cannot decide it by set overlap, and the ladder refuses to ask.
        """
        for repo in (substrates.sqlite, substrates.postgres):
            assert await _resolve(repo, "ab") is None


# ---------------------------------------------------------------------------
# The divergences the suite found
# ---------------------------------------------------------------------------


class TestTheRepairedDivergences:
    """The three divergences this suite found, and what they were repaired to.

    **These were defects, not contracts.** Each test below was written
    first, recording the behaviour the two substrates had *today*, and each
    one failed when the defect was fixed — a measurement that turns green
    only when the thing it measures is broken. They are here in their
    repaired form, and the repair is named in each docstring, because a
    test that only fails when the bug is present is a test that has to be
    deleted the moment the bug is fixed, and deleting it is how the bug
    comes back.
    """

    async def test_the_exact_rung_answers_with_the_same_row_on_both_substrates(
        self, case_variant_substrates: _Substrates
    ) -> None:
        """The exact rung has a total order, so the answer does not follow the rows.

        The schema's ``UNIQUE`` on ``canonical_name`` is case-*sensitive*, so
        the table can hold ``café`` and ``Café`` as two rows. The exact rung
        compares with ``lower()``, and both engines fold the ASCII ``C``, so
        **both engines see one key**.

        Before the repair the rung had no ``ORDER BY`` and no ``LIMIT``: each
        engine answered with whichever of the two rows its scan reached
        first, and the two substrates held the rows in opposite orders — so
        they answered with different rows for the same term, and the answer
        was a function of the physical order.

        **The repair** is in the exact rung: it carries the unique id as its
        ordering key, the way ``docs/adr/0025`` requires of every ``LIMIT``
        cut. The id is a ``TEXT`` primary key on SQLite and a ``UUID`` on
        PostgreSQL, and the canonical hyphenated form's lexicographic order
        is the UUID's byte order, so ``ORDER BY id ASC`` is the same order on
        both engines.
        """
        sqlite_answer = await _resolve(case_variant_substrates.sqlite, "café")
        postgres_answer = await _resolve(case_variant_substrates.postgres, "café")

        assert sqlite_answer is not None and postgres_answer is not None
        # Both engines now answer with the same row — the one the total
        # order puts first, not the one their scan reached first.
        assert sqlite_answer.id == postgres_answer.id
        assert sqlite_answer.id == "00000000-0000-4000-8000-000000000011"

    async def test_the_two_phrase_queries_ask_the_same_question(
        self, substrates: _Substrates
    ) -> None:
        """The phrase predicate folds the term the same way on both substrates.

        SQLite's matcher folds the case of the term — the FTS5 trigram
        tokenizer is case-insensitive by default — and matches "Widget
        Service" against a row stored as "Widget Service". PostgreSQL's
        predicate compares a **lowercased column** with a bound pattern, and
        ``LIKE`` is case-sensitive, so before the repair a term carrying an
        upper-case letter matched no phrase row at all: the two candidate
        sets were different, and the repository drew its candidates from
        different queries on the two substrates.

        **The repair** is in the seam: the fold happens in Python, once, in
        ``dialect.fold_case``, and both engines' phrase predicates are built
        from the folded text. A fold left to the engine is a fold the two
        engines do not agree on.
        """
        sqlite_candidates = await _candidates(substrates.sqlite_db, "Widget Service", "phrase")
        postgres_candidates = await _candidates(
            substrates.postgres_db, "Widget Service", "phrase"
        )
        assert sqlite_candidates == {"00000000-0000-4000-8000-000000000001"}
        assert postgres_candidates == sqlite_candidates

    async def test_the_two_substrates_answer_the_same_for_one_term(
        self, phrase_substrates: _Substrates
    ) -> None:
        """One term, one corpus, one answer.

        The corpus holds a long name that *contains* "Widget Service" and a
        near-miss that shares eleven of the term's twelve grams without
        containing it.

        Before the repair the two substrates answered differently here, for
        two reasons at once: PostgreSQL's phrase query found nothing (the
        term was not folded), so it fell through to the broad query and
        answered "Widget Servic"; while SQLite's phrase query found the long
        name, the broad query — the only one that can see the near-miss —
        never ran, the long name was scored at 0.2727, below the floor, and
        the answer was **no match**.

        **The repairs** are the fold above, and the fallback: the broad query
        now runs when the phrase rows do not produce a candidate that clears
        the floor, not merely when the phrase query returned nothing. The
        phrase query is a *precision* pass, and a precision pass that hides
        a row the gate would have accepted is not a precision pass.
        """
        sqlite_answer = await _resolve(phrase_substrates.sqlite, "Widget Service")
        postgres_answer = await _resolve(phrase_substrates.postgres, "Widget Service")

        assert sqlite_answer is not None and postgres_answer is not None
        assert sqlite_answer.id == postgres_answer.id
        assert sqlite_answer.canonical_name == "Widget Servic"
        assert postgres_answer.canonical_name == "Widget Servic"
        assert sqlite_answer.match == "fuzzy"
        assert postgres_answer.match == "fuzzy"
        assert sqlite_answer.score == pytest.approx(0.9167, abs=1e-4)
        assert postgres_answer.score == pytest.approx(0.9167, abs=1e-4)


# ---------------------------------------------------------------------------
# The divergence this suite records rather than repairs
# ---------------------------------------------------------------------------


class TestTheRecordedDivergence:
    """The case fold of the *column* is the engine's, and it is not the same.

    **This test passes while the divergence exists.** It is a
    *measurement*, not a contract: it records what the two substrates do
    with a name carrying ``U+0130`` ``İ`` — the one code point the three
    engines fold three ways — so that the limitation is a number somebody
    measured rather than an assumption somebody made. The class is here
    precisely because the divergence is **not** repaired: the repair
    needs a stored, Python-computed normalisation (ADR-0029's limitation
    (a)), which is a schema change in both DDL files plus a backfill.

    The direction of the divergence matters, and it is **one-directional**:
    PostgreSQL's locale-aware ``lower()`` folds ``İ`` to a single ``i``,
    while SQLite's ASCII-only ``lower()`` and FTS5's simple fold leave it
    alone. So **PostgreSQL sees rows the other substrate cannot see**, and
    the two answers are not the same answer — sometimes one substrate
    resolves and the other does not, and sometimes both resolve the
    *same* entity with a different match kind and a different score.

    A test that records a divergence is a test that has to be *moved*
    when the divergence is repaired, not deleted: it becomes a member of
    :class:`TestTheRepairedDivergences`, where a failure means the
    repair is done.
    """

    async def test_the_two_substrates_answer_the_same_entity_differently(
        self, fold_substrates: _Substrates
    ) -> None:
        """The same entity, two answers: a different match kind and score.

        The term ``"istanbul"`` against the stored name ``"İstanbul"``.
        PostgreSQL's ``lower()`` folds the stored name to ``"istanbul"``,
        so its *exact* rung answers — match kind ``exact``, score 1.0.
        SQLite's ``lower()`` is ASCII-only and leaves ``İ`` alone, so the
        exact rung does not answer, the fuzzy rung does, and it scores
        0.625 (five of the term's six grams are shared, over the union of
        the two gram sets).

        The two substrates therefore disagree about *how* they found the
        same entity — and the match kind is part of the cross-substrate
        contract, so this is a divergence and not a rounding difference.
        """
        sqlite_answer = _triple(await _resolve(fold_substrates.sqlite, "istanbul"))
        postgres_answer = _triple(await _resolve(fold_substrates.postgres, "istanbul"))

        assert sqlite_answer == ("00000000-0000-4000-8000-000000000021", "fuzzy", 0.625)
        assert postgres_answer == ("00000000-0000-4000-8000-000000000021", "exact", 1.0)

    async def test_the_divergence_can_hide_a_row_from_one_substrate(
        self, fold_substrates: _Substrates
    ) -> None:
        """One substrate resolves the term; the other does not.

        The term ``"izmir"`` against the stored name ``"İZMİR"``.
        PostgreSQL's exact rung answers (its ``lower()`` folds the stored
        name to ``"izmir"``); SQLite's does not, and its fuzzy rung
        cannot either — the gate scores the pair at 1/7, below the floor.

        This is the direction of the divergence: the locale-aware fold
        sees a row the ASCII-only fold cannot, so the answers differ in
        *whether* they resolve, not only in how.
        """
        sqlite_answer = await _resolve(fold_substrates.sqlite, "izmir")
        postgres_answer = await _resolve(fold_substrates.postgres, "izmir")

        assert sqlite_answer is None, f"sqlite resolved {sqlite_answer!r}"
        assert postgres_answer is not None
        assert postgres_answer.id == "00000000-0000-4000-8000-000000000022"
        assert postgres_answer.match == "exact"

    async def test_the_broad_candidate_sets_diverge(
        self, fold_substrates: _Substrates
    ) -> None:
        """The candidate sets differ, so the gate is given different rows.

        The count proxy counts the term's grams in the *column*, and the
        column is folded by the engine. For the term ``"izmir"`` and the
        row ``"İZMİR"``: PostgreSQL's ``lower()`` gives ``"izmir"``, which
        holds the term's grams, so the row is a candidate; SQLite's gives
        ``"İZMİR"``, which holds none of them, so the row is not.

        The count proxy is the *numerator* the gate counts with, so the
        divergence is not a difference in ranking: it is a difference in
        which rows the gate is shown at all.
        """
        sqlite_candidates = await _candidates(fold_substrates.sqlite_db, "izmir", "broad")
        postgres_candidates = await _candidates(fold_substrates.postgres_db, "izmir", "broad")

        assert sqlite_candidates == set()
        assert postgres_candidates == {"00000000-0000-4000-8000-000000000022"}
