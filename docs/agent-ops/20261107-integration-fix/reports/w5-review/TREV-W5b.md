<!-- ORCH-DIGEST:START -->
agent: TREV-W5b | scope: wave 5 fix c2e746c+cef92c2 / TREV-W5 §§5-6 survivors | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 20 (17 new + 3 rewritten/renamed) | fail-before/pass-after confirmed: 10 / reasoned 10 (pass-at-base pins, overlay-verified) / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 6 | NIT 4
flaky_suspects: attention-window updated_at tie order (low risk, no flake observed)
missing_edge_cases: P1 1 (deferred threading pin, needs src fix) | P2 4 | P3 3
mutation_survivors: 2 of 14 executed plausible bugs (cap sort-order, threshold >= vs >) + 1 disclosed evasion class (producer multi-line)
changed_behavior_without_tests: allocator-settings threading (deferred, no test); attention malformed/tie/bound + hostile-row/summary remainders (scoped out, note below)
confidence: high (pass-after 74 green in 4 files, fail-before 10/74 at base overlay, 14 mutations executed in TEMP overlay, 3x repeats)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W5b

## 0. Method

- **Blind pass first.** I read the fix diff `0bcb62c..HEAD -- src/ tests/` (4 src
  files: `memory/creator.py`, `memory/curator.py`, `runtime.py`, `tui/runner.py`;
  4 test files) plus the changed src hunks and all four test files *before*
  opening `W5-FIX-01.md` / `W5-FIX-DOCS2.md`. TREV-W5 (my prior review, §5 MAJORs
  R-W5-001..004 + §6 P1s) was the only prior input. Implementer claims were
  audited in §5/§7 only after the blind pass.
- **Pass-after [RAN].** `.venv/Scripts/python.exe -m pytest
  tests/context/test_allocator_scheduling_p007b.py tests/memory/test_cost_gate_p008.py
  tests/agents/test_coding_specialist_p011.py -q` → **51 passed**
  (16 + 14 + 21). Plus `tests/tui/test_dashboard.py -k "not test_ledger_with_events"`
  → **74 passed / 1 deselected** across all four files (24 dashboard tests
  collected; the excluded `test_ledger_with_events` fails with the pre-existing
  `dashboard.py:347 "%H:%:%S"` `ValueError`, identical at base — category B,
  not a fix regression).
- **Fail-before [RAN].** Extracted `0bcb62c:src` to a TEMP overlay
  (`%TEMP%\w5b_base`, verified `paa.context.metrics.__file__` resolved into the
  overlay) and ran the four HEAD test files with `PYTHONPATH` pointed at it:
  **10 failed / 64 passed** of 74. The 10 failures are exactly the fix-behavior
  tests (discriminator, 2 wiring, cursor spy, cap, 2 injection, 3 attention —
  listed in §1); the other 10 new/changed tests pass at base because they pin
  pre-existing correct behavior (see table). Overlay removed afterwards.
- **Mutations [RAN, TEMP overlay only].** 14 plausible bugs applied to a TEMP
  copy of HEAD `src/` (`%TEMP%\w5b_head`), each followed by the targeted test and
  a restore. 12 caught, 2 survive (§3). The repo was never edited.
- **Repeats [RAN].** The 3 non-dashboard files ran 3× consecutively (all green).
  Dashboard attention tests green on their runs. No reverse-order run (the
  isolation-sensitive coding runtime tests pre-date the fix; R-W5-011 residual
  noted in §5).
- **No full-suite run** (standing orders §16). Agents+memory 557 count from
  W5-FIX-01 §5 is **NOT-RUN** by me (implementer-claimed, spot-consistent with
  the 51 file-scoped green). Ruff/mypy not re-run by me; FIX-01 claims
  ruff 0 / mypy 3-baseline [ASSUMED from report, not verified].

## 1. Per test/suite assessment

Legend: Fb = fails at base `0bcb62c` [RAN via overlay]. "Pin" = passes at base by
design (pins pre-existing correct behavior the fix did not change).

### 1a. `tests/agents/test_coding_specialist_p011.py` (3 changed, 0 net-new)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_every_mutating_tool_is_covered_by_policy` (rewritten) | **Yes** — exact mutating set `{"submit_task","run_task","remember_fact"}` + exact read-only half `set(READ_ONLY_TOOLS)` + union equality | Yes for the claimed cases [RAN mutations M1/M1b]: adding `"delete_everything"` (glob-matched) **and** adding `"summarize"` (unmatched) both fail. **Pin** at base (surface unchanged — correct) | Yes | No | **R-W5-001 FIXED.** The tautology is gone: the test now derives the mutating set the same way the policy does, then pins its *value*, and pins the read-only half against `READ_ONLY_TOOLS` from the source — so a new tool in either class fails loudly until a human classifies it. `coding.py:55-65` surface (6 read-only + 3 mutating) matches. Verified the derivation by hand: none of the 3 mutating names matches any `DEFAULT_MUTATING_TOOLS` glob (`permissions.py:127-137`), so all 3 enter via `MUTATING_RUNTIME_TOOLS` — the exact-set assert is what makes a 4th member loud |
| `test_specialist_package_exports_only_coding` (rewritten) | Yes — file set `["__init__.py","coding.py"]` | Pin at base; [RAN mutation M2] creating `specialists/research.py` in the overlay **fails** it | Yes | No | **R-W5-005 FIXED.** Reads the directory off the imported package (`Path(pkg.__file__).parent`), the correct lock for "no stubs" since `__init__.py` never imports submodules |
| `test_fifty_scripted_tasks_delegate_successfully_over_50_lineages` (renamed) | Partially — counts successes over 50 fresh lineages | Pin (same body as before) | Yes — fresh lineage per task (avoids node ceiling) | Scripted model always succeeds; target hardcoded `"coding"` | **R-W5-002 partially addressed (claim fixed, gate weakened).** Name + docstring now honestly state delegation-success, named-target router bypass per ADR-0041, and no classifier in path — exactly what TREV asked. **But** the threshold dropped from `== 50` to `>= 45` (test:218): up to 5 silent delegation failures now pass. With a deterministic scripted model there is no reason to tolerate any failure; the 90% gate belongs to a future classifier test, not this one (R-W5b-01, MINOR) |

### 1b. `tests/context/test_allocator_scheduling_p007b.py` (7 new, 1 replaced, 8→16)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_partial_cap_breach_emits_with_effective_limit` | Yes — cap 2, indexed 1, `limit=100` → 1 event, `effective_limit==1`, `threshold==2`, `limit==100`, returns 0 | Pin at base (2nd emit site pre-exists); [RAN mutation M4] deleting the 2nd emit **fails** it | Yes — window pinned via private attrs (no wall-clock), same accepted pattern as TREV-W5 §4 | FakeStore re-implements dedupe faithfully (TREV-W5 §1c, unchanged) | **R-W5-004 FIXED.** Small correctness note in its favor: the test asserts `effective_limit==1` (the *effective* value, not budget or limit) and uses `limit=100` so budget (1) < limit — the only shape that reaches the 2nd site without hitting the full-defer branch (`creator.py:1596-1614`) |
| `test_discriminator_carries_process_id` | Yes — `_DRAIN_PROCESS_ID` in discriminator, `drain_throttled:` prefix | Yes (Fb + [RAN M12] dropping the nonce fails) | Yes — window pinned; nonce is process-constant | FakeStore | Verifies the REV R-W5-004 fix. Asserts presence, not exact shape — an exact-shape assert (3 segments) would be marginally stronger; low value |
| `test_start_creates_five_supervised_tasks` | Yes — `len(daemon._tasks)==5` + `daemon.started` log with `loops==5`, all on stubbed-idle loops | Pin at base; [RAN M3] deleting the allocation task **fails** it (count 4) | Yes — no timing; `stop()` cancels | Loops stubbed, but that is the unit under test (scheduling, not tick) | **R-W5-003 (scheduling half) FIXED.** Weakness: it pins *count*, not *identity* — swapping the allocation task for a duplicate dispatch passes. The deletion disease (R-W4-002) is caught; a swap mutant survives (R-W5b-02, MINOR). A name assertion would need task introspection the test currently avoids |
| `test_tick_raise_logs_and_loop_continues` | Yes — orchestrator raises once then succeeds; `calls==2`, `daemon.allocation_failed` + `daemon.allocation_completed` logged | Pin at base; [RAN M11] removing the tick call **fails** it | Yes — fake `_sleep` sets `_stop` after 2 passes, mirroring the reflection-tick shape; no wall-clock | FlakyOrchestrator raises `RuntimeError("boom")` then returns `underfed` | **R-W5-003 (survival half) FIXED.** The loop body, the log event, and continuation are all asserted together — the three things a naive tick test would miss. Note it cannot detect the deferred settings gate-split (R-W5-006): the fake accepts no `settings=` kwarg and the daemon passes none |
| `test_build_memory_creator_threads_ledger_identity` | Yes — `creator._ledger is ledger` over the *real* `_build_memory_creator` | Yes (Fb: base signature has no `ledger=` → send would `TypeError`; [RAN M10] dropping the forward fails) | Yes — `tmp_path` home (fixed per FIX-01 §4 self-review finding) | No — real builder, real ledger fixture | **R-W5-001 wiring FIXED (builder half).** Identity (`is`) rather than type-shape is the right assert for threading |
| `test_runtime_build_creator_breach_emits_to_real_ledger` | Yes — full `Runtime.build`, window exhausted, 1 real `DRAIN_THROTTLED` read back from the real ledger | Yes (Fb + M10) | Yes — window pinned; `runtime.close()` in `finally` | `_ScriptedModel` stub is for the runtime build, not the unit under test | **R-W5-001 wiring FIXED (production half).** This is the test W5-IMPL-03's NEED said was missing: it proves the production path emits, not just the creator-side emit with fakes |
| `test_no_workers_alloc_enqueue_outside_queue_docstring` | Yes — line-shaped grep over `src/`: `.enqueue(` + `WORKERS_ALLOC`/`workers:alloc` off the one allowed file (`storage/queue/__init__.py`, whose line 13 is the docstring example) | Pin (no offender at base); predicate executed on synthetic lines [RAN §0]: same-line call **matches**, multi-line call **evades** | Yes — pure file scan, UTF-8 reads | No | **R-W5-006 (producer half) FIXED as far as a line lock goes, with the evasion honestly disclosed** in the test docstring and FIX-01 §4. Two residuals: multi-line enqueue evades (by construction) and the allow-list exempts the *whole* queue `__init__.py`, so a producer added into that file also evades (R-W5b-03, MINOR) |
| `test_operator_fed_statement_present` (replaces docstring test) | Substrings only ("operator-fed", "workers:alloc") | Pin | Yes | No | Same assertions as the old test, now sitting beside the grep lock that does the real work. Fine as documentation, must not be read as a lock — the class docstring now says this correctly |

### 1c. `tests/memory/test_cost_gate_p008.py` (5 new, 9→14)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_scan_crosses_multiple_pages_no_duplicates` | Yes — 12 facts, batch 5, all ids suggested, no dups | **Pin** (SQLite pagination worked at base — the cursor bug was PG-only) | Yes | No | Correct multi-page behavior pin; the PG half is the spy's job (below). `sorted()` comparison is appropriately order-insensitive here |
| `test_first_page_issues_no_id_param` | Yes — spy on `db.fetch_all`: first page SQL has no `id >`, params exactly `(batch,)`; ≥2 pages; page 2 has `id >` | Yes (Fb + [RAN M6] reverting the first page to `id > ?` with `""` fails) | Yes — deterministic call-shape assert, no timing | Spy wraps the real `fetch_all` (delegates), not a stub | **R-W5-002 pinned by query shape**, honestly labeled NOT-RUN on PG in the docstring. Gap: it asserts page-2 SQL but not that page-2's param *is* a real id; the `[RAN M6]` revert is still caught, and a page-2-`""` mutant would repeat page 1 and fail the no-duplicates test — the pair covers it, marginally |
| `test_suggestions_capped_at_k_with_summary_count` | Partially — cap (limit→5) + 10 facts → 5 kept, one `suggestion_summary` line (total 10, kept 5), exactly 5 per-fact lines | Yes (Fb: no cap/summary) + [RAN M8] uncapping fails | Yes | No | **Cap boundedness FIXED, top-K order NOT pinned (R-W5b-01).** All 10 facts are `inf` totals, so kept-set is the 5 lowest fact_ids by the tie-break; the test asserts only counts. [RAN M8b] flipping the sort to ascending (keep *lowest*-cost) still passes. Asserting the kept set (e.g. with distinct finite totals, assert the 5 highest survive) is a 3-line addition. Also: UUID fact_ids are random per run, so even a kept-set assert must use distinct totals, not rely on id order |
| `test_injected_enabled_gate_suggests_with_global_disabled` + `test_injected_disabled_gate_overrides_global_enabled` | Yes — both directions of injector-wins | Yes (Fb: base ctor has no `cost_gate=` → `TypeError`) | Yes — env + `reset_settings_cache` via helpers; real curator | No | **R-W5-008 curator-side FIXED.** The production half (daemon `cost_gate=` at `daemon.py:418`) is the deferred NEED — no test for it exists or can pass until the src line lands (§7) |

### 1d. `tests/tui/test_dashboard.py` (4 new attention tests, 20→24)

All four use the **real ledger + real `_attention_top`/`build_panel_fields`** —
no mocks, no over-mocking anywhere in the new tests.

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Notes |
|---|---|---|---|---|
| `test_attention_top_same_item_twice_keeps_latest` | Yes — 1 row, score 0.2 (latest, not best 0.8) | Yes (Fb: base appends both) + [RAN M7b] unique-key dedupe break fails | Yes | The "latest-wins, not max" direction is the load-bearing semantic and is correctly distinguished from a max-keep regression |
| `test_attention_top_later_lineage_wins_across_lineages` | Yes — cross-lineage overwrite, newer lineage's score + correlation carried | Yes (Fb) | Yes, modulo the `updated_at` tie caveat (see §4) | Depends on `open_correlations` returning `updated_at ASC` (`store.py:357`) so the newer lineage iterates later — a real dependency on the store contract, correctly exercised through the real store rather than assumed |
| `test_attention_top_window_is_most_recent_lineages` | Yes — limit stubbed to 2, oldest highest-score lineage excluded | Yes (Fb) + [RAN M7] head-window flip fails | Yes — count + scores + order all deterministic *given* insertion order = `updated_at` order | The right recency pin. Risk note only (§4): `ORDER BY updated_at ASC` has no unique tie-break, so two lineages created in the same timestamp grain could order arbitrarily. No flake observed across repeats |
| `test_attention_top_terminal_lineage_drops_out` | Yes — `MUTATION_COMMITTED` closes the lineage → panel `None` | **Pin** (terminal filter pre-exists; the fix documents it) | Yes | Honest documented-behavior pin. Matches the new `_attention_top` docstring contract ("panel shows in-flight attention only") |

## 2. Coverage of changed lines/branches, error paths, boundaries

[Reasoned from the diff + test bodies; no new coverage tooling run beyond TREV-W5's
baseline. Changed-line mapping:]

- `runtime.py` ledger threading — both halves covered (builder identity +
  production breach → real ledger). Mutation-verified (M10).
- `creator.py` discriminator nonce — presence pinned; same-window dedupe under
  the new key is still covered by the pre-existing `test_second_breach_same_window_dedupes`
  (renders green in pass-after). The full-defer + partial-cap branches are now
  both pinned (TREV-W5 34% creator coverage gap closed for the emit sites).
  Emit-failure swallow log (`creator.py:2058-2059` region) remains unpinned
  (TREV §6 P2-11, out of FIX scope — carried).
- `curator.py` cursor — both branches pinned (first-page shape spy + multi-page
  behavior). Cap block pinned for count/summary/log-count but **not** for
  sort-order/kept-set (R-W5b-01) and not for the empty-`collected` no-summary
  branch. Injection both directions pinned. Hostile-row skip, empty-batch
  `break`, `use_count<0` raise, and `summary()` count-only exclusion remain
  unpinned (TREV R-W5-009, scoped out — carried).
- `daemon.py` — scheduling + tick-failure now pinned (was the headline gap).
  Curation-lock contention for the allocation tick remains unpinned (TREV §6
  P3-14, hard-to-test without hooks — carried, low value).
- `runner.py::_attention_top` — dedupe, latest-wins, recency window, terminal
  drop now pinned. Malformed payloads, tie-break, `max_results` fallbacks,
  per-lineage read-failure skip, `_PANEL_SCAN_LIMIT` bound, and panel-isolation
  `except` remain unpinned (TREV R-W5-008, scoped out — carried except the
  four new tests, which are the R-W5-003 core).
- `coding.py` tool surface — exact-set pins close R-W5-001/005. The
  node-ceiling block emit, discriminator-collision question, and self-cycle
  event-presence (TREV R-W5-010) remain unpinned (scoped out — carried).
- **Boundary that is still missing by scope**: `total == threshold` exactly.
  TREV R-W5-007 was P1 in §6 but FIX-01 §9 explicitly lists it as skipped per
  brief. [RAN M9] `>=`→`>` passes all 14 cost-gate tests — confirmed survivor,
  carried as R-W5b-04 (MINOR, one-line test: threshold `2.0` against the
  hand-computed total-2 fact).

## 3. Mutation thinking

All mutations [RAN] against a TEMP copy of HEAD `src/`; the repo was never edited.
`M4-control` (no-op anchor) was a sanity check that the harness restores state —
all other rows are real one-edit mutants.

| Plausible bug | Caught? | By which test |
|---|---|---|
| Tool surface: add uncovered mutating `"delete_everything"` | **Caught** | `test_every_mutating_tool_is_covered_by_policy` (M1) |
| Tool surface: add unmatched `"summarize"` | **Caught** | same (M1b) |
| Stub: add `specialists/research.py` | **Caught** | `test_specialist_package_exports_only_coding` (M2) |
| Daemon: delete `start()` allocation task | **Caught** | `test_start_creates_five_supervised_tasks` (M3) |
| Daemon: swap allocation task for a duplicate dispatch | Survives (count still 5) | — (R-W5b-02, reasoned not executed) |
| Drain: delete the partial-cap (2nd) emit | **Caught** | `test_partial_cap_breach_emits_with_effective_limit` (M4) |
| Drain: drop `_DRAIN_PROCESS_ID` from discriminator | **Caught** | `test_discriminator_carries_process_id` (M12) |
| Wiring: drop `ledger=ledger` forward | **Caught** | both `TestProductionWiring` tests (M10) |
| Cursor: first page back to `id > ?` with `""` | **Caught** | `test_first_page_issues_no_id_param` (M6) |
| Cap: remove the cap (`kept = collected`) | **Caught** | `test_suggestions_capped_at_k_with_summary_count` (M8) |
| Cap: sort ascending (keep lowest-cost) | **Survives** | — (R-W5b-01, M8b) |
| Threshold: `>=` → `>` | **Survives** | — (R-W5b-04 = TREV R-W5-007, M9) |
| Attention: head window instead of tail | **Caught** | `test_attention_top_window_is_most_recent_lineages` (M7) |
| Attention: break dedupe (unique per-event key) | **Caught** | `test_attention_top_same_item_twice_keeps_latest` (M7b) |
| Tick: remove `_run_allocation_tick()` call from loop | **Caught** | `test_tick_raise_logs_and_loop_continues` (M11) |

**12 caught / 2 survived (of 14 executed)**, plus 1 disclosed-by-construction
evasion class (producer multi-line enqueue; predicate executed on synthetic
lines [RAN §1b]). **All four TREV-W5 MAJOR survivors are now caught:** R-W5-001
(M1/M1b), R-W5-002 claim-level (rename — see R-W5b-06 for the residual
threshold softening), R-W5-003 (M3/M11), R-W5-004 (M4). The two survivors are
both MINOR and both are test-strength gaps, not behavior gaps — the code sorts
descending and clips at `>=` correctly today; the tests cannot tell.

## 4. Flakiness evidence (repeated runs) and test-suite health

- 3 consecutive runs of the 51 tests in the three non-dashboard files: all
  green [RAN]. Dashboard attention tests green on the pass-after run. No
  reverse-order run; the only order-sensitive surface (coding runtime tests
  bypassing `paa_home`/`reset_settings_cache`, TREV R-W5-011) is untouched by
  the fix and still carries the same nit (R-W5b-07, NIT).
- No wall-clock asserts anywhere new: drain windows pinned by direct attribute
  set (deterministic, accepted per TREV-W5 §4); daemon tick-raise uses a fake
  `_sleep` (no real sleep, no hang surface — the fake sets `_stop` after 2
  passes and the loop condition is re-checked); `start()` test awaits
  `stop()` which cancels the stubbed loops.
- **Flaky suspect (low, unobserved):** `test_attention_top_window_is_most_recent_lineages`
  and `test_attention_top_later_lineage_wins_across_lineages` depend on
  `open_correlations` ordering by `updated_at ASC` (`store.py:357`), which has
  **no unique secondary key** — two lineages created within the same timestamp
  grain order arbitrarily, and the recency window would then be non-deterministic.
  AGENTS.md's "A `LIMIT` cut needs a total order" rule applies to this query shape.
  Creation was sequential in the test and the `db`/`ledger` fixtures are
  function-scoped, so the risk is small, but the test suite's green runs do not
  prove the tie case impossible (R-W5b-05, MINOR). A store-side tie-break
  (`updated_at, correlation_id`) or a test-side timestamp stagger would close it.
- Suite health: the two `Runtime.build` tests (coding registration + production
  wiring) remain the slowest W5 files but are tmp-isolated and bounded. The
  producer grep walks all of `src/` per run (~hundreds of files, fast enough to
  stay in the file's 0.61 s scoped run [RAN]).

## 5. Findings (R-W5b-nnn, severity, location, evidence, recommended fix)

### MINOR

- **R-W5b-01 — Suggestion-cap test does not pin which suggestions are kept.**
  `tests/memory/test_cost_gate_p008.py::TestCostGateSuggestionCap`
  (`test_suggestions_capped_at_k_with_summary_count`, :256-281) asserts counts
  (5 kept, summary total=10/kept=5, 5 per-fact logs) but not the kept set. All
  10 facts have `total_cost == inf`, so any 5-subset satisfies it. [RAN M8b:
  flipping `collected.sort` to ascending in `curator.py:547-549` — keeping the
  *lowest*-cost entries, contradicting the test's own class docstring "top-K
  kept" — passes all 14 cost-gate tests.] **Fix:** give the facts distinct
  finite totals (vary `importance`/`confidence`) and assert the kept
  `total_cost`s are the 5 highest (order matters: assert
  `report.cost_gate_suggestions` is sorted `-total_cost` desc). Note UUID
  fact_ids make the id tie-break unassertable across runs — use distinct
  totals, not id order, for the kept-set assert.
- **R-W5b-02 — `start()` test pins task count, not task identity.**
  `tests/context/test_allocator_scheduling_p007b.py:334-358`. Deleting the
  allocation task is caught [RAN M3], but *replacing* it with a duplicate of
  another loop (a one-line copy/paste slip in the task list) leaves
  `len==5` and `loops==5` green. **Fix (small):** assert each task's coroutine
  name or wrap-and-record which loop functions were scheduled. Alternatively
  capture the `name` arg of `_supervise` via a spy — but `_supervise` is
  internal; cheapest honest form is `asyncio.current_task` name introspection
  or a scheduled-with recorder. Low priority: the realistic disease (deletion)
  is already caught.
- **R-W5b-03 — Producer lock: two evasion classes (one disclosed).**
  `tests/context/test_allocator_scheduling_p007b.py:297-319`. (a) Multi-line
  `enqueue(\n  WORKERS_ALLOC\n)` evades the line-shaped predicate — disclosed
  in the test docstring and FIX-01 §4, confirmed by predicate execution [RAN
  §1b]. (b) Undisclosed: the allow-list exempts the **whole**
  `storage/queue/__init__.py` file, so a producer added *inside* that file
  evades too. **Fix (optional):** none required for this wave given disclosure;
  if the lock is hardened later, exempt by line range (the docstring example
  line 13) rather than by file, and note the multi-line limit in the allocator
  docstring where a W6 producer author would read it.
- **R-W5b-04 — Exact-threshold boundary still survives (TREV R-W5-007, carried).**
  `curator.py:534` `>=` vs `>`: above uses total 2 ≥ 1.0, below uses 2 < 3.0.
  [RAN M9] `>=`→`>` passes all 14 cost-gate tests. FIX-01 §9 discloses this as
  skipped per brief, so this is a carried residual, not a fix defect. **Fix:**
  one test with threshold exactly `2.0` (the hand-computed total) asserting 1
  suggestion.
- **R-W5b-05 — Attention recency tests ride an un-tied `ORDER BY`.**
  `tests/tui/test_dashboard.py:602-659` + `src/paa/ledger/store.py:357`
  (`open_correlations` orders by `updated_at ASC` alone). Per AGENTS.md's
  total-order rule, ties at the window cut are undefined. No flake observed
  across repeats [RAN §0/§4], but the tests themselves cannot distinguish
  "recency works" from "timestamps happened to order." **Fix:** either give
  `open_correlations` a unique secondary key (store change — outside this fix's
  test scope, acceptable as a follow-up) or stagger the test lineages'
  `updated_at` explicitly so the window assertion does not depend on storage
  timestamps.
- **R-W5b-06 — 50-task threshold softened from `== 50` to `>= 45`.**
  `tests/agents/test_coding_specialist_p011.py:218`. The rename/docstring
  honesty (TREV R-W5-002's core ask) is done well, but the 90% gate lets up to
  5 silent delegation failures pass with a deterministic scripted model —
  failures that would be invisible (the loop counts only `ok`). **Fix:**
  restore `assert routed == 50` (or at minimum log each non-ok task id). The
  90% number belongs in the future classifier-precision test the docstring
  describes, where it gates a stochastic classifier.

### NIT

- **R-W5b-07 — R-W5-011 isolation nit carried:** coding runtime tests still
  build `Settings(home=tmp_path/…)` without `paa_home`/`reset_settings_cache`.
  Green in both orders; harden when next touched. The duplicate assert
  `test_registered_name_is_coding` (:137-138, two identical lines) also carried.
- **R-W5b-08 — Discriminator shape asserted by substring, not exact.**
  `test_discriminator_carries_process_id` checks prefix + nonce presence but
  not the 3-segment shape (`drain_throttled:<proc>:<ms>:<cap>`). A trailing
  garbage segment would pass. One `split(":")` assert would pin it.
- **R-W5b-09 — Cursor spy does not assert page-2 param is a real id.**
  `tests/memory/test_cost_gate_p008.py:219-249` checks page-2 SQL contains
  `id >` but not `pages[1]["params"][0]` is a non-empty id. Covered in
  combination with the multi-page no-duplicates test (a page-2 `""` repeats
  page 1 → dups → fails), but the spy's own docstring promises "a real id
  value" — assert it.
- **R-W5b-10 — `TestProducerLock.test_operator_fed_statement_present` duplicates
  the old docstring test verbatim.** Harmless now that the grep is the real
  lock, but the file header comment (`test_allocator_scheduling_p007b.py:9-11`,
  "Reversal: …a synthetic producer would break the docstring pin") still
  describes the *old, inaccurate* claim TREV-W5 R-W5-006 corrected. Update
  the header to credit the grep lock.

## 6. Missing edge cases and tests (prioritized)

### P1 (must add — but blocked on a src fix, not a test fix)

1. **Allocator-settings threading pin (REV R-W5-006 / TREV R-W5-008 production
   half).** Scenario: Runtime with non-global `Settings` where
   `settings.allocator.enabled=True` but global disabled (and vice versa) →
   tick and allocator agree (completed pass vs `None`, never `"disabled"`
   logged as completed). Expected: new test fails until
   `orchestrator.run_allocation(settings=…)` + `daemon.py:297` threading lands.
   Belongs in `test_allocator_scheduling_p007b.py::TestDaemonScheduling` once
   W5-FIX-02 owns `orchestrator.py` + `daemon.py`. **No test exists today** —
   confirmed by grep [RAN §1c]: `run_allocation` still reads
   `get_settings().allocator` at `orchestrator.py:1171`, and the FakeOrchestrator
   takes no `settings=` kwarg. This is the one changed-behavior-without-tests
   item; unlike the above it needs the src fix first (FIX-01 §7 scratch repro
   is the evidence, honestly not committed as a red pin).

### P2 (should add — all carried from TREV-W5, scoped out of FIX §3)

2. **Attention malformed/tie/bound** (TREV §6 P2-5): bool/string score skipped,
   non-dict payload skipped, equal scores → `item_id` asc, `max_results`
   0/-1/`"x"` → 5, one lineage raising on `read_correlation` (others still rank).
   Table-driven, belongs in `tests/tui/test_dashboard.py` beside the new
   freshness tests.
3. **Cost hostile rows + pagination edge + summary** (TREV §6 P2-6): SQL-insert
   `importance=2.0` / `initial_confidence=2.0` / `use_count=-1` / text rows →
   `errors>=1`, valid facts still suggested; `summary()` carries an int count
   and no `inf`. None of these were in FIX §3 (pagination half was).
4. **Emit-failure swallow** (TREV §6 P2-11): ledger whose `append` raises →
   drain returns normally, `memory.drain_throttled_emit_failed` logged.
5. **Self-cycle event assert + node-ceiling block emit** (TREV R-W5-010):
   assert `STATE_ROLLBACK_TRIGGERED` in `test_self_delegation_is_cycle_block`;
   exceed `max_plan_nodes` → emit with `reason=delegation_recursion_block`.
   Also decide+pin same-depth distinct-block dedupe semantics.

### P3 (nice to have)

6. Router classification precision when a classifier exists (the honest T12
   gate; docstring already scopes it correctly).
7. `threshold=inf` curve, superseded-fact exclusion from the cost scan, window
   rollover after 1 s (second window re-emits), tick-vs-maintenance lock
   contention flag fake.

Categories considered and judged N/A with reason: unicode/encoding and ReDoS
(no pattern input in the diff); oversized payloads (suggestion cap now bounds
report/log growth — the one load concern in the diff — and is pinned for
count); authz bypass (no auth surface changed); backward compatibility
(default-off flags, optional `ledger=`/`cost_gate=` kwargs, additive enum —
pins pass at base as verified); clock/timezone (windows pinned, not slept).

## 7. What I could not verify

- **Full suite** — not run (standing orders §16). Scoped suites around the diff
  (74 selected) are green; `ruff`/`mypy` and the agents+memory 557 count are
  implementer-claimed [ASSUMED from W5-FIX-01 §5, not re-run by me].
- **Postgres cursor correctness** — PG suite NOT-RUN (no DSN), same as the fix
  author. The PG-shape spy is a query-shape pin, genuinely strong for what it
  is (the exact param tuple the PG driver would receive), but it is not a
  PG execution. Never claimed green.
- **Production `cost_gate=` threading from the daemon** — the NEED is real and
  verified in code (`daemon.py:418` constructs `MemoryCurator` without
  `cost_gate=`, so production still reads the global). The curator-side
  injection tests prove the mechanism both ways; they do not prove the wire.
  Residual, not a test defect.
- **D-FORMULA / D-DERIVATIONS choices** — unchanged from TREV-W5 §7: the tests
  pin the ratio shape as implemented; whether the shape is *intended* remains
  an operator/ADR-owner call with `REVIEW_REQUIRED`.
- **Docs-lock residual** — W5-FIX-DOCS2 claims lock 5/5 green at `cef92c2`
  with one correction (sandbox.md:193 → `runtime.py:517`, not FIX-01's
  proposed :446). Not re-run by me; not a test-quality finding either way.
- **Mutation `summary()`-leak** — verdict by reading only (no test asserts the
  summary either way, §6 P2-3), not executed. The other 14 were executed.

## Claims audit (W5-FIX-01 §5 vs my runs)

- "221 passed, 0 failed" (4 files): **consistent** — my 4-file run is 74 passed
  (51 in 3 files, dashboard 23 of 24 excluding the pre-existing strftime
  failure; the 221 includes `tests/context/test_metrics.py` 170, which I did
  not re-run as out of fix scope). The fix-surface subset (74) is green [RAN].
- "agents+memory 557 passed, 1 x-skip" / "tui 64 passed, 1 FAILED (pre-existing
  strftime)": **spot-consistent** — tui failure reproduced exactly (same test,
  same `ValueError` at `dashboard.py:347`) [RAN]. The 557 was NOT-RUN by me.
- "ruff 0 / mypy 3 pre-existing": [ASSUMED from report, not verified].
- §4 mutation list: **12 of 12 claimed-caught mutations confirmed by my own
  executed runs** (wiring, cursor spy, uncap, auto-delete n/a-changed,
  dedupe, window, start-deletion, 2nd-emit, stub, same-line producer predicate);
  "adding a same-line `.enqueue(WORKERS_ALLOC)` anywhere fails the producer
  lock" is true for *any file except the exempted queue `__init__.py`* — the
  report's §4 doesn't name the exemption, a small overstatement (R-W5b-03b).
- §7 DEFERRED-DEPENDENCY (R-W5-006): **accurate and honestly evidenced** —
  `orchestrator.py:1144,1171` confirm the split from the code side; no red pin
  was committed (correct call: a red test would break the suite).
- §9 scope honesty: **accurate** — exact-threshold, attention malformed/tie/
  bound, hostile-row/summary (except pagination), block-emit asymmetries, and
  R-W5-011 isolation are all correctly listed as skipped, and each maps to a
  carried item in §6 above. No claim of coverage that the tests don't have —
  except the small producer-lock overstatement noted above and the `>= 45`
  threshold, which the report doesn't mention weakening (R-W5b-06).

## COMMS last line read

`2026-11-07T20:35:00Z W5-FIX-DOCS2 -> ORCH | DONE | W5-FIX-DOCS2` (156 lines at
start; re-read before finishing — no new lines relevant to this review).

