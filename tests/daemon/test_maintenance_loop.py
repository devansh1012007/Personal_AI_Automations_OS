"""Daemon maintenance loop drives MemoryCurator (C04 / T-004, test T05).

Proves one daemon maintenance tick runs all curator phases — decay,
prune-edges, consolidate, orphans, reconcile-vectors, analyze — under the
existing curation budget, instead of a bare DecaySweeper sweep. Also pins
the R3 degrade rules: curator construction failure falls back to the sweep,
while a run_maintenance failure propagates (no silent double-run), and a
None vector store still allows the sweep.

Reversal: reverting the daemon to bare DecaySweeper.sweep() makes
test_maintenance_tick_drives_all_curator_phases fail (consolidate / edges /
orphans / reconcile never run via the tick).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.daemon import Daemon
from paa.memory.curator import CurationPhase
from paa.memory.facts import FactRepository
from paa.runtime import Runtime
from paa.storage.relational.database import to_iso, utc_now

pytestmark = pytest.mark.asyncio


class _ScriptedModel:
    """Stand-in model so Runtime.build never touches a provider."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {}


@pytest.fixture
async def runtime(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real Runtime against an isolated tmp_path SQLite DB, no backends."""
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=_ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


async def _stale_fact(rt: Runtime, entity: str) -> str:
    """Insert one fact idle long enough to be below its prune floor."""
    repo = FactRepository(rt.db)
    eid = await repo.upsert_entity("project", entity)
    fid = await repo.add_fact(eid, "status", "stale", confidence=1.0)
    await rt.db.execute(
        "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
        (to_iso(utc_now() - timedelta(days=3000)), fid),
    )
    return fid


async def _weak_plus_strong_edges(rt: Runtime) -> None:
    repo = FactRepository(rt.db)
    a = await repo.upsert_entity("project", "EdgeA")
    b = await repo.upsert_entity("project", "EdgeB")
    stamp = to_iso(utc_now())
    for weight in (0.05, 0.5):
        await rt.db.execute(
            "INSERT INTO hot_serving_relationships "
            "(id, from_entity_id, to_entity_id, rel_type, weight, valid_from) "
            "VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), a, b, "DEPENDS_ON", weight, stamp),
        )
        stamp = to_iso(utc_now() + timedelta(microseconds=1))


async def _duplicates(rt: Runtime) -> None:
    repo = FactRepository(rt.db)
    eid = await repo.upsert_entity("project", "DupEntity")
    await repo.add_fact(eid, "status", "active", confidence=0.9)
    await repo.add_fact(eid, "status", "active", confidence=0.7)


async def _orphan(rt: Runtime) -> None:
    """Insert one fact whose entity does not exist (FK disabled)."""
    import aiosqlite

    orphan_entity = str(uuid.uuid4())
    now = to_iso(utc_now())
    path = rt.db.path
    raw = await aiosqlite.connect(path, isolation_level=None)
    try:
        await raw.execute("PRAGMA foreign_keys=OFF")
        await raw.execute(
            "INSERT INTO hot_serving_active_facts "
            "(id, entity_id, predicate, object_value, memory_domain, "
            "initial_confidence, importance, created_at, last_queried_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), orphan_entity, "p", "v", "semantic", 1.0, 0.5, now, now),
        )
        await raw.commit()
    finally:
        await raw.close()


class _FakeVectors:
    """Minimal vector store exposing count/retain_only for reconcile.

    Production note (REV R-W2-005): no shipped ``VectorStore`` implements
    ``retain_only`` today — this fake proves the curator's optional-hook
    wiring, not that any shipped store reconciles. With a store lacking the
    hook, reconcile silently no-ops (``vectors_reconciled == 0``) and the
    pass still completes; the no-hook case is pinned by
    ``test_maintenance_tick_without_retain_only_skips_reconcile``.
    """

    def __init__(self) -> None:
        self.last_live: set[str] | None = None

    async def count(self, name: str) -> int:
        return 2

    async def retain_only(self, name: str, live: set[str]) -> int:
        self.last_live = set(live)
        return 1


async def test_maintenance_tick_drives_all_curator_phases(runtime: Runtime) -> None:
    """T05: one tick runs decay + prune + consolidate + orphans + reconcile + analyze."""
    await _stale_fact(runtime, "StaleEntity")
    await _weak_plus_strong_edges(runtime)
    await _duplicates(runtime)
    await _orphan(runtime)

    # One indexed fact so reconcile has a live set to retain.
    repo = FactRepository(runtime.db)
    eid = await repo.upsert_entity("project", "IndexedEntity")
    fid = await repo.add_fact(eid, "status", "indexed", confidence=1.0)
    await runtime.db.execute(
        "UPDATE hot_serving_active_facts SET embedding_status = 'indexed' WHERE id = ?",
        (fid,),
    )

    fake = _FakeVectors()
    runtime.vector_store = fake
    daemon = Daemon(runtime)
    # Skip the first-tick dry-run preview so this test exercises the full
    # deleting pass; preview behavior is pinned by
    # test_first_tick_is_preview_then_deletes.
    daemon._curate_preview_done = True

    report = await daemon._run_curation_tick()

    assert report is not None
    assert report.completed_fully
    assert set(report.completed_phases) == set(CurationPhase)
    assert report.facts_evicted == 1
    assert report.edges_pruned == 1
    assert report.duplicates_merged == 1
    assert report.orphans_removed == 1
    assert report.vectors_reconciled == 1
    assert fake.last_live is not None and fid in fake.last_live
    # Resume state is cleared after a full pass.
    assert daemon._curate_next_phase is None


async def test_maintenance_tick_works_without_vector_store(runtime: Runtime) -> None:
    """Daemon boots and ticks with vector_store=None (degraded reconcile)."""
    assert runtime.vector_store is None
    await _stale_fact(runtime, "NoVectorStale")
    daemon = Daemon(runtime)
    # Skip preview so this test exercises the full deleting pass.
    daemon._curate_preview_done = True

    report = await daemon._run_curation_tick()

    assert report is not None
    assert report.facts_evicted == 1
    assert report.vectors_reconciled == 0
    assert CurationPhase.RECONCILE_VECTORS in report.completed_phases


async def test_maintenance_tick_falls_back_to_sweep_when_curator_construction_fails(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Construction failure degrades to bare sweep (never raises, no double-run)."""
    stale_id = await _stale_fact(runtime, "FallbackStale")

    import paa.memory.curator as curator_mod

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("curator wiring exploded")

    monkeypatch.setattr(curator_mod, "MemoryCurator", _boom)
    daemon = Daemon(runtime)

    report = await daemon._run_curation_tick()

    assert report is None
    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 0  # bare sweep still evicted the stale fact


async def test_maintenance_tick_propagates_curator_error_without_fallback(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run_maintenance error propagates (loop logs it) — no double sweep."""
    stale_id = await _stale_fact(runtime, "NoDoubleRunStale")

    import paa.memory.curator as curator_mod

    real_curator = curator_mod.MemoryCurator

    class _Exploding(real_curator):  # type: ignore[misc]
        async def run_maintenance(self, **kwargs: Any) -> Any:
            raise RuntimeError("maintenance exploded")

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Exploding)
    daemon = Daemon(runtime)

    with pytest.raises(RuntimeError, match="maintenance exploded"):
        await daemon._run_curation_tick()

    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 1  # no fallback sweep double-ran


async def test_maintenance_tick_uses_configured_budget_and_resume_phase(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget comes from settings (2.0h -> 7200s); resume threads start_phase."""
    import paa.memory.curator as curator_mod
    from paa.memory.curator import CurationReport

    captured: dict[str, Any] = {}

    class _Capturing(real := curator_mod.MemoryCurator):  # type: ignore[misc]
        async def run_maintenance(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return CurationReport(
                started_at=utc_now(),
                finished_at=utc_now(),
                stopped_early_at=CurationPhase.PRUNE_EDGES,
            )

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Capturing)
    daemon = Daemon(runtime)
    daemon._curate_next_phase = CurationPhase.ANALYZE

    report = await daemon._run_curation_tick()

    assert captured["budget_seconds"] == pytest.approx(7200.0)
    assert captured["start_phase"] is CurationPhase.ANALYZE
    assert report is not None
    assert daemon._curate_next_phase is CurationPhase.PRUNE_EDGES
    assert captured["dry_run"] is True  # first tick previews


async def test_first_tick_is_preview_then_deletes(runtime: Runtime) -> None:
    """REV R-W2-002: the first tick previews (no deletes), the next deletes."""
    stale_id = await _stale_fact(runtime, "PreviewStale")
    await _weak_plus_strong_edges(runtime)
    # Weak edge would be pruned by a full tick.
    daemon = Daemon(runtime)

    preview = await daemon._run_curation_tick()

    assert preview is not None
    # Preview reports would-delete counts but deletes nothing.
    assert preview.facts_evicted >= 1
    assert preview.edges_pruned >= 1
    assert preview.vectors_reconciled == 0
    assert daemon._curate_preview_done is True
    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 1
    weak = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_relationships WHERE weight < 0.10"
    )
    assert weak == 1

    real = await daemon._run_curation_tick()

    assert real is not None
    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 0
    weak = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_relationships WHERE weight < 0.10"
    )
    assert weak == 0


async def test_orphan_purge_skips_when_entity_index_empty(runtime: Runtime) -> None:
    """REV R-W2-004: an empty entity index must not wipe all facts."""
    import aiosqlite

    from paa.memory.curator import MemoryCurator

    orphan_entity = str(uuid.uuid4())
    now = to_iso(utc_now())
    path = runtime.db.path
    raw = await aiosqlite.connect(path, isolation_level=None)
    try:
        await raw.execute("PRAGMA foreign_keys=OFF")
        fact_id = str(uuid.uuid4())
        await raw.execute(
            "INSERT INTO hot_serving_active_facts "
            "(id, entity_id, predicate, object_value, memory_domain, "
            "initial_confidence, importance, created_at, last_queried_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (fact_id, orphan_entity, "p", "v", "semantic", 1.0, 0.5, now, now),
        )
        await raw.commit()
    finally:
        await raw.close()

    assert await runtime.db.fetch_value("SELECT COUNT(*) FROM hot_serving_entity_index") == 0
    report = await MemoryCurator(runtime.db).run_maintenance()
    assert report.orphans_removed == 0
    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (fact_id,)
    )
    assert remaining == 1


async def test_import_failure_falls_back_to_sweep(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV R-W2-003: an importer failure still degrades to the bare sweep."""
    import sys
    import types

    stale_id = await _stale_fact(runtime, "ImportFailStale")

    class _FailingModule(types.ModuleType):
        def __getattr__(self, name: str) -> Any:
            raise ImportError("curator module broken")

    fake = _FailingModule("paa.memory.curator")
    monkeypatch.setitem(sys.modules, "paa.memory.curator", fake)
    daemon = Daemon(runtime)

    report = await daemon._run_curation_tick()

    assert report is None
    remaining = await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 0


async def test_daemon_loop_survives_exploding_curator(runtime: Runtime) -> None:
    """TREV P1: the maintenance loop retries when the tick raises."""
    import asyncio

    daemon = Daemon(runtime, maintenance_interval=0.01)
    calls = 0

    async def _exploding() -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("maintenance exploded")

    monkeypatch_wait = daemon._run_curation_tick
    daemon._run_curation_tick = _exploding  # type: ignore[method-assign]
    task = asyncio.create_task(daemon._maintenance_loop())
    try:
        await asyncio.sleep(0.15)
        assert calls >= 2
        assert not task.done()
    finally:
        daemon._stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    assert monkeypatch_wait is not None  # silence unused


async def test_maintenance_tick_without_retain_only_skips_reconcile(
    runtime: Runtime,
) -> None:
    """A store lacking the retain_only hook completes with 0 reconciled."""

    class _NoHookVectors:
        async def count(self, name: str) -> int:
            return 1

    await _stale_fact(runtime, "NoHookStale")
    runtime.vector_store = _NoHookVectors()
    daemon = Daemon(runtime)
    daemon._curate_preview_done = True

    report = await daemon._run_curation_tick()

    assert report is not None
    assert report.vectors_reconciled == 0
    assert set(report.completed_phases) == set(CurationPhase)


async def test_zero_budget_first_tick_resumes_on_second(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real 0.0-budget tick stops at phase one; the next tick resumes there."""
    import paa.memory.curator as curator_mod

    real_curator = curator_mod.MemoryCurator
    first_calls = 0

    class _FirstZero(real_curator):  # type: ignore[misc]
        async def run_maintenance(self, **kwargs: Any) -> Any:
            nonlocal first_calls
            first_calls += 1
            if first_calls == 1:
                kwargs["budget_seconds"] = 0.0
            return await super().run_maintenance(**kwargs)

    monkeypatch.setattr(curator_mod, "MemoryCurator", _FirstZero)
    daemon = Daemon(runtime)
    # First tick previews; keep preview but force the zero budget.
    first = await daemon._run_curation_tick()
    assert first is not None
    assert first.stopped_early_at is CurationPhase.DECAY
    assert daemon._curate_next_phase is CurationPhase.DECAY

    daemon._curate_preview_done = True
    second = await daemon._run_curation_tick()
    assert second is not None
    assert second.completed_fully
    assert daemon._curate_next_phase is None
