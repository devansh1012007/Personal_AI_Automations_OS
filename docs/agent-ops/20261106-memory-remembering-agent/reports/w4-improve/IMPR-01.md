<!-- ORCH-DIGEST:START -->
agent: W4-IMPR-01 | scope: Wave 4 review findings + 3 stale daemon tests | status: DONE
findings: FIXED 11 | REJECTED 0 (with evidence) | DEFERRED 2 | TOTAL 13
blockers_major_open: none
code_commits: d7801a8 | report_commit: pending
tests_added_or_strengthened: 8 new, 5 strengthened
ownership_violations: none
confidence: high (scoped memory+daemon green, ruff clean, mypy baseline 3, reversal run in pre-fix worktree)
last_comms_line_read: [2026-11-06T20:54:30Z] W4-IMPL-08 -> ORCH | FINDING | T-007 (COMMS.md:53)
<!-- ORCH-DIGEST:END -->

# Improvement report — W4-IMPR-01

## 1. Finding-by-finding disposition
| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| Gate stale: construction-failure fallback | MAJOR (gate) | FIXED | `tests/daemon/test_maintenance_loop.py::test_maintenance_tick_falls_back_to_sweep_when_curator_construction_fails` + e2e `test_construction_failure_fallback_respects_preview` now drive two real ticks, assert preview-then-delete; src consumes preview (R-W4-001) [VERIFIED src/paa/daemon.py:280-290] [RAN post-fix 11 daemon + 18 e2e passed; pre-fix worktree fails with _curate_preview_done False is True] | code |
| Gate stale: import-failure fallback | MAJOR (gate) | FIXED | `test_import_failure_falls_back_to_sweep` same two-tick preview contract; [RAN daemon 11 passed post-fix, 2 failed pre-fix] | code |
| Gate stale: orphan `test_orphan_purge_skips_when_entity_index_empty` | MAJOR (gate) | FIXED | Chose correct-the-assertion (keep test, update docstring). NULL-provenance dummy is import dust, so refined guard sweeps it: `orphans_removed 0->1`, remaining 1->0 [VERIFIED src/paa/memory/curator.py:332-368 provenanced guard; tests/memory/test_curator.py:138 test_empty_index_skips_provenanced_facts owns the skip leg] | code |
| R-W4-001 fallback preview never consumed | MAJOR | FIXED | Daemon consumes preview after successful fallback dry-run; permanent breakage still warns every tick via `daemon.curator_unavailable` + sweep would-delete counts. Test drives two real ticks, no manual flip [VERIFIED daemon.py:277-290] | code |
| R-W4-002 single erroring phase blocks deletes forever | MINOR | FIXED (document + loud warning + test) | Kept `errors==0` gate (safe preview-forever). Evidence: curator isolates per phase (`report.errors+=1`, continue [VERIFIED curator.py:214-219]) but `CurationReport` has no per-phase fields [VERIFIED curator.py:72-85], so daemon cannot know which phase errored; relaxing to `completed_fully`-only could delete never-previewed phases. Added `daemon.curation_preview_incomplete` WARNING on every incomplete preview + docstring trade-off. Test proves two errored ticks keep fact + flag False + warning. | code |
| R-W4-003 pending_count COUNT every drain incl. idle | MINOR | FIXED | `0 if not pending else COUNT` — same predicate, count is 0 by definition [VERIFIED creator.py:1515-1525 vs facts.py:1066-1074]. Spy tests prove idle skips, backlog still COUNTs. [RAN pre-fix worktree idle spy fails 1 vs 0] | code |
| R-W4-004 outcome asymmetry IGNORED vs PROCESSED | MINOR | FIXED | Added `outcome="processed"` to PROCESSED INFO line (additive, prefer direction). Both branches share `outcome/facts/entities/dropped/drop_counts`. [VERIFIED creator.py:1171-1212] [RAN pre-fix worktree outcome None vs processed] | code |
| R-W4-005 e2e bypasses drain path | NIT | FIXED | New `test_drain_backlog_then_recall`: write without sink -> pending + semantic miss -> restart -> drain -> indexed -> `_semantic` hit. Proves backlog leg. [RAN passes post-fix; synchronous e2e still passes] | code |
| R-W4-006 failure-path gauge | NIT | FIXED | `embedding_drain_failed` now COUNTs once (cold path) and logs `pending_count` + derived `backlog_remaining`. [VERIFIED creator.py:1537-1551] [RAN pre-fix worktree pending_count None vs 2] | code |
| T-01 e2e bypasses `_build_memory_creator` threading | MINOR | FIXED (option i) | New `test_runtime_threaded_creator_paraphrase_retrieved_after_restart` builds via `paa.runtime._build_memory_creator` with real store+embedder, asserts sink identity, indexed, restart hit. Docstring narrowed to creator-level + threaded pin. [VERIFIED runtime.py:433-493 threading] | code |
| T-02 single-candidate ranking untested | MINOR | FIXED | New `test_paraphrase_decoy_ranking`: Aurora/status/swift vs Borealis/status/swift (subject-change avoids contradiction quarantine which filters by entity_id [VERIFIED contradiction.py:129-145]), asserts top id/content + `relevance` strict order. Catches inverted sort. | code |
| P1 §6 second item drain->recall | P1 | FIXED | Same test as R-W4-005 (one test satisfies both). | code |
| T-03 stale note "no vector sink wired" in runtime_tools.py | MINOR | DEFERRED-DEPENDENCY | Out of write-set (runtime_tools.py READ-ONLY). One-line fix: change `note` text to keyword-only boundary note; owner updates + flexible `note` existence assertion added to test 2 (`assert "note" in payload`, not stale text). NEED posted. | code (assertion only) |
| T-04 dims hardcoded 384 | NIT | FIXED | Added `assert embedder.dimensions == ACTIVE_FACTS.dimensions` in e2e/threaded/decoy/backlog tests (legible failure if spec changes). [VERIFIED store.py:121-126 384] | code |
| T-05 failure gauge | NIT | FIXED | Same as R-W4-006. | code |
| Docs-refs breakage from creator +1 line | gate | DEFERRED-DEPENDENCY | Two citations now blank (`creator.py:1453`, `creator.py:1273-1300` from the `outcome` line shift). Docs outside write-set; NEED posted. No docs edited. | NEED |

## 2. Code changes (per file, with rationale; root-cause level, no band-aids)
- `src/paa/daemon.py` (~+36 lines in `_run_curation_tick` docstring + fallback/flag blocks):
  - R-W4-001: after `await DecaySweeper(...).sweep(dry_run=dry_run)`, `if dry_run: self._curate_preview_done=True`. Raising sweep propagates and never consumes (no swallow). Each fallback tick already warns `daemon.curator_unavailable` + `decay.sweep_completed` would-delete counts, so permanent breakage stays loud.
  - R-W4-002: `elif dry_run: log.warning("daemon.curation_preview_incomplete", ...)` with `completed_fully/errors/stopped_early_at`. No gate change; docstring documents why `errors==0` stays (no per-phase fields, relaxing risks unpreviewed deletes). Budget-stop resume via `_curate_next_phase` is distinct and still completes.
  - Also replaced stale "bare sweep" docstring with dry-run-aware wording (REV §6/§8 nit).
- `src/paa/memory/creator.py` (~+12 lines):
  - R-W4-004: PROCESSED `memory.signal_processed` gains `outcome="processed"` (one-line addition before `facts=...`).
  - R-W4-003: idle branch `pending_count = 0 if not pending else await self._pending_embedding_count()` + comment + docstring idle-skip note. Constant SQL, same predicate, no new query when empty.
  - R-W4-006: failure branch COUNTs once then logs `pending_count` + `backlog_remaining=pending_count>0`. Cold path, one indexed aggregate, no amplifier.
  - No try/except added, no type bypass, no schema/config/API change.

## 3. Test changes (added edge cases, strengthened assertions, flake fixes)
- Strengthened existing:
  - `test_construction_failure_fallback_respects_preview`: two real ticks, flag assert, no manual flip (R-W4-001). Fails pre-fix without src fix.
  - `test_maintenance_tick_falls_back_to_sweep_when_curator_construction_fails` and `test_import_failure_falls_back_to_sweep`: two-tick preview-then-delete, flag assert.
  - `test_orphan_purge_skips_when_entity_index_empty`: docstring + `1/0` expectations per refined guard.
  - `test_recall_facts_synonym_only_miss`: `assert "note" in payload` + truthy (T-03 flexible pin).
  - `test_e2e_paraphrase_retrieved_after_restart`: dims assert (T-04).
- Added (8) in `tests/memory/test_e2e_remember_recall.py`:
  - `test_processed_emits_outcome_processed` (R-W4-004), `test_drain_failure_line_carries_pending_count` (R-W4-006), `test_idle_drain_skips_pending_count` spy + `test_pending_count_gauge_when_sink_absent_counts_backlog` spy companion (R-W4-003), `test_errored_preview_blocks_deletes_and_warns` two-tick preview-forever + warning (R-W4-002), `test_runtime_threaded_creator_paraphrase_retrieved_after_restart` (T-01), `test_paraphrase_decoy_ranking` (T-02), `test_drain_backlog_then_recall` (R-W4-005/P1).
- P2/P3 from TREV (superseded filter parity, payload confidence/importance, partial sink return, combined partial+errored) NOT implemented — out of brief P1 scope; gap noted in §6.

## 4. Optimizations applied and evidence (and ones declined, with reason)
- Applied: R-W4-003 idle COUNT skip (see §1). Evidence: spy counts 0 idle, 1 with backlog; gauge values unchanged (0 idle, 2 backlog). Cost removed: one indexed COUNT per ~1 Hz idle daemon drain.
- Applied: R-W4-006 failure COUNT (adds 1 query on cold error path for operator parity; no idle cost).
- Declined: relax `errors==0` to `completed_fully`-only (per §1 evidence, risks unpreviewed deletes without per-phase fields; needs curator.py change out of write-set).
- Declined: move COUNT into `facts.py` repository method (REV §7 #3 NOT-WORTH-IT, parity churn).
- Declined: delete orphan duplicate (kept with corrected assertion so daemon suite documents the W3 delta; `test_curator` owns the skip leg).

## 5. Commands run and results (scoped)
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py -q -> 18 passed]` (10 pre-existing + 8 new)
- `[RAN .venv\Scripts\python.exe -m pytest tests/daemon/test_maintenance_loop.py -q -> 11 passed]` (3 gate stale now green)
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/memory/ tests/daemon/ -q -> all passed]` (brief scope)
- `[RAN .venv\Scripts\python.exe -m ruff check src/paa/daemon.py src/paa/memory/creator.py tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py -> All checks passed!]`
- `[RAN .venv\Scripts\python.exe -m mypy src/paa/daemon.py src/paa/memory/creator.py -> Success: no issues found in 2 source files]`; `[RAN mypy src -> 3 errors in src/paa/sandbox/subprocess_backend.py:548]` (baseline 3, unchanged)
- Reversal (pre-fix worktree @ c8892df, PYTHONPATH pinned, new tests copied in):
  - e2e file: `13 passed, 5 failed` — failures exactly `test_construction_failure_fallback_respects_preview` (False is True), `test_processed_emits_outcome_processed` (None vs processed), `test_drain_failure_line_carries_pending_count` (None vs 2), `test_idle_drain_skips_pending_count` ([1] vs []), `test_errored_preview_blocks_deletes_and_warns` (0 vs 1 warning)
  - daemon file: `9 passed, 2 failed` — both fallback preview-consumption tests (False is True); orphan corrected test passes pre-fix because curator Wave3 guard already swept (proves the fix is daemon-flag-only)
- `[RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 4 passed, 1 failed]` — `creator.py:1453` + `creator.py:1273-1300` blank from +1 `outcome` line (DEFERRED-DEPENDENCY, NEED posted; docs READ-ONLY). Daemon line shifts did not trigger blank blanks.

## 6. Residual risks and anything the delta reviewer should check
- DEFERRED-DEPENDENCY (NEED posted): `src/paa/coder/runtime_tools.py:807-810` one-line note fix (`"semantic recall unavailable: facts are not embedded (no vector sink wired)"` is false post-C01; should state keyword-only boundary). Test only pins `note` existence.
- DEFERRED-DEPENDENCY (NEED posted): `docs/data/persistence-and-storage.md:102` + `docs/memory-polish/plans/Plan1.md:203` creator citations shifted by the `outcome` line; owner must re-derive both. The `path:line` lock cannot see content-skew citations (daemon docstring wording may still cite post-shift lines that are non-blank but wrong — reviewer should read cites near daemon 233-320 and creator 1200-1560).
- Delta reviewer should check: (1) spy tests monkeypatch `_pending_embedding_count` (private) — acceptable in-test precedent (`_sink/_extractor` in sibling runtime tests), but confirm no mypy/ruff looseness; (2) `SimpleNamespace()` adapter in T-01 relies on empty `extraction_model` default (no adapter use [VERIFIED config.py:279]) — brittle if default ever changes; (3) decoy uses Borealis subject-change to dodge contradiction quarantine — verify ordering is stable across runs (bag hash + exact cosine, no ties); (4) R-W4-002 keeps preview-forever for any erroring phase including vector-reconcile outage (which self-increments errors [VERIFIED curator.py:413-414]) — operator must fix the phase to resume deletes; warning makes this loud but retention still stalls.
- Long-run: Source — root-cause at call sites (flag consumption, warning, idle skip, additive key, cold COUNT). Load — removes ~1 Hz idle COUNT, adds cold error COUNT; no N+1, no unbounded fetch. Hostile — new SQL constant, no interpolation/LIKE; synonym hash input only. Operability — rollback = revert commits, no migration; observe via `pending_count` on INFO+ERROR, `outcome` on both branches, `curation_preview_incomplete` warning. Reversal — 5 e2e + 2 daemon tests fail pre-fix as above; no other tests flipped.
- Not verified: full suite, live Qdrant, real embedder fidelity (fake proves wiring only).

## 7. Commits
code: d7801a8 | report: pending
