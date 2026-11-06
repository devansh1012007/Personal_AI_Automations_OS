<!-- ORCH-DIGEST:START -->
agent: W1-DELTA-REV | scope: Wave 1 delta review (e966b68 + 5b8d6ee) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 0 | NIT 2
claims_audit: 30 / 0
band_aids_found: none
top_3_risks: live vector stack untested (fakes only) | backlog_remaining boolean not count | concurrent drain wastes embed cost (safe/idempotent)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: done
confidence: high (scoped tests 36 passed, ruff clean, diff+code read; no full suite per standing orders)
<!-- ORCH-DIGEST:END -->

# Code review — W1-DELTA-REV

## 0. Method
- Diff range reviewed: ONLY improvement commits `e966b68` (code) and `5b8d6ee` (docs), via `git show e966b68` / `git show 5b8d6ee` [RAN git show -> 7 files code, 10 docs]. Did NOT re-review original Wave 1 implementation. Note `git diff aadb42f..HEAD -- src/ tests/ docs/` also contains operator commits `f8c12a6`/`aee3810` which are outside the delta.
- Files read in context: `src/paa/memory/creator.py` (drain+enqueue), `src/paa/runtime.py` (`_build_memory_creator`, `Runtime.build`, `_try_vector`), `src/paa/memory/facts.py::pending_embeddings`, `src/paa/daemon.py`, and all changed test files.
- Commands: scoped pytest (36), ruff on changed files (clean). No full suite (STANDING-ORDERS §16).
- COMMS last line read at start: `[2026-11-06T18:30:00Z] W1-IMPL-01 -> ORCH | DONE | T-001` [VERIFIED docs/agent-ops/20261106-memory-remembering-agent/COMMS.md:27].
- Order: diff+code first (blind), then IMPR-01/IMPR-02 reports.

## 1. Blind-pass findings (before reading implementer reports)
- Drain `backlog_remaining` probe is one `pending_embeddings(1)` LIMIT-1 query [VERIFIED src/paa/memory/creator.py:1283]; failure path hardcodes `True` [VERIFIED src/paa/memory/creator.py:1273] — correct because early return guarantees `batch_size>0` on that path, but worth confirming.
- `_build_memory_creator` plain-missing branch is now `log.debug` while its own docstring still says "with a warning" [VERIFIED src/paa/runtime.py:457 vs 489] — stale doc, NIT.
- Concurrent test contains a dead loop `for fact in await pending...: assert fact.id  # unreachable` [VERIFIED tests/memory/test_creator.py:574-575] — cosmetic, NIT.
- No new BLOCKER/MAJOR seen in blind pass: no unbounded loop, no blanket swallow added, no `noqa`/`type: ignore` added (one `type: ignore` actually removed), no schema/public-API change.

## 2. Claims audit
| # | Implementer claim | Verified? | Evidence / finding |
|---|---|---|---|
| 1 | R-W1-001a/02 Runtime.build composition test exists and asserts same fake pair | YES | [VERIFIED tests/runtime/test_memory_creator.py:322-384] monkeypatches `_try_vector/_try_graph/_try_queue`, asserts `isinstance(sink, VectorEmbeddingSink)` + `sink._store is store` + `sink._embedder is embedder`; threads at [VERIFIED src/paa/runtime.py:334-336]. Reversal sound. |
| 2 | R-W1-001b/01 daemon drain test exists, seeds pending, polls to indexed | YES | [VERIFIED tests/daemon/test_drain_e2e.py:165-206] seeds via `add_fact`, attaches recording sink, `Daemon(..., ingestion_interval=0.02)`, `_wait_for(indexed, 5.0)`, asserts pending empty. |
| 3 | R-W1-03 fakes record collection/texts, tests assert them | YES | [VERIFIED tests/memory/test_creator.py:245-278] `_FakeEmbedder.batches`, `_FakeVectorStore.collections` + `upsert->int` (matches real [VERIFIED src/paa/storage/vector/store.py:490]); asserts `["active_facts"]` + `[[statement]]` [VERIFIED tests/memory/test_creator.py:325,332]. Multi-fact asserts one batch [VERIFIED :~660]. |
| 4 | R-W1-002 log honesty rename + backlog probe | YES | [VERIFIED src/paa/memory/creator.py:1243-1289] `pending`->`batch_size`, `backlog_remaining` boolean probe, LIMIT-1, docstring documents presence-not-count. |
| 5 | R-W1-003 warning->debug for plain-missing | YES | [VERIFIED src/paa/runtime.py:483-489] plain branch `debug`, construction-exception branch stays `warning` [:481]; `_try_vector` warning at [:409-410] remains single signal. |
| 6 | R-W1-005 short-return warning | YES | [VERIFIED src/paa/memory/creator.py:838-845] `memory.embedding_batch_short` before `zip(strict=False)` [:868]; facts stay pending (no mark). |
| 7 | R-W1-006 monkeypatch restore | YES | [VERIFIED tests/runtime/test_memory_creator.py] `monkeypatch.setattr(creator_mod,"VectorEmbeddingSink",_BrokenSink)`; old `# type: ignore` removed. |
| 8 | R-W1-04 limit/partial/105 tests | YES | [VERIFIED tests/memory/test_creator.py:460+] limit=2, non-positive, 105->100+5, partial/empty/unknown-id, concurrent `asyncio.gather`. |
| 9 | R-W1-05 real-sink raises + API/CLI mark-isolation | YES | Real-sink tests + [VERIFIED tests/api/test_memory.py] `test_drain_failure_isolated_when_mark_embedded_raises` (200+REPORT_FIELDS) and CLI equivalent (exit 0). Note: `mark_embedded` raising still bubbles out of `drain_pending_embeddings` [VERIFIED src/paa/memory/creator.py:1277-1278] — isolation is correctly at API/CLI callers, which is what tests pin. Pre-existing behavior, not a delta regression. |
| 10 | R-W1-06 payload ranking exactness | YES | Tolerance asserts vs fact confidence/importance, far-from-defaults guard [VERIFIED tests/memory/test_creator.py:346-370]. |
| 11 | R-W1-07 clarifying docstrings (not reversal) | YES | Pinning docstrings updated; true reversal is #1 above. |
| 12 | R-W1-09 tmpdir + CLI cache teardown | YES | `_settings(tmp_path,...)` under `tmp_path/settings_home`; CLI fixture `yield` + unconditional `reset_settings_cache()`. |
| 13 | R-W1-007 REJECTED (unredacted str(exc)) | SOUND | File convention is unredacted: [VERIFIED src/paa/memory/creator.py:833,999,1223,1270] all `error=str(exc)`; vector/embedder errors carry names/counts, no secrets shown in enqueue path. Whole-module redaction is separate hygiene, not Wave-1 blocker. Agree. |
| 14 | R-W1-08 REJECTED (white-box privates) | SOUND | No public sink accessor [VERIFIED src/paa/memory/creator.py:906] only `_sink`; existing tests already use `_extractor` [VERIFIED tests/runtime/test_memory_creator.py:70]. Renaming = churn without behavior. Agree. |
| 15 | R-W1-004 DEFERRED (superseded stay indexed) | SOUND | `pending_embeddings` excludes `superseded_by IS NULL` [VERIFIED src/paa/memory/facts.py:1070], so drain cannot crash on superseded; actual point deletion belongs to Wave 2 reconciler. Deferral honest. |
| 16 | P3-11..15 DEFERRED | SOUND | Unknown-id incidentally pinned (#8); others need reconciler/hostile-payload work beyond brief. Listed, not hidden. |
| 17 | IMPR-02 17 citations fixed, correct lines | YES | Spot-verified: [_acquire_sandbox src/paa/runtime.py:496] [VERIFIED], [_try_vector :400], [_try_queue :423], header :394, [validation lazy import :571], [daemon construction :189], [DecaySweeper sweep :173], [README diagram :174]. All non-blank and hold claimed symbol. |
| 18 | IMPR-02 out-of-scope flags (359/372, 5s vs 30s) | SOUND | [VERIFIED src/paa/runtime.py:359,372] are `recovery=`/`model_router=` lines, not `_try_*` — pre-existing approximate, correctly left out of 17-blank scope. 5s/30s prose mismatch is operator-doc, not introduced here. |
| 19 | 19 tests added/strengthened, 80 passed (IMPR-01) | PLAUSIBLE | My scoped re-run covers the delta files: 36 passed (see §9); full 80 not re-run per scoped rule. No contradiction. |
| 20 | No forbidden files / no band-aids | YES | Diff touches only WRITE-SET files; no `noqa`, no new `type: ignore`, no sleep/retry, no loop-until-empty [RAN git diff grep -> only test `Any` fakes]. |

Claims checked: 20 groups / 30 individual (17 docs citations counted separately). Unsupported or false: 0.

## 3. Long-run test (independent answers)
- Source: Drain probe is bounded LIMIT-1 [VERIFIED src/paa/memory/creator.py:1283]; no new DB write beyond existing `mark_embedded`. Short-return warning is log-only. Severity downgrade is log-only.
- Load (100x, concurrency): 105-backlog test proves drained-not-paginated (100+5). Concurrent double-drain: final state asserts pending empty + 2 indexed [VERIFIED tests/memory/test_creator.py:555-580]; `sum>=2` is weak but final-state assertion is strong. Waste (duplicate embed) is safe: point-id upsert is idempotent, acknowledged in IMPR-01 §6. No unbounded loop introduced (one batch/call).
- Hostile input: No new untrusted input surface. `limit<=0` guard [VERIFIED src/paa/memory/creator.py:1253] defeats SQLite `LIMIT -1`. Unknown-id sink return leaves fact pending (pinned). Extractor caps untouched.
- Operability: Rollback = revert `e966b68`/`5b8d6ee`; no migration, no config change. Observability improved (batch_size/backlog_remaining/batch_short). `backlog_remaining` boolean cannot distinguish 1 vs 4,900 — documented limitation, DO-LATER count API.
- Reversal: Removing `vector_store=/embedder=` at runtime.py:334-336 fails #1; removing daemon drain fails #2; wrong collection/text fails #3 (fakes now record); ignoring `limit` fails #8; reverting any docs citation to old number re-fails blank test (old lines blank [VERIFIED IMPR-02 pre-fix run]). All reversal paths are genuine, not tautological.

## 4. Findings
### R-W1-DELTA-01 [NIT] Stale docstring in `_build_memory_creator`
- Location: `src/paa/runtime.py:457` · Evidence: docstring says degrades "with a warning" but plain-missing branch is now `log.debug` [:489]. Why: minor doc drift, could mislead next editor. Fix: update docstring to "warning on construction failure, debug on plain-missing" in a later touch; not blocking.
### R-W1-DELTA-02 [NIT] Dead loop in concurrent test
- Location: `tests/memory/test_creator.py:574-575` · Evidence: loop over `pending_embeddings()` after asserting it is `[]` never executes. Why: cosmetic noise, could confuse readers. Fix: delete the two lines in a later touch; not blocking.
- No BLOCKER, MAJOR, or MINOR findings in the delta.

## 5. Attempts to break it (concrete inputs/interleavings)
- Sink returns subset: only returned ids marked, remainder stays pending [VERIFIED tests/memory/test_creator.py partial test] — passed.
- Sink returns `[]`: returns 0, all stay pending — passed.
- Sink returns unknown id `"nope-not-a-fact"`: `mark_embedded` matches 0 rows, fact stays pending, drain returns 1 (bogus count) — pinned as known behavior; not introduced by delta.
- Embedder/store raises (real sink): returns 0, fact stays pending, write-path stays pending — passed.
- `mark_embedded` raises: bubbles to API/CLI callers, which isolate (200/exit 0) — tested at both callers.
- Concurrent drains: both fetch same batch, both enqueue, both mark; final state consistent — passed (waste-only).
- Limit 0/-5: no-op, no `LIMIT -1` surprise — passed.

## 6. Dimension checklist
- Correctness: PASS — probe, short-return, severity, fake fidelity all match code semantics.
- Root cause vs symptom: PASS — composition fixed by adding real wiring tests, not by weakening existing ones; fake fixed at source (record + return `int`).
- Regression/compat: PASS — log fields additive/renamed (`pending`->`batch_size`); no public API shape change; no schema change.
- Concurrency/idempotency: PASS with noted waste — idempotent upsert, no lost facts.
- Error handling: PASS — degrade-never-raise preserved; sink failure never flips to `failed`.
- Security/abuse: PASS — no new SQL/LIKE surface (probe uses existing `?`-bound API); no secret logging introduced; rejected redaction reasoning sound.
- Performance/scalability: PASS — one LIMIT-1 probe per drain, partial-index backed; no COUNT, no loop-until-empty.
- Migration/rollback: PASS — revert-only, no migration.
- Observability: IMPROVED — backlog visibility + short-batch warning; boolean limitation documented.
- Maintainability/conventions: PASS — minimal diffs, DECISION/INVARIANT comments where non-obvious; NITs above only.
- Docs impact: PASS for delta scope — 17 blank citations fixed and verified non-blank + semantically correct; 359/372 + 5s/30s correctly flagged as separate follow-ups, not regressions.

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | `FactRepository.count_pending()` | Boolean probe can't size backlog [VERIFIED creator.py:1243-1251] | Operator visibility | Medium (new API, parity-safe additive) | Low | DO-LATER (Wave 2+) |
| 2 | Claim/lease for concurrent drains | Duplicate embed cost under parallel drains | Cost only, no correctness | Medium | Medium | DO-LATER (only if parallel drains common) |
| 3 | Loop-until-empty drain | Banned by brief; unbounded under large backlog | — | — | — | NOT-WORTH-IT (daemon cadence already drains) |

## 8. Cross-agent / cross-task interactions observed
- None new. Delta is confined to T-001 memory wiring; no shared hotspots (`schema_sqlite/postgres.sql`, `pyproject.toml`, CI) touched [RAN git show --stat]. IMPR-02 flag on `component-interactions.md:223 runtime.py:387` and `overview.md:359/372` are pre-existing approximations for ORCH to schedule, not delta regressions.

## 9. What I could not verify
- Full suite / full 80-test count: NOT RUN (scoped-checks rule §16). Scoped re-run only:
  - [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/runtime/test_memory_creator.py::TestRuntimeBuildEmbeddingSink tests/daemon/test_drain_e2e.py::test_daemon_ingestion_loop_drains_pending_embeddings tests/conventions/test_docs_references.py -p no:warnings` -> `36 passed in 6.11s`, EXIT:0]
  - [RAN `.venv\Scripts\python.exe -m ruff check src/paa/memory/creator.py src/paa/runtime.py tests/memory/test_creator.py tests/runtime/test_memory_creator.py tests/daemon/test_drain_e2e.py tests/api/test_memory.py tests/cli/test_memory.py` -> `All checks passed!`]
- Reversal mutations (removing kwargs / daemon drain) were NOT re-executed by me; I verified the tests' assertions would fail by inspection against the call sites. IMPR-01 claims those reversals RAN — [ASSUMED] their [RAN] evidence, consistent with code.
- Live Qdrant/SentenceTransformer stack: NOT verified (fakes only, same limitation IMPR-01 disclosed).
- mypy: NOT RUN (no src logic change beyond log calls; ruff clean; baseline 3 win32 errors per mission are untouched).
