"""The nightly curator: maintenance phases, the wall-clock budget, and the
hardcoded refusal to auto-resolve contradictions."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from paa.memory.curator import CurationPhase, MemoryCurator
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, to_iso, utc_now


@pytest.fixture
def repo(db: Database) -> FactRepository:
    return FactRepository(db)


async def _fact(db: Database, repo: FactRepository, entity: str, pred: str, val: str, *, idle_days: float = 0, conf: float = 1.0) -> str:
    eid = await repo.upsert_entity("project", entity)
    fid = await repo.add_fact(eid, pred, val, confidence=conf)
    if idle_days:
        await db.execute(
            "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
            (to_iso(utc_now() - timedelta(days=idle_days)), fid),
        )
    return fid


class TestDecayPhase:
    async def test_evicts_stale_facts(self, db: Database, repo: FactRepository) -> None:
        await _fact(db, repo, "Alpha", "status", "fresh", idle_days=1)
        await _fact(db, repo, "Beta", "status", "stale", idle_days=3000)

        report = await MemoryCurator(db).run_maintenance()

        assert report.facts_evicted == 1
        assert report.completed_fully


class TestEdgePruning:
    async def test_prunes_weak_edges(self, db: Database, repo: FactRepository) -> None:
        a = await repo.upsert_entity("project", "A")
        b = await repo.upsert_entity("project", "B")
        now = to_iso(utc_now())
        for weight in (0.05, 0.5):
            await db.execute(
                "INSERT INTO hot_serving_relationships "
                "(id, from_entity_id, to_entity_id, rel_type, weight, valid_from) "
                "VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), a, b, "DEPENDS_ON", weight, now),
            )
            # distinct valid_from to satisfy the unique constraint
            now = to_iso(utc_now() + timedelta(microseconds=1))

        report = await MemoryCurator(db).run_maintenance()

        assert report.edges_pruned == 1  # only the 0.05 edge
        remaining = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_relationships")
        assert remaining == 1


class TestConsolidation:
    async def test_exact_duplicates_merge(self, db: Database, repo: FactRepository) -> None:
        eid = await repo.upsert_entity("project", "Alpha")
        await repo.add_fact(eid, "status", "active", confidence=0.9)
        await repo.add_fact(eid, "status", "active", confidence=0.7)
        await repo.add_fact(eid, "status", "active", confidence=0.5)

        report = await MemoryCurator(db).run_maintenance()

        assert report.duplicates_merged == 2
        survivors = await db.fetch_all(
            "SELECT use_count FROM hot_serving_active_facts WHERE superseded_by IS NULL"
        )
        assert len(survivors) == 1

    async def test_differing_values_are_never_merged(self, db: Database, repo: FactRepository) -> None:
        """Same entity+predicate, different value = contradiction, NOT duplicate.
        The curator must leave these for a human (RFC §2.1 agent 9)."""
        eid = await repo.upsert_entity("project", "Alpha")
        await repo.add_fact(eid, "colour", "blue", confidence=0.9)
        await repo.add_fact(eid, "colour", "red", confidence=0.9)

        report = await MemoryCurator(db).run_maintenance()

        assert report.duplicates_merged == 0
        rows = await db.fetch_value(
            "SELECT COUNT(*) FROM hot_serving_active_facts WHERE superseded_by IS NULL"
        )
        assert rows == 2, "the curator auto-resolved a contradiction — it must not"


class TestOrphans:
    async def test_orphan_sweep_removes_danglers(self, db: Database, repo: FactRepository) -> None:
        """A fact whose entity is gone must be swept.

        The schema's FK is ``ON DELETE CASCADE``, so orphans do not arise
        normally — they only appear from a cross-backend import that did not
        enforce the FK. We reproduce that here by dropping the FK constraint on
        a throwaway connection (``PRAGMA foreign_keys`` only takes effect
        outside a transaction, which the pooled write connection is between
        statements), inserting a dangling fact, then running the sweep.
        """
        import aiosqlite

        orphan_entity = str(uuid.uuid4())
        now = to_iso(utc_now())
        raw = await aiosqlite.connect(db.path, isolation_level=None)
        try:
            await raw.execute("PRAGMA foreign_keys=OFF")
            await raw.execute(
                "INSERT INTO hot_serving_active_facts "
                "(id, entity_id, predicate, object_value, memory_domain, initial_confidence, "
                " importance, created_at, last_queried_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), orphan_entity, "p", "v", "semantic", 1.0, 0.5, now, now),
            )
            await raw.commit()
        finally:
            await raw.close()

        report = await MemoryCurator(db).run_maintenance()
        assert report.orphans_removed == 1


class TestBudget:
    async def test_budget_stops_the_pass_between_phases(self, db: Database, repo: FactRepository) -> None:
        """A zero-length budget must stop before any phase runs and record where."""
        report = await MemoryCurator(db).run_maintenance(budget_seconds=0.0)
        assert not report.completed_fully
        assert report.stopped_early_at is CurationPhase.DECAY
        assert report.completed_phases == []

    async def test_resume_from_a_later_phase(self, db: Database, repo: FactRepository) -> None:
        report = await MemoryCurator(db).run_maintenance(start_phase=CurationPhase.ANALYZE)
        assert CurationPhase.ANALYZE in report.completed_phases
        assert CurationPhase.DECAY not in report.completed_phases


class TestRobustness:
    async def test_a_failing_phase_does_not_abort_the_pass(
        self, db: Database, repo: FactRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        curator = MemoryCurator(db)

        async def boom(report, *, now):  # noqa: ANN001, ANN202
            raise RuntimeError("phase exploded")

        monkeypatch.setattr(curator, "_phase_prune_edges", boom)
        report = await curator.run_maintenance()

        assert report.errors == 1
        # Later phases still ran despite the one failure.
        assert CurationPhase.ANALYZE in report.completed_phases
