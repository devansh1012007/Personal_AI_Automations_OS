"""P006 — FactRepository grouped counts (T-006-P006)."""

from __future__ import annotations

from paa.memory.domains import MemoryDomain
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database


class TestGroupedCounts:
    async def test_empty_table_returns_zeros(self, db: Database) -> None:
        repo = FactRepository(db)
        counts = await repo.count_facts_by_domain()
        assert counts == {"total": 0, "by_domain": {}, "by_status": {}}
        assert counts["total"] == 0

    async def test_counts_group_by_domain_and_status(self, db: Database) -> None:
        repo = FactRepository(db)
        eid = await repo.upsert_entity("project", "Alpha")
        await repo.add_fact(eid, "status", "active", domain=MemoryDomain.SEMANTIC)
        await repo.add_fact(eid, "status", "live", domain=MemoryDomain.SEMANTIC)
        await repo.add_fact(eid, "status", "done", domain=MemoryDomain.EPISODIC)
        # Mark one fact embedded so by_status spans two statuses.
        pending = await repo.pending_embeddings()
        await repo.mark_embedded([pending[0].id])

        counts = await repo.count_facts_by_domain()
        assert counts["total"] == 3
        assert counts["by_domain"] == {"semantic": 2, "episodic": 1}
        # Fresh facts are pending; one was marked indexed.
        assert counts["by_status"].get("pending", 0) == 2
        assert counts["by_status"].get("indexed", 0) == 1

    async def test_superseded_rows_excluded_by_default(self, db: Database) -> None:
        repo = FactRepository(db)
        eid = await repo.upsert_entity("project", "Alpha")
        v1 = await repo.add_fact(eid, "status", "planning")
        v2 = await repo.add_fact(eid, "status", "active")
        await repo.supersede(v1, v2)

        live = await repo.count_facts_by_domain()
        assert live["total"] == 1
        assert live["by_domain"] == {"semantic": 1}

        everything = await repo.count_facts_by_domain(include_superseded=True)
        assert everything["total"] == 2
