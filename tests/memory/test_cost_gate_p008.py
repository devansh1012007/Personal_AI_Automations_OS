"""Advisory memory-cost gate — PLAN:integration-fix:P008 (docs/adr/0040).

The curator ANALYZE phase emits deprecation SUGGESTIONS when the advisory
total cost crosses ``cost_gate.threshold``. The suggestion never deletes,
never quarantines, never marks stale — the fact stays in the hot set.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import structlog

from paa.config import reset_settings_cache
from paa.memory.curator import CurationPhase, MemoryCurator
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database


def _enable_gate(
    monkeypatch: pytest.MonkeyPatch, threshold: str = "1.0"
) -> None:
    monkeypatch.setenv("PAA_COST_GATE__ENABLED", "true")
    monkeypatch.setenv("PAA_COST_GATE__THRESHOLD", threshold)
    reset_settings_cache()


def _disable_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAA_COST_GATE__ENABLED", "false")
    reset_settings_cache()


async def _fact(
    db: Database,
    repo: FactRepository,
    entity: str,
    pred: str,
    val: str,
    *,
    confidence: float = 1.0,
    importance: float = 0.5,
) -> str:
    eid = await repo.upsert_entity("project", entity)
    return await repo.add_fact(
        eid, pred, val, confidence=confidence, importance=importance
    )


class TestCostGateDisabled:
    async def test_disabled_emits_no_suggestions_and_calls_no_cost(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _disable_gate(monkeypatch)
        repo = FactRepository(db)
        await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        import paa.context.metrics as metrics_mod

        calls: list[tuple[float, float, float, float, float]] = []
        real = metrics_mod.total_memory_cost

        def counting(*args: float) -> float:
            calls.append(args)  # type: ignore[arg-type]
            return real(*args)  # type: ignore[arg-type]

        monkeypatch.setattr(metrics_mod, "total_memory_cost", counting)
        report = await MemoryCurator(db).run_maintenance()
        assert report.cost_gate_suggestions == []
        assert calls == []

    async def test_default_disabled_is_inert(
        self, db: Database, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No env set: CostGateSettings.enabled defaults False.
        repo = FactRepository(db)
        await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        report = await MemoryCurator(db).run_maintenance()
        assert report.cost_gate_suggestions == []


class TestCostGateUtilityZero:
    async def test_zero_utility_suggests_and_fact_remains_hot(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        repo = FactRepository(db)
        fid = await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        with structlog.testing.capture_logs() as logs:
            report = await MemoryCurator(db).run_maintenance()
        assert len(report.cost_gate_suggestions) == 1
        suggestion = report.cost_gate_suggestions[0]
        assert suggestion["fact_id"] == fid
        assert suggestion["total_cost"] == math.inf
        assert suggestion["threshold"] == pytest.approx(1.0)
        assert any(entry["event"] == "cost_gate.suggestions" for entry in logs)
        # The fact stays in the hot set — cost never deletes.
        remaining = await db.fetch_value(
            "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert remaining == 1

    async def test_analyze_only_suggests(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        repo = FactRepository(db)
        fid = await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        report = await MemoryCurator(db).run_maintenance(start_phase=CurationPhase.ANALYZE)
        assert report.cost_gate_suggestions[0]["fact_id"] == fid


class TestCostGateThreshold:
    async def test_finite_total_above_threshold_suggests(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        repo = FactRepository(db)
        await _fact(db, repo, "Alpha", "status", "fresh", confidence=1.0, importance=1.0)
        report = await MemoryCurator(db).run_maintenance()
        # N=1 -> storage 1, use_count 0 -> retrieval 1, total 2 >= 1.
        assert len(report.cost_gate_suggestions) == 1

    async def test_finite_total_below_threshold_is_quiet(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "3.0")
        repo = FactRepository(db)
        await _fact(db, repo, "Alpha", "status", "fresh", confidence=1.0, importance=1.0)
        report = await MemoryCurator(db).run_maintenance()
        assert report.cost_gate_suggestions == []

    async def test_empty_hot_set_is_quiet(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        report = await MemoryCurator(db).run_maintenance()
        assert report.cost_gate_suggestions == []


class TestCostGateIdempotency:
    async def test_same_cost_twice_same_suggestion_no_delete(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        repo = FactRepository(db)
        await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        first = await MemoryCurator(db).run_maintenance()
        second = await MemoryCurator(db).run_maintenance()
        assert first.cost_gate_suggestions == second.cost_gate_suggestions
        assert len(second.cost_gate_suggestions) == 1
        count = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert count == 1

    async def test_dry_run_still_suggests_writes_nothing(
        self,
        db: Database,
        paa_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _enable_gate(monkeypatch, "1.0")
        repo = FactRepository(db)
        fid = await _fact(db, repo, "Alpha", "status", "fresh", importance=0.0)
        report = await MemoryCurator(db).run_maintenance(dry_run=True)
        assert len(report.cost_gate_suggestions) == 1
        remaining = await db.fetch_value(
            "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (fid,)
        )
        assert remaining == 1
