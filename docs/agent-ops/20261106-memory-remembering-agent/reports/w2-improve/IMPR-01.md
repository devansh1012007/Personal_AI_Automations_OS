<!-- ORCH-DIGEST:START -->
agent: W2-IMPR-01 | scope: T-003+T-004 wave2 findings R-W2-001..007 + TREV tests | status: DONE
findings: FIXED 10 | REJECTED 0 (with evidence) | DEFERRED 4
blockers_major_open: none
code_commits: 2c72f96 | report_commit: 8356876 (+ this bookkeeping commit)
tests_added_or_strengthened: 19 (5 agents, 3 coder, 3 api, 2 cli, 6 daemon; 2 daemon strengthened)
ownership_violations: none
confidence: high (packet-elements fix + preview + guard with fail-before proof; ruff clean, mypy=baseline 3)
<!-- ORCH-DIGEST:END -->

# Improvement report — W2-IMPR-01

## 1. Finding-by-finding disposition

| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| CODE R-W2-001 touch superset not packet.elements | MAJOR | FIXED | `src/paa/agents/context_agents.py` planner+worker now touch `[e.id for e in packet.elements if not e.id.startswith("graph:")]` after leak/hydration guards, before success return. Root cause: gatherer drops low-confidence/low-relevance/over-budget candidates before `packet.elements=tuple(selected)` [VERIFIED src/paa/context/gatherer.py:447-461,505-512,573]; touching pre-pack superset reset clocks for never-delivered facts. Graph ids excluded explicitly. New tests: low-confidence dropped stays 0 while kept advances; low-relevance semantic stays 0. Reversal proven: temp mutant touching `hot+semantic` fails new test `assert 1 == 0` [RAN limited pytest]. | code commit pending |
| CODE R-W2-002 first tick deletes permanently | MAJOR | FIXED (dry-run preview) | `MemoryCurator.run_maintenance(dry_run=False)` added [VERIFIED src/paa/memory/curator.py]; DECAY uses `sweep(dry_run=)`, PRUNE/CONSOLIDATE/ORPHANS count-only, RECONCILE/ANALYZE no-op in dry-run; `daemon._curate_preview_done` gates first tick dry-run, later ticks delete; `daemon.curation_started/completed` carry `dry_run` + preview `curator.dry_run_preview` warning. Archive still `None` because `Runtime` exposes no writer [VERIFIED src/paa/runtime.py fields, src/paa/daemon.py docstring] — documented, not fabricated. Reversal: `dry_run=False` mutant fails `test_first_tick_is_preview_then_deletes` (`assert 0 == 1` remaining) [RAN]. | code commit pending |
| CODE R-W2-003 import outside try | MINOR | FIXED | `from paa.memory.curator import MemoryCurator` moved inside construction `try` in `src/paa/daemon.py:250`; import failure now degrades to bare sweep + `daemon.curator_unavailable`. New test `test_import_failure_falls_back_to_sweep` via fake module `__getattr__` raising ImportError. | code commit pending |
| CODE R-W2-004 orphan empty-index wipe | MINOR | FIXED | Guard at top of `_phase_orphans`: `SELECT COUNT(*) FROM hot_serving_entity_index`; if 0, warn `curator.orphan_purge_skipped_empty_index` + skip [VERIFIED src/paa/memory/curator.py]. Regression test `test_orphan_purge_skips_when_entity_index_empty` (FK-off fact, empty index, full curator pass, remains 1). Reversal: `entity_count=1` mutant fails `assert 1 == 0` orphans_removed [RAN]. | code commit pending |
| CODE R-W2-005 budget 7200s false + wedge | MINOR | FIXED | Wording corrected in `daemon._run_curation_tick` docstring + `MemoryCurator.run_maintenance` docstring: budget checked *between* phases, worst case = budget + longest phase, not hard cap; resume in-memory only, restart resets to DECAY + previews again, zero/tiny budget stalls at same phase. Wedge recovery: invalid `start_phase` caught (`ValueError` → warn `curator.invalid_start_phase_reset`, reset to DECAY) instead of raising every tick. `dry_run` also logged. No worst-case claim retained. | code commit pending |
| CODE R-W2-006 raw str(exc) + missing None guard | MINOR | FIXED | `context_agents.py`, `coder/runtime_tools.py`, `cli/main.py` helpers now `error=redact(str(exc))` via lazy `from paa.models.base import redact` [VERIFIED src/paa/models/base.py:256]; `api/app.py` already redacted. All four helpers now `if db is None or not fact_ids: return`. Consistent policy, lazy import avoids top-level line shift. | code commit pending |
| CODE R-W2-007 4 helpers duplication | NIT | DEFERRED | Consolidation deferred: shared helper needs new module or edit to `src/paa/memory/decay.py` (READ-ONLY, outside WRITE-SET); creating new file outside WRITE-SET would violate ownership. Helpers now consistent (redact + None guard), drift closed without churn. Follow-up when line budget allows. | n/a |
| TREV MAJOR failed-recall no-touch | MAJOR | FIXED | Worker hydration (`test_worker_hydration_failure_touches_nothing`), worker leak (`test_worker_leak_raise_touches_nothing` with real fact id), API 400 (`test_search_400_touches_nothing` NUL), API 429 (`test_facts_429_touches_nothing` via `_admit` patch), coder NUL (`test_recall_nul_query_touches_nothing`), CLI NUL (`test_failed_search_touches_nothing`). All assert `use_count==0`. | code commit pending |
| TREV MAJOR daemon loop survival | MAJOR | FIXED | `test_daemon_loop_survives_exploding_curator`: `Daemon(maintenance_interval=0.01)`, exploding tick, task alive + calls≥2 after 0.15s. | code commit pending |
| TREV MAJOR batching | MAJOR | FIXED | `test_touch_batched_one_call_per_recall`: counting `touch_many` wrapper (delegates to real), 3 facts → 1 call with 3 deduped ids. Pins plan load contract. | code commit pending |
| TREV MINOR coder fallback | MINOR | FIXED | `test_recall_keyword_fallback_advances_use_count`: `remember_fact("Project Alpha is active")`, query `"active"` (entity miss, text hit) → `use_count==1`. | code commit pending |
| TREV MINOR no-retain_only | MINOR | FIXED | `_FakeVectors` docstring corrected + `test_maintenance_tick_without_retain_only_skips_reconcile`: count-only fake → `vectors_reconciled==0`, all 6 phases complete. | code commit pending |
| TREV MINOR empty-recall symmetry | MINOR | FIXED | API `test_empty_search_touches_nothing`, CLI `test_empty_search_touches_nothing`, coder `test_recall_empty_result_touches_nothing` (DB-state; spy distinction deferred as P3-11). | code commit pending |
| TREV MINOR 0.0-budget resume | MINOR | FIXED | `test_zero_budget_first_tick_resumes_on_second`: first real tick forced `budget_seconds=0.0` via subclass wrapper → `stopped_early_at==DECAY`, daemon stores DECAY; second tick (preview skipped) completes fully. | code commit pending |
| TREV P2 concurrency / empty-DB / phase-failure / ingestion-load | P2 | DEFERRED | Not in brief §3 MUST-DO (brief lists 4 MINOR items only). Concurrency harness, empty-DB tick, daemon-level phase-failure, concurrent ingestion+maintenance are valuable but need new timing/concurrency design; deferred to keep this wave scoped. No behavior changed there. | n/a |
| TREV P3 superseded-semantic / empty-spy-all / archive-explicit / replay-reset | P3 | DEFERRED | Per brief “defer P3 unless trivial”. P3-13 (no-retain) done as MINOR. Others documented but not added: superseded touch is retention-neutral + needs design lock; empty-spy distinction is cosmetic vs harmless `touch_many([])` no-op; archive-explicit just codifies None gap already in daemon docstring; replay-reset needs rebuild entry point + design decision. | n/a |

## 2. Code changes (per file, root-cause)

- `src/paa/agents/context_agents.py`: planner `handle()` + worker `handle()` source list changed from candidate superset to delivered `packet.elements` minus `graph:`; helper adds lazy `redact` and keeps `db is None` guard. Placement (after hydration/leak guards, before success) unchanged — fixes the delivered-vs-retrieved axis, not placement.
- `src/paa/coder/runtime_tools.py`, `src/paa/api/app.py`, `src/paa/cli/main.py`: helpers add `db is None` guard; coder/CLI add `redact(str(exc))` (API already had it). No call-site payload changes; coder/API/CLI touch sets already equaled returned sets.
- `src/paa/memory/curator.py`: `run_maintenance` gains `dry_run=False` (additive, default preserves all existing callers); `_phase_*` handlers gain `dry_run` param (default preserves direct-handler callers); orphan guard; invalid `start_phase` reset; budget docstring corrected; dry-run completion logs.
- `src/paa/daemon.py`: `__init__` adds `_curate_preview_done=False`; `_run_curation_tick` docstring corrected (between-phase budget, in-memory resume, wedge), import moved inside `try`, `dry_run` threaded to `run_maintenance`, `dry_run` logged in started/completed, flag set True after any successful curator return (construction-fallback does not consume preview).
- DECISION: dry-run preview over phase-limited first tick — preview reuses real counting SQL, gives exact would-delete volumes for R3 snapshot, and needs no new config; phase-limited would have required new `phases` param or separate sweeper call and still deleted decay on first tick. Dry-run deletes nothing on first tick.
- DECISION: lazy `redact` import inside helpers (existing `structlog` pattern) instead of top-level imports — avoids shifting doc citations while closing drift. INVARIANT: `touch_many` still takes deduped ids, one transactional `execute_many`, errors isolated (never raise into recall).

## 3. Test changes

Added 19 tests, strengthened 2 (`test_maintenance_tick_drives_all_curator_phases`, `test_maintenance_tick_works_without_vector_store` set `_curate_preview_done=True` to exercise full pass; preview itself pinned separately). All tests use real SQLite/Runtime fixtures, no mocks of unit under test, no sleeps except 0.15s loop-survival window (interval 0.01, deterministic call-count assert), no shared DB/ports.

- `tests/agents/test_context_agents.py` (+5): low-confidence drop, low-relevance semantic drop, worker hydration no-touch, worker leak no-touch, batching one-call.
- `tests/coder/test_runtime_tools.py` (+3): keyword-fallback touch, empty no-touch, NUL no-touch.
- `tests/api/test_memory.py` (+3): 400 no-touch, 429 no-touch, empty no-touch.
- `tests/cli/test_memory.py` (+2): empty no-touch, NUL no-touch.
- `tests/daemon/test_maintenance_loop.py` (+6): preview-then-deletes, orphan empty-index, import-fallback, loop survival, no-retain hook, zero-budget resume. `_FakeVectors` docstring corrected.

## 4. Optimizations applied and declined

- Applied: packet-elements touch shrinks write set by dropped tail (fewer `execute_many` rows per recall); orphan guard is one `COUNT(*)` per orphan phase (cheap vs wipe risk).
- Declined: cross-recall batching/write-behind (TREV #6 NOT WORTH IT — staleness/loss on crash for <1 round-trip); consolidate batch bound (pre-existing curator debt, now safer via preview + budget wording; needs design, not in fix scope); single shared helper (R-W2-007 deferred, ownership).

## 5. Commands run and results (scoped)

- [RAN .venv\Scripts\python.exe -m pytest tests/agents/test_context_agents.py tests/coder/test_runtime_tools.py tests/daemon/ tests/api/test_memory.py tests/cli/test_memory.py -q -> 142 passed (1 StarletteDeprecationWarning unrelated)] (final run; intermediate single-file runs also green)
- [RAN .venv\Scripts\python.exe -m ruff check src tests -> All checks passed!]
- [RAN .venv\Scripts\python.exe -m mypy src -> Found 3 errors in src\paa\sandbox\subprocess_backend.py:548 (killpg/getpgid/SIGKILL, win32) identical to baseline 00-mission.md; 0 new]
- Reversal evidence (temp mutants, restored after): R-W2-001 mutant (`hot+semantic`) fails `test_dropped_low_confidence_fact_is_not_touched` `assert 1 == 0`; R-W2-004 mutant (`entity_count=1`) fails `test_orphan_purge_skips_when_entity_index_empty` `assert 1 == 0`; R-W2-002 mutant (`dry_run=False`) fails `test_first_tick_is_preview_then_deletes` `assert 0 == 1` remaining. Import/loop/batching/failed-recall are construction/mutant guards (fail if guard/order/catch removed by inspection + DB asserts).
- Full suite `pytest tests/ -q` NOT run (scoped-check rule §16); docs-reference lock not re-run (already red on daemon.py/README, docs outside WRITE-SET).

## 6. Residual risks and anything the delta reviewer should check

- Pre-existing: decay-archive gap (`archive_writer=None`, evictions without cold-lake summary), in-memory resume loss on restart (now previews again), worst-case tick = budget + longest phase (operator must bound phase durations), `retain_only` absent on shipped stores (reconcile no-ops in prod until hook implemented). All documented in code; none introduced here.
- Reviewer should check: `dry_run` deliberate `%` — `sweep(dry_run=True)` still reports `evicted` as would-delete count (no delete, `archived==0`); preview `vectors_reconciled` stays 0 by design (no vector mutation in preview); fallback sweep on first-tick construction failure does delete decay (pre-existing behavior, new phases still previewed next tick because flag only set after successful curator return).
- `src/paa/daemon.py` + `curator.py` line shifts extend the already-red docs-reference lock; human re-point pass must read claims, not just re-number (AGENTS.md rule).
- Out-of-scope: none new. CONFLICT/NEEDED: none. `docs/` edits deferred to orchestrator pass per ownership.

## 7. Self-review / Long-run

- Source/Load/Hostile/Operability/Reversal: touch remains one transactional `execute_many` per recall, now over delivered set only; ids still DB-returned bound params; NUL/empty guards intact; daemon tasks still separate; rollback = revert code commit (no migration, no config).
- No band-aids: no blanket swallow (touch/curator catches remain scoped+logged), no sleeps/retries, no `Any` widening, no test weakening (existing full-tick tests explicitly opt out of preview rather than asserting preview as full), no TODO.
- Last COMMS line read before report: `[2026-11-06T18:51:53Z] W2-REV-01 -> ORCH | DONE ...` (line 34); no CONFLICT lines seen.

## 8. Commits

- code: 2c72f96 (11 files: 6 src + 5 tests, explicit paths)
- report: FILL_AFTER_REPORT_COMMIT (this file, docs-only)
