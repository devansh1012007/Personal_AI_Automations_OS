"""The fact/entity repository: idempotent entities, versioned facts, decayed reads."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paa.memory.domains import MemoryDomain
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, to_iso, utc_now


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
        fid = await repo.add_fact(eid, "status", "active", domain=MemoryDomain.SEMANTIC, confidence=1.0)
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
        fid = await repo.add_fact(eid, "status", "stale", domain=MemoryDomain.SEMANTIC, confidence=1.0)
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
