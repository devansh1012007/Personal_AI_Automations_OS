<!-- ORCH-DIGEST:START -->
agent: FINAL-REV-01 | scope: whole-branch Waves 1-4 (eb6606e..HEAD, src/tests/docs excl. agent-ops) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 3 | NIT 2
claims_audit: 14 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: stale coder note misstates sink state to model; fallback sweep floor divergence (injected vs global settings); e2e paraphrase bypasses touch so use-count proof is indirect
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: done (code-read + scoped runs; no pre-fix worktree re-run)
confidence: high
<!-- ORCH-DIGEST:END -->

# Final correctness review — FINAL-REV-01

## 0. Method
Diff reviewed: `git diff eb6606e24d33f56b1dd7d2d811110b77940b8318..HEAD -- src/ tests/ docs/` (48 files per brief, +5933/−471 budget; stat shows 126 files incl. run folder). Blind pass on src diff first (creator/drain, daemon preview, curator/dry-run, decay/floor, config, touch paths, runtime wiring), then work-ledger + gate-w4-2 + spot checks. Scoped runs only per standing orders. Read COMMS.md through line 59.

## 1. Blind-pass findings (before implementer reports)
- Drain/preview disjoint: `drain_pending_embeddings` (creator.py:1490) never reads/writes `_curate_preview_done` (daemon.py:62,265); preview only gates curator delete phases + fallback sweep. No shared mutable state. Concurrent drain-vs-decay races resolve to no-op UPDATE / orphan-vector-till-next-reconcile. Correct.
- Counters/IGNORED coherent: Wave-3 `_snapshot_drop_counts` (creator.py:1714) + per-task ContextVar (creator.py:489,862) feeds both Wave-4 `signal_processed` branches (IGNORED creator.py:1171, PROCESSED creator.py:1205) with shared `outcome/facts/dropped/drop_counts`. No double-count; batch aggregates same fields (creator.py:1251). Correct.
- Delivered-only touch coherent except e2e shape: planner/worker `handle()` touch `packet.elements` minus `graph:` (context_agents.py:328,394) — correct. E2E `test_e2e_paraphrase_retrieved_after_restart` calls `_semantic` directly, bypassing `handle()`/touch (see FINAL-03).
- No merge artifacts found: no conflict markers, no whitespace-only churn, hooks wired once each (embed sink runtime.py:334, drain 3 callers daemon/api/cli, touch 5 paths + helper, curator tick daemon.py:200).
- Config floor path has divergence only on fallback (see FINAL-02).
- Stale coder note is factually wrong post-C01 (see FINAL-01).

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| (A1) Remember→recall by paraphrase later run | YES | [VERIFIED tests/memory/test_e2e_remember_recall.py:test_e2e_paraphrase_retrieved_after_restart] real NumpyVectorStore + synonym embedder, close/reopen, `_semantic("rapid state aurora")` hits `swift` with content; [RAN pytest test_e2e_remember_recall.py + test_maintenance_loop.py -> 29 passed] |
| (A2) No permanently-pending backlog after drain + counter | YES | [VERIFIED creator.py:1490 drain + creator.py:1565 COUNT] + `pending_count` gauge; [RAN test_creator.py -> 50 passed] incl. backlog drain + failure gauge |
| (A3) Recall touches use_count/last_queried_at, confidence reflects use | YES (indirect via handle, see FINAL-03) | [VERIFIED context_agents.py:328, api/app.py:210, cli/main.py:540, coder/runtime_tools.py:792 all call DecaySweeper.touch_many decay.py:305]; [RAN tests/agents/test_context_agents.py + tests/api/test_memory.py -> 46 passed] |
| (A4) Curator phases on schedule + observable logs | YES | [VERIFIED daemon.py:170 maintenance loop -> _run_curation_tick daemon.py:200 -> curator.run_maintenance curator.py:141 with budget/resume/dry_run]; logs `daemon.curation_started/completed`, `curation_preview_incomplete` |
| (A5) Gate stays zero (ruff 0, mypy baseline 3, pytest vs baseline) | YES | [VERIFIED gates/gate-w4-2.md ruff 0, mypy 3 win32 pre-existing]; [RAN ruff 10 files -> All checks passed; mypy 10 files -> Success]; conventions 5 passed [RAN test_docs_references.py -> 5 passed] |
| C01b payload alignment (write object_value/confidence/importance) | YES | [VERIFIED creator.py:1073-1086] writes reader keys; [VERIFIED context_agents.py:254-257] reads them with 0.8/0.5 defaults for legacy points |
| C02 drain idempotent, never terminal `failed`, limit 100 | YES | [VERIFIED creator.py:1490-1563] early return on limit≤0, stays pending on sink error, idle COUNT skip R-W4-003 (predicate parity facts.py:1066 vs creator.py:1567) |
| D3 touch all recall (not coder-only), D5 deterministic ranking unchanged | YES | 5 paths touched (planner, worker, coder, API×2, CLI×2) [VERIFIED grep _touch_recalled 12 hits]; no LLM rerank added; gatherer unchanged |
| D4 curator scheduled in daemon, no cron double-run | YES | ledger A-002 re-verified Q1; [VERIFIED daemon.py:179 calls _run_curation_tick] |
| C05 counters + I/we→user, protocol-compatible | YES | [VERIFIED creator.py:475,758,594 counter increments + first-person mapping]; LLM parity [VERIFIED creator.py:919+]; [RAN test_llm_extractor.py -> 29 passed] incl. concurrent-drain counter test |
| C06 explicit-only floor + deprecated decay_lambda | YES | [VERIFIED config.py:281 explicit_prune_floor via model_fields_set, config.py:295 warn validator; decay.py:168 constructor forward; curator.py:135 forwards injected settings] |
| W4 preview discipline (fallback/no-error gates) | YES | [VERIFIED daemon.py:279 fallback sweep dry_run=dry_run + consume on success only; daemon.py:302 `completed_fully and errors==0`; daemon.py:304-319 warning] |
| Runtime threading pinned (not just helper) | YES | [VERIFIED runtime.py:334 threads vector_store/embedder into _build_memory_creator; tests/runtime/test_memory_creator.py TestRuntimeBuildEmbeddingSink + e2e test_runtime_threaded_creator...] [RAN test_memory_creator.py -> 13 passed] |
| Docs citations accurate (conventions green gate-w4-2) | YES | [RAN test_docs_references.py -> 5 passed]; spot-checked creator.py:1462 `_enqueue_embeddings` + daemon.py:127 dispatch match updated docs claims |

## 3. Long-run test
- **Source:** fixes at source (sink payload keys, drain COUNT, touch on delivered set, explicit-floor forwarding, preview gates) — no symptom patches, no blanket try/catch (each except logs + continues by design, drain/ingestion isolation intentional).
- **Load (100x):** drain 1 batch/call @ ~1Hz (limit 100), COUNT only when pending non-empty or on failure; `touch_many` single `execute_many`, not N+1. No unbounded fetch/loop. Daemon budget checked between phases; worst-case budget+longest phase documented (daemon.py:212). No leak.
- **Hostile input:** no new SQL interpolation/LIKE (COUNT constant, bound params); `_touch_recalled` ids are DB-returned strings, touch UPDATE bound. Extractor containment (NUL/escape/caps) untouched by C05 beyond counters. No injection surface added.
- **Operability:** no migration (parity lock untouched); rollback = revert commits. Observe via `memory.embedding_drain` INFO (batch/indexed/pending_count), `memory.embedding_drain_failed` ERROR, `signal_processed` outcome/dropped, `daemon.curation_completed` summary + `curation_preview_incomplete` WARNING. Permanent phase error = loud preview-forever (safe, documented R-W4-002).
- **Reversal:** payload keys → e2e content empty; sink wiring → `indexed` fails; drain → backlog test pending; touch → use_count tests fail; preview gates → daemon fallback/preview tests fail; counters → drop-count tests fail; floor → explicit-floor tests fail. (Relied on per-wave pre-fix evidence + code read; no separate pre-fix worktree run here.)

## 4. Findings

### FINAL-01 [MINOR] Stale coder recall note misstates sink state (cross-wave C01 leftover)
- Location: `src/paa/coder/runtime_tools.py:807-810` · Evidence: note text `"semantic recall unavailable: facts are not embedded (no vector sink wired)"` returned on every `_RecallFacts` call, yet C01 wired `VectorEmbeddingSink` (runtime.py:334) and e2e proves sink+semantic live. W4-IMPR-01 posted NEED (COMMS line 55) but one-line fix sat outside write-sets; e2e only pins `note` existence (test_recall_facts_synonym_only_miss), not content. · Why it matters: every coder recall tells the model the sink is absent — false after C01, undermines trust, contradicts success-criterion-1 narrative; facts still correct so not MAJOR. · Recommended fix: replace with keyword-only disclosure per T08 decision (e.g. "keyword recall; semantic recall is planner-side over embedded vectors") and assert that substring in the e2e test.

### FINAL-02 [MINOR] Fallback sweep bypasses injected prune floor (config source divergence)
- Location: `src/paa/memory/decay.py:175-177` + `src/paa/daemon.py:279` · Evidence: curator path forwards `self._settings.explicit_prune_floor()` (curator.py:135); fallback constructs bare `DecaySweeper(self._rt.db)` which reads global `get_settings().memory`. Production both equal (runtime = global singleton), but injected-Settings tests/deployments silently mix sources. · Why: an operator setting `PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR` explicitly via runtime Settings but not global (or vice-versa in tests) changes eviction set only on fallback ticks. · Recommended fix: pass `prune_confidence_floor=settings.explicit_prune_floor()` explicitly in the fallback, so both ticks read the same source (one-line, removes divergence).

### FINAL-03 [MINOR] E2E paraphrase bypasses touch — use-count proof is indirect
- Location: `tests/memory/test_e2e_remember_recall.py:test_e2e_paraphrase_retrieved_after_restart` + `test_drain_backlog_then_recall` · Evidence: both call `builder._semantic(...)` directly (no `handle()`), which returns elements without calling `_touch_recalled` (touch lives in `handle()`, context_agents.py:328). No assertion on `use_count`/`last_queried_at` after paraphrase hit. Touch-on-semantic is proved separately at handle level, but the flagship e2e does not close criterion (A3) end-to-end. · Why: a regression that removed touch from the planner handle would still pass the e2e flagship. · Recommended fix: add one integrated assertion — drive `PlannerContextBuilder.handle()` with the paraphrase goal and assert `use_count+1` + `last_queried_at` advanced for the delivered fact.

### FINAL-04 [NIT] Duplicated `_touch_recalled` helpers (4 copies, divergent signatures)
- Location: `src/paa/agents/context_agents.py:403`, `src/paa/api/app.py:346`, `src/paa/cli/main.py:699`, `src/paa/coder/runtime_tools.py:884` · Evidence: same dedup + try/log/touch logic; only context_agents types `RelationalBackend|None`, others `Any`. · Why: fix in one (e.g. redaction, dedup) may miss others; convention drift risk. · Recommended fix (DO-LATER): single helper in `paa.memory.decay` (e.g. `touch_recalled(db, ids)`) called by all four. Not a behaviour bug.

### FINAL-05 [NIT] Drain `limit` never plumbed; reconcile preview reports 0
- Location: `src/paa/memory/creator.py:1490` + callers daemon.py:165, api/app.py:282, cli/main.py:647 (all use default 100); `src/paa/memory/curator.py:394` `if dry_run: return` without counting. · Evidence: API `limit` controls `run_batch`, not drain; operator cannot tune drain batch. Preview `vectors_reconciled` always 0, so preview totals understate would-be work (minor log honesty gap). · Why: low impact (default 100 reasonable; preview still safe). · Recommended fix (DO-LATER, NOT-WORTH-IT now): document default; optionally count would-reconcile in preview or log a distinct flag. Do not block merge.

## 5. Attempts to break it (concrete inputs/interleavings)
1. Two concurrent drains + 150 pending, limit 100: both fetch overlapping head (no locking on drain) → both enqueue same ids, both `mark_embedded` (idempotent UPDATE), second drain's COUNT sees post-mark truth. No double-index harm (upsert idempotent), no fact loss. Safe.
2. Sink fails + fact deleted by concurrent decay between fetch and `mark_embedded`: UPDATE 0 rows, orphan vector cleaned next reconcile. No crash. Safe.
3. Fallback sweep raises on preview tick: flag not consumed (exception before daemon.py:289) → next tick still previews. Safe (verified in W4-D1).
4. One phase permanently errors: `errors>0` keeps flag unset forever, warning every tick, deletes blocked. Safe/loud (R-W4-002, test_errored_preview_blocks_deletes). Liveness cost accepted.
5. `pending_embeddings` empty + LIMIT small: idle skip reports pending_count 0 without COUNT — exact because same predicate (facts.py:1066 vs creator.py:1567). Safe.
6. Explicit floor 1.0 + immutable domain mix: immutable skipped before threshold (decay.py:227), mutable evicted aggressively. Operator footgun but documented; not a bug.
7. NUL/hostile recall query: api `search_facts_by_text` refuses NUL (existing facts.py policy); touch never interpolates. No injection.

## 6. Dimension checklist
Correctness ✅ · root cause vs symptom ✅ · regression/compat ✅ (additive drop_counts/outcome/pending_count; decay_lambda kept parsing, prune floor inert unless explicit → per-domain byte-identical) · concurrency/idempotency ✅ (ContextVar counters R-W3-004; drain idempotent; claim atomic) · error handling ✅ (drain/ingestion isolation, touch never fails recall, budget/resume) · security/abuse ✅ (no new surface; bound params) · performance ✅ (idle COUNT removed; single touch_many; single drain batch) · migration/rollback ✅ (no schema; revert-safe) · observability ✅ (gauges + outcome symmetry + preview-incomplete warning) · maintainability/conventions ⚠️ (FINAL-04 duplication; rest clean, ruff/mypy scoped green) · docs impact ✅ (citations green; content spot-checked).

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | Per-phase outcome in CurationReport (R-W4-002 proper fix) | curator.py:72-85 no per-phase fields | Unblocks deletes when one phase errors | Medium | Changing delete semantics | DO-LATER (warning makes current loud; explicitly deferred) |
| 2 | Single `_touch_recalled` helper in decay | 4 copies listed in FINAL-04 | Low (readability, drift-proofing) | Low | Signature churn | DO-LATER |
| 3 | Plumb drain `limit` from API/CLI | 3 callers use default | Low | Low | API surface change | NOT-WORTH-IT now |

## 8. Cross-agent / cross-task interactions observed
No conflicts in final scope. Waves sequenced correctly on hotspots (creator W1→W3, daemon W1→W2, config W3 only). Deferred items tracked: R-W2-D-01/02 closed in W4 (fallback preview + partial/errored preview consumption tests), IGNORED INFO closed (R-W4-004), orphan provenance refinement (W3) holds in daemon test. Q1/R1/R3 remain operator pre-enable items (work-ledger §Deferred), not code defects. Operator docs commits (f8c12a6/aee3810/90e1aae/88124f2) were docs-only, no source conflict. No new coupling introduced by W4 delta (W4-D1 carries only the NIT below, inherited).

## 9. What I could not verify
- Full suite (scoped only per standing orders; full suite needs 900s+ and shows 12 pre-existing Windows failures per 00-mission.md — not re-run).
- Pre-fix reversal in a worktree (relied on per-wave transcripts + code-read consistency).
- Live Qdrant / real embedder ranking fidelity (fake synonym/hash only; R1 precision check remains operator-post-launch).
- Content-skew of non-blank docs citations beyond the lock + spot checks (conventions gate is non-blank only).
- Load/concurrency under real daemon with 100x signal volume (reasoned only; no shared-env load run).

## 10. Commands run and results (scoped)
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py tests/daemon/test_maintenance_loop.py -q -> 29 passed (18 e2e + 11 daemon, 100%)]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_creator.py -q -> 50 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_decay.py tests/memory/test_curator.py -q -> 68 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/agents/test_context_agents.py tests/api/test_memory.py -q -> 46 passed (1 Starlette httpx deprecation warning, pre-existing)]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/cli/test_memory.py tests/coder/test_runtime_tools.py -q -> 81 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/runtime/test_memory_creator.py -q -> 13 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/memory/test_llm_extractor.py -q -> 29 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 5 passed]`
- `[RAN .venv\Scripts\python.exe -m ruff check <10 touched src files> -> All checks passed!]`
- `[RAN .venv\Scripts\python.exe -m mypy <10 touched src files> -> Success: no issues found]`

COMMS last line read: 59 (2026-11-06T21:14:26Z ORCH WAVE-END Wave 4 closed)
