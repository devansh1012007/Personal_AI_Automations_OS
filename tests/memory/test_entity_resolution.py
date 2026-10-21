"""Entity resolution: the candidate set, the gate, and the total order.

Entity resolution is the one place in the repository layer where the
*database* and the *gate* are two halves of a single decision. The database
decides which rows the gate gets to look at; the gate decides which of those
rows the term refers to. The split only works if the database's candidate
set is a **superset** of the rows the gate would accept — narrow it below
that set and the gate never sees the row it would have accepted, so the
answer is "no match" for a term the gate would have resolved.

So the properties pinned here are the ones that make the split sound:

* the candidate set is a superset of the gate's acceptance set — the gate's
  own precondition, asked as a query;
* the broad query asks *that* question and nothing broader, so a term whose
  words include ``OR`` does not pull the table in with it;
* the broad query is the fallback when the phrase rows do not clear the
  floor — a *gate* decision, not a query decision;
* an alias is scored on its own, not as the JSON blob the column holds;
* the answer does not depend on the order the rows were written in, and
  each rung's *direction* is pinned — the fuzzy gate takes the greater id
  among equal scores, the exact, alias and write paths take the lesser;
* the FTS5 quoting is an injection barrier, and a hostile term is *answered*
  as text rather than refused;
* a NUL is refused before any statement is issued at all, and so is a term
  with more grams than the candidate query can carry;
* a non-ASCII alias is invisible to the count proxy — the alias column is
  JSON text, and ``json.dumps`` escapes non-ASCII — so the answer is "no
  match" on both substrates even though the gate would accept the pair.

Every test here runs on the embedded substrate: no server, no ``psycopg``,
no extra. The half that needs a live PostgreSQL to compare against is a
different suite, and it is marked as such.

**The expectations are computed here, not asked of the implementation.** The
gram model below is written out rather than imported, so that a change to
the gate's model shows up as a *disagreement* between the test and the code
instead of as two copies of the same mistake. The one thing the test does
borrow is the gate itself — :func:`paa.memory.facts.trigram_similarity` —
because "the set the gate accepts" is a statement about the gate, and the
property under test is that the candidate query is a superset of *it*.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple

import pytest

from paa.memory.facts import _MAX_TERM_GRAMS, FactRepository, trigram_similarity
from paa.storage.relational.database import Database, dumps, loads, to_iso, utc_now

#: The floor the fuzzy rung gates at. The same default the repository uses,
#: named here so a test that changes it has to say so.
_FLOOR = 0.34

#: A corpus with the shapes that break naive matching: a space, a hyphen, a
#: digit, mixed case, and aliases. The names are the ones the plan measured
#: against, so a regression shows up as the same number it did before.
#:
#: ``"Elli"`` is in there for a measured reason. Under the gate's *old*
#: padded model, ``"Eli"`` and ``"Elli"`` shared three grams and were
#: accepted at 0.5 — and every one of those three grams was a padding gram,
#: which no trigram index can hold, so the pair was invisible to the index
#: while the gate accepted it. It is the smallest instance of the class the
#: whole change is about, and the one the corpus needs in it.
_CORPUS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Widget Service", ("Gadget Assembly",)),
    ("ACME-42 Corp", ("ACME",)),
    ("alpha-beta", ("Alpha Beta",)),
    ("johnson alice", ("Alice Johnson",)),
    ("Deploy Pipeline", ("Deploy",)),
    ("Orchid Service", ()),
    ("Doctor Orchid", ()),
    ("Alice Johnson", ()),
    ("Abcx Service", ()),
    ("Abcy Service", ()),
    ("Elli", ()),
)

#: The terms the corpus is asked about. Two of them are the measured cases:
#: ``"Widgt Servce"`` is the typo whose candidate set used to be *empty* on
#: the embedded substrate while the server substrate returned two entities,
#: and ``"Eli"`` is the pair that shares only padding grams.
_TERMS: tuple[str, ...] = (
    "Widgt Servce",
    "Johnson OR Alice",
    "Service",
    "Abcz Service",
    "Widget",
    "Orchid",
    "ACME-42",
    "alpha beta",
    "Deploy",
    "Gadget Assembly",
    "Eli",
)

#: Terms whose words carry an ``OR`` — the operator FTS5 would read as one
#: if the term reached the matcher as a query rather than as text.
_LEAK_TERMS: tuple[str, ...] = (
    "Johnson OR Alice",
    "Doctor OR Widget",
)

#: The two entities that score *exactly* the same against the term below,
#: with ids fixed so that the insertion order is the only thing that varies
#: between the two databases the test builds.
_TIE_TERM = "Abcz Service"
_TIE_PAIR: tuple[tuple[str, str], ...] = (
    ("ent-aaa", "Abcx Service"),
    ("ent-bbb", "Abcy Service"),
)

#: The id the tie must go to.
#:
#: The candidates arrive in the order ``(count proxy DESC, e.id ASC)`` and
#: the gate's ``>=`` takes the **last** maximum — so among two rows with
#: the same score and the same count, the **greater** id wins. (The two
#: rows hold the same count: six of the term's nine grams each, the other
#: three being the ones the changed character breaks.)
_TIE_WINNER = "ent-bbb"
_TIE_LOSER = "ent-aaa"

#: The corpus for the fallback policy: a name that *contains* the term and
#: scores **below** the floor, and a name that does not contain the term and
#: scores **above** it.
#:
#: The pair is what makes the policy visible. The first row is a phrase
#: match — the term is a substring of it — so the phrase query answers, and
#: the gate scores it 0.2727, below the floor. The second row is a fuzzy
#: match only: the term is *not* a substring of it, so only the broad query
#: can see it, and the gate scores it 0.9167. A fallback that runs only
#: when the phrase query found nothing therefore answers "no match".
_FALLBACK_CORPUS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Widget Service Of The International Department", ()),
    ("Widget Servic", ()),
)
_FALLBACK_TERM = "Widget Service"

#: A term with more grams than the ceiling allows.
#:
#: Five hundred distinct CJK ideographs: every 3-gram of them is distinct
#: (the characters are, so the windows are), and no case folding changes
#: one — so the term's gram count is 498, over the 400-gram ceiling. The
#: count is asserted by the test that uses it, so the term cannot quietly
#: stop being over the ceiling.
_OVER_THE_CEILING_TERM = "".join(chr(0x4E00 + index) for index in range(500))

#: The hostile terms. Each carries a character FTS5 reads as syntax, and
#: each is *nearly* a corpus name — so a term that were parsed as a query
#: rather than as text would reach a row it has no business reaching, and a
#: term that were refused would answer "no match" for the wrong reason.
_HOSTILE_TERMS: tuple[str, ...] = (
    'Widget" OR "Xyz',
    "Widget*",
    "Widget AND Xyz",
    "Widget OR Xyz",
    "Widget NEAR Xyz",
    "(Widget)",
    "Widget^",
    "Widget-",
    "Widget:Xyz",
)


class _Row(NamedTuple):
    """One entity row, with the alias blob exactly as the index holds it.

    The blob is the *text* of the alias array — the JSON, quotes and all —
    because that is the text the candidate query's measure is taken over on
    both substrates. A test that measured over the expanded aliases would be
    measuring a different thing than the engine measures.
    """

    name: str
    blob: str
    aliases: tuple[str, ...]


@pytest.fixture
async def repo(db: Database) -> FactRepository:
    """A repository over an empty entity index."""
    return FactRepository(db)


@pytest.fixture
async def corpus(db: Database) -> FactRepository:
    """A repository over the corpus above."""
    repository = FactRepository(db)
    for name, aliases in _CORPUS:
        await repository.upsert_entity("project", name, aliases=list(aliases) or None)
    return repository


# ---------------------------------------------------------------------------
# The measurements the tests are assertions about
# ---------------------------------------------------------------------------


def _grams(text: str) -> set[str]:
    """The character trigrams of the raw, case-folded text.

    The model FTS5's ``trigram`` tokenizer indexes: the 3-grams of the text
    as written, case-folded, with no padding and no whitespace-run
    collapsing. Written out here rather than imported, so that the
    expectations in this file do not move when the implementation does — a
    test whose oracle is the code under test cannot fail.
    """
    folded = text.lower()
    if len(folded) < 3:
        return set()
    return {folded[i : i + 3] for i in range(len(folded) - 2)}


async def _rows(db: Database) -> list[_Row]:
    """Every entity row, with its alias blob as the index holds it."""
    raw = await db.fetch_all("SELECT canonical_name, aliases FROM hot_serving_entity_index")
    out: list[_Row] = []
    for row in raw:
        blob = str(row["aliases"] or "")
        aliases = tuple(str(alias) for alias in (loads(blob, []) or []))
        out.append(_Row(str(row["canonical_name"]), blob, aliases))
    return out


def _accepted(term: str, rows: Sequence[_Row], floor: float = _FLOOR) -> set[str]:
    """The gate's acceptance set, computed by brute force over every row.

    This is the decision the repository makes in Python: score the term
    against the row's canonical name and against each of its aliases, keep
    the best, and keep the row when the best score is at or above the floor.
    It is computed here over *every* row in the table, with no candidate
    query involved at all — which is the point: it is the set the candidate
    query has to be a superset of.
    """
    accepted: set[str] = set()
    for row in rows:
        score = max(
            [trigram_similarity(term, row.name)]
            + [trigram_similarity(term, alias) for alias in row.aliases]
        )
        if score >= floor:
            accepted.add(row.name)
    return accepted


def _gram_sharing(term: str, rows: Sequence[_Row]) -> set[str]:
    """The rows whose text shares at least one of the term's grams.

    The broad candidate query asks exactly this question, so this is the set
    the query must return: no more (a term's ``OR`` pulling the table in)
    and no less (a row the gate would accept being invisible to the query).
    """
    grams = _grams(term)
    return {row.name for row in rows if grams & (_grams(row.name) | _grams(row.blob))}


async def _candidate_rows(
    db: Database,
    mode: Literal["phrase", "broad"],
    term: str,
    limit: int = 100,
) -> list[tuple[str, tuple[str, ...]]]:
    """The rows one of the two candidate queries returns, with their aliases.

    The aliases come along because the gate scores them *individually*, and
    a helper that could not see them could not reproduce the gate's
    decision — which is what the fallback policy is a decision about.
    """
    grams = tuple(sorted(_grams(term)))
    sql, params = db.dialect.entity_candidates_sql(
        mode=mode, term=term, grams=grams, limit=limit
    )
    rows = await db.fetch_all(sql, params)
    return [
        (
            str(row["canonical_name"]),
            tuple(str(alias) for alias in (loads(row["aliases"], []) or [])),
        )
        for row in rows
    ]


async def _candidates(
    db: Database,
    mode: Literal["phrase", "broad"],
    term: str,
    limit: int = 100,
) -> set[str]:
    """The rows one of the two candidate queries returns."""
    return {name for name, _ in await _candidate_rows(db, mode, term, limit)}


async def _candidate_ids(
    db: Database,
    mode: Literal["phrase", "broad"],
    term: str,
    limit: int = 100,
) -> list[str]:
    """The ids one candidate query returns, **in the order it returns them**.

    The order is the point: the query orders by ``(count proxy DESC,
    e.id ASC)``, so the order of the rows it returns says which key
    decided — the count, or the id.
    """
    grams = tuple(sorted(_grams(term)))
    sql, params = db.dialect.entity_candidates_sql(
        mode=mode, term=term, grams=grams, limit=limit
    )
    rows = await db.fetch_all(sql, params)
    return [str(row["id"]) for row in rows]


def _clears_the_floor(
    term: str,
    candidates: Sequence[tuple[str, tuple[str, ...]]],
    floor: float = _FLOOR,
) -> bool:
    """Whether the gate would accept any of these candidates.

    The repository's own rule: the best of the row's canonical name's score
    and of its aliases' scores, against the floor — and the floor is
    inclusive, so a candidate scoring exactly the floor is accepted.
    """
    for name, aliases in candidates:
        score = max(
            [trigram_similarity(term, name)]
            + [trigram_similarity(term, alias) for alias in aliases]
        )
        if score >= floor:
            return True
    return False


async def _consulted(db: Database, term: str, limit: int = 100) -> set[str]:
    """The rows the resolution's own candidate selection consults.

    The policy is the repository's: the phrase query is a precision pass,
    and the broad query is the fallback that runs when the **phrase rows do
    not produce a candidate that clears the floor** — not merely when the
    phrase query found nothing.

    The two are different, and the difference was a defect: the phrase
    query asks about substrings and the gate asks about gram overlap, so
    the phrase query is not a superset of the gate's acceptance set. A
    fallback that ran only on an empty result therefore never showed the
    gate a row it would have accepted.
    """
    phrase = await _candidate_rows(db, "phrase", term, limit)
    if _clears_the_floor(term, phrase):
        return {name for name, _ in phrase}
    return {name for name, _ in await _candidate_rows(db, "broad", term, limit)}


async def _insert(db: Database, entity_id: str, name: str, aliases: Sequence[str] = ()) -> None:
    """Write one entity row with a *chosen* id.

    The ids are fixed because they are the variable the test needs to hold
    still: the same two entities, with the same ids, written in two
    different orders, are the same data — and the answer must be the same.
    """
    stamp = to_iso(utc_now())
    await db.execute(
        "INSERT INTO hot_serving_entity_index "
        "(id, class, canonical_name, aliases, importance_weight, attributes,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (entity_id, "project", name, dumps(list(aliases)), 0.5, "{}", stamp, stamp),
    )


async def _entity_ids(db: Database) -> list[str]:
    """The ids in the entity index, in id order."""
    rows = await db.fetch_all("SELECT id FROM hot_serving_entity_index ORDER BY id ASC")
    return [str(row["id"]) for row in rows]


def _spy(issued: list[str], failures: list[str], original: Any) -> Any:
    """Wrap a statement method so the test can see what was asked.

    ``issued`` collects the statements; a statement that *raised* is
    collected in ``failures`` too, because "the engine refused the
    question" and "the engine answered it" are different answers and the
    caller cannot tell them apart.
    """

    async def _recorder(sql: str, params: Any = ()) -> Any:
        issued.append(sql)
        try:
            return await original(sql, params)
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            raise

    return _recorder


# ---------------------------------------------------------------------------


class TestTheCandidateSet:
    """The candidate set must be a superset of the gate's acceptance set.

    The gate decides; the database only decides which rows the gate is
    allowed to look at. That division is sound only if the database's set
    contains every row the gate would have accepted — so the property is
    measured here by brute force over *every* row in the table, with no
    candidate query involved, and compared with what the query returned.
    """

    async def test_the_broad_candidate_set_is_a_superset_of_the_acceptance_set(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The gate's precondition, asked as a query.

        A pair the gate accepts shares at least one gram — a Jaccard of 0.34
        cannot be reached without one — so "shares at least one gram" is
        exactly the gate's precondition, and the broad query asks it.

        The measured cases this pins: the term ``"Widgt Servce"`` against
        the entity ``"Widget Service"``, whose candidate set used to be
        **empty** on the embedded substrate while the server substrate
        returned two entities; and the term ``"Eli"`` against the entity
        ``"Elli"``, whose shared grams were all padding grams, which no
        trigram index holds.
        """
        rows = await _rows(db)
        for term in _TERMS:
            accepted = _accepted(term, rows)
            broad = await _candidates(db, "broad", term)
            assert accepted <= broad, (
                f"the candidate query narrowed below the gate's acceptance set for "
                f"{term!r}: accepted={sorted(accepted)} candidates={sorted(broad)}"
            )

    async def test_the_candidates_the_resolution_consults_are_a_superset(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The contract as deployed, not as the predicate states it.

        The resolution consults the phrase query's rows when they produce a
        candidate that clears the floor, and the broad query's rows
        otherwise, so the set the gate actually sees is the one asserted
        here — and it has to contain every row the gate would have
        accepted, or the answer is "no match" for a term the gate would
        have resolved.

        **The corpus is what makes this true, and the reason is worth
        knowing.** The phrase query is not itself a superset of the gate's
        acceptance set: a pair the gate accepts need not be a substring of
        anything, and the phrase query only answers about substrings. Here
        the rows are either substring matches that clear the floor or fuzzy
        matches that are not substrings of anything, so the two sets
        coincide. A corpus holding *both* a weak substring match that
        clears the floor and a stronger fuzzy match would break this — the
        phrase query would answer, the broad query would not run, and the
        gate would never see the row it would have scored highest. That
        narrowing is a property of the phrase-first policy, not of the
        candidate query, and it is recorded in the W3 report rather than
        pinned here.
        """
        rows = await _rows(db)
        for term in _TERMS:
            accepted = _accepted(term, rows)
            consulted = await _consulted(db, term)
            assert accepted <= consulted, (
                f"the rows the resolution consults are not a superset of the gate's "
                f"acceptance set for {term!r}: accepted={sorted(accepted)} "
                f"consulted={sorted(consulted)}"
            )

    async def test_the_or_token_leak_is_closed(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """A term whose words carry ``OR`` must not pull the table in with it.

        ``MATCH`` is a query language, so a term that reaches the matcher as
        an expression rather than as text is read as one: ``OR`` is an
        operator, and an operator drags in every row on one side of it. The
        old code passed one parameter that meant an expression on one
        substrate and a literal on the other, so the word ``OR`` itself
        contributed grams to the comparison and matched a large fraction of
        the table.

        The assertion is an equality, not an inclusion: the broad query must
        return **exactly** the rows that share a gram with the term. Too
        many is the leak; too few is the same narrowing the superset test
        pins, seen from the other side.
        """
        rows = await _rows(db)
        for term in _LEAK_TERMS:
            expected = _gram_sharing(term, rows)
            broad = await _candidates(db, "broad", term)
            assert broad == expected, (
                f"the broad query answered a different question for {term!r}: "
                f"expected={sorted(expected)} got={sorted(broad)}"
            )
            # The row that shares no gram with either term: the leak would
            # put it in the set, because the word "OR" alone is enough to
            # pull a row in.
            assert "ACME-42 Corp" not in broad, (
                f"{term!r} pulled a row that shares no gram with it into the "
                f"candidate set: {sorted(broad)}"
            )


class TestTheFallbackPolicy:
    """The broad query is the fallback when the phrase rows miss the floor.

    The phrase query asks about *substrings* and the gate asks about *gram
    overlap*, so the phrase set is not a superset of the gate's acceptance
    set: a row the gate would have accepted can be invisible to it. The
    fallback therefore has to be a **gate** decision — the broad query runs
    when the phrase rows do not produce a candidate that clears the floor —
    and not a query decision (the broad query runs when the phrase query
    returned nothing).

    The two policies are indistinguishable on a corpus where the phrase
    rows either clear the floor or do not exist, which is why the policy
    needs a corpus of its own: a phrase row that *misses* the floor beside
    a fuzzy row that clears it.
    """

    async def test_the_broad_query_runs_when_the_phrase_rows_miss_the_floor(
        self, db: Database
    ) -> None:
        """The row only the broad query can see is found.

        The corpus holds a name the term spells out — a phrase match — that
        the gate scores **below** the floor, and a name it does not spell
        out that the gate scores **above** it. Under the old policy the
        phrase query answered, the gate scored its row, missed the floor,
        and the broad query — the only one that can see the second row —
        never ran: the answer was "no match" for a term the gate would
        have resolved.
        """
        repository = FactRepository(db)
        for name, aliases in _FALLBACK_CORPUS:
            await repository.upsert_entity("project", name, aliases=list(aliases) or None)

        # The positive controls, so the test is about the *policy* and not
        # about the phrase query failing to find a substring it should find.
        assert await _candidates(db, "phrase", _FALLBACK_TERM) == {
            "Widget Service Of The International Department"
        }, "the phrase query did not find the row the term spells out"
        assert (
            trigram_similarity(
                _FALLBACK_TERM, "Widget Service Of The International Department"
            )
            < _FLOOR
        ), "the phrase row is supposed to miss the floor"
        assert (
            trigram_similarity(_FALLBACK_TERM, "Widget Servic") >= _FLOOR
        ), "the broad row is supposed to clear the floor"
        assert "Widget Servic" not in await _candidates(db, "phrase", _FALLBACK_TERM), (
            "the broad row is supposed to be invisible to the phrase query"
        )

        resolved = await repository.resolve_entity(_FALLBACK_TERM)

        assert resolved is not None, (
            "the broad query never ran, so the row the gate would have "
            "accepted was never scored"
        )
        assert resolved.canonical_name == "Widget Servic"
        assert resolved.match == "fuzzy"
        assert resolved.score == pytest.approx(0.9167, abs=1e-4)

    async def test_the_consulted_set_follows_the_same_policy(
        self, db: Database
    ) -> None:
        """The helper the suite measures with follows the implementation.

        The helper is an oracle, and an oracle that models the *old*
        policy measures a set the repository never builds — so the two are
        kept in step, and this test says so: with the phrase rows missing
        the floor, the consulted set is the **broad** one.
        """
        repository = FactRepository(db)
        for name, aliases in _FALLBACK_CORPUS:
            await repository.upsert_entity("project", name, aliases=list(aliases) or None)

        consulted = await _consulted(db, _FALLBACK_TERM)

        assert consulted == {"Widget Service Of The International Department", "Widget Servic"}


class TestTheAlias:
    """An alias is scored on its own, not as the blob the column holds.

    The gate scores each alias individually, because that is what an alias
    *is* — a name the entity answers to. The engine used to score the raw
    JSON blob instead, which is a different text: the blob carries the
    quotes and the brackets, and a blob can score zero against a term one
    of its own aliases scores 1.0 against. A row the engine cannot see is
    a row the gate never gets to score.
    """

    async def test_an_exact_alias_is_found_and_wins(
        self, db: Database, repo: FactRepository
    ) -> None:
        """The alias rung, unchanged: an exact alias is a fact, not a guess.

        The positive control for the fuzzy rung below — the term resolves,
        and it resolves as an *alias* rather than as a trigram guess.
        """
        await repo.upsert_entity("project", "Gadget Assembly", aliases=["Service"])
        await repo.upsert_entity("project", "Servic Depot")

        resolved = await repo.resolve_entity("Service")

        assert resolved is not None
        assert resolved.canonical_name == "Gadget Assembly"
        assert resolved.match == "alias"

    async def test_an_alias_is_scored_individually_by_the_gate(
        self, db: Database, repo: FactRepository
    ) -> None:
        """The alias's own score, and the alias's own row, in the answer.

        The term is the plural of the alias, so the alias rung does not
        catch it and the fuzzy rung has to: the entity is found *through its
        alias*, and the score it reports is the alias's own score —
        ``5/6`` — and not the score of the JSON blob the alias column holds
        (``5/9``), and not the score of the canonical name (``0``).

        The competing row is a candidate (it shares the term's grams) and
        the gate accepts it, so the alias's row has to *win*: a candidate
        set that ranks the rows wrongly picks the other one.
        """
        await repo.upsert_entity("project", "Gadget Assembly", aliases=["Service"])
        await repo.upsert_entity("project", "Servic Depot")

        resolved = await repo.resolve_entity("Services")

        assert resolved is not None
        assert resolved.canonical_name == "Gadget Assembly"
        assert resolved.match == "fuzzy"
        # The individual alias's score, not the blob's and not the
        # canonical name's.
        assert resolved.score == pytest.approx(trigram_similarity("Services", "Service"))
        assert resolved.score == pytest.approx(5 / 6)
        # And the score the *blob* would have produced is a different
        # number, so the assertion above is not vacuous.
        assert resolved.score != pytest.approx(trigram_similarity("Services", '["Service"]'))
        assert resolved.score != pytest.approx(trigram_similarity("Services", "Gadget Assembly"))

    async def test_the_candidate_query_sees_the_alias(
        self, db: Database, repo: FactRepository
    ) -> None:
        """The engine's measure has to reach the alias as well.

        The candidate query's measure is taken over the canonical name and
        the alias text, so a row whose *alias* carries the term's grams is
        a candidate. If the alias column were not read, the row the gate
        would have scored highest would never reach the gate at all.
        """
        await repo.upsert_entity("project", "Gadget Assembly", aliases=["Service"])
        await repo.upsert_entity("project", "Servic Depot")

        rows = await _rows(db)
        broad = await _candidates(db, "broad", "Services")

        assert broad == _gram_sharing("Services", rows)
        assert "Gadget Assembly" in broad

    async def test_the_cut_keeps_the_row_the_gate_would_have_scored_highest(
        self, db: Database, repo: FactRepository
    ) -> None:
        """The measure that orders the candidates is the gate's own numerator.

        The candidate rows are cut to a limit, and the cut is made on a
        count of the term's grams in each row's text — the gate's own
        numerator, so the row that survives the cut is the row the gate
        would have scored highest. Here that row is the one whose *alias*
        carries the term, so a measure that ignored the alias column would
        cut the right row and keep the wrong one.
        """
        await repo.upsert_entity("project", "Gadget Assembly", aliases=["Service"])
        await repo.upsert_entity("project", "Servic Depot")

        resolved = await repo.resolve_entity("Services", candidate_limit=1)

        assert resolved is not None
        assert resolved.canonical_name == "Gadget Assembly"
        assert resolved.score == pytest.approx(5 / 6)


class TestTheTotalOrder:
    """The answer must not depend on the order the rows were written in.

    The ``LIMIT`` cut is made against a total order, and a total order is
    not a preference: it is a rule that gives the same answer for the same
    data whatever the engine happened to do with the rows. The old order
    was the engine's own relevance score, whose ties are broken by
    whatever the engine's plan produced — so the winner was a function of
    the heap layout, and the same seven rows written in a different order
    gave a different answer.

    **The rungs do not agree on the direction, and each one is pinned.**
    The fuzzy gate's ``>=`` takes the *last* maximum of the rows it was
    given, and the rows arrive ``(count proxy DESC, e.id ASC)`` — so
    among two rows with the same score and the same count the **greater**
    id wins. The exact and alias rungs ask for the *first* row of an
    ``id ASC`` order, so among two rows that answer them the **lesser**
    id wins. A rule that is only "the same answer every time" is not
    enough: a test that compares two runs of the same rule cannot tell a
    rule from its opposite, so each of the three rungs is pinned against
    the row it must pick.
    """

    async def test_the_answer_does_not_depend_on_the_insertion_order(
        self, tmp_path: Path
    ) -> None:
        """Two entities with identical scores, written in two orders.

        The scores are equal by construction — ``"Abcx Service"`` and
        ``"Abcy Service"`` differ in one character and score identically
        against ``"Abcz Service"`` — so nothing but the order can decide
        between them. The ids are fixed, so the two databases hold the same
        data and differ only in the order it was written in.

        **And the direction is pinned, not just the agreement.** Two runs
        of the same rule agree whatever the rule is, so the agreement
        alone cannot tell "the greater id wins" from "the lesser id
        wins" — and the gate's ``>=`` (the last maximum) and a ``>`` (the
        first) are exactly that difference. The rule is: the candidates
        arrive ``id ASC`` and the gate takes the last maximum, so the
        **greater** id wins.
        """
        assert trigram_similarity(_TIE_TERM, "Abcx Service") == trigram_similarity(
            _TIE_TERM, "Abcy Service"
        )
        assert trigram_similarity(_TIE_TERM, "Abcx Service") >= _FLOOR

        answers: list[tuple[str, str, float]] = []
        for index, order in enumerate((_TIE_PAIR, tuple(reversed(_TIE_PAIR)))):
            database = Database(tmp_path / f"order-{index}.db")
            await database.connect()
            try:
                for entity_id, name in order:
                    await _insert(database, entity_id, name)
                # The candidates arrive in id order, so the *count* did not
                # decide — it is equal for the two rows — and the id did.
                assert await _candidate_ids(database, "broad", _TIE_TERM) == [
                    _TIE_LOSER,
                    _TIE_WINNER,
                ], "the candidates did not arrive in id order, so the id did not decide"
                resolved = await FactRepository(database).resolve_entity(_TIE_TERM)
                assert resolved is not None, f"no match at all for {_TIE_TERM!r}"
                answers.append((resolved.canonical_name, resolved.id, resolved.score))
            finally:
                await database.close()

        assert answers[0] == answers[1], (
            "the same data, written in a different order, gave a different answer: "
            f"{answers[0]} vs {answers[1]}"
        )
        # The direction: among equal scores and equal counts, the greater
        # id wins — the gate's ``>=`` takes the last maximum of a list
        # ordered by id.
        assert answers[0][1] == _TIE_WINNER, (
            f"the tie-break took {answers[0][1]!r}; among equal scores the "
            f"greater id must win"
        )

    async def test_the_exact_rung_answers_with_the_same_row_in_any_order(
        self, tmp_path: Path
    ) -> None:
        """The exact rung has a total order too, and it is the *lesser* id.

        The schema's ``UNIQUE`` on ``canonical_name`` is case-*sensitive*,
        so the table can hold ``café`` and ``Café`` as two rows while the
        rung's ``lower()`` comparison sees them as *one* key. Without an
        order the answer was whichever row the engine's scan reached
        first — and the engine's scan is the physical order, so the two
        substrates answered differently over the same data.

        The rung asks for the **first** row of an ``id ASC`` order, so the
        answer is the **lesser** id. That is the opposite direction from
        the fuzzy gate's, and both are pinned.
        """
        pair = (("ent-aaa", "café"), ("ent-bbb", "Café"))
        answers: list[tuple[str, str]] = []
        for index, order in enumerate((pair, tuple(reversed(pair)))):
            database = Database(tmp_path / f"exact-{index}.db")
            await database.connect()
            try:
                for entity_id, name in order:
                    await _insert(database, entity_id, name)
                resolved = await FactRepository(database).resolve_entity("café")
                assert resolved is not None, "the exact rung did not answer at all"
                answers.append((resolved.canonical_name, resolved.id))
            finally:
                await database.close()

        assert answers[0] == answers[1], (
            "the same data, written in a different order, gave a different answer: "
            f"{answers[0]} vs {answers[1]}"
        )
        assert answers[0][1] == "ent-aaa", (
            f"the exact rung took {answers[0][1]!r}; it asks for the first row "
            f"of an id-ASC order, so the lesser id must win"
        )

    async def test_the_write_path_refreshes_the_row_the_read_path_answers_with(
        self, tmp_path: Path
    ) -> None:
        """The write path's lookup is ordered too, and the same way.

        The schema's ``UNIQUE`` on ``canonical_name`` is case-*sensitive*,
        so the table can hold ``café`` and ``Café`` as two rows while the
        lookup's ``lower()`` comparison sees them as *one* key. An
        unordered lookup would refresh whichever row the engine's scan
        reached first — and the two substrates would refresh *different*
        rows, while :meth:`resolve_entity`, which is ordered, reads one
        of them.

        The order is the unique ``id``, so the write path and the read
        path agree on which row the entity is: the **lesser** id, the same
        row the exact rung answers with.

        The rows are written in the *reverse* id order on purpose: an
        unordered lookup would find the row the engine's scan reaches
        first, and the scan reaches the first row written — so the
        reversed order is what makes the difference visible.
        """
        pair = (("ent-bbb", "Café"), ("ent-aaa", "café"))
        database = Database(tmp_path / "write-path.db")
        await database.connect()
        try:
            for entity_id, name in pair:
                await _insert(database, entity_id, name)

            refreshed = await FactRepository(database).upsert_entity(
                "project", "café", aliases=["Fresh Alias"]
            )

            assert refreshed == "ent-aaa", (
                f"the write path refreshed {refreshed!r}; it must refresh the "
                f"row the read path answers with"
            )
            rows = await _entity_ids(database)
            assert rows == ["ent-aaa", "ent-bbb"], (
                f"the write path created a third row: {rows}"
            )
        finally:
            await database.close()

    async def test_the_alias_rung_answers_with_the_same_row_in_any_order(
        self, tmp_path: Path
    ) -> None:
        """The alias rung has a total order too, and it is the *lesser* id.

        The alias column can hold the same alias under two canonical names
        that differ only in case, and a cut with no order is a cut on an
        order the SQL does not define. The rung asks for the **first** row
        of an ``id ASC`` order, so the answer is the **lesser** id.

        The rung deliberately carries no ``LIMIT`` — with one, the engine
        could satisfy the order by walking the id index and stopping at
        the first match, and the table-wide JSON expansion would then be
        evaluated for some rows and not others, which would make the
        corrupt-row degradation a function of the id order rather than a
        property of the rung.
        """
        pair = (("ent-aaa", "Widget Service"), ("ent-bbb", "Gadget Assembly"))
        answers: list[tuple[str, str]] = []
        for index, order in enumerate((pair, tuple(reversed(pair)))):
            database = Database(tmp_path / f"alias-{index}.db")
            await database.connect()
            try:
                for entity_id, name in order:
                    await _insert(database, entity_id, name, aliases=["Service"])
                resolved = await FactRepository(database).resolve_entity("Service")
                assert resolved is not None, "the alias rung did not answer at all"
                assert resolved.match == "alias", f"the alias rung did not answer: {resolved}"
                answers.append((resolved.canonical_name, resolved.id))
            finally:
                await database.close()

        assert answers[0] == answers[1], (
            "the same data, written in a different order, gave a different answer: "
            f"{answers[0]} vs {answers[1]}"
        )
        assert answers[0][1] == "ent-aaa", (
            f"the alias rung took {answers[0][1]!r}; it asks for the first row "
            f"of an id-ASC order, so the lesser id must win"
        )


class TestTheQuotingBarrier:
    """A term is text, and text is answered rather than refused.

    ``MATCH`` is a query language, so a term that reaches it unquoted is
    read as a query: an ``AND`` is an operator, a ``*`` is a prefix, a
    ``"`` closes a phrase. The quoting is the barrier, and it has to hold
    on both sides: the term must be answered as text (the engine's answer
    is the one the text deserves) and it must not raise.
    """

    async def test_a_hostile_term_is_answered_as_text(
        self, db: Database, corpus: FactRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every hostile term resolves to no match, raises nothing, and is answered.

        The terms are *nearly* corpus names, so a term read as a query
        rather than as text would reach a row it has no business reaching.

        "No match" alone would not prove much: a term the engine *refused*
        also answers "no match", and the refusal is invisible to the
        caller. So the statements are watched, and a statement that raised
        is a failure — the term has to be answered by the engine, not
        refused by it.
        """
        issued: list[str] = []
        failures: list[str] = []
        monkeypatch.setattr(db, "fetch_all", _spy(issued, failures, db.fetch_all))

        for term in _HOSTILE_TERMS:
            resolved = await corpus.resolve_entity(term)
            assert resolved is None, f"{term!r} resolved to {resolved!r}"

        assert failures == [], (
            f"the engine refused a term instead of answering it: {failures}"
        )
        assert issued, "the spy recorded nothing, so it proves nothing"

    async def test_a_hostile_term_does_not_reach_a_row_as_a_query(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The candidate sets are the ones the *text* deserves.

        The phrase query asks "is the term a substring of the row's text?",
        and no hostile term is a substring of any row — so the phrase set is
        empty. It would not be if the term were parsed as a query. The broad
        query asks "does the row share a gram with the term?", so its set is
        the gram-sharing rows — a refused statement would leave it empty,
        and the refusal would be indistinguishable from "no match" at the
        call site.
        """
        rows = await _rows(db)
        for term in _HOSTILE_TERMS:
            assert await _candidates(db, "phrase", term) == set(), (
                f"{term!r} reached a row as a query rather than as text"
            )
            assert await _candidates(db, "broad", term) == _gram_sharing(term, rows), (
                f"the broad query did not answer {term!r} as text"
            )


class TestTheNulRefusal:
    """A NUL is refused before any statement is issued.

    The two substrates refuse a NUL in *different* places, and the
    difference is why the refusal lives in the repository: SQLite truncates
    a string at a NUL when it reads it through the C string API, so a NUL
    inside a pattern turns it into a bare wildcard that matches every row,
    and psycopg refuses the bound parameter outright with an error that
    carries no SQLSTATE — which no classification could catch.

    So the refusal has to happen in one place, before the term is bound
    anywhere. The outcome alone cannot prove that: on the embedded
    substrate a NUL that reached the engine would also answer "no match".
    What proves it is the statement count.
    """

    async def test_a_nul_term_resolves_to_nothing_and_issues_no_statement(
        self, db: Database, corpus: FactRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No statement at all — not the exact rung, not the fuzzy rung.

        The term is one that *would* match: the fuzzy rung would have found
        ``"Widget Service"`` for it. The refusal is therefore visible: the
        term never reaches the engine, and the answer is "no match" rather
        than an exception the ingestion batch has to survive.
        """
        issued: list[str] = []
        failures: list[str] = []
        monkeypatch.setattr(db, "fetch_one", _spy(issued, failures, db.fetch_one))
        monkeypatch.setattr(db, "fetch_all", _spy(issued, failures, db.fetch_all))
        monkeypatch.setattr(db, "fetch_value", _spy(issued, failures, db.fetch_value))
        monkeypatch.setattr(db, "execute", _spy(issued, failures, db.execute))

        assert await corpus.resolve_entity("Widget\x00Service") is None
        assert issued == [], f"the refused term reached the substrate: {issued}"

        # The positive control: the spy is live, and a term without a NUL
        # does produce statements — so the empty list above means "refused
        # before asking", not "the spy is broken".
        resolved = await corpus.resolve_entity("Widgt Servce")
        assert resolved is not None
        assert issued, "the spy recorded nothing, so it proves nothing"


class TestTheGramCeiling:
    """A term with too many grams is refused before any statement.

    The count proxy is a sum of two ``CASE`` expressions per gram, and it
    sits in the statement's ``ORDER BY`` — so SQLite refuses the statement
    at about 500 grams ("expression tree is too large"), and that refusal
    is **not** a ``QueryRejected``: it escapes the fail-closed wrapper and
    takes the caller with it. The server substrate has no such limit and
    answers "no match". So the ceiling has to be in Python, and it has to
    be the *same* ceiling on both substrates.

    The refusal is made before any statement is issued — beside the NUL's
    — so the two substrates cannot disagree about *how* they answer, and
    the answer is "no match" on both.
    """

    async def test_a_term_over_the_ceiling_resolves_to_nothing_and_issues_no_statement(
        self, db: Database, corpus: FactRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No match, and no statement at all.

        The term is over the ceiling by construction — asserted here, so the
        test cannot quietly become a test of a term *under* it — and the
        statements are watched, because the outcome alone cannot prove
        where the refusal happened: a statement the engine refused also
        answers "no match", and on the server substrate there is no
        statement to refuse at all.

        The positive control is what keeps the test honest — it shows the
        spy records the statements that *are* issued, so an empty list
        afterwards means "refused before asking" and not "the spy is
        broken".
        """
        assert len(_grams(_OVER_THE_CEILING_TERM)) > _MAX_TERM_GRAMS, (
            "the term is not over the ceiling, so the test would prove nothing"
        )

        issued: list[str] = []
        failures: list[str] = []
        monkeypatch.setattr(db, "fetch_one", _spy(issued, failures, db.fetch_one))
        monkeypatch.setattr(db, "fetch_all", _spy(issued, failures, db.fetch_all))
        monkeypatch.setattr(db, "fetch_value", _spy(issued, failures, db.fetch_value))
        monkeypatch.setattr(db, "execute", _spy(issued, failures, db.execute))

        assert await corpus.resolve_entity(_OVER_THE_CEILING_TERM) is None
        assert issued == [], f"the refused term reached the substrate: {issued}"
        assert failures == [], f"the refused term raised: {failures}"

        # The positive control: the spy is live.
        resolved = await corpus.resolve_entity("Widgt Servce")
        assert resolved is not None
        assert issued, "the spy recorded nothing, so it proves nothing"


class TestTheEscapedAliasBlob:
    """A non-ASCII alias is invisible to the count proxy.

    The alias column holds JSON *text*, and ``json.dumps`` escapes
    non-ASCII — so the alias ``"ééé"`` is stored as
    ``["\\u00e9\\u00e9\\u00e9"]``, and the count proxy's pattern, built
    from the folded *text*, never matches the escaped blob.

    The breakage is **symmetric**: the blob is the same text on both
    substrates, so both answer "no match" and the superset property fails
    on both rather than on one. The fix is a stored normalised form
    (ADR-0029's limitation (c)), and this test is what says it is still
    open.
    """

    async def test_a_non_ascii_alias_resolves_to_no_match(
        self, db: Database, repo: FactRepository
    ) -> None:
        """The gate accepts the pair; the candidate query cannot see the row.

        The gate scores the alias ``"ééé"`` at 0.5 against the term
        ``"éééx"`` — over the floor — so the pair is one the gate would
        accept. The candidate query never sees the row, so the answer is
        "no match", on both substrates.
        """
        await repo.upsert_entity("project", "Widget Service", aliases=["ééé"])

        # The column really does hold the escaped text — that is the whole
        # mechanism, and the assertion below would be unreadable without it.
        blob = await db.fetch_value(
            "SELECT aliases FROM hot_serving_entity_index "
            "WHERE canonical_name = 'Widget Service'"
        )
        assert blob == '["\\u00e9\\u00e9\\u00e9"]'

        # The gate accepts the pair: the alias scores 0.5 against the term.
        assert trigram_similarity("éééx", "ééé") == pytest.approx(0.5)
        assert trigram_similarity("éééx", "ééé") >= _FLOOR

        # And the row is not a candidate, because the escaped blob holds
        # no gram of the term.
        assert await _candidates(db, "broad", "éééx") == set()

        # So the answer is "no match" — on both substrates, since the blob
        # is the same text on both.
        assert await repo.resolve_entity("éééx") is None


class TestTheTwoQueries:
    """The phrase query and the broad query are two different questions.

    The phrase query asks "is the term a substring of the row's text?"; the
    broad query asks "does the row's text share at least one of the term's
    grams?". They are not two spellings of one question, and collapsing
    them would lose either the substring match (a row the term spells out)
    or the fuzzy one (a row the term is a typo of).
    """

    async def test_a_term_that_is_a_substring_is_found_by_the_phrase_query(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The phrase query answers about substrings."""
        assert await _candidates(db, "phrase", "Widget Service") == {"Widget Service"}

    async def test_a_term_that_shares_only_grams_is_found_by_the_broad_query(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The broad query answers about shared grams, and only it does.

        ``"Widgt Servce"`` is a typo of ``"Widget Service"``: it shares
        grams with the row, and it is not a substring of it, so the phrase
        query finds nothing and the broad query finds the row. This is the
        measured case whose candidate set used to be *empty*.
        """
        assert await _candidates(db, "phrase", "Widgt Servce") == set()
        assert "Widget Service" in await _candidates(db, "broad", "Widgt Servce")

    async def test_the_fuzzy_rung_falls_back_to_the_broad_query(
        self, db: Database, corpus: FactRepository
    ) -> None:
        """The fallback is what makes the typo resolvable at all.

        The term is not a substring of any row, so the resolution has to
        reach the broad query to find the entity the gate would accept.
        """
        resolved = await corpus.resolve_entity("Widgt Servce")

        assert resolved is not None
        assert resolved.canonical_name == "Widget Service"
        assert resolved.match == "fuzzy"
