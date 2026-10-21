"""The fact/entity repository: idempotent entities, versioned facts, decayed reads.

Why the resolution path is defended
-----------------------------------
Entity resolution is the one place in the repository layer written to *fail
closed*: a query the engine refuses must answer "no match", because the
ingestion batch that asked the question must not go down with it. The catch
that does this is written for the neutral error vocabulary, so every error
the substrate can raise in that place has to be *in* that vocabulary — and
the family a corrupt row falls into has to be caught by it.

Why the *row conversion* is defended
------------------------------------
The two substrates store an identifier differently. SQLite stores it as TEXT
and hands back a ``str``; a PostgreSQL ``UUID`` column hands back a
:class:`uuid.UUID`. The model declares the identifier as text, so the
*reader* coerces — and every test in this file runs against the embedded
substrate, where the row already holds the value the model wants. Deleting
the coercion would not fail one of them.

That is how a fact read on the server substrate can end up carrying a
:class:`uuid.UUID` in a field declared as text — and the fact's id is the
vector point's id, which is a pydantic field declared as text, so the
value does not survive the trip.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from paa.memory.domains import MemoryDomain
from paa.memory.facts import FactRepository, _trigrams, trigram_similarity
from paa.storage.relational.database import Database, to_iso, utc_now
from paa.storage.relational.errors import MalformedDocument

#: The vocabulary view of the entity index's FTS5 table. ``fts5vocab`` is the
#: only way to read the *tokenizer's own output* back, and the tokenizer's
#: output is what the gate's model has to agree with.
_FTS_VOCAB = "hot_serving_entity_fts_vocab"


@pytest.fixture
def repo(db: Database) -> FactRepository:
    return FactRepository(db)


class TestEntities:
    async def test_upsert_is_idempotent_on_canonical_name(self, repo: FactRepository) -> None:
        a = await repo.upsert_entity("project", "Project Alpha")
        b = await repo.upsert_entity("project", "Project Alpha")
        assert a == b

    async def test_importance_takes_the_maximum_not_the_latest(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha", importance=0.9)
        await repo.upsert_entity("project", "Alpha", importance=0.1)
        entity = await repo.get_entity(eid)
        assert entity is not None
        assert entity.importance == pytest.approx(0.9)

    async def test_aliases_are_unioned(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("person", "Ada", aliases=["A. Lovelace"])
        await repo.upsert_entity("person", "Ada", aliases=["Countess"])
        entity = await repo.get_entity(eid)
        assert entity is not None
        assert set(entity.aliases) >= {"A. Lovelace", "Countess"}

    async def test_blank_name_rejected(self, repo: FactRepository) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            await repo.upsert_entity("project", "   ")

    async def test_resolve_entity_exact(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Widget Service")
        resolved = await repo.resolve_entity("Widget Service")
        assert resolved is not None and resolved.id == eid

    async def test_a_term_the_engine_would_reject_resolves_to_no_match(
        self, repo: FactRepository
    ) -> None:
        """Resolution fails closed, and the failure is not the batch's.

        A NUL byte in the term is refused by the *repository*, before the
        engine ever sees it — SQLite's full-text matcher would reject the
        statement, and psycopg would refuse the bound parameter outright.
        The refusal lives in the repository so that the answer is the same
        on both substrates: "no match", not an exception the ingestion
        batch has to survive.

        The catch that makes the *other* rejected queries fail closed was
        dead code for as long as it existed: a read raised the driver's
        own exception, and the catch was written for the neutral one. It
        is load-bearing again — for a malformed ``MATCH`` expression and
        for a corrupt row — and the NUL refusal no longer needs it.
        """
        await repo.upsert_entity("project", "Widget Service")
        assert await repo.resolve_entity("a\x00b") is None
        assert await repo.resolve_entity("\x00") is None

    async def test_a_nul_term_never_reaches_the_substrate(
        self, repo: FactRepository, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is the *repository's*, and this is what proves it.

        The outcome alone cannot prove where the refusal happened: on the
        embedded substrate the NUL path degrades to "no match" whether
        the repository refused the term or the engine rejected the
        statement. So the substrate is watched: a term carrying a NUL
        must not produce a statement at all.

        The positive control is what keeps the test honest — it shows the
        spy records the statements that *are* issued, so the empty list
        afterwards means "refused before asking", not "the spy is broken".
        """
        await repo.upsert_entity("project", "Widget Service")

        issued: list[str] = []
        real_fetch_one = db.fetch_one
        real_fetch_all = db.fetch_all

        async def _fetch_one(sql: str, params: Any = ()) -> Any:
            issued.append(sql)
            return await real_fetch_one(sql, params)

        async def _fetch_all(sql: str, params: Any = ()) -> Any:
            issued.append(sql)
            return await real_fetch_all(sql, params)

        monkeypatch.setattr(db, "fetch_one", _fetch_one)
        monkeypatch.setattr(db, "fetch_all", _fetch_all)

        assert await repo.resolve_entity("a\x00b") is None
        assert issued == [], f"the refused term reached the substrate: {issued}"

        # The positive control: the spy is live, and the ladder stops at
        # the exact rung for an exact match.
        resolved = await repo.resolve_entity("Widget Service")
        assert resolved is not None
        assert len(issued) == 1, issued
        assert "canonical_name" in issued[0], issued

    async def test_a_rejected_query_does_not_break_the_next_resolution(
        self, repo: FactRepository
    ) -> None:
        """Failing closed is not the same as being left broken."""
        await repo.upsert_entity("project", "Widget Service")
        await repo.resolve_entity("a\x00b")
        resolved = await repo.resolve_entity("Widget Service")
        assert resolved is not None

    async def test_a_corrupt_row_fails_closed_like_a_rejected_query(
        self, repo: FactRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt row must not be the thing that stops the batch.

        On the server substrate the entity's alias column is text that the
        dialect casts to ``jsonb`` in order to expand it, so a row holding
        something that is not JSON raises ``22P02``. That is the *data's*
        fault rather than a broken query, so it has its own class — but it
        is a member of the rejected-query family, and the fail-closed
        contract does not change with the class: resolution answers "no
        match" rather than taking the ingestion batch down.

        The failure surfaces in the *alias* lookup, and the alias lookup
        scans the table — so ONE corrupt row breaks resolution for every
        term. That is the case this defends.

        The error raised here carries ``substrate="postgres"`` on a
        *SQLite* database, which is the point of the fake: the handler
        that does the degrading catches the neutral *class*, so it fires
        whichever substrate produced the error.
        """

        await repo.upsert_entity("project", "Widget Service", aliases=["Gadget Assembly"])

        # The positive control: the term matches the alias of a healthy
        # row, so "no match" afterwards is a *change* — and a test that
        # only asserted "no match" would pass just as well with the alias
        # rung removed entirely.
        before = await repo.resolve_entity("Gadget Assembly", fuzzy=False)
        assert before is not None, "the alias rung did not resolve a matching alias"
        assert before.match == "alias"

        async def _raise(self: Any, sql: str, params: Any = ()) -> Any:
            if "a.value" in sql:
                raise MalformedDocument("malformed document: x", substrate="postgres")
            return await _original_fetch_one(self, sql, params)

        _original_fetch_one = type(repo._db).fetch_one
        monkeypatch.setattr(type(repo._db), "fetch_one", _raise)

        assert await repo.resolve_entity("Gadget Assembly", fuzzy=False) is None


class TestTheCorruptRow:
    """One unreadable row must not break resolution for every term.

    The alias column is JSON held as *text* on both substrates. The server
    dialect casts it back to ``jsonb`` to expand it; the embedded dialect
    expands it with ``json_each``. Either way the expansion reads *every*
    row, so a single corrupt row breaks the alias rung for every term —
    and the rung has to answer "no match" rather than take the ingestion
    batch down with it.
    """

    async def test_a_corrupt_row_fails_closed_and_the_row_was_consulted(
        self, repo: FactRepository, db: Database
    ) -> None:
        """The real thing, on the embedded substrate.

        The row is genuinely corrupt — the alias column holds something
        that is not JSON — so the failure is raised by the engine and
        classified by the substrate boundary, not simulated by a fake.

        The schema's ``CHECK (json_valid(aliases))`` means a corrupt row
        cannot be written through the repository at all: it is a row that
        arrived by another route, which is exactly what the classifier's
        corrupt-document case is a defence against.

        ``fuzzy=False`` isolates the alias rung, so the assertion is about
        the rung that fails rather than about a trigram guess.
        """
        await repo.upsert_entity("project", "Widget Service", aliases=["Gadget Assembly"])
        await repo.upsert_entity("project", "Other Service", aliases=["Sprocket Assembly"])

        # The positive control: both terms match the alias of a healthy
        # row, so "no match" afterwards is a change rather than a
        # standing fact.
        before = await repo.resolve_entity("Gadget Assembly", fuzzy=False)
        assert before is not None, "the alias rung did not resolve a matching alias"
        assert before.match == "alias"
        other = await repo.resolve_entity("Sprocket Assembly", fuzzy=False)
        assert other is not None, "the alias rung did not resolve a matching alias"
        assert other.match == "alias"

        # The check constraint has to be out of the way for the row to be
        # written corrupt at all.
        await db.execute("PRAGMA ignore_check_constraints=ON")
        await db.execute(
            "UPDATE hot_serving_entity_index SET aliases = 'not json at all' "
            "WHERE canonical_name = 'Widget Service'"
        )
        await db.execute("PRAGMA ignore_check_constraints=OFF")

        # One corrupt row breaks the alias rung for *every* term — the
        # second alias was perfectly good, and it stops resolving too.
        assert await repo.resolve_entity("Gadget Assembly", fuzzy=False) is None
        assert await repo.resolve_entity("Sprocket Assembly", fuzzy=False) is None
        # And the degradation is the alias rung's alone: the exact rung
        # does not read the alias column, and both rows are still there.
        exact = await repo.resolve_entity("Widget Service")
        assert exact is not None
        assert exact.canonical_name == "Widget Service"
        exact_other = await repo.resolve_entity("Other Service")
        assert exact_other is not None
        assert exact_other.canonical_name == "Other Service"


class TestTheGateModel:
    """The gate's grams must be the index's grams.

    The gate decides which entity a name refers to; the database only decides
    which rows the gate gets to look at. That division is only sound if the
    gate's gram set is the one the index holds — a gate that scores a pair
    the index cannot hold accepts a row that no candidate query over the
    index can return, and the two substrates then disagree about the answer.

    The gate's model used to be the *padded* one: two leading spaces, one
    trailing, whitespace runs collapsed. That is not what FTS5's ``trigram``
    tokenizer indexes, and the difference was load-bearing. The terms
    ``"aba"`` and ``"abba"`` shared only *padding* grams under it, so the
    gate scored them 0.50 — over the 0.34 floor — while the tokenizer's token
    sets for the two were disjoint. The gate accepted a pair the index could
    not see at all.

    These tests pin the tokenizer's model: the 3-grams of the raw,
    case-folded text.
    """

    @pytest.fixture
    async def fts_vocab(self, db: Database) -> AsyncIterator[None]:
        """The vocabulary view of the entity index's FTS5 table.

        It lists the tokens the index *holds*, per document and per column,
        which is the only way to compare the gate's model with the
        tokenizer's.
        """
        await db.execute(
            f"CREATE VIRTUAL TABLE {_FTS_VOCAB} "
            "USING fts5vocab(hot_serving_entity_fts, 'instance')"
        )
        yield

    async def _tokens(self, db: Database, rowid: int) -> set[str]:
        """The tokens the index holds for one document's canonical name."""
        rows = await db.fetch_all(
            f"SELECT term FROM {_FTS_VOCAB} WHERE doc = ? AND col = 'canonical_name'",
            (rowid,),
        )
        return {str(row["term"]) for row in rows}

    async def test_the_gate_grams_are_the_tokens_the_index_holds(
        self, repo: FactRepository, db: Database, fts_vocab: None
    ) -> None:
        """The two sets are equal, over a corpus that exercises the edges.

        Spaces, punctuation, digits and mixed case — because those are what
        the padding and the collapsing used to change.
        """
        corpus = (
            "Widget Service",
            "ACME-42 Corp",
            "alpha-beta",
            "Widgt Servce",
            "johnson alice",
            "Deploy  Pipeline",
        )
        for name in corpus:
            await repo.upsert_entity("project", name)

        # A row that arrived by another route. The repository collapses a
        # double space when it stores a name, but the index holds whatever
        # it was given — and the gate has to score the text the index holds.
        await db.execute(
            "INSERT INTO hot_serving_entity_index "
            "(id, class, canonical_name, aliases, created_at, updated_at) "
            "VALUES ('direct-1', 'project', 'Widget  Service', '[]', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

        rows = await db.fetch_all(
            "SELECT rowid, canonical_name FROM hot_serving_entity_index"
        )
        assert len(rows) == len(corpus) + 1
        for row in rows:
            stored = str(row["canonical_name"])
            tokens = await self._tokens(db, int(row["rowid"]))
            assert tokens == _trigrams(stored), (
                f"the gate's model drifted from the tokenizer's for {stored!r}: "
                f"index={sorted(tokens)} gate={sorted(_trigrams(stored))}"
            )

    async def test_a_pair_that_shares_no_gram_is_refused(
        self, repo: FactRepository, db: Database, fts_vocab: None
    ) -> None:
        """The measured defect, closed.

        ``"aba"`` and ``"abba"`` share no gram of their own text — the
        tokenizer's token sets are ``{"aba"}`` and ``{"abb", "bba"}``, and
        their intersection is empty. Under the padded model the gate scored
        the pair 0.50, over the 0.34 floor, on three grams the index cannot
        hold; so no candidate query over the index could have been a
        superset of the gate's acceptance set.
        """
        first = await repo.upsert_entity("project", "aba")
        second = await repo.upsert_entity("project", "abba")

        first_row = await db.fetch_one(
            "SELECT rowid FROM hot_serving_entity_index WHERE id = ?", (first,)
        )
        second_row = await db.fetch_one(
            "SELECT rowid FROM hot_serving_entity_index WHERE id = ?", (second,)
        )
        assert first_row is not None and second_row is not None

        first_tokens = await self._tokens(db, int(first_row["rowid"]))
        second_tokens = await self._tokens(db, int(second_row["rowid"]))
        assert first_tokens == {"aba"}
        assert second_tokens == {"abb", "bba"}
        assert first_tokens.isdisjoint(second_tokens)

        # The gate's score follows the index: the pair shares nothing, so
        # the gate refuses it.
        assert trigram_similarity("aba", "abba") == 0.0
        assert trigram_similarity("aba", "aba") == 1.0

    def test_a_text_shorter_than_a_trigram_is_compared_as_text(self) -> None:
        """The one case the gate cannot decide by set overlap.

        A text shorter than three characters has no grams at all, so there
        is nothing to overlap: the two are compared as text instead. Equal
        after trimming and case-folding is 1.0, anything else is 0.0.
        """
        assert _trigrams("ab") == set()
        assert _trigrams("abc") == {"abc"}

        assert trigram_similarity("ab", "AB") == 1.0
        assert trigram_similarity("ab", "cd") == 0.0
        # A short text and a long one are compared as text too, so they are
        # equal only when they are the same text. Under the padded model
        # this pair scored 0.40 on two padding grams — over the floor.
        assert trigram_similarity("ab", "abc") == 0.0


class TestFacts:
    async def test_add_and_query(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        await repo.add_fact(eid, "status", "active", confidence=0.9)
        facts = await repo.query_facts(entity_id=eid)
        assert len(facts) == 1
        assert facts[0].object_value == "active"

    async def test_query_returns_effective_decayed_confidence(self, repo: FactRepository) -> None:
        """A fact stored at 1.0 but two years idle must read as decayed, not 1.0."""
        eid = await repo.upsert_entity("project", "Alpha")
        old = utc_now() - timedelta(days=730)
        fid = await repo.add_fact(
            eid, "status", "active", domain=MemoryDomain.SEMANTIC, confidence=1.0
        )
        # Backdate the last_queried_at so decay has elapsed.
        await repo._db.execute(
            "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
            (to_iso(old), fid),
        )
        facts = await repo.query_facts(entity_id=eid)
        assert facts[0].confidence < 1.0, "stored value returned; decay not applied at read time"

    async def test_min_confidence_filters_on_decayed_value(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        old = utc_now() - timedelta(days=2000)
        fid = await repo.add_fact(
            eid, "status", "stale", domain=MemoryDomain.SEMANTIC, confidence=1.0
        )
        await repo._db.execute(
            "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
            (to_iso(old), fid),
        )
        # Decayed well below 0.5; a 0.5 floor should exclude it.
        assert await repo.query_facts(entity_id=eid, min_confidence=0.5) == []

    async def test_invalid_confidence_rejected(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        with pytest.raises(ValueError, match=r"\[0,1\]"):
            await repo.add_fact(eid, "x", "y", confidence=1.5)

    async def test_supersede_chain(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        v1 = await repo.add_fact(eid, "status", "planning")
        v2 = await repo.add_fact(eid, "status", "active")
        await repo.supersede(v1, v2)
        # v1 is superseded, so the live query returns only v2.
        live = await repo.query_facts(entity_id=eid)
        assert [f.object_value for f in live] == ["active"]

    async def test_pending_embeddings_lifecycle(self, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        fid = await repo.add_fact(eid, "status", "active")
        pending = await repo.pending_embeddings()
        assert fid in {f.id for f in pending}
        await repo.mark_embedded([fid])
        assert fid not in {f.id for f in await repo.pending_embeddings()}


# ---------------------------------------------------------------------------
# The row → model conversion
# ---------------------------------------------------------------------------


class _ServerShapedBackend:
    """A backend whose fact row carries the shapes the *server* produces.

    ``id``, ``entity_id`` and ``source_signal_id`` are ``UUID`` columns
    there, so psycopg hands back :class:`uuid.UUID` objects; SQLite
    stores the same values as TEXT and hands back strings. The fact
    model declares them as text, so the reader has to coerce.

    The fake answers every query with the one row it was given: it
    serves the read path under test and nothing else.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetch_one(self, sql: str, params: Any = ()) -> Any:
        return self._row

    async def fetch_all(self, sql: str, params: Any = ()) -> list[Any]:
        return [self._row]

    async def fetch_value(self, sql: str, params: Any = ()) -> Any:
        return None

    async def execute(self, sql: str, params: Any = ()) -> int:
        return 0

    async def execute_many(self, sql: str, rows: Any) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def table_names(self) -> list[str]:
        return ["hot_serving_active_facts", "hot_serving_entity_index"]


def _server_shaped_fact_row(**overrides: Any) -> dict[str, Any]:
    """A fact row in the shape the server substrate hands back."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "entity_id": uuid.uuid4(),
        "predicate": "status",
        "object_value": "active",
        "memory_domain": MemoryDomain.SEMANTIC.value,
        "memory_scope": "global",
        "initial_confidence": 0.9,
        "importance": 0.5,
        "use_count": 3,
        "source_signal_id": uuid.uuid4(),
        "provenance": "{}",
        "embedding_status": "pending",
        "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "last_queried_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "superseded_by": None,
        "entity_name": "Alpha",
    }
    row.update(overrides)
    return row


class TestTheFactRowConversion:
    """The conversion, on a row the embedded substrate cannot produce.

    The fact's identifiers are declared as text on the model, and every
    consumer of them treats them as text: the vector sink's point id is
    a pydantic field declared as ``str``, and a
    :class:`uuid.UUID` is not one.
    """

    async def test_a_uuid_fact_id_arrives_as_text(self) -> None:
        """The fact's own id is text on the model.

        Without the coercion the model carries a
        :class:`uuid.UUID` — and the vector sink's point id, which is
        that value, is a pydantic field declared as ``str`` and refuses
        it.
        """
        fact_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_fact_row(id=fact_id))

        fact = await FactRepository(backend).get_fact(str(fact_id))  # type: ignore[arg-type]

        assert fact is not None
        assert fact.id == str(fact_id)
        assert isinstance(fact.id, str)

    async def test_a_uuid_entity_id_arrives_as_text(self) -> None:
        """The entity the fact is about is text on the model too.

        The entity id is the other end of the same join, and it is
        looked up by text everywhere else in the repository layer — a
        fact that carries a different *type* for it cannot be matched
        to its entity.
        """
        entity_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_fact_row(entity_id=entity_id))

        fact = await FactRepository(backend).get_fact("fact")  # type: ignore[arg-type]

        assert fact is not None
        assert fact.entity_id == str(entity_id)
        assert isinstance(fact.entity_id, str)

    async def test_a_uuid_source_signal_id_arrives_as_text(self) -> None:
        """The evidence pointer is text on the model too.

        The pointer is what makes a fact's evidence walkable: it
        resolves to a cold-lake signal row, and the lookup is by id.
        A pointer of another type does not resolve.
        """
        signal_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_fact_row(source_signal_id=signal_id))

        fact = await FactRepository(backend).get_fact("fact")  # type: ignore[arg-type]

        assert fact is not None
        assert fact.source_signal_id == str(signal_id)
        assert isinstance(fact.source_signal_id, str)

    async def test_a_null_source_signal_id_stays_none(self) -> None:
        """A NULL is a NULL — not ``"None"`` and not ``""``.

        The column is nullable, and the call site is the thing that
        keeps it a NULL; the coercion is never handed the NULL to
        invent a value for.
        """
        backend = _ServerShapedBackend(_server_shaped_fact_row(source_signal_id=None))

        fact = await FactRepository(backend).get_fact("fact")  # type: ignore[arg-type]

        assert fact is not None
        assert fact.source_signal_id is None
        assert fact.superseded_by is None

    async def test_the_fact_id_is_usable_as_a_vector_point_id(self) -> None:
        """The consequence, in the consumer that actually breaks.

        The memory creator hands each fact's id to the vector sink as
        the point's id, and that id is a pydantic field declared as
        ``str``. A :class:`uuid.UUID` is not a string, so the write
        fails with a validation error — on the substrate that produces
        UUIDs, and only there.
        """
        # Imported here rather than at module scope so that the vector
        # package is not on this module's import path: the fact tests
        # must not depend on the vector extra being installed.
        from paa.storage.vector.store import VectorPoint

        fact_id = uuid.uuid4()
        backend = _ServerShapedBackend(_server_shaped_fact_row(id=fact_id))

        fact = await FactRepository(backend).get_fact(str(fact_id))  # type: ignore[arg-type]

        assert fact is not None
        point = VectorPoint(id=fact.id, vector=[0.0, 1.0], payload={})
        assert point.id == str(fact_id)
