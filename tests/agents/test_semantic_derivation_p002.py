"""T02: query-time confidence derivation on the vector path (P-002, Fix A).

``ContextBuilder._semantic`` must derive confidence at query time with live
``last_queried_at`` (D13: derive at query, never store) instead of trusting the
Qdrant payload snapshot frozen at ingest. The payload ``confidence`` stays as
an enqueue-time provenance snapshot (asserted unchanged); ranking and the
gatherer floor see the derived value. Hydration is ONE batched query, never
N+1; hydration failure degrades to the payload snapshot (tier still serves);
unknown domains pass through without crashing. All tests here fail before
Fix A (they return the frozen payload value / no hydration query) and pass
after.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from paa.agents.context_agents import PlannerContextBuilder
from paa.config import ContextSettings
from paa.context.gatherer import BoundedContextGatherer, ContextElement, _rank_key
from paa.memory.creator import VectorEmbeddingSink
from paa.memory.decay import effective_confidence
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, from_iso, to_iso, utc_now
from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

_TOKEN_RE = re.compile(r"[a-z]+")

#: Synonyms the builder's fake embedder folds together so a paraphrase query
#: hits the stored statement. Mirrors the e2e synonym pattern.
_SYNONYMS: dict[str, str] = {"rapid": "swift", "quick": "swift", "state": "status"}


class _SynonymEmbedder:
    """Deterministic 384-d synonym-aware embedder (real-store tests)."""

    dimensions = 384
    model_name = "synonym-fake-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text.lower()):
            canonical = _SYNONYMS.get(token, token)
            digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class _Hit:
    def __init__(self, hit_id: str, payload: dict[str, Any], score: float = 0.95) -> None:
        self.id = hit_id
        self.score = score
        self.payload = payload


class _FakeVectors:
    def __init__(self, hits: list[_Hit]) -> None:
        self._hits = hits

    async def search(
        self, collection: str, vector: Any, limit: int = 30
    ) -> list[_Hit]:
        return self._hits[:limit]


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


async def _age_fact(db: Database, fact_id: str, days: int = 300) -> str:
    old_dt = utc_now() - timedelta(days=days)
    old_iso = to_iso(old_dt)
    await db.execute(
        "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
        (old_iso, fact_id),
    )
    return old_iso


async def test_t02_derived_confidence_and_provenance_and_floor(
    db: Database, tmp_path: Path
) -> None:
    """T02 core: C0=1.0 enqueued, aged past half-life, ranks by derived value.

    Payload snapshot stays 1.0 (provenance); ``_semantic`` returns the derived
    value via ``effective_confidence`` and the gatherer confidence_floor drops
    it even though the payload says 1.0.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_id = await repo.add_fact(
        entity_id, "status", "swift", domain="semantic", confidence=1.0, importance=0.5
    )
    fact = await repo.get_fact(fact_id)
    assert fact is not None

    vec_dir = tmp_path / "vectors"
    embedder = _SynonymEmbedder()
    store = NumpyVectorStore(vec_dir)
    await store.ensure_spec(ACTIVE_FACTS)
    try:
        sink = VectorEmbeddingSink(store, embedder)
        indexed = await sink.enqueue([fact])
        assert list(indexed) == [fact_id]

        # Payload provenance snapshot at enqueue time.
        probe = (await embedder.embed(["Widget status swift"]))[0]
        raw_hits = await store.search("active_facts", probe, limit=10)
        assert raw_hits
        assert raw_hits[0].payload["confidence"] == pytest.approx(1.0, abs=1e-6)

        old_iso = await _age_fact(db, fact_id, days=300)
        now = utc_now()
        builder = PlannerContextBuilder(db=db, vector_store=store, embedder=embedder)
        elements = await builder._semantic("Widget status swift", now=now)

        assert elements
        assert elements[0].id == fact_id
        expected = effective_confidence(1.0, from_iso(old_iso), "semantic", now=now)
        assert elements[0].confidence == pytest.approx(expected, abs=1e-6)
        assert elements[0].confidence < 0.70
        assert elements[0].confidence != pytest.approx(1.0, abs=1e-6)

        # Payload is still the enqueue snapshot — never rewritten (D13).
        raw_hits_after = await store.search("active_facts", probe, limit=10)
        assert raw_hits_after
        assert raw_hits_after[0].payload["confidence"] == pytest.approx(1.0, abs=1e-6)

        # Gatherer confidence_floor (0.70) drops the derived value even though
        # the payload says 1.0. Hot-pool is the floor-filtered pool; semantic
        # ranking also uses relevance*confidence on the derived value.
        gatherer = BoundedContextGatherer(ContextSettings())
        assert gatherer.settings.confidence_floor == pytest.approx(0.70, abs=1e-6)
        pool = gatherer._candidate_pool(
            hot_facts=list(elements), semantic_matches=[], blocked_domains=frozenset()
        )
        assert pool == []
        # A payload-trusting element (1.0) would have survived the floor.
        payload_trusting = ContextElement(
            id=fact_id, content="swift", relevance=1.0, confidence=1.0, memory_domain="semantic"
        )
        pool_payload = gatherer._candidate_pool(
            hot_facts=[payload_trusting], semantic_matches=[], blocked_domains=frozenset()
        )
        assert [e.id for e in pool_payload] == [fact_id]
    finally:
        await store.close()


async def test_missing_confidence_key_hydrated_value_wins(db: Database) -> None:
    """Boundary: hit missing payload keys must not leak the 0.8/0.5 defaults.

    The hydrated row exists, so derived confidence, hydrated importance and
    hydrated domain win even when the payload omits every ranking key.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_id = await repo.add_fact(
        entity_id, "is", "foo_bar", domain="tool", confidence=1.0, importance=0.77
    )
    old_iso = await _age_fact(db, fact_id, days=30)
    now = utc_now()
    hit = _Hit(
        fact_id,
        payload={"object_value": "foo_bar", "predicate": "is"},
        score=0.95,
    )
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors([hit]), embedder=_FakeEmbedder()
    )
    elements = await builder._semantic("widget foo_bar", now=now)
    assert len(elements) == 1
    expected = effective_confidence(1.0, from_iso(old_iso), "tool", now=now)
    assert elements[0].confidence == pytest.approx(expected, abs=1e-6)
    assert elements[0].confidence != pytest.approx(0.8, abs=1e-6)
    assert elements[0].importance == pytest.approx(0.77, abs=1e-6)
    assert elements[0].importance != pytest.approx(0.5, abs=1e-6)
    assert elements[0].memory_domain == "tool"


async def test_hydration_db_failure_degrades_to_payload_snapshot(
    db: Database, monkeypatch: Any
) -> None:
    """Partial failure: DB explodes during hydration, tier still serves.

    Fallback is the payload snapshot plus a ``context.vector_search_failed``
    warning; the semantic tier must never be emptied by a hydration failure.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_id = await repo.add_fact(
        entity_id, "is", "foo_bar", domain="semantic", confidence=1.0, importance=0.5
    )
    hit = _Hit(
        fact_id,
        payload={
            "object_value": "foo_bar",
            "confidence": 0.9,
            "importance": 0.6,
            "predicate": "is",
            "memory_domain": "semantic",
        },
    )
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors([hit]), embedder=_FakeEmbedder()
    )

    async def _boom(sql: str, params: Any = ()) -> Any:
        raise RuntimeError("db exploded")

    monkeypatch.setattr(db, "fetch_all", _boom)
    with structlog.testing.capture_logs() as captured:
        elements = await builder._semantic("widget foo_bar")
    assert elements, "hydration failure must not empty the semantic tier"
    assert len(elements) == 1
    assert elements[0].confidence == pytest.approx(0.9, abs=1e-6)
    events = [e.get("event") for e in captured]
    assert "context.vector_search_failed" in events


async def test_unknown_domain_passthrough_not_crash(db: Database) -> None:
    """Hostile input: unknown memory_domain passes through, never crashes.

    Mirrors ``_to_fact``: stored value passthrough + ``memory.fact_unknown_domain``
    warning. The tier still serves the hit.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_id = await repo.add_fact(
        entity_id, "is", "foo_bar", domain="not_a_domain", confidence=0.9, importance=0.5
    )
    hit = _Hit(
        fact_id,
        payload={
            "object_value": "foo_bar",
            "confidence": 1.0,
            "importance": 0.5,
            "predicate": "is",
            "memory_domain": "not_a_domain",
        },
    )
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors([hit]), embedder=_FakeEmbedder()
    )
    with structlog.testing.capture_logs() as captured:
        elements = await builder._semantic("widget foo_bar", now=utc_now())
    assert len(elements) == 1
    # Passthrough: initial stored value, not decayed, not the payload 1.0.
    assert elements[0].confidence == pytest.approx(0.9, abs=1e-6)
    events = [e.get("event") for e in captured]
    assert "memory.fact_unknown_domain" in events


async def test_idempotent_same_now_same_derived(db: Database) -> None:
    """Idempotency: same search with the same injected now is deterministic."""
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_id = await repo.add_fact(
        entity_id, "is", "foo_bar", domain="semantic", confidence=1.0, importance=0.5
    )
    await _age_fact(db, fact_id, days=300)
    now = utc_now()
    hit = _Hit(
        fact_id,
        payload={
            "object_value": "foo_bar",
            "confidence": 1.0,
            "importance": 0.5,
            "predicate": "is",
            "memory_domain": "semantic",
        },
    )
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors([hit]), embedder=_FakeEmbedder()
    )
    first = await builder._semantic("widget foo_bar", now=now)
    second = await builder._semantic("widget foo_bar", now=now)
    assert len(first) == 1 and len(second) == 1
    assert first[0].confidence == pytest.approx(second[0].confidence, abs=1e-6)


async def test_batch_hydrate_one_query_per_search(db: Database, monkeypatch: Any) -> None:
    """Load: hydration is ONE batched query, not per-hit N+1.

    Thirty hits issue exactly one ``fetch_all`` for the whole batch. A per-hit
    ``get_fact`` loop would issue thirty and fail this test.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_ids = [
        await repo.add_fact(entity_id, "is", f"foo_bar_{i}", domain="semantic", confidence=1.0)
        for i in range(30)
    ]
    for fid in fact_ids:
        await _age_fact(db, fid, days=10)
    hits = [
        _Hit(
            fid,
            payload={
                "object_value": "foo_bar",
                "confidence": 1.0,
                "importance": 0.5,
                "predicate": "is",
                "memory_domain": "semantic",
            },
        )
        for fid in fact_ids
    ]
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors(hits), embedder=_FakeEmbedder()
    )
    calls: list[str] = []
    original = db.fetch_all

    async def _counting(sql: str, params: Any = ()) -> Any:
        calls.append(sql)
        return await original(sql, params)

    monkeypatch.setattr(db, "fetch_all", _counting)
    elements = await builder._semantic("widget foo_bar", now=utc_now())
    assert len(elements) == 30
    assert len(calls) == 1
    assert "WHERE id IN" in calls[0]


async def test_load_100x_hydration_query_count(db: Database, monkeypatch: Any) -> None:
    """Load 100x: the hydration cost stays linear in searches, not hits.

    100 identical searches over 5 hits issue exactly 100 hydration queries
    (one per search), never 500 (per-hit).
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fact_ids = [
        await repo.add_fact(entity_id, "is", f"foo_bar_{i}", domain="semantic", confidence=1.0)
        for i in range(5)
    ]
    for fid in fact_ids:
        await _age_fact(db, fid, days=10)
    hits = [
        _Hit(
            fid,
            payload={
                "object_value": "foo_bar",
                "confidence": 1.0,
                "importance": 0.5,
                "predicate": "is",
                "memory_domain": "semantic",
            },
        )
        for fid in fact_ids
    ]
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors(hits), embedder=_FakeEmbedder()
    )
    calls: list[str] = []
    original = db.fetch_all

    async def _counting(sql: str, params: Any = ()) -> Any:
        calls.append(sql)
        return await original(sql, params)

    monkeypatch.setattr(db, "fetch_all", _counting)
    now = utc_now()
    for _ in range(100):
        elements = await builder._semantic("widget foo_bar", now=now)
        assert len(elements) == 5
    assert len(calls) == 100


async def test_ranking_uses_derived_not_payload(db: Database) -> None:
    """Ranking truth: two hits with identical payload 1.0 order by derived.

    The fresh hit (queried now) outranks the stale hit (idle 300 days) even
    though both payloads claim 1.0 — ``relevance * confidence`` sees the
    derived values through the gatherer's ``_rank_key`` tiers.
    """
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", "Widget")
    fresh_id = await repo.add_fact(
        entity_id, "is", "foo_bar fresh", domain="semantic", confidence=1.0, importance=0.5
    )
    stale_id = await repo.add_fact(
        entity_id, "is", "foo_bar stale", domain="semantic", confidence=1.0, importance=0.5
    )
    await _age_fact(db, stale_id, days=300)
    now = utc_now()
    hits = [
        _Hit(
            stale_id,
            payload={
                "object_value": "foo_bar stale",
                "confidence": 1.0,
                "importance": 0.5,
                "predicate": "is",
                "memory_domain": "semantic",
            },
            score=0.95,
        ),
        _Hit(
            fresh_id,
            payload={
                "object_value": "foo_bar fresh",
                "confidence": 1.0,
                "importance": 0.5,
                "predicate": "is",
                "memory_domain": "semantic",
            },
            score=0.95,
        ),
    ]
    builder = PlannerContextBuilder(
        db=db, vector_store=_FakeVectors(hits), embedder=_FakeEmbedder()
    )
    elements = await builder._semantic("widget foo_bar", now=now)
    assert len(elements) == 2
    by_id = {e.id: e for e in elements}
    assert by_id[fresh_id].confidence > by_id[stale_id].confidence
    ordered = sorted(elements, key=lambda e: _rank_key(e, frozenset(), 0.85))
    assert ordered[0].id == fresh_id


async def test_vector_failure_still_returns_empty(db: Database) -> None:
    """Vector-store failure keeps the existing ``[]`` path unchanged."""

    class _ExplodingVectors:
        async def search(self, collection: str, vector: Any, limit: int = 30) -> Any:
            raise RuntimeError("vector store down")

    builder = PlannerContextBuilder(
        db=db, vector_store=_ExplodingVectors(), embedder=_FakeEmbedder()
    )
    with structlog.testing.capture_logs() as captured:
        elements = await builder._semantic("widget foo_bar")
    assert elements == []
    events = [e.get("event") for e in captured]
    assert "context.vector_search_failed" in events
