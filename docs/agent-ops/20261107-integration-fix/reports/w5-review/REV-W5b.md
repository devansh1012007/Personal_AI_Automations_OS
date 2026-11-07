<!-- ORCH-DIGEST:START -->
agent: REV-W5b | scope: wave-5 fixes (c2e746c FIX-01 + cef92c2 DOCS2) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 0 | MINOR 1 | NIT 7
claims_audit: 16 claims checked / 1 false (tui count) + 1 partial (deleted scratch repro)
band_aids_found: none
top_3_risks: deferred allocator-settings split still live for non-global Settings (MINOR, pre-existing); PG cursor proven by shape-pin only (execution NOT-RUN); producer lock line-shaped (disclosed)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: done (executed tool-pin simulations + test-behavior reads; TREV mutations now caught)
confidence: high (blind pass complete; all in-scope MAJORS fixed and proven; scoped suites re-run by reviewer)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W5b (independent, read-only re-review of wave-5 fixes)

## 0. Method
Diff range reviewed: `0bcb62c..HEAD` (c2e746c FIX-01 code+tests, cef92c2 DOCS2 docs, 2 report commits 2d62a44/bb2a5ce).
Files read in context: runtime.py (build + `_build_memory_creator`), creator.py (drain 1533-1647, emitter 2031-2089), curator.py (ctor 129-169, `_phase_analyze` 460-570), tui/runner.py (`_attention_top` 171-239), ledger/store.py (`read_correlation` 312-329, `open_correlations` 348-360), daemon.py (start 91-102, tick 266-309, creator fallback 335-347, curator site 412-423), orchestrator.py (`run_allocation` 1144-1173), allocator.py docstring 42-50, queue `__init__` docstring, postgres.py `fetch_all` 557-576, coding.py tool names, all 4 fixed test files (new/changed regions), briefs/W5-FIX-01.md §3-§4 (scope), docs diff cef92c2.
Commands run: scoped pytest (221-file set, agents+memory, tui, locks), full-tree ruff, full `mypy src`, collect-only counts, docs-holder line dumps. Order followed: blind pass on the diff first, then FIX-01/DOCS2 reports (§2 follows). COMMS read to last line 156 (W5-FIX-DOCS2 DONE); no COMMS appends (brief: edit only this report).

## 1. Blind-pass findings (before reading implementer reports)

### R-W5-001 [MAJOR, prior] Production DRAIN_THROTTLED unwired — FIXED
- Location: `src/paa/runtime.py:346-353,514`. Evidence: `Runtime.build` now passes `ledger=ledger` into `_build_memory_creator`, whose new keyword-only `ledger: Any = None` forwards to `MemoryCreator(..., ledger=ledger)`. `_build_memory_creator` has no other production callers (grep). `TestProductionWiring` proves it two ways: identity (`creator._ledger is ledger`) and an end-to-end `Runtime.build` breach reaching the real ledger as exactly one `DRAIN_THROTTLED` [RAN green]. Residual (NIT, R-W5b-003): the `or MemoryCreator(db)` fallbacks at daemon.py:344 and api/app.py:292 remain ledger-less, but they fire only when `runtime.memory_creator` is falsy, which `Runtime.build` never produces — degenerate/hand-built runtimes only. coder/runtime_tools.py:620 constructs a creator for `process_signal` only (no drain), so it cannot suppress a breach.

### R-W5-002 [MAJOR, prior] Cost-gate PG cursor — FIXED (shape-pinned)
- Location: `src/paa/memory/curator.py:497-516`. Evidence: first page issues `WHERE superseded_by IS NULL ORDER BY id LIMIT ?` with params `(batch,)` — no id predicate; later pages keep the total-order keyset `id > cursor`; cursor re-passed as `str(row["id"])`. Spy-shape honesty: `test_first_page_issues_no_id_param` intercepts at `db.fetch_all`, i.e. pre-translation SQL; I verified `postgres.py:561` translates only placeholders (`_qmark_to_pyformat`), params pass through unchanged, and psycopg/PG casts a valid UUID string in `id > $1`, so the pinned property (no bad param on page 1) is exactly the engine-relevant one. The test docstring honestly states PG is NOT-RUN. SQLite multi-page test (12 facts, batch 5) proves no duplicates/misses. PG execution remains unexecuted — a shared-env limit, disclosed, not a fix defect.

### R-W5-003 [MAJOR, prior] attention_top stale/dup/terminal — FIXED
- Location: `src/paa/tui/runner.py:204-239`. Evidence: window is the freshest 50 (`heads[-_PANEL_SCAN_LIMIT:]`, runner.py:208) given `open_correlations` is `updated_at ASC` (store.py:357 VERIFIED); dedupe by `item_id` with later events overwriting (runner.py:226-234), where read order is `state_version ASC` (store.py:326 VERIFIED) so later = latest, not best; terminal drop is an explicit documented contract (runner.py:195-199) and a test pins it. Four new tests cover same-item-latest (0.2 after 0.8), cross-lineage later-wins with correlation carried, recency window excluding the stale highest-score lineage, and terminal→None — all [RAN green]. Empty→None preserved.

### REV R-W5-004 [MINOR, prior] Discriminator epoch — FIXED
- Location: `src/paa/memory/creator.py:2028,2076-2079`. `_DRAIN_PROCESS_ID = uuid4().hex` at import, mixed into the discriminator. Same-process same-window dedupe preserved (existing dedupe test still passes [RAN]); a fresh process records its own breach instead of colliding with monotonic-relative stamps. Cost is a bounded duplicate-telemetry class (one per process per window), stated in the module docstring. Fork-after-import would share the nonce — out of scope for the single-process deployment.

### REV R-W5-005 / TREV R-W5-001 [MINOR/MAJOR, prior] Tool-policy tautology — FIXED
- Location: `tests/agents/test_coding_specialist_p011.py:151-174`. Exact pins: `mutating == {"submit_task","run_task","remember_fact"}` and `read_only == set(READ_ONLY_TOOLS)`. I executed the two failure simulations against live imports: adding uncovered `"frobnicate"` fails the read-only half; adding glob-matched `"delete_everything"` fails the exact mutating set. Both are loud now. (Pre-existing duplicate assert at :137-138 remains — R-W5b-008.)

### TREV R-W5-003 [MAJOR, prior] Daemon scheduling untested — FIXED (count, not identity)
- Location: `tests/context/test_allocator_scheduling_p007b.py:334-396`. `test_start_creates_five_supervised_tasks` (5 tasks + `daemon.started loops=5`) and `test_tick_raise_logs_and_loop_continues` (raise → `allocation_failed`, second tick → `allocation_completed`, 2 calls) [RAN green]. Gap (R-W5b-002): the start test asserts task count, not that one of them is the allocation task — TREV's delete-task mutation is caught (4≠5), a swap mutation would not be. Code read confirms daemon.py:100 explicitly schedules `_allocation_loop`, so this is test-strength only.

### TREV R-W5-004 [MAJOR, prior] Partial-cap emit — FIXED
- Location: `tests/context/test_allocator_scheduling_p007b.py:253-271`. Cap 2, indexed 1, limit 100 → one event with `effective_limit==1`, `threshold==2`, `limit==100`. I read the drain body: both emit sites (creator.py:1587 full-defer, :1607 partial) precede the pending fetch (:1615), so the empty-queue test shape is honest.

### TREV R-W5-005/006 [MINOR, prior] Locks — FIXED (one disclosed limit)
- Package file-set glob `["__init__.py","coding.py"]` (test:120-121) catches stub modules. Producer lock scans `src/` for same-line `.enqueue(` + `WORKERS_ALLOC`/`workers:alloc` outside the queue docstring file; I grepped all 6 `.enqueue(` sites: only the queue docstring example names `WORKERS_ALLOC`; the others enqueue `ORCHESTRATOR_CORE` or to the embedding sink. Disclosed limit (R-W5b-004): a multi-line call evades the line-shaped lock; the docstring contract is the other half. Adequate for the operator-fed decision.

### REV R-W5-007 [MINOR, prior] Suggestion cap — FIXED
- Location: `src/paa/memory/curator.py:544-565`. Top-100 by deterministic total order `(-total_cost, fact_id)`, one `cost_gate.suggestion_summary` count line, per-fact lines only for kept rows. Test monkeypatches the limit to 5 and asserts kept/total/per-fact counts [RAN green]. Note: `collected` still holds every crossing fact (O(N) memory per pass) — see §7 DO-LATER.

### REV R-W5-008 [NIT, prior] Cost-gate injection — FIXED curator-side; threading DEFERRED
- `MemoryCurator(cost_gate=...)` added (curator.py:139,149); both override directions tested [RAN green]. Production threading (`daemon.py:418` one-liner) is deferred with R-W5-006 — see R-W5b-001.

### REV R-W5-006 [MINOR, prior] Allocator-settings threading — DEFERRED, correctly
- Verified against code at HEAD: daemon gate reads `self._rt.settings.allocator.enabled` (daemon.py:293-295); `run_allocation` still reads `get_settings().allocator` (orchestrator.py:1165-1171). The split is live. Legitimacy: the fix brief FORBIDS `orchestrator.py` and allows daemon.py tests-only (§4), while the fix necessarily edits both — standing order 2 requires a NEED, not an ownership breach. COMMS NEED at 19:45Z names the exact change (settings kwarg on `run_allocation` + daemon callsite + test) and the report proposes it verbatim. Repro: scratch was executed then deleted, so I verified the mechanism by direct code read instead (above) — the split is real. Residual fails only for non-global-Settings deployments (daemon passes, allocator sees disabled, `"disabled"` logged as a completed pass — a silent no-op); default global deployments are unaffected; the inverse direction (rt disabled) blocks safely. Rated MINOR (R-W5b-001), follow-up W5-FIX-02 endorsed.

### Docs (cef92c2) spot-read ≥8 — HOLDS
- Holder text dumped at HEAD: `runtime.py:419/432/442/517/560/571/592` = `_try_vector`/`_try_graph`/`_try_queue`/`_acquire_sandbox`/`_build_agents`/anti-goals load/validation lazy import (all sentence-true); `creator.py:1268/1533/196/1631` = `run_batch`/drain def/docstring/`enqueue(pending)`; `creator.py:1491` = provenance arg inside the commit-site `add_fact` call (best-holder, acceptable); `curator.py:175/263` = `run_maintenance`/`_phase_decay`; `daemon.py:188/418` unchanged cites hold. DOCS2's correction of FIX-01's sandbox.md:193 proposal (`:446` → `:517`) is right — the sentence names `_acquire_sandbox`, whose def is at :517 (VERIFIED). One pre-existing lock-blind stale cite survives: ADR-0036 → `cli/main.py:636` for `_MAX_DRAIN_ERROR_CHARS` (constant actually at :650) — pre-dates this fix, lock-blind class (R-W5b-007).

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| FIX-01: 221 scoped passed (alloc 16 + gate 14 + coding 21 + metrics 170) | YES | [RAN] same 4 files `-q` → 221 dots exit 0; collect-only 16/14/21/170 |
| FIX-01: agents+memory 557 passed + 1 xskip | YES | [RAN] `tests/agents/ tests/memory/` exit 0; collect 558 (=557+1 skip) |
| FIX-01: tui "64 passed, 1 FAILED" | FALSE (count) | [RAN] `tests/tui/` collect = 52 (25+22+2+3), 51 passed + 1 failed. Substance true: the one failure is the pre-existing `dashboard.py:347 "%H:%:%S"` ValueError, quoted below — R-W5b-006 |
| FIX-01: ruff 0 full tree | YES | [RAN] `ruff check src tests` → All checks passed |
| FIX-01: mypy total 3 (baseline) | YES | [RAN] `mypy src` → 3 errors, all `sandbox/subprocess_backend.py:548`; scoped 4 files clean |
| FIX-01: docs lock residual = 1 cite (sandbox.md:193) | YES (corroborated) | DOCS2 reports 4 pass + 1 fail at 2d62a44; at HEAD I ran the lock → 5 passed |
| FIX-01: mutation list "caught by construction" | YES (as reasoned) | Framed as mental check, not executed. I executed the tool-pin pair (both fail loudly); wiring/partial-cap/window removals fail their named tests by construction (test asserts the exact removed behavior) |
| FIX-01: R-W5-006 scratch repro executed, split proven | PARTIAL | Scratch deleted; mechanism independently confirmed by reading daemon.py:293-297 vs orchestrator.py:1165-1171. Not re-executed by reviewer |
| FIX-01: allocator docstring already operator-fed (42-50) | YES | Read allocator.py:42-50 — operator-fed statement present |
| FIX-01: no daemon/allocator/types/orchestrator edits | YES | `git diff 0bcb62c..c2e746c --stat` = 4 src + 4 test files only |
| FIX-01: `_PANEL_SCAN_LIMIT=0` → all heads (slice quirk) | YES | `heads[-0:] == heads`; disclosed, never set in prod |
| DOCS2: lock 5 passed | YES | [RAN] docs lock 5 + import lock 21 = 26 dots exit 0 |
| DOCS2: sandbox.md:193 → runtime.py:517 (corrects FIX-01 :446) | YES | :517 is `async def _acquire_sandbox` (read); :446 is `return get_queue` inside `_try_queue` |
| DOCS2: 13 docs files, numbers only | YES | cef92c2 stat = 13 docs files + report; sampled hunks are number-only |
| DOCS2: post-edit holder audit (40 lines) | YES (sampled) | ≥10 holders re-read by reviewer, all sentence-true (§1) |
| DOCS2: pre-existing-drift judgment calls disclosed | YES | Pattern cites + validation:83 + flexability-audit:26 flagged as drift, each re-pointed to a demonstrable holder |

16 claims checked; 1 false (tui count, non-material), 1 partial (deleted repro).

## 3. Long-run test (independent answers)
- **Source:** Cursor fix derives from the DDL type pair (SQLite TEXT vs Postgres UUID), not from tests — re-derived. Attention latest-wins derives from `read_correlation` ordering, verified in store.py. D-FORMULA/D-DERIVATIONS were confirmed sound by REV-W5 and untouched here.
- **Load (100x):** Cost-gate pass output bounded at 100 suggestions + 1 summary + ≤100 per-fact lines regardless of fact count [RAN test]. The scan remains O(N) keyset — necessary. Memory still O(crossings) in `collected` (§7 DO-LATER). Allocation tick bounded (25/pass). attention_top ≤50 full-lineage reads per tick — pre-existing panel shape, unchanged complexity.
- **Hostile input:** Cursor param is a DB-sourced id, parameterized — no injection surface. Hostile stored rows still error-skip one-by-one (unchanged try/except). Score/NaN in panel payloads: filtered by `isinstance` guards (TREV malformed pin remains out-of-scope, disclosed). Producer-lock enumerates the tree deterministically.
- **Operability:** Every feature remains flag-off reversible (cost gate flag, allocator flag, `ledger=` unwrap). Rollback of any single hunk fails its named test (wiring identity, spy shape, cap count, dup pair, 5-task count, partial-cap payload). Observations added: `cost_gate.suggestion_summary`; production `DRAIN_THROTTLED` now live; per-process discriminator means restart telemetry duplicates are bounded and visible, not lost.
- **Reversal:** Removing `ledger=` fails both wiring tests (identity + real-ledger event); removing the first-page branch fails the spy test (SQLite alone would pass — the spy is what pins PG shape); uncapping fails the cap test; deleting the 5th task fails the count test; deleting the 2nd emit fails the partial-cap test; reverting any docs re-point restores non-holder text (lock catches the blank subset). Terminal-drop and default-inert pins pass at any revision by design.

## 4. Findings
### R-W5b-001 [MINOR] Allocator-settings + cost-gate threading still split for non-global Settings (carried residual)
- Location: `src/paa/agents/orchestrator.py:1165-1171`, `src/paa/daemon.py:293-297,418` · Evidence: code reads above; NEED posted; repro mechanism confirmed. Why it matters: a Runtime built with non-global `Settings(allocator.enabled=True)` (tests, embedded use) gets a silent no-op tick (`"disabled"` logged as completed); the daemon curator still ignores `rt.settings.cost_gate`. Default (global) deployments are byte-identical. Recommended fix: the report's §7 change (own orchestrator.py + daemon.py in W5-FIX-02), including the gate-agreement test. Correctly left unlanded here — landing it would have breached the write-set.
### R-W5b-002 [NIT] Daemon start test asserts count, not task identity
- Location: `tests/context/test_allocator_scheduling_p007b.py:334-358`. A swap mutation (drop allocation, duplicate dispatch) keeps 5 tasks and `loops=5`. Recommended: assert the allocation coroutine/name is among the created tasks (e.g. inspect task `get_name()`/coro qualname).
### R-W5b-003 [NIT] Degenerate creator fallbacks remain ledger-less
- Location: `src/paa/daemon.py:344`, `src/paa/api/app.py:292`. Only reachable with hand-built runtimes; worth one comment or a `ledger=self._rt.ledger` when the fallback is ever touched.
### R-W5b-004 [NIT] Producer lock is line-shaped
- Location: `tests/context/test_allocator_scheduling_p007b.py:297-319`. A multi-line `.enqueue(\n StreamName.WORKERS_ALLOC, …)` evades it (disclosed in the test docstring). Current tree clean. DO-LATER: AST-based check.
### R-W5b-005 [NIT] Cross-lineage freshness = head touch time, not allocation time
- Location: `src/paa/tui/runner.py:208`. A lineage touched by unrelated events recently looks fresh even if its allocation is old; identical `updated_at` timestamps tie the winner. Acceptable for an advisory panel; the within-lineage latest-wins is exact.
### R-W5b-006 [NIT] FIX-01 tui count overstated (64 vs 51+1)
- Evidence: collect-only 52 (§2). Non-material: the failure identity and pass substance were verified by reviewer.
### R-W5b-007 [NIT] Pre-existing lock-blind stale cite survives
- `docs/adr/0036-allocator-and-attention-loop.md` → `src/paa/cli/main.py:636` (constant at :650). Pre-dates this fix; known sweep class. One-line re-point when docs are next touched.
### R-W5b-008 [NIT] Duplicate assert retained
- `tests/agents/test_coding_specialist_p011.py:137-138` (pre-existing TREV nit, untouched).

## 5. Attempts to break it
- Tool pin: simulated `"frobnicate"` (uncovered) → read-only pin fails [RAN]; simulated `"delete_everything"` (glob-matched) → exact mutating pin fails [RAN]. Both prior TREV-surviving mutations now caught.
- Producer: grepped every `.enqueue(` in `src/` (6 sites) — no `WORKERS_ALLOC` outside the queue docstring example; non-alloc sites use `ORCHESTRATOR_CORE`/sink. Multi-line evasion reasoned (R-W5b-004).
- PG cursor: reasoned psycopg param passthrough + PG text→uuid cast for a valid UUID string; first-page param count is the engine-relevant property and it is pinned. PG server execution NOT-RUN.
- Attention: reasoned tie on equal `updated_at` (R-W5b-005); empty-heads slice, `heads[-50:]` with <50 heads, and monkeypatched 0 traced through the code.
- Cap: traced `-inf` sort ordering (inf totals first), no-summary on empty collected, and `>=` threshold semantics unchanged.
- Drain: read both emit sites and confirmed emit-before-fetch; `limit <= 0` early-return (pre-existing) emits nothing.
- Daemon: trace of swap mutation (R-W5b-002); tick-raise path exercised by the new test [RAN green].
- NOT attempted: PG live run, full suite, container paths, concurrency under two processes sharing a ledger (reasoned single-process deployment).

## 6. Dimension checklist
- Correctness: PASS — all three MAJORS fixed at the mechanism; formula/derivations untouched and previously confirmed sound.
- Root cause vs symptom: PASS — wiring at the construction site, cursor at the param, freshness at ordering+dedupe; no symptom patches.
- Regression/compat: PASS — kwarg defaults preserve existing constructions; discriminator change is telemetry-only (old events simply don't dedupe against new keys — bounded); curator cursor returns identical result sets on SQLite; EventType set unchanged (diff-checked).
- Concurrency/idempotency: PASS — per-process discriminator preserves in-process dedupe; allocation tick still serialized on the shared curation lock; no new races.
- Error handling: PASS — degrade posture unchanged (emitter swallows, per-row skip, panel isolation).
- Security/abuse: PASS — no new input surface; cursor id is DB-sourced and parameterized.
- Performance/scalability: PASS with DO-LATER heapq for `collected` memory (§7).
- Migration/rollback: PASS — no schema changes; flag-off + unwrap revert paths; docs-only commit cleanly revertible.
- Observability: PASS — summary line, production telemetry now live, tick outcome logging unchanged.
- Maintainability/conventions: PASS — enum style, zero-baseline (ruff 0, mypy 3 baseline), import lock 21 passed [RAN]; duplicate assert (R-W5b-008) pre-existing.
- Docs impact: PASS — lock green [RAN]; holders spot-read ≥10 sentence-true; one pre-existing stale cite (R-W5b-007).

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | curator.py `collected` full list | Every crossing fact materialized before top-K cut | O(N) dicts per pass at 100x (log/report bounded, memory not) | low (heapq.nlargest over iterator + count) | low | DO LATER |
| 2 | producer lock line-regex | Multi-line call evades (R-W5b-004) | Lock completeness only | med (AST walk) | low | DO LATER |
| 3 | discriminator wall-clock rework | Process nonce already closes the restart collision | None new | med | med | NOT WORTH IT |

## 8. Cross-agent / cross-task interactions observed
- The deferred fix's two edits (orchestrator.py + daemon.py) are exactly the files the brief forbade — the NEED correctly names a follow-up task (W5-FIX-02) rather than this wave.
- Curator injection landed while daemon threading deferred: default path unchanged (global fallback inside the ctor), injected path proven by both-direction tests, so no half-wired production state.
- DOCS2 corrected FIX-01's own `:446` proposal to `:517` with rationale — hypothesis corrected by evidence, per standing orders.
- TREV-W5b is running in parallel (untracked TREV-W5b.md present) — no file overlap with this report.

## 9. What I could not verify
- PostgreSQL substrate for the cursor fix (no DSN/server) — shape-pinned only, as all parties disclose.
- Full test suite (standing orders §16) — scoped suites re-run by reviewer: 221-file set, agents+memory, tui, both convention locks.
- The deleted R-W5-006 scratch repro (mechanism verified by read; scratch not recoverable).
- Forked-process discriminator sharing (reasoned; single-process deployment).

## Commands run
- [RAN] `pytest tests/context/test_allocator_scheduling_p007b.py tests/memory/test_cost_gate_p008.py tests/agents/test_coding_specialist_p011.py tests/context/test_metrics.py -q` → 221 dots, exit 0
- [RAN] `pytest tests/agents/ tests/memory/ -q` → exit 0 (557 passed + 1 xskip by count)
- [RAN] `pytest tests/tui/ -q` → 51 passed, 1 FAILED: `test_ledger_with_events` ValueError at `dashboard.py:347 "%H:%:%S"` (pre-existing baseline, identical to REV-W5/TREV-W5 evidence); new attention tests 4/4 pass
- [RAN] `ruff check src tests` → All checks passed; `mypy src` → 3 errors, all pre-existing `sandbox/subprocess_backend.py:548`; scoped mypy on 4 fixed files clean
- [RAN] `pytest tests/conventions/test_docs_references.py tests/conventions/test_import_conventions.py -q` → 26 dots, exit 0
- [RAN] tool-pin failure simulations via live imports (both fail loudly, as pinned)
- [RAN] `git diff 0bcb62c..HEAD --stat` + docs-holder line dumps (§1)
