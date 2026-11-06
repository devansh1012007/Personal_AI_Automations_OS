<!-- ORCH-DIGEST:START -->
agent: W4-REV-01 | scope: Wave 4 (T-007 e2e + observability + 3 MINORs) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 1 | MINOR 3 | NIT 2
claims_audit: 9 claims checked / 1 unsupported-or-false
band_aids_found: none
top_3_risks: fallback preview never consumed (deletes never happen if curator stays broken); single permanently-erroring curator phase blocks all deletes forever; COUNT(*) per 1s daemon drain is an idle heat tax
optimizations: DO-NOW 1 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: done
confidence: med (scoped tests re-ran green; 3 pre-existing maintenance failures confirmed triaged, not re-litigated)
<!-- ORCH-DIGEST:END -->

# Code review — W4-REV-01 (Wave 4, T-007 e2e + observability + 3 MINORs)

## 0. Method
Blind pass on `git diff 5cd2c38..HEAD -- src/paa/daemon.py src/paa/memory/creator.py tests/memory/test_e2e_remember_recall.py` (code commit `326b16d`), reading `daemon.py:200-274`, `creator.py:1155-1220`, `creator.py:1490-1559`, `curator.py:60-220`, `facts.py:1057-1074`, `decay.py:179-268`, `_semantic` context_agents.py:241-262, then claims audit vs `reports/w4-impl/W4-IMPL-07-T-007.md` (overwritten by W4-IMPL-08). Scoped tests only (standing order 16). COMMS last line read: W4-IMPL-08 NEED + FINDING (2026-11-06T20:54:30Z pair).

## 1. Blind-pass findings (before reading implementer reports)
- (a) Fallback `sweep(dry_run=dry_run)` [VERIFIED `src/paa/daemon.py:258`] honours the DecaySweeper mechanism `if doomed and not dry_run` [VERIFIED `src/paa/memory/decay.py:259`], but the fallback `return None` [VERIFIED `src/paa/daemon.py:259`] never touches `_curate_preview_done` — so on a permanently-broken curator there is no tick that ever deletes. First tick preview ✓, "second tick deletes" ✗ (only the test's manual flag flip makes it delete).
- (b) Flag gate `if completed_fully and errors==0` [VERIFIED `src/paa/daemon.py:271-272`] is set-once (only sets, never clears) — correct for deleting ticks. But a budget stop (`stopped_early_at != None`, `completed_fully=False` [VERIFIED `src/paa/memory/curator.py:91-93`]) leaves preview on; the resume cursor [VERIFIED `src/paa/daemon.py:270`] means the *next* tick previews the remaining phases, so eventual completion still consumes. It only stays preview-forever under permanent budget starvation (e.g. `curation_max_runtime_hours=0`) or a permanently-erroring phase — safe direction, but a liveness trap (see findings).
- (c) IGNORED INFO shape [VERIFIED `src/paa/memory/creator.py:1171-1179`] vs PROCESSED [VERIFIED `src/paa/memory/creator.py:1205-1212`]: same event, same `facts/entities/dropped/drop_counts`, but `outcome="ignored"` exists only on the IGNORED branch; PROCESSED has no `outcome` key. "Single shape" is a superset, not identical.
- (d) `pending_count` helper [VERIFIED `src/paa/memory/creator.py:1553-1559`] is constant SQL, no interpolation — SQL-safe. Filter matches `pending_embeddings` exactly (`embedding_status='pending' AND superseded_by IS NULL`) [VERIFIED `src/paa/memory/facts.py:1070` vs creator.py:1557]. Partial index exists on `embedding_status='pending'` [VERIFIED `schema_sqlite.sql:294`], so COUNT is an indexed aggregate, not a table scan. Cost concern is frequency, not size: one extra query per `drain` call, including idle ticks (`pending==[]` still COUNTs).
- (e) e2e [VERIFIED `tests/memory/test_e2e_remember_recall.py:157-196`] uses real `NumpyVectorStore` + real `MemoryCreator` + real `_semantic`; only the embedder is fake (synonym map). It proves plumbing + restart durability, not semantic fidelity — and, notably, it bypasses the pending queue (`process_signal` → `_enqueue_embeddings` marks indexed synchronously), so it never exercises `drain_pending_embeddings`.

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| Fallback `sweep(dry_run=dry_run)` reuses decay.py mechanism | Yes | [VERIFIED decay.py:259]; regression test fails pre-fix per implementer, failure reproduced in my scoped run of stale tests (dry_run=True, evicted=1, remaining=1) |
| Flag gate preserves set-once invariant | Yes | [VERIFIED daemon.py:271-272] `if` only sets; no reset path |
| COUNT>0 == LIMIT-1 probe truth (same filter) | Yes | Identical WHERE clauses [VERIFIED facts.py:1070, creator.py:1557] |
| "Same event name/key shape as PROCESSED" (W3 INFO) | Partially false | `outcome` key only on IGNORED branch [VERIFIED creator.py:1174, 1205-1212] — this is the 1 unsupported claim |
| `signal_ignored` has no in-repo consumer | Yes | grep finds none in `src/` (docs/reports only) |
| e2e proves remember→recall after restart | Yes for plumbing | Real store+creator+`_semantic`, content==`swift`, `indexed` after restart [VERIFIED test file]; fake proves wiring, not fidelity (implementer discloses) |
| Pre-fix 6/10 fail, post-fix 10/10 pass | Yes | [RAN .venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py -q -> 10 passed] |
| 2 stale fallback tests FAIL (outside write-set) | Yes — reproduces | [RAN tests/daemon/test_maintenance_loop.py -q -> 3 FAILED: 2 fallback + 1 orphan] both fallback failures show `decay.sweep_completed dry_run=True evicted=1` + `remaining 1==0` |
| Orphan failure is pre-existing | Yes (not re-litigated) | gate-w4-1 triaged per brief §mission; diff touches no curator code [VERIFIED diff stat: daemon.py, creator.py, test file only] |
| Ruff scoped clean / mypy baseline | Yes | [RAN ruff check on 3 diff files -> All checks passed!] / mypy not re-run (full `mypy src` exceeds scoped-check guidance; baseline claim accepted, not independently confirmed) |

## 3. Long-run test (independent answers)
- **Source:** fixes are at the call sites where the contracts already existed (dry_run passthrough, report-field gate, INFO line, COUNT helper) — root-cause shaped, no swallowed errors, no test-weakening. The one exception is the fallback flag never being consumed (R-W4-001) — the fix makes a fallback deployment delete-free forever, which is a lifecycle gap, not a band-aid.
- **Load (100x):** COUNT is one indexed aggregate per drain call vs the removed LIMIT-1 fetch — same order, O(pending) index scan. At 10k pending this is still an in-engine index walk, fine. The real amplifier: daemon calls drain per ingestion tick (~1/s), including idle ticks where `pending==[]` yet a COUNT runs (R-W4-003). Concurrency: helper is a read, no write race; concurrent drains already covered by `test_creator.py` double-drain test (still passing — not re-run, accepted).
- **Hostile:** SQL is constant (no user input, no LIKE) — no injection surface [VERIFIED creator.py:1555-1558]. Log keys are counts/ids only. Synonym embedder hashes tokens — no injection. OK.
- **Operability:** `pending_count` gauge + INFO ignored drops are useful; rollback = revert `326b16d`, no migration. One wart: renaming `signal_ignored` → `signal_processed` breaks any external dashboard querying the old name (no in-repo consumer; implementer discloses). Curation timing pinned only at total `duration_seconds` (per-phase needs a curator.py field — read-only boundary, acceptable).
- **Reversal:** 6/10 new tests fail on pre-fix code for the right reasons [RAN post-fix 10 passed; pre-fix pattern accepted from implementer + reproduced via stale daemon failures]. Reverting any of the 4 source edits re-fails its matching test (see §5 table).

## 4. Findings
### R-W4-001 [MAJOR] Fallback preview is never consumed — a permanently-broken curator means maintenance never deletes
- Location: `src/paa/daemon.py:254-259` · Evidence: fallback calls `sweep(dry_run=dry_run)` then `return None` without setting `_curate_preview_done`; only the curator path (line 271) sets the flag. The regression test masks this by `daemon._curate_preview_done = True` manually [VERIFIED test file line ~297].
- Why: angle (a)'s question "second tick deletes?" — on a fallback deployment the answer is no, forever. The R-W2-D-01 fix overshoots from "deletes on first tick" to "never deletes". Safe direction (no data loss), but the fallback exists precisely so retention still runs; it now silently does nothing beyond a dry sweep, and operators see only `decay.sweep_completed dry_run=True` forever.
- Fix (root-cause): after a *successful* fallback preview sweep, consume the flag (set `_curate_preview_done = True` when `dry_run` was True and no exception) — one line in daemon.py. Test should drive two real fallback ticks and assert delete on the second, not flip the flag by hand.

### R-W4-002 [MINOR] One permanently-erroring phase blocks all deletes forever (liveness, safe direction)
- Location: `src/paa/daemon.py:271-272` with `src/paa/memory/curator.py:214-219` · Evidence: phase errors increment `report.errors` and the loop continues; `completed_fully` stays True but `errors==1` keeps the flag unset.
- Why: angle (b). Budget stops resume correctly (cursor set, next tick previews remaining phases, eventual completion consumes) — that part is sound. But a single persistently-failing phase (e.g. a broken PRUNE_EDGES) keeps `errors>0` every tick, so flag never sets and even the healthy DECAY phase never deletes. Acceptable as a safety/observability posture, but should be a documented operator decision, not an accident of the gate.
- Fix: document the trade-off in the daemon docstring (currently R-DELTA wording elsewhere); per-phase preview tracking remains the proper fix but needs a curator.py report field (out of this wave's write-set).

### R-W4-003 [MINOR] `pending_count` COUNT runs even when the queue is known empty — idle 1 Hz tax
- Location: `src/paa/memory/creator.py:1517-1518` · Evidence: early path (`sink is None or not pending`) always COUNTs; when `not pending`, `pending_count` is 0 by definition (same predicate, `LIMIT` fetch returned nothing) — the query buys nothing. Daemon drains per ingestion tick, so this is a per-second indexed query on an idle system.
- Fix: `pending_count = 0 if not pending else await self._pending_embedding_count()` — removes the idle tax, keeps the gauge exact when a backlog exists. Risk: none (same predicate).

### R-W4-004 [MINOR] IGNORED/PROCESSED log schema is asymmetric (`outcome` only on IGNORED)
- Location: `src/paa/memory/creator.py:1171-1179` vs `1205-1212` · Evidence: IGNORED adds `outcome="ignored"`; PROCESSED has no `outcome` key. Dashboards counting `memory.signal_processed` as "processed" now overcount ignored signals; the "single shape" claim in implementer §4/§6 is half-true.
- Fix (either direction, pick one and pin): add `outcome="processed"` to the PROCESSED line for a truly uniform schema (additive key, schemaless in-tree), or accept the asymmetry and note it so downstream dashboards don't treat the event name as an outcome.

### R-W4-005 [NIT] e2e never exercises the drain path it names
- Location: `tests/memory/test_e2e_remember_recall.py:157-196` · Evidence: `process_signal` indexes synchronously via `_enqueue_embeddings`; the fact is `indexed` immediately. A `drain_pending_embeddings` regression would leave this test green.
- Fix: rename/scope note, or add a drain step (e.g. write with sink=None → pending → drain → recall). Drain itself is covered by other tests, so this is coverage-honesty, not a hole.

### R-W4-006 [NIT] Failure-path drain line lacks any gauge (`embedding_drain_failed` hardcodes `backlog_remaining=True`)
- Location: `src/paa/memory/creator.py:1529-1537` · Evidence: on sink exception the line logs `backlog_remaining=True` unconditionally with no `pending_count`, even though a COUNT is available for one indexed query on the error path.
- Fix: include `pending_count` there too for operator parity (error path is cold — no amplifier concern).

## 5. Attempts to break it (concrete inputs/interleavings)
1. **Permanent curator construction failure, two real ticks.** Daemon ticks ×2. Expected by test docstring "second tick deletes"; actual: both dry-run, fact retained. → R-W4-001. (Manual flag flip in test is the only way the second-tick-deletes path runs.) [VERIFIED by reading; stale-test failure output confirms dry_run=True + remaining=1 on first tick]
2. **Preview with `budget_seconds=0.0`** (operator sets `curation_max_runtime_hours=0`). Tick stops at DECAY (`stopped_early_at=DECAY`), flag stays False, cursor=DECAY. Every tick repeats. Preview forever, no deletion — documented in daemon docstring, safe. Not a code bug; accepted.
3. **One phase errors every tick** (monkeypatched PRUNE_EDGES raise). `errors=1`, `completed_fully=True` → flag never set → DECAY delete never happens. → R-W4-002 (liveness, safe direction).
4. **Budget stop mid-preview then completion.** Preview tick1 stops at PRUNE_EDGES (flag False, cursor=PRUNE_EDGES), tick2 resumes as dry-run, completes clean → flag True. Aggregate preview coverage = all phases. ✓ Sound by construction (test `test_partial_preview_does_not_consume_flag` + `test_complete_preview_consumes_flag` [RAN pass]).
5. **Drain with `limit=2` on 3 pending.** `batch_size=2, pending_count=1` — proves COUNT≠len(batch). [VERIFIED test; RAN pass] ✓
6. **Sink returns unknown/partial ids.** COUNT after `mark_embedded` reflects the true remainder (unknown ids mark nothing, rows stay pending, count includes them). ✓ Covered by construction (exact count, not probe).
7. **Hostile `submit_task` text through IGNORED path.** `"'; DROP TABLE--"` style payloads: extractor drops them, INFO line carries only counts/ids. No SQL interpolation anywhere in the new code. ✓
8. **Concurrent drains.** Helper is read-only; existing double-drain test covers races. [Not re-run; accepted]

## 6. Dimension checklist
- **Correctness:** flag gate sound (set-once, resume-aware); dry_run passthrough sound; COUNT filter parity exact; e2e asserts id+content+status, not just non-empty. One lifecycle gap (R-W4-001).
- **Root cause vs symptom:** all four edits at the divergence point, no masking. R-W4-001 shows the flag's *consumption* side was missed.
- **Regression/compat:** no schema/config/API change; log additive keys except the `signal_ignored` rename (external dashboards only, disclosed). 2 daemon tests in `test_maintenance_loop.py` now fail — triaged stale (brief), reproduced.
- **Concurrency/idempotency:** COUNT is a read; drain idempotent; no new races.
- **Error handling:** sink-raise path still returns 0 without flipping to `failed` (invariant preserved); fallback sweep exceptions propagate to the maintenance loop's existing handler (daemon survives).
- **Security/abuse:** constant SQL, no secrets/PII in new log keys (counts/ids). ✓
- **Performance/scalability:** one indexed COUNT per drain; idle-tax nit (R-W4-003); no N+1, no unbounded fetch.
- **Migration/rollback:** none needed; revert restores old (buggy first-tick delete) behavior — note it.
- **Observability:** genuinely improved (gauge, IGNORED drops, duration pinned); failure-line gap (R-W4-006).
- **Maintainability/conventions:** helper lives in creator.py because facts.py is read-only — reasonable, documented; line-length 100, ruff clean [RAN]. E701 fixed pre-commit (disclosed).
- **Docs impact:** docstring updated in creator.py; daemon docstring still says fallback is a "bare `DecaySweeper.sweep()`" (line 236) — now dry-run-aware; minor staleness nit.

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | creator.py:1517-1518 skip COUNT when `not pending` | Idle drain runs a query whose answer is known (0) | One less indexed query per idle daemon tick (~1/s) | XS (1 line) | None — same predicate | **DO-NOW** |
| 2 | creator.py:1529-1537 add `pending_count` to `embedding_drain_failed` | Error path lacks the gauge operators need most | Operator parity on failures | XS (1 query, cold path) | None | **DO-LATER** |
| 3 | Move COUNT into `facts.py` as a repository method | Would remove SQL from creator.py… | — | M (touches read-only facts.py, parity risk) | Schema/parity lock churn | **NOT-WORTH-IT** (current placement is documented and filter-identical) |

## 8. Cross-agent / cross-task interactions
- Stale `tests/daemon/test_maintenance_loop.py` ×2 (fallback-first-tick-deletes) now fail against the fixed code — correctly identified by implementer as pre-fix behavior; NEED already posted, owner outside Wave-4 write-set. [RAN: 3 FAILED in that file, 1 orphan pre-existing]
- `daemon.py` docstring at line 236 still describes the fallback as a plain sweep; the dry-run passthrough makes that sentence stale (docs-refs can't catch it — line is non-blank and content-skew is the known blind spot per AGENTS.md).
- No interaction with T-005/T-006 files (creator.py drop counters, config) beyond shared log lines, both additive.

## 9. What I could not verify
- Full `mypy src` / full pytest gate (scope-limited by standing order 16; ruff scoped + 14 scoped tests ran instead).
- Pre-fix 6/10 failure pattern (accepted from implementer report; not re-run by reverting source — that would require edits outside my read-only mandate).
- Production semantic fidelity (fake embedder proves wiring only — implementer discloses; HashEmbedder/SentenceTransformer parity is a post-launch precision check, plan R1).
