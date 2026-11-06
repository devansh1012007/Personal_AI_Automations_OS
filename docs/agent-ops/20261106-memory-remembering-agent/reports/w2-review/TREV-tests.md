<!-- ORCH-DIGEST:START -->
agent: W2-TREV-01 | scope: wave 2 / T-003 T04 + T-004 T05 | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 19 | fail-before/pass-after confirmed: 14 / reasoned: 5 / not-possible: 0
findings: BLOCKER 0 | MAJOR 3 | MINOR 3 | NIT 1
flaky_suspects: none
missing_edge_cases: P1 3 | P2 7 | P3 5
mutation_survivors: 4 of 11 plausible bugs not caught
changed_behavior_without_tests: failed-recall no-touch (worker hydration/leak, API 400/429), CLI/API empty-recall, coder keyword-fallback touch, _maintenance_loop survival after tick raises, touch batching (one touch_many per recall)
confidence: med (core fail-before + mutation a-e/g proven; loop survival, batching, failure-path no-touch not pinned)
<!-- ORCH-DIGEST:END -->

# Test quality review — W2-TREV-01

## 0. Method (temporary worktree used? repeat counts, coverage tooling)

- READ-ONLY on source; only this report file written. No repo test/source mutation.
- Verified diff via `git diff 0cdf531..HEAD -- <10 files>` (801 insertions, 10 deletions): 5 files under `src/paa/` (context_agents/api/cli/coder/daemon) + 5 test files. [VERIFIED diff stat above]
- Counting: collected exactly 19 new tests — agents 6, coder 2, api 3, cli 3, daemon 5. [RAN `--collect-only -q -> 6/3/3/2/5`]
- Pass/fail evidence:
  - [RAN `.venv\Scripts\python.exe -m pytest <19 scoped -q>` → 19 passed, 1 unrelated StarletteDeprecationWarning] (first pass)
  - [RAN same scoped command → 19 passed] (repeat 1), [RAN same → 19 passed] (repeat 2) — no flakiness in 3 runs.
  - [RAN broader scoped `tests/agents/test_context_agents.py tests/coder/test_runtime_tools.py tests/api/test_memory.py tests/cli/test_memory.py tests/daemon/ -q` → 123 passed] (114 T-003 files + 9 daemon incl. 4 pre-existing drain e2e). Zero failures citing wave-2 files; pre-existing 12 baseline failures in `00-mission.md` are outside these files and were not re-run (standing-order scoped-check limit).
- Fail-before: YES, temp worktree used. [RAN `git worktree add --detach <temp> 0cdf531`; copied only the 5 test files over; `PYTHONPATH=<temp>/src` + shared venv python]
  - T-003 overlay: 9 failed, 5 passed [RAN temp-worktree T-003 subset]. The 9 failures are the use_count advance tests (`assert 0 == 1` / confidence not increasing); the 5 passes are the isolation/empty guards (expected to pass both ways — see §1).
  - T-004 overlay: 5 failed [RAN temp-worktree `tests/daemon/test_maintenance_loop.py -q` → `AttributeError: 'Daemon' object has no attribute '_run_curation_tick'` ×5]. Every daemon test calls the new private seam absent at `0cdf531`.
  - Worktree removed after the run: [RAN `git worktree remove --force ...; git worktree list` → only main checkout]. No stash/checkout/clean used.
- No coverage tooling run (no plugin baseline assumed); branch coverage judged by reading the 5 test files and the 5 src diffs + the seams they exercise.
- COMMS last line read: `[2026-11-07T05:30:00Z] W2-IMPL-03 -> ORCH | FINDING | T-003 | ...` (33 lines total at read time, plus the later `DONE T-003` line at 18:42:54Z in the same file version).

## 1. Per test/suite assessment | Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes

All 19 assert DB/HTTP/CLI/report end-state, not internal call sequences, except the budget/resume stub (which asserts kwargs + stored phase — justified because the value only exists as a hand-off). No test mocks the unit under test (`handle`, `recall_facts.run`, routes, CLI `_do`, `_run_curation_tick` are all the real code); stubs replace only dependencies (vector/embedder/touch/curator).

### T-003 (14): `TestTouchOnRecall` (agents), `TestRecallTouch` (coder/api/cli)

| Test | Behavior? | Fails w/o fix? | Deterministic? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| agents `test_planner_hot_recall_advances_use_count_and_last_queried` | YES — DB row use_count=1, last_queried>old | PASS→FAIL pre-fix (0==1) | YES — 60d aged stamp, tmp DB | No | Core T04 proof for planner hot path |
| agents `test_planner_touch_makes_effective_confidence_reflect_use` | YES — effective_confidence(fresh) > (stale) | FAIL pre-fix (equal) | YES — same aging, real `effective_confidence` | No | Ties touch to decay contract, not just counters |
| agents `test_planner_semantic_recall_touches_the_hit_fact` | YES — hot∩semantic dedupe to 1 | FAIL pre-fix (0==1) | YES — deterministic fakes | Light fake (see §2) | Would catch dedupe removal (→2); proves semantic hit ids touchable |
| agents `test_worker_touches_only_the_facts_it_delivers` | YES — operational 1, narrative 0 | FAIL pre-fix (op 0==1) | YES | No | Security-boundary pin (INV-03 risk 1). Catches pre-filter touch mutant (narrative would be 1) |
| agents `test_empty_recall_touches_nothing` | YES (negative) | Passes both ways BY DESIGN | YES | No | Baseline guard; cannot alone distinguish early-return vs touch_many([]) no-op |
| agents `test_touch_failure_does_not_fail_the_recall` | YES — handle ok + value present | Passes both ways by design; fails if try/except removed | YES | No — `_boom` matches `touch_many(self, ids, **kw)` signature | If helper let the RuntimeError propagate this fails; good isolation pin |
| coder `test_recall_advances_use_count_and_last_queried` | YES — use_count=1, advanced stamp, count=1 | FAIL pre-fix (0==1) | YES — 60d aging | No | Entity-hit path only; keyword-fallback path untested (§6 P2) |
| coder `test_recall_touch_failure_returns_facts_unchanged` | YES — count=1 AND `"id" not in factors[0]` | Passes both ways (no-touch baseline also has no id); fails if raise propagates or if id added to payload | YES | No | Contract pin: no `id` added to serialized dicts (§3 d) |
| api `test_facts_endpoint_touches_returned_facts` | YES — 200 + use_count=1 + advanced | FAIL pre-fix (0==1) | YES | No | Live route via TestClient, real DB |
| api `test_search_endpoint_touches_returned_facts` | YES — 200 + use_count=1 | FAIL pre-fix (0==1) | YES | No | Keyword search path |
| api `test_touch_failure_does_not_fail_the_endpoint` | YES — 200 + list body | Passes both ways by design | YES | No | Payload-type check optional but harmless |
| cli `test_facts_command_touches_returned_facts` | YES — exit 0 + use_count=1 + advanced | FAIL pre-fix (0==1) | YES — `_age_facts` reopens DB deterministically | No | Real Typer invoke via `_invoke` |
| cli `test_search_command_touches_returned_facts` | YES — exit 0 + use_count=1 + advanced | FAIL pre-fix (0==1) | YES | No | Same |
| cli `test_touch_failure_does_not_fail_the_command` | YES — exit 0 + `"Acme Corp" in stdout` | Passes both ways by design | YES | No | Output-unchanged pin |

Summary T-003: 9 advance tests are true reversal tests (FAIL pre-fix → PASS post-fix). 5 isolation/empty tests pass both ways, which is correct for guards whose value is detecting regression of the swallow/early-return property, not the advance itself. No test asserts log text; no test spies on `touch_many` call counts (so batching is unproven — see R-W2-003).

### T-004 (5): `tests/daemon/test_maintenance_loop.py`

| Test | Behavior? | Fails w/o fix? | Deterministic? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_maintenance_tick_drives_all_curator_phases` | YES — real curator on seeded DB: all 6 phases completed, evicted=1/pruned=1/merged=1/orphans=1/reconciled=1, fake live set contains indexed fact, resume cleared | FAIL pre-fix (no `_run_curation_tick`) | YES — 3000d idle far from floors; no sleeps/ports; sqlite tmp | No — fake replaces external vector store only | Strong T05 proof; only weak point is `retain_only` does not exist on real stores (§2) |
| `test_maintenance_tick_works_without_vector_store` | YES — evicts, reconciled=0, RECONCILE phase listed | FAIL pre-fix | YES | No | Degrade pin; also the implicit `archive_writer=None` case (runtime exposes none) |
| `test_maintenance_tick_falls_back_to_sweep_when_curator_construction_fails` | YES — returns None, stale gone (bare sweep ran once), no raise | FAIL pre-fix | YES | Stub replaces `MemoryCurator` on construction — appropriate fault injection | Pins fallback-single-sweep |
| `test_maintenance_tick_propagates_curator_error_without_fallback` | YES — raises RuntimeError AND stale remains (no second sweep) | FAIL pre-fix | YES | Subclass of real curator, honest `run_maintenance` override | Pins no-double-run; does NOT pin loop survival (see R-W2-002) |
| `test_maintenance_tick_uses_configured_budget_and_resume_phase` | YES — budget≈7200, start_phase=ANALYZE forwarded, stored PRUNE_EDGES | FAIL pre-fix | YES — no timing dependence (approx on int seconds) | Stub `run_maintenance` returns synthetic report — narrowest possible stub, asserts hand-off not curator internals | Would catch budget=None/hardcoded drift and resume-reset mutants |

T-004 summary: all 5 are real-code-driven (tick is the real `_run_curation_tick`; DB is a real Runtime SQLite fixture identical to `test_drain_e2e.py`, `tmp_path` PAA_HOME, scripted model, `enable_optional_backends=False`). Restarting via `_maintenance_loop` is never exercised — every test calls the tick directly, with the loop's `except` + `_supervise` taken on faith (see §3 f).

## 2. Coverage of changed lines/branches, error paths, boundaries

**T-003 coverage vs diff:**

- `context_agents._touch_recalled`: None/[] early-return (empty test → [], None path untested — [ASSUMED] None never occurs in practice since `self._db` is always set; worker/planner always pass a real DB), dedupe (semantic-overlap test), success (hot test), touch-raises (isolation test). Log `debug/warning` lines unasserted (fine — observability, not contract). Worker post-filter touch: fully covered; planner hot+semantic touch: covered. **NOT covered:** failed-recall returns that bypass touch (worker `hydration_required` at `context_agents.py:363-375`, leak-raise at `377-386`) — the touch placement in `handle()` vs inside `_hot_facts` is exactly the claimed design decision and has no negative test. API `search_facts` ValueError→400 (`app.py:230-233`) and `_admit`→429 paths that skip touch likewise untested.
- `coder` helper: success + raise covered. Entity branch covered; keyword-fallback branch (`search_facts_by_text`, `runtime_tools.py:789-790`) not exercised by the new tests (existing tests cover the branch for correctness, but not its touch). **NOT covered:** empty coder recall (helper early-return copy in this file untested here; the None/[] guard is copy-pasted into 4 files but only exercised in the agents copy).
- `api` helper: success (facts + search) + raise covered. 400 path (`q` with NUL etc. raising ValueError before touch) and backpressure 429 (before touch) untested. `redact(str(exc))` in the warning untested (cosmetic).
- `cli` helper: success (facts + search) + raise covered. `if not rows: return` early paths (`main.py:535-538`, `558-561`) that skip touch untested for CLI (empty CLI recall performing no touch is only proven for agents).
- Batching: no test spies on `execute_many`/`touch_many` call count. A per-fact loop or N calls would pass every T-003 test. The brief explicitly requires "batched via one touch_many per recall" — the batching contract is unpinned.

**T-004 coverage vs diff:**

- Tick branches: success (2 tests), construction-fail fallback (1), run-fail propagate (1), budget/resume hand-off (1). Resume-clear on full pass asserted via `_curate_next_phase is None`. `archive_writer=getattr(..., None)` exercised implicitly in every test (Runtime exposes no writer) but never asserted as a value. `log.info` started/completed lines unasserted.
- **NOT covered:** `_maintenance_loop` itself (sleep/catch/loop, `daemon.py:166-178`) — no test drives the loop with a short `maintenance_interval` and an exploding curator to prove the daemon keeps ticking; `_supervise` restart (`daemon.py:98-108`) likewise unexercised. Empty-DB tick (zero facts/edges) not tested — curator `_phase_reconcile_vectors` early-returns on `not count` (`curator.py:288-289`) and the tick over an empty store is not proven to complete with all-zero counts. Curator-internal budget stop already has a pre-existing test (`test_budget_stops_the_pass_between_phases` in `tests/memory/test_curator.py:140`) — not in daemon scope and not a gap here.
- **Fake-contract drift (minor):** `_FakeVectors` in both the agents test and the daemon test models a subset of `VectorStore` [VERIFIED `src/paa/storage/vector/store.py:494-514`: `search(collection, query_vector, limit=10, score_threshold=None, filters=None)`, `count(collection)`]. Production calls use only those positional/keyword args (`_semantic` → `search("active_facts", vector, limit=limit)` at `context_agents.py:246`; curator → `count("active_facts")`), so the fakes match the exercised call sites. However `retain_only` exists on **no** real backend [VERIFIED grep `retain_only` over `src/` → only `curator.py:293` getattr] — the daemon main test proves the optional-hook wiring (`vectors_reconciled=1` with a fake), not that any shipped store reconciles. The `SearchHit` shape (`hit.id`, `hit.score`, `hit.payload`) matches `_semantic` usage; `embed` fake matches `await embedder.embed([goal]))[0]`.

## 3. Mutation thinking | Plausible bug | Caught? | By which test

| # | Mutation | Caught? | Evidence |
|---|---|---|---|
| a | Remove touch call entirely (all 4 _touch_recalled call sites) | YES — 9/9 advance tests | Temp-worktree pre-fix run: `assert 0 == 1` ×9 |
| b | Touch raises; recall must still return | YES | agents/coder/api/cli isolation tests each force `touch_many` raise and assert success payload; removing the helper try/except would fail all 4 |
| c | Worker touches pre-filter superset (narrative included) | YES | `test_worker_touches_only_the_facts_it_delivers` asserts narrative `use_count==0`. Moving touch into `_hot_facts` fails it |
| d | Coder touches serialized dicts (no `id`) → KeyError swallowed → no-op | YES | coder advance test asserts `use_count==1`; wrong-id mutant leaves 0 even though the exception is swallowed (helper catches broadly), so the test does catch it. Contract half also pinned by `"id" not in factors[0]` |
| e | Daemon curator call removed → sweep-only | YES | Main test asserts 6 phases + 4 non-decay counts + fake `retain_only` live set — a bare sweep produces none of these |
| f | Curator exception propagates and daemon dies | **PARTIAL / loop-level SURVIVOR** | Tick-level propagation + no-double-sweep pinned (propagates test). Loop-level mutant (delete `except` in `_maintenance_loop`, or supervisor exit) would still pass all 5 — loop is never driven. See R-W2-002 |
| g | Budget ignored (pass None / hardcoded) | YES at seam | Budget test asserts `captured["budget_seconds"] == approx(7200)`. Mutant passing `None` or a stale literal fails it. Curator-internal between-phase stop already covered pre-existing (`test_budget_stops_the_pass_between_phases`) |
| h | Dedupe removed (touch hot+semantic as concatenated list) | YES | Semantic-overlap test asserts `use_count==1`, mutant would produce 2 |
| i | N+1 touch (loop `touch` per fact instead of one `touch_many`) | **SURVIVOR** | No call-count spy; every use_count assertion still passes. Batching is the plan's load contract |
| j | Touch moved before guard (worker hydration/leak, API 400/429) | **SURVIVOR** | No failed-recall no-touch test; early touch still passes the delivered-only test in the success case |
| k | Planner also touches graph synthetic ids (`graph:…`) | SURVIVOR but harmless | No fact row matches, so silent SQL no-op; undetectable via DB state and retention-neutral. Not a risk, noted for completeness |
| l | Coder touch only on entity branch (fallback branch untouched) | **SURVIVOR** for fallback mutant | Existing coder test only hits the entity branch; a touch nested inside the `if entity is not None` branch would pass it |

Result: mutations a-e, g, h caught; f caught only at the tick seam; i, j, l plus loop-level f survive — 4 of 11 not caught.

## 4. Flakiness evidence (repeated runs) and test-suite health

- 3 consecutive runs of the 19 scoped tests, all passed [see §0]. Broader scoped run (123 tests including all callers' files + `tests/daemon/`) passed once — no regressions in neighbouring recall/daemon tests, and the 4 pre-existing `test_drain_e2e.py` ingestion tests stay green alongside the new tick file (no sibling interference symptom; W2-IMPL-03/W2-IMPL-04 write-sets are disjoint).
- Determinism design: 60-day (`context/api/coder/cli`) and 3000-day (daemon) idle clocks are far from any prune floor, avoiding boundary races; `last_queried_at > old` cannot flake on clock speed. No sleeps, no polling, no fixed ports, no subprocess; CLI tests reopen the DB via `asyncio.run` in `tmp_path` fixtures, function-scoped. The only timing-sensitive surface — budget approx — compares an integer product (`2.0*3600`), not wall time.
- Suite health signal: the single warning `StarletteDeprecationWarning` (testclient/httpx) is an environment chatter on the API tests, not a failure and not wave-2-caused. No skips, no xfails in the 19.

## 5. Findings (R-W<N>-nnn, severity, location, evidence, recommended fix)

- **R-W2-001 — MAJOR — `tests/agents/test_context_agents.py`, `tests/api/test_memory.py` (missing negative tests).** No test proves a *failed* recall touches nothing: worker `hydration_required` / leak-raise (`context_agents.py:363-386`) and API search `ValueError → 400` / `_admit → 429` paths. The entire handle-level placement rationale (IMPL-03 §4, INV-03 risk 1) rests on touch-after-guards, yet a mutant that touches before the guard passes all 19. **Fix:** seed facts, drive each failure path (e.g. worker `requires=["missing"]` forcing hydration failure; `GET /memory/search?q=<NUL>` → 400), assert `use_count==0` for every seeded fact.
- **R-W2-002 — MAJOR — `tests/daemon/test_maintenance_loop.py` (tick ≠ loop).** All 5 tests call `_run_curation_tick()` directly. Nothing drives `_maintenance_loop` to prove an exploding curator does not kill the daemon (loop-level `except` + `_supervise` restart at `daemon.py:98-108,166-178` are assumed). A loop mutant that deletes the catch passes all 5. **Fix:** construct `Daemon(runtime, maintenance_interval=0.01)`, monkeypatch curator to raise, run `_maintenance_loop` as a task with `_stop` set after 2–3 iterations, assert tick was attempted twice / loop exited via `_stop`, not via the exception.
- **R-W2-003 — MAJOR — all four T-003 files (batching unproven).** No spy asserts `touch_many` is invoked once per recall with the deduped list; a per-fact loop passes everything while breaking the plan's write-amplification contract. **Fix:** monkeypatch `DecaySweeper.touch_many` with a counting wrapper (delegate to the real method), run one planner recall over N facts, assert one call with N deduped ids.
- **R-W2-004 — MINOR — coder keyword-fallback branch.** New coder tests only hit `resolve_entity` success; `search_facts_by_text` fallback (`runtime_tools.py:789-790`) has no touch assertion. **Fix:** add a query that misses the entity tier (e.g. a fact whose text matches but whose entity name does not resolve) and assert touch on the fallback result.
- **R-W2-005 — MINOR — `tests/daemon/test_maintenance_loop.py:_FakeVectors` over-claims reconcile.** `retain_only` exists on no shipped `VectorStore` (only the curator's `getattr` hook at `curator.py:293`); `vectors_reconciled==1` proves hook wiring, not prod-store behaviour, and in production reconcile silently no-ops (`vectors_reconciled` stays 0). **Fix:** document in the test docstring that prod stores skip reconcile until they implement the hook; or add a no-`retain_only` fake test pinning `vectors_reconciled==0` with phases still completing. No behaviour change needed — this is a test-comment precision issue.
- **R-W2-006 — MINOR — per-surface empty-recall guards.** Only agents' empty test exists; api/cli/coder helper copies have independent `if not fact_ids: return` guards that are never exercised. A crash in one copy's empty path would pass the suite. **Fix:** mirror the empty-recall test for API (`GET /memory/search?q=nomatch` → touch not called / use_count stays 0) and CLI (`search` with no matches → exit 0, no touch). Low risk, cheap symmetry.
- **R-W2-007 — NIT — daemon test budget coverage.** `test_maintenance_tick_uses_configured_budget_and_resume_phase` asserts the hand-off; no daemon test shows a real budget stop (`stopped_early_at` from a genuine `run_maintenance` with tiny budget) resuming on the *next* tick. Pre-existing curator tests cover the stop itself; the daemon's second-tick resume from a real stop is unproven (the stub fabricates the report). **Fix:** run one real tick with `budget_seconds` monkeypatched to `0.0` via settings, assert `_curate_next_phase` is set, run a second tick that resumes there.

## 6. Missing edge cases and tests (prioritized) P1/P2/P3 with concrete scenarios

### P1 (add before relying on the guarantee)
1. **Failed recall must not touch.** Sketch: worker — seed 1 operational fact, `handle` worker message with `step.requires=["no-such-primitive"]` → assert `result.ok is False` and `use_count==0`. Leak path — construct a packet that triggers the narrative-leak `ValueError` (or monkeypatch `narrative_domains` to include the seeded domain) and assert the raise leaves clocks untouched. API — `GET /memory/search?q=%00` → 400, seeded fact's `use_count` unchanged. This closes mutation j and is the evidentiary backbone of the handle-level placement decision.
2. **Daemon loop survives an exploding curator.** Sketch: `Daemon(runtime, maintenance_interval=0.01)` + exploding `run_maintenance`; `asyncio.create_task(daemon._maintenance_loop())`; let the fake clock/sleep run ~3 iterations (no real 6h wait — inject interval); assert the task is still alive and the tick was attempted ≥2 times, then set `daemon._stop` and await. Catches mutation f at the loop level; guards the `except` at `daemon.py:174-177` and supervisor restart.
3. **Touch batching pinned (one call per recall).** Sketch: wrap `DecaySweeper.touch_many` with a counter delegating to the real method; planner recall returning 5 hot + 3 semantic (2 overlapping) facts → assert exactly 1 call with 6 deduped ids. Catches mutation i; also covers the dedupe list handed to `touch_many` rather than just the DB end-state.

### P2 (should add; gaps leave real regressions undetectable)
4. **Coder keyword-fallback touch.** Sketch: `remember_fact("Project Alpha is active")`, then `recall_facts` with a query that does not resolve to an entity but matches by text (e.g. `"active"`); assert `use_count==1` on the fallback fact. Closes mutation l.
5. **Concurrent recalls.** Sketch: `asyncio.gather` 10× `planner.handle(...)` over the same fact; assert `use_count==10` and `last_queried_at` advanced without `sqlite3.OperationalError: database is locked`. `touch_many`'s `use_count = use_count + 1` is atomic in SQL [VERIFIED `decay.py:271-275`], so this should pass; if it does not, the failure mode is lock contention on SQLite, which is precisely what operators need to know before the daemon era.
6. **Curator/daemon tick on empty DB.** Sketch: fresh runtime, no facts/edges; `await daemon._run_curation_tick()` → assert report non-None, `completed_fully`, all counts 0, all 6 phases completed. Pins that first-boot maintenance is a no-op rather than a crash (single most likely daemon-lifecycle regression shape).
7. **Curator phase failure mid-run at daemon level.** Sketch: monkeypatch one phase handler (e.g. `_phase_consolidate`) to raise; tick → assert report has `errors==1`, later phases still in `completed_phases`, daemon returns a report (not None, no raise). Pre-existing curator tests cover the curator half; the daemon half (tick does not degrade to the construction fallback) is the untested seam.
8. **Maintenance during ingestion load.** Sketch: start `daemon._maintenance_loop` (interval 0.05) + `daemon._ingestion_loop` task concurrently against seeded signals for ~1s, cancel cleanly; assert `run_batch` processed signals and a curation report completed, with no exception in either task. Structural separation (separate tasks) is the claim; no test exercises it. (Lower priority only because `test_drain_e2e.py` already proves ingestion is green with the tick present.)
9. **API 400/429 and CLI empty no-touch.** Sketch: 400 — invalid `q` (NUL) → 400 + no touch; 429 — force `_admit` false (mirror the existing `TestBackpressure` setup in `test_memory.py`) → 429 + no touch. CLI — `search` with no matches → exit 0, `use_count==0`. Documents that "failed/empty recall touches nothing" holds uniformly, not just for agents.

### P3 (nice-to-have / documentation-grade)
10. **Superseded semantic hits.** Sketch: seed a fact, supersede it, return it as a `_semantic` hit via the fake vector store; assert planner handle still succeeds and the superseded row's `use_count` increments (or does not — either way, retention-neutral because sweep filters `superseded_by IS NULL` [VERIFIED `decay.py:171-177`]). The value is pinning the *documented* decision that no validation query is issued, so a future "fix" that adds one must consciously update the test.
11. **Touch on empty result, all surfaces.** Covered for agents; extend the spy version (mutation: empty → `touch_many` never called) to api/cli/coder so the dead-code-looking early return in each copy is pinned individually. Distinguishes "didn't call" from "called with []" (today indistinguishable via DB state because `touch_many([])` also no-ops [VERIFIED `decay.py:268-269`]).
12. **`archive_writer=None` made explicit.** Sketch: after the main daemon test's tick, assert `report` counts show eviction happened with `archive_writer` resolved `None` (e.g. via the existing `_stale_fact` eviction + `facts_evicted==1` + no cold-lake summary row, or simply a comment pinning the degrade). This codifies — not fixes — the pre-existing archiving gap so a future writer-wiring change knows the expected before-state.
13. **Vector-store reconcile with a store lacking `retain_only`.** Sketch: fake with only `count` (no `retain_only`) → tick completes, `vectors_reconciled==0`, all phases completed. Mirrors production reality today (R-W2-005).
14. **`use_count` reset after hot-replay (derived state).** Sketch: seed fact, recall (use_count=1), then rebuild the hot tables from the cold lake (whatever rebuild entry point the repo uses), assert `use_count==0` and `last_queried_at` ≈ rebuild time, with a comment citing the derived-state exception (`_touch_recalled` docstrings). This *documents the data loss* as accepted behaviour; if the team wants replay-persistent retention clocks instead, this test is the tripwire that forces the design discussion. It must be written as a behaviour document, not as an aspiration.

Already covered (do NOT re-list as missing): `vector_store=None` degrade (daemon test 2), budget hand-off + resume threading (daemon test 5), touch-on-empty for agents, per-phase curator failure (pre-existing `test_curator.py`), curator budget stop (pre-existing `test_curator.py`).

## 7. What I could not verify

- Full-suite gate (`pytest tests/ -q`) NOT run — standing orders §16 scoped-check limit; baseline 12 Windows failures from `00-mission.md` not re-confirmed. No ruff/mypy run (read-only review; implementer reports record ruff clean / mypy = 3 pre-existing, not independently re-verified).
- No load test at scale, no concurrency harness (SQLite locking under parallel recalls is reasoned from `touch_many`'s SQL, not measured), no chaos/crash test (DB closed mid-recall / mid-tick).
- No production vector-store verification: `retain_only` is absent from shipped backends (grep evidence above), so whether prod reconcile ever does work cannot be verified from these tests — flagged as R-W2-005, not as a daemon failure.
- Cold-lake rebuild path for the P3 replay-reset sketch was not traced end-to-end (the rebuild entry point was not located in this scoped review); the sketch assumes an existing rebuild helper.
- Did not review `test_drain_e2e.py` internal assertions beyond confirming its 4 tests pass alongside the new file; ingestion-vs-maintenance concurrency remains a missing test (P2 #8).

## Long-run test (standing orders §8)

- **Source:** `touch_many` SQL `use_count = use_count + 1` + `last_queried_at` stamp [VERIFIED `decay.py:264-275`]; curator budget checked between phases [VERIFIED `curator.py:172-179`]; loop catch + supervisor [VERIFIED `daemon.py:98-108,166-178`].
- **Load (100×):** one `execute_many` per recall; N+1 mutant uncaught (R-W2-003). Concurrency unmeasured (P2 #5). Daemon tick idempotent-ish: re-running phases on already-pruned data yields zeroes — main test's exact counts would fail loudly on double-execution, so double-sweep mutants are caught.
- **Hostile input:** ids are DB-returned strings bound as parameters; NUL/empty upstream refusals are untouched and untested by the new tests (they pass through the same code paths as before). No new LIKE, no new input surface in the tick.
- **Operability:** daemon logs carry budget + `summary()` counts every tick (T-004 tests assert the report those logs print, not the log lines themselves — acceptable). Resume state is in-memory only; restart resets to DECAY — known limitation, documented in the impl reports, not a test failure.
- **Reversal:** reverting 3506e30 fails 9/14 T-003 tests; reverting 70dd91f fails 5/5 T-004 tests — both confirmed by the temp-worktree runs in §0. The 5 pass-both-ways tests are reversal-proof only for the swallow/empty property, which is the correct design for guards.

## Out-of-scope findings

- None beyond those in §7 and the two pre-existing docs-reference lock failures (`src/paa/daemon.py` line shifts from W2-IMPL-04, README hygiene) reported by both implementers and not re-litigated here. No new COMMS warranted: no CONFLICT with a peer agent (all scoped files green in 3 runs); R-W2-002/R-W2-003 are test-completeness items for the orchestrator to queue, not CONFLICT lines.

## 8. Verification of implementer claims (claims audit)

- W2-IMPL-03 claims (114 scoped = 100 + 14 new; pre-fix 9 failed `assert 0 == 1`): CONFIRMED — recount 14, broader run 123 (100 T-003 files + 5 new daemon + 4 drain e2e + pre-existing daemon count arithmetic agrees), temp-worktree pre-fix 9 failed / 5 passed with the same assertions.
- W2-IMPL-04 claims (5 new; 9 passed in `tests/daemon/`; fail-without-fix via missing `_run_curation_tick`): CONFIRMED — 5 counted, `tests/daemon/` green in the broader run, worktree run shows AttributeError ×5.
- Both reports' "no other file edited / docs deferred" claims: not re-verified beyond the diff stat showing the expected 10 files (docs edits would appear in the stat and do not).
