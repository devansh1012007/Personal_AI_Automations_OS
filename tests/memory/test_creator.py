"""The Memory Creator: real-time ETL from cold-lake signal to hot serving.

The RFC-critical behaviours: extract structured facts, ignore conversational
noise, contain malformed input without partial writes, and quarantine (never
auto-resolve) contradictions.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from paa.memory.creator import (
    MemoryCreator,
    ProcessingOutcome,
    RuleBasedExtractor,
    SignalEnvelope,
)
from paa.memory.facts import Fact, FactRepository
from paa.storage.relational.database import Database, to_iso, utc_now


def _row(payload: Any, *, channel: str = "test", persisted: bool = False) -> dict[str, Any]:
    """A signal shaped like a cold_lake_signals row, for direct processing."""
    return {
        "id": str(uuid.uuid4()),
        "channel": channel,
        "external_id": None,
        "raw_payload": json.dumps(payload),
        "received_at": to_iso(utc_now()),
        "persisted": persisted,
    }


@pytest.fixture
def creator(db: Database) -> MemoryCreator:
    return MemoryCreator(db)


def _signal_row(subject: str) -> dict[str, Any]:
    """A signal asserting one triple about ``subject``."""
    return _row({"facts": [{"subject": subject, "predicate": "status", "object": "active"}]})


class TestExtraction:
    async def test_explicit_facts_are_written(self, creator: MemoryCreator, db: Database) -> None:
        signal = _row(
            {"facts": [{"subject": "Project Alpha", "predicate": "status", "object": "active"}]}
        )
        result = await creator.process_signal(signal)

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        stored = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert stored == 1

    async def test_conversational_noise_is_ignored(self, creator: MemoryCreator) -> None:
        result = await creator.process_signal(_row("thanks, that's great!"))
        assert result.outcome is ProcessingOutcome.IGNORED

    async def test_multiple_facts_from_one_signal(self, creator: MemoryCreator) -> None:
        signal = _row(
            {
                "facts": [
                    {"subject": "Alpha", "predicate": "status", "object": "active"},
                    {"subject": "Alpha", "predicate": "owner", "object": "Ada"},
                ]
            }
        )
        result = await creator.process_signal(signal)
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2


class TestMalformedContainment:
    async def test_bare_scalar_is_malformed_and_writes_nothing(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """A payload asserting nothing must be marked malformed with zero writes."""
        result = await creator.process_signal(_row(42))
        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_no_partial_commit_on_malformed(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        # A payload that is neither noise nor a valid assertion set.
        result = await creator.process_signal(_row(None))
        assert result.outcome in (ProcessingOutcome.MALFORMED, ProcessingOutcome.IGNORED)
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0


class TestContradictionQuarantine:
    async def test_conflicting_fact_is_quarantined_not_written(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """An incumbent 'colour=blue' plus an incoming 'colour=red' for the same
        entity must quarantine, not overwrite — RFC §4.2 forbids auto-resolution."""
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Alpha")
        await repo.add_fact(eid, "colour", "blue", confidence=0.95)

        signal = _row(
            {"facts": [{"subject": "Alpha", "predicate": "colour", "object": "red"}]}
        )
        result = await creator.process_signal(signal)

        assert result.outcome is ProcessingOutcome.QUARANTINED
        assert result.requires_human_attestation
        # The incumbent stands; the challenger was NOT written as a live fact.
        live = await db.fetch_all(
            "SELECT object_value FROM hot_serving_active_facts WHERE superseded_by IS NULL"
        )
        values = {r["object_value"] for r in live}
        assert "red" not in values


class TestBatchIsolation:
    async def test_one_poison_signal_does_not_abort_the_batch(self, db: Database) -> None:
        from paa.storage.relational.database import dumps

        # Insert three real signals: good, poison (bare scalar), good.
        async def insert(payload: Any) -> None:
            await db.execute(
                "INSERT INTO cold_lake_signals "
                "(id, received_at, channel, raw_payload, content_hash, sync_status) "
                "VALUES (?,?,?,?,?, 'unprocessed')",
                (
                    str(uuid.uuid4()),
                    to_iso(utc_now()),
                    "test",
                    dumps(payload),
                    "h" + uuid.uuid4().hex[:20],
                ),
            )

        await insert({"facts": [{"subject": "A", "predicate": "p", "object": "v1"}]})
        await insert(99)  # poison: bare scalar
        await insert({"facts": [{"subject": "B", "predicate": "p", "object": "v2"}]})

        report = await MemoryCreator(db).run_batch(limit=10)

        assert report.claimed == 3
        # Both good signals produced facts despite the poison in the middle.
        written = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert written == 2


class TestClaimOrder:
    """Signals are claimed in arrival order, not in random-id order.

    Two signals ingested inside the same clock tick share a
    ``received_at``, and the id is a random UUID — so "oldest first" needs
    an explicit secondary key or ingestion order stops meaning anything
    under load. The secondary key is ``arrival_seq``, which is what the
    column is for.
    """

    async def test_same_tick_signals_are_claimed_in_insertion_order(
        self, db: Database
    ) -> None:
        from paa.storage.relational.database import to_iso, utc_now

        creator = MemoryCreator(db)
        stamp = to_iso(utc_now())
        for index in range(4):
            await db.execute(
                "INSERT INTO cold_lake_signals (id, received_at, channel, raw_payload, "
                "content_hash, sync_status) VALUES (?, ?, 'test', '{}', ?, 'unprocessed')",
                (f"sig-{index}", stamp, f"hash-{index}"),
            )

        claimed = await creator._claim(10)

        assert [envelope.id for envelope in claimed] == [
            "sig-0",
            "sig-1",
            "sig-2",
            "sig-3",
        ]


class _RecordingSink:
    """An embedding sink that records what it was handed."""

    def __init__(self) -> None:
        self.batches: list[list[Fact]] = []

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        self.batches.append(list(facts))
        return [fact.id for fact in facts]


class TestEmbeddingHandOff:
    """Newly written facts reach the vector substrate.

    The hand-off was dead: an ``await`` inside a generator expression makes
    it an *async* generator, and the synchronous comprehension over it raised
    ``TypeError: 'async_generator' object is not iterable`` on every signal
    that wrote a fact. Nothing caught it, so the failure only ever appeared
    once a sink was attached — which no caller does today.
    """

    async def test_new_facts_are_handed_to_the_sink(self, db: Database) -> None:
        sink = _RecordingSink()
        creator = MemoryCreator(db, embedding_sink=sink)

        result = await creator.process_signal(_signal_row("Project Alpha"))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(sink.batches) == 1
        assert [fact.id for fact in sink.batches[0]] == result.facts_written

    async def test_indexed_facts_are_marked_embedded(self, db: Database) -> None:
        sink = _RecordingSink()
        creator = MemoryCreator(db, embedding_sink=sink)

        result = await creator.process_signal(_signal_row("Project Beta"))

        assert result.outcome is ProcessingOutcome.PROCESSED
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (result.facts_written[0],),
        )
        assert [row["embedding_status"] for row in statuses] == ["indexed"]

    async def test_a_failing_sink_leaves_the_facts_pending(self, db: Database) -> None:
        """``failed`` is terminal; a transient sink error must not exclude a
        fact from semantic recall forever."""

        class BrokenSink:
            async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
                raise RuntimeError("vector substrate down")

        creator = MemoryCreator(db, embedding_sink=BrokenSink())
        result = await creator.process_signal(_signal_row("Project Gamma"))

        assert result.outcome is ProcessingOutcome.PROCESSED
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (result.facts_written[0],),
        )
        assert [row["embedding_status"] for row in statuses] == ["pending"]


class _FakeEmbedder:
    """Deterministic 384-d embedder returning one unit-ish vector per text.

    Records the texts it was asked to embed (R-W1-03): the sink must embed
    ``[fact.statement]`` for the batch, not ids or empty strings.
    """

    dimensions = 384
    model_name = "fake-embedder-v1"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[1.0, 0.0] + [0.0] * 382 for _ in texts]


class _FakeVectorStore:
    """In-memory vector store capturing upserted points for assertions.

    Records the collection each upsert targeted (R-W1-03): the sink must
    write to ``"active_facts"``, not an ad-hoc name.
    """

    def __init__(self) -> None:
        self.points: dict[str, Any] = {}
        self.collections: list[str] = []

    async def upsert(self, collection: str, points: Sequence[Any]) -> int:
        self.collections.append(collection)
        for point in points:
            self.points[point.id] = point
        return len(points)

    async def search(self, collection: str, vector: Any, *, limit: int = 30) -> list[Any]:
        from paa.storage.vector import SearchHit

        return [
            SearchHit(id=p.id, score=0.99, payload=dict(p.payload))
            for p in list(self.points.values())[:limit]
        ]

    async def count(self, collection: str) -> int:
        return len(self.points)


class TestVectorSinkSemanticRoundTrip:
    """C01b: sink payload keys must match what _semantic reads.

    Reversal: removing object_value/confidence/importance from the payload
    makes _semantic return content == "" (reader default), so the round-trip
    assertion fails.
    """

    async def test_wired_sink_indexes_and_marks_indexed(self, db: Database) -> None:
        """T01 (pinning): triple-payload process_signal with a real
        VectorEmbeddingSink leaves zero pending rows and one point in
        active_facts.

        This test PINS the sink->store hand-off; it does not prove
        Runtime.build wiring (R-W1-07). The composition proof lives in the
        Runtime.build test in tests/runtime/test_memory_creator.py.
        """
        from paa.memory.creator import VectorEmbeddingSink

        store = _FakeVectorStore()
        embedder = _FakeEmbedder()
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))

        result = await creator.process_signal(_signal_row("Project Sigma"))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert len(store.points) == 1
        fact_id = result.facts_written[0]
        assert fact_id in store.points
        # R-W1-03: the sink must target the canonical collection and embed
        # the fact's statement — a wrong-collection or wrong-text sink
        # must fail here.
        assert store.collections == ["active_facts"]
        fact = await FactRepository(db).get_fact(fact_id)
        assert fact is not None
        assert embedder.batches == [[fact.statement]]
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert [row["embedding_status"] for row in statuses] == ["indexed"]

    async def test_payload_carries_reader_keys(self, db: Database) -> None:
        from paa.memory.creator import VectorEmbeddingSink

        store = _FakeVectorStore()
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, _FakeEmbedder()))
        await creator.process_signal(_signal_row("Project Payload"))

        assert len(store.points) == 1
        point = next(iter(store.points.values()))
        assert point.payload["object_value"] == "active"
        assert point.payload["predicate"] == "status"
        assert point.payload["statement"] != ""
        # R-W1-06: pin the exact ranking values, not just >0 — a swap or a
        # constant would otherwise survive. Fact.confidence is recomputed
        # (decayed) at read time, so compare with a tight tolerance rather
        # than bit-equal floats.
        fact = await FactRepository(db).get_fact(point.id)
        assert fact is not None
        assert abs(float(point.payload["confidence"]) - fact.confidence) < 1e-6
        assert abs(float(point.payload["importance"]) - fact.importance) < 1e-9
        # The write-path facts carry rule-extractor values far from the
        # reader defaults (0.8/0.5); equality above subsumes the check,
        # but keep it explicit so a future defaults change is noticed.
        assert abs(float(point.payload["confidence"]) - 0.8) > 1e-6 or fact.confidence == 0.8
        assert abs(float(point.payload["importance"]) - 0.5) > 1e-9 or fact.importance == 0.5

    async def test_semantic_round_trip_returns_non_empty_content(
        self, db: Database
    ) -> None:
        from paa.agents.context_agents import PlannerContextBuilder
        from paa.memory.creator import VectorEmbeddingSink

        store = _FakeVectorStore()
        embedder = _FakeEmbedder()
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))
        result = await creator.process_signal(_signal_row("Project RoundTrip"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]
        fact = await FactRepository(db).get_fact(fact_id)
        assert fact is not None

        builder = PlannerContextBuilder(db=db, vector_store=store, embedder=embedder)
        elements = await builder._semantic("Project RoundTrip status")
        assert elements, "semantic search returned no elements"
        assert elements[0].content != "", "semantic content must be the fact object_value"
        assert elements[0].content == "active"
        # R-W1-06: the element's ranking values come from the payload, not
        # the reader defaults, and match the written fact. Compare with a
        # tight tolerance — Fact.confidence is decayed at read time.
        assert abs(elements[0].confidence - fact.confidence) < 1e-6
        assert abs(elements[0].importance - fact.importance) < 1e-9


class TestDrainPendingEmbeddings:
    """C02: drain_pending_embeddings replays pending -> indexed, idempotently.

    Reversal: without the method every drain test raises AttributeError;
    without the call sites the API/CLI drains leave the fact pending.
    """

    async def test_drain_indexes_pending_and_is_idempotent(self, db: Database) -> None:
        """T02 second half: a fact committed with no sink (pending) is
        indexed by a later drain; a second drain returns 0."""
        creator_no_sink = MemoryCreator(db)
        result = await creator_no_sink.process_signal(_signal_row("Project Delta"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]

        pending = await FactRepository(db).pending_embeddings()
        assert [f.id for f in pending] == [fact_id]

        sink = _RecordingSink()
        creator_with_sink = MemoryCreator(db, embedding_sink=sink)
        indexed = await creator_with_sink.drain_pending_embeddings()
        assert indexed == 1
        assert await FactRepository(db).pending_embeddings() == []
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert [row["embedding_status"] for row in statuses] == ["indexed"]

        # Idempotent: nothing left to drain.
        assert await creator_with_sink.drain_pending_embeddings() == 0

    async def test_drain_with_failing_sink_leaves_pending_then_recovers(
        self, db: Database
    ) -> None:
        """A failing sink never flips to terminal failed; next drain flips."""
        creator_no_sink = MemoryCreator(db)
        result = await creator_no_sink.process_signal(_signal_row("Project Epsilon"))
        fact_id = result.facts_written[0]

        class BrokenSink:
            async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
                raise RuntimeError("vector substrate down")

        failing = MemoryCreator(db, embedding_sink=BrokenSink())
        assert await failing.drain_pending_embeddings() == 0
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert [row["embedding_status"] for row in statuses] == ["pending"]

        recovering = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await recovering.drain_pending_embeddings() == 1
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert [row["embedding_status"] for row in statuses] == ["indexed"]

    async def test_drain_without_sink_returns_zero_and_leaves_pending(
        self, db: Database
    ) -> None:
        creator = MemoryCreator(db)
        result = await creator.process_signal(_signal_row("Project Zeta"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert await creator.drain_pending_embeddings() == 0
        pending = await FactRepository(db).pending_embeddings()
        assert len(pending) == 1

    async def test_drain_empty_backlog_returns_zero(self, db: Database) -> None:
        creator = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await creator.drain_pending_embeddings() == 0

    async def _seed_pending(self, db: Database, *, n: int, prefix: str) -> list[str]:
        """Seed ``n`` pending facts directly via the repository (no extractor).

        Uses one entity + ``n`` facts; every new fact defaults to
        ``pending`` (schema default). Counts are what the drain limit tests
        assert — order is pinned separately by ``test_drain_oldest_first_order``.
        """
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", f"{prefix}-entity")
        ids: list[str] = []
        for i in range(n):
            ids.append(await repo.add_fact(eid, "status", f"{prefix}-v{i}"))
        return ids

    async def test_drain_limit_bounds_the_batch(self, db: Database) -> None:
        """P1-3 (R-W1-04): ``limit`` bounds one drain; repeated drains progress.

        Reversal: ignoring ``limit`` would drain all 3 on the first call,
        so ``assert indexed == 2`` fails if the bound is removed.
        """
        await self._seed_pending(db, n=3, prefix="limit")
        creator = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await creator.drain_pending_embeddings(limit=2) == 2
        assert len(await FactRepository(db).pending_embeddings()) == 1
        assert await creator.drain_pending_embeddings() == 1
        assert await FactRepository(db).pending_embeddings() == []

    async def test_drain_limit_non_positive_returns_zero(self, db: Database) -> None:
        """P1-3: ``limit<=0`` is a no-op (guards SQLite ``LIMIT -1``)."""
        await self._seed_pending(db, n=2, prefix="nonpos")
        creator = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await creator.drain_pending_embeddings(limit=0) == 0
        assert await creator.drain_pending_embeddings(limit=-5) == 0
        assert len(await FactRepository(db).pending_embeddings()) == 2

    async def test_drain_105_backlog_drained_not_paginated(self, db: Database) -> None:
        """P1-3: default ``limit=100`` drains 100 then 5 (drained, not OFFSET).

        Deliberate owner update (REV-W4 C1 rating: STALE EXPECTATION).
        The old assertion drained 100 then 5 on ONE creator back-to-back,
        which pins pre-throttle unbounded draining. ADR-0036 added a
        windowed per-second cap (``drain_max_per_second``, default 100):
        the first drain exhausts the window, so the second call on the
        same instance indexes 0 and logs ``memory.embedding_drain_throttled``
        — the intended behaviour, pinned canonically by the T15 tests.
        This test's intent (105 backlogs drain fully across calls, never
        OFFSET-paginated, nothing dropped) is preserved by draining the
        remaining 5 through a FRESH creator (its own window), which proves
        the full backlog still drains and nothing is lost to the throttle.
        """
        import structlog.testing as _st

        await self._seed_pending(db, n=105, prefix="backlog")
        creator = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await creator.drain_pending_embeddings() == 100
        assert len(await FactRepository(db).pending_embeddings()) == 5
        # Second call in the same window: fully deferred, logged, no drop.
        with _st.capture_logs() as captured:
            assert await creator.drain_pending_embeddings() == 0
        assert [e for e in captured if e.get("event") == "memory.embedding_drain_throttled"]
        assert len(await FactRepository(db).pending_embeddings()) == 5
        # A fresh creator has its own window, so the remainder drains.
        fresh = MemoryCreator(db, embedding_sink=_RecordingSink())
        assert await fresh.drain_pending_embeddings() == 5
        assert await FactRepository(db).pending_embeddings() == []

    async def test_drain_partial_sink_return_marks_only_returned(self, db: Database) -> None:
        """P1-4: a sink returning a subset marks only the returned ids."""
        await self._seed_pending(db, n=2, prefix="partial")
        pending_before = await FactRepository(db).pending_embeddings()
        assert len(pending_before) == 2
        first_pending_id = pending_before[0].id
        second_pending_id = pending_before[1].id

        class _PartialSink:
            async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
                return [facts[0].id]

        creator = MemoryCreator(db, embedding_sink=_PartialSink())
        assert await creator.drain_pending_embeddings() == 1
        statuses = await db.fetch_all(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (first_pending_id,),
        )
        assert [r["embedding_status"] for r in statuses] == ["indexed"]
        pending = await FactRepository(db).pending_embeddings()
        assert len(pending) == 1
        assert pending[0].id == second_pending_id

    async def test_drain_empty_sink_return_marks_nothing(self, db: Database) -> None:
        """P1-4: a sink returning ``[]`` indexes nothing, all stay pending."""
        await self._seed_pending(db, n=2, prefix="empty-ret")

        class _EmptySink:
            async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
                return []

        creator = MemoryCreator(db, embedding_sink=_EmptySink())
        assert await creator.drain_pending_embeddings() == 0
        assert len(await FactRepository(db).pending_embeddings()) == 2

    async def test_drain_unknown_id_sink_return_is_noop(self, db: Database) -> None:
        """Partial/unknown ids: ``mark_embedded`` on a non-existent id is a no-op."""
        await self._seed_pending(db, n=1, prefix="unknown")

        class _UnknownSink:
            async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
                return ["nope-not-a-fact"]

        creator = MemoryCreator(db, embedding_sink=_UnknownSink())
        # Returns the sink's (bogus) id count; the UPDATE matches 0 rows.
        assert await creator.drain_pending_embeddings() == 1
        # The real fact was never marked, so it stays pending for the next drain.
        assert len(await FactRepository(db).pending_embeddings()) == 1

    async def test_concurrent_double_drain_loses_nothing(self, db: Database) -> None:
        """P2-6 (R-W1-05): two concurrent drains over 2 pending lose nothing.

        Deterministic: both drains use the recording sink (idempotent
        upserts). The sum of indexed counts equals the batch size and every
        fact ends up ``indexed`` — a duplicate point would only waste embed
        cost, not lose data (see REV-code §3).
        """
        import asyncio

        await self._seed_pending(db, n=2, prefix="concurrent")
        sink = _RecordingSink()
        creator = MemoryCreator(db, embedding_sink=sink)
        results = await asyncio.gather(
            creator.drain_pending_embeddings(),
            creator.drain_pending_embeddings(),
        )
        assert sum(results) >= 2
        assert await FactRepository(db).pending_embeddings() == []
        for fact in await FactRepository(db).pending_embeddings(limit=100):
            assert fact.id  # unreachable; documents the invariant
        # Both facts indexed regardless of which drain won the race.
        indexed_ids = await db.fetch_all(
            "SELECT id FROM hot_serving_active_facts WHERE embedding_status = 'indexed'"
        )
        assert len(indexed_ids) == 2

    async def test_real_sink_embedder_raise_leaves_pending(self, db: Database) -> None:
        """P2-7 (R-W1-05): real VectorEmbeddingSink with a raising embedder."""
        from paa.memory.creator import VectorEmbeddingSink

        await self._seed_pending(db, n=1, prefix="embedfail")

        class _RaisingEmbedder:
            dimensions = 384
            model_name = "raising"

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embedder down")

        creator = MemoryCreator(
            db, embedding_sink=VectorEmbeddingSink(_FakeVectorStore(), _RaisingEmbedder())
        )
        assert await creator.drain_pending_embeddings() == 0
        assert len(await FactRepository(db).pending_embeddings()) == 1

    async def test_real_sink_store_raise_leaves_pending(self, db: Database) -> None:
        """P2-7: real VectorEmbeddingSink with a raising store.upsert."""
        from paa.memory.creator import VectorEmbeddingSink

        await self._seed_pending(db, n=1, prefix="storefail")

        class _RaisingStore:
            async def upsert(self, collection: str, points: Sequence[Any]) -> int:
                raise RuntimeError("store down")

        creator = MemoryCreator(
            db, embedding_sink=VectorEmbeddingSink(_RaisingStore(), _FakeEmbedder())
        )
        assert await creator.drain_pending_embeddings() == 0
        assert len(await FactRepository(db).pending_embeddings()) == 1

    async def test_write_path_embedder_raise_stays_pending(self, db: Database) -> None:
        """P2-7: ``process_signal`` with a raising real sink processes but stays pending."""
        from paa.memory.creator import VectorEmbeddingSink

        class _RaisingEmbedder:
            dimensions = 384
            model_name = "raising"

            async def embed(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("embedder down")

        creator = MemoryCreator(
            db, embedding_sink=VectorEmbeddingSink(_FakeVectorStore(), _RaisingEmbedder())
        )
        result = await creator.process_signal(_signal_row("Project WriteFail"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]
        status = await db.fetch_value(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert status == "pending"

    async def test_multi_fact_signal_through_wired_sink_indexes_all(self, db: Database) -> None:
        """P2-9: one signal, 2 triples → 2 points, 2 indexed, payload keys on both."""
        from paa.memory.creator import VectorEmbeddingSink

        store = _FakeVectorStore()
        embedder = _FakeEmbedder()
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))
        signal = _row(
            {
                "facts": [
                    {"subject": "MultiAlpha", "predicate": "status", "object": "active"},
                    {"subject": "MultiAlpha", "predicate": "owner", "object": "Ada"},
                ]
            }
        )
        result = await creator.process_signal(signal)
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2
        assert len(store.points) == 2
        assert store.collections == ["active_facts"] * 1  # one batch upsert
        expected_statements: list[str] = []
        for fact_id in result.facts_written:
            fact = await FactRepository(db).get_fact(fact_id)
            assert fact is not None
            expected_statements.append(fact.statement)
        assert embedder.batches == [expected_statements]
        for fact_id in result.facts_written:
            status = await db.fetch_value(
                "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
                (fact_id,),
            )
            assert status == "indexed"
            point = store.points[fact_id]
            assert point.payload["object_value"] in {"active", "Ada"}


def _envelope(payload: Any) -> SignalEnvelope:
    """An unpersisted envelope for direct extractor assertions."""
    return SignalEnvelope(
        id=str(uuid.uuid4()),
        channel="test",
        payload=payload,
        raw=json.dumps(payload) if not isinstance(payload, str) else payload,
        external_id=None,
        received_at=None,
        persisted=False,
    )


class TestFirstPersonMapping:
    """C05 (T06): first-person pronoun subjects resolve to ``user``.

    Reversal: removing the mapping yields entity "I am" for the sentence
    case and entity "I"/"we" for the triple cases -- the ``canonical_name``
    and ``entity_name`` assertions below fail. No upper-case assumption:
    "WE" maps the same way "We" does.
    """

    async def test_i_am_based_writes_user_fact(self, creator: MemoryCreator, db: Database) -> None:
        result = await creator.process_signal(_row("I am based in Berlin"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert result.drop_counts.total == 0
        entities = await db.fetch_all(
            "SELECT canonical_name, importance_weight FROM hot_serving_entity_index"
        )
        assert [(r["canonical_name"], r["importance_weight"]) for r in entities] == [("user", 0.3)]
        fact = await FactRepository(db).get_fact(result.facts_written[0])
        assert fact is not None
        assert fact.importance == pytest.approx(0.3)

    async def test_we_subject_maps_to_user_at_extractor_level(self) -> None:
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope("We owns Acme"))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "user"
        assert candidates[0].importance == pytest.approx(0.3)
        assert candidates[0].confidence == pytest.approx(0.60)
        assert extractor.drop_counts.pronouns == 0

    async def test_uppercase_we_maps_to_user(self) -> None:
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope("WE owns Acme"))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "user"

    async def test_triple_subject_i_maps_to_user(self) -> None:
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(
            _envelope({"facts": [{"subject": "I", "predicate": "status", "object": "active"}]})
        )
        assert len(candidates) == 1
        assert candidates[0].entity_name == "user"
        assert candidates[0].importance == pytest.approx(0.3)

    async def test_key_value_subject_maps_to_user(self) -> None:
        """M2: key-value path (subject from top level, 'status: v' in text)."""
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(
            _envelope({"subject": "I", "text": "status: active"})
        )
        assert len(candidates) == 1
        assert candidates[0].entity_name == "user"
        assert candidates[0].importance == pytest.approx(0.3)
        assert candidates[0].predicate == "status"
        assert candidates[0].object_value == "active"

    async def test_structured_walk_subject_maps_to_user(self) -> None:
        """M2: _walk structured path (scalar pairs under a first-person subject)."""
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope({"subject": "I", "status": "active"}))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "user"
        assert candidates[0].importance == pytest.approx(0.3)
        assert candidates[0].predicate == "status"
        assert candidates[0].object_value == "active"

    async def test_contraction_subject_is_unmapped_and_pinned(self) -> None:
        """N1: "I'm" is a single token that matches neither "i" nor "we",
        so the current behaviour is a plain entity "I'm" at default
        importance, zero drops. Pinned (not fixed): mapping contractions
        would be a behaviour change this task did not approve."""
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope("I'm based in Berlin"))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "I'm"
        assert candidates[0].importance == pytest.approx(0.5)
        assert extractor.drop_counts.total == 0

    async def test_user_lead_word_is_not_first_person(self) -> None:
        """P1-2: a subject that merely starts with "user" ("User owns Acme")
        is its own entity at default importance; the first-word map fires
        only on i/we tokens, never on "user"."""
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope("User owns Acme"))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "User"
        assert candidates[0].importance == pytest.approx(0.5)
        assert extractor.drop_counts.total == 0

    async def test_empty_and_pronoun_only_inputs_are_ignored(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """P1-3: "" / "I" / "we" alone carry no predicate, so they are
        IGNORED with no entity or fact rows; bare pronouns count as
        no_match exactly once, the empty string counts nothing."""
        totals = []
        for payload in ("", "I", "we"):
            result = await creator.process_signal(_row(payload))
            assert result.outcome is ProcessingOutcome.IGNORED
            totals.append(result.drop_counts.total)
        assert totals == [0, 1, 1]
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_entity_index") == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_user_entity_merge_uses_max_importance(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """R-W3-007 pin: a pre-existing entity named "user" is the SAME row
        the first-person map lands on (idempotent upsert); importance
        merges by max(), so the 0.8 row keeps 0.8 while the *fact* stays
        0.3. No second row is created."""
        repo = FactRepository(db)
        await repo.upsert_entity("concept", "user", importance=0.8)
        result = await creator.process_signal(_row("I am based in Berlin"))
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        entities = await db.fetch_all(
            "SELECT canonical_name, importance_weight FROM hot_serving_entity_index"
        )
        assert [(r["canonical_name"], r["importance_weight"]) for r in entities] == [("user", 0.8)]
        fact = await repo.get_fact(result.facts_written[0])
        assert fact is not None
        assert fact.importance == pytest.approx(0.3)

    async def test_non_first_person_fact_keeps_default_importance(self) -> None:
        extractor = RuleBasedExtractor()
        candidates = await extractor.extract(_envelope("The pipeline is green"))
        assert len(candidates) == 1
        assert candidates[0].entity_name == "pipeline"
        assert candidates[0].importance == pytest.approx(0.5)


class TestExtractionDropCounts:
    """C05 (T06): silent-drop classes are counted, not silent.

    Every assertion reads the public ``ProcessingResult.drop_counts`` /
    ``extractor.drop_counts`` fields -- never the extractor's private
    counter. Reversal: removing the counters leaves every field 0, so
    the nonzero assertions fail; removing the mapping above additionally
    turns hedged/question lines into facts or drops silently again.
    """

    async def test_hedge_yields_zero_candidates_with_counter(self, creator: MemoryCreator) -> None:
        result = await creator.process_signal(_row("Maybe Acme owns Beta"))
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.hedges == 1
        assert result.drop_counts.total == 1
        assert result.drop_counts.as_dict()["hedges"] == 1

    async def test_question_yields_zero_candidates_with_counter(
        self, creator: MemoryCreator
    ) -> None:
        result = await creator.process_signal(_row("Is the pipeline green?"))
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.questions == 1
        assert result.drop_counts.total == 1

    async def test_non_first_person_pronoun_still_dropped_and_counted(
        self, creator: MemoryCreator
    ) -> None:
        result = await creator.process_signal(_row("You owns Acme"))
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.pronouns == 1
        assert result.drop_counts.total == 1

    async def test_mixed_signal_counts_each_class_once(self, creator: MemoryCreator) -> None:
        text = "Maybe Acme owns Beta. Is the pipeline green? You owns Acme."
        result = await creator.process_signal(_row(text))
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.hedges == 1
        assert result.drop_counts.questions == 1
        assert result.drop_counts.pronouns == 1
        assert result.drop_counts.total == 3

    async def test_noise_line_and_no_match_are_counted(self, creator: MemoryCreator) -> None:
        noise = await creator.process_signal(_row("thanks"))
        assert noise.outcome is ProcessingOutcome.IGNORED
        assert noise.drop_counts.noise == 1
        assert noise.drop_counts.total == 1

        nomatch = await creator.process_signal(_row("hello world"))
        assert nomatch.outcome is ProcessingOutcome.IGNORED
        assert nomatch.drop_counts.no_match == 1
        assert nomatch.drop_counts.total == 1

    async def test_subjectless_structured_payload_is_counted(self, creator: MemoryCreator) -> None:
        result = await creator.process_signal(_row({"status": "active"}))
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.subjectless == 1
        assert result.drop_counts.total == 1

    async def test_non_storable_candidate_is_counted_by_creator_not_extractor(
        self, creator: MemoryCreator
    ) -> None:
        # The triple path extracts "thanks" as a candidate; the creator's
        # _is_storable filter discards it (noise object value). Counted
        # exactly once: extractor counters stay 0, non_storable is 1.
        result = await creator.process_signal(
            _row({"facts": [{"subject": "Acme", "predicate": "status", "object": "thanks"}]})
        )
        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.non_storable == 1
        assert result.drop_counts.malformed_skipped == 0
        assert result.drop_counts.total == 1

    async def test_counters_reset_per_signal_and_snapshot_is_a_copy(self) -> None:
        extractor = RuleBasedExtractor()
        await extractor.extract(_envelope("Maybe Acme owns Beta"))
        first = extractor.drop_counts
        assert first.hedges == 1
        await extractor.extract(_envelope("Acme owns Beta"))
        assert first.hedges == 1  # the snapshot was not mutated by signal two
        assert extractor.drop_counts.total == 0

    async def test_result_summary_surfaces_drop_counts(self, creator: MemoryCreator) -> None:
        result = await creator.process_signal(_row("Maybe Acme owns Beta"))
        summary = result.summary()
        assert summary["dropped"] == 1
        assert summary["drop_counts"]["hedges"] == 1
        # Pre-existing keys are untouched (additive-only contract). The
        # exact-set equality pins the additive keys too: if summary()
        # grows a key, this fails loudly instead of silently widening
        # the contract (M3).
        assert set(summary) == {
            "signal_id", "outcome", "candidates", "facts_written",
            "conflicts", "error", "dropped", "drop_counts",
        }

    async def test_batch_report_aggregates_but_summary_stays_unchanged(self, db: Database) -> None:
        from paa.storage.relational.database import dumps

        async def insert(payload: Any) -> None:
            await db.execute(
                "INSERT INTO cold_lake_signals "
                "(id, received_at, channel, raw_payload, content_hash, sync_status) "
                "VALUES (?,?,?,?,?, 'unprocessed')",
                (
                    str(uuid.uuid4()),
                    to_iso(utc_now()),
                    "test",
                    dumps(payload),
                    "h" + uuid.uuid4().hex[:20],
                ),
            )

        await insert({"facts": [{"subject": "Good", "predicate": "status", "object": "active"}]})
        await insert("Maybe Acme owns Beta")
        report = await MemoryCreator(db).run_batch(limit=10)
        assert report.processed == 1
        assert report.ignored == 1
        assert report.dropped == 1
        assert report.drop_counts.hedges == 1
        # BatchReportView(**report.summary()) in api/app.py unpacks this
        # dict; the summary shape must stay exactly as existing consumers
        # expect.
        assert set(report.summary()) == {
            "claimed", "processed", "ignored", "malformed",
            "quarantined", "errors", "facts_written",
        }
