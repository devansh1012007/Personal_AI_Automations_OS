<!-- ORCH-DIGEST:START -->
agent: TREV-W5 | scope: wave 5 / T-008-P008, T-011-P011, T-007b-P007b, docs 1268d54 | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 58 | fail-before/pass-after confirmed: 53 / reasoned: 0 / not-possible: 0 (5 pass-at-base pins verified by run)
findings: BLOCKER 0 | MAJOR 4 | MINOR 7 | NIT 3
flaky_suspects: none
missing_edge_cases: P1 4 | P2 7 | P3 5
mutation_survivors: 5 of 11 plausible bugs not caught
changed_behavior_without_tests: daemon _allocation_loop/start, drain partial-cap emit, attention_top skip/malformed branches, curator hostile-row + summary count, node-limit block emit, handle() model=None / non-list tool_calls
confidence: high (fail-before run against base overlay + 11 mutations actually executed in TEMP overlay; pass-after 228 green)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W5

## 0. Method

- **Blind pass first.** I read the full diff `1d7c5bf..HEAD -- src/ tests/` and all four
  test surfaces (`tests/memory/test_cost_gate_p008.py` 190 lines,
  `tests/agents/test_coding_specialist_p011.py` 402 lines,
  `tests/context/test_allocator_scheduling_p007b.py` 249 lines, appended
  `tests/context/test_metrics.py` +94 and `tests/tui/test_dashboard.py` +63)
  plus the changed src (`metrics.py`, `curator.py`, `orchestrator.py`,
  `specialists/coding.py`, `runtime.py`, `daemon.py`, `creator.py`,
  `tui/runner.py`, `allocator.py` docstring, `core/types.py`) *before* opening
  any implementer report. Implementer reports (`W5-IMPL-01/02/03`, `W5-DOCS`)
  were read only after that pass, for the claims audit in §5/§7.
- **Pass-after [RAN].**
  `.venv/Scripts/python.exe -m pytest tests/memory/test_cost_gate_p008.py tests/agents/test_coding_specialist_p011.py tests/context/test_allocator_scheduling_p007b.py tests/context/test_metrics.py tests/tui/test_dashboard.py -k "not test_ledger_with_events"`
  → **228 passed** (9 + 21 + 8 + 170 + 20). The excluded
  `test_ledger_with_events` fails at HEAD *and* at base with the identical
  pre-existing `dashboard.py:347 "%H:%:%S"` typo [RAN against base overlay,
  same `ValueError: Invalid format string` at `dashboard.py:347`] — it is
  baseline category B, not a W5 regression. W5-IMPL-03's FINDING on it is accurate.
- **Fail-before [RAN, no repo edits].** I extracted `1d7c5bf:src` into a TEMP
  overlay (`git archive --format=tar`, `tarfile` extract to
  `%TEMP%\w5base`) and ran the HEAD test files with
  `$env:PYTHONPATH="$env:TEMP\w5base\src"` (verified
  `paa.context.metrics.__file__` resolved into the overlay and
  `hasattr(total_memory_cost) == False`). Result: **53 failed / 175 passed**
  of the 228 selected. Of the 58 W5-*added* tests, 53 fail at base and 5 pass
  (all negative/back-compat pins that correctly hold at base — see §1).
  Failures are for the right reasons (spot-checked `--tb=line`: e.g.
  `attention_top` ranked fails on `isinstance(top, list)` because base returns
  `None`; cost-gate fails on missing `total_memory_cost`/`cost_gate_suggestions`).
  Overlay removed afterwards; `git status` shows only this report as my artifact.
- **Mutations [RAN, TEMP overlay only].** 11 plausible bugs were applied to a
  TEMP copy of HEAD `src/` (`%TEMP%\w5mut_src`, via scripts in `%TEMP%`, never
  to the repo) and the relevant suite re-run. 6 caught, 5 survived (§3).
- **Repeat/order [RAN].** The 38 tests in the three new files were run 3×
  consecutively (all green) and once in reverse file order (all green).
  Hypothesis suites (`test_total_memory_cost_never_nan`,
  `test_total_memory_cost_decreases_with_utility`, run standalone and inside
  the full file) green.
- **Coverage [RAN]** with `--cov` over the 228 selected tests: `specialists/coding.py`
  100%, `metrics.py` 99% (misses at 274/630 are pre-existing, not W5 lines),
  `curator.py` 77%, `daemon.py` 26%, `creator.py` 34%, `tui/runner.py` 41%.
  The W5-relevant uncovered lines are listed in §2 and §5.
- **Gate [RAN].** `ruff check src tests` → All checks passed (0).
  `mypy src` → 3 errors, all pre-existing `sandbox/subprocess_backend.py:548`
  (baseline A-001, unchanged) [RAN].
  `tests/conventions/test_docs_references.py` → 5 passed [RAN]; spot-checked
  re-points (`curator.py:160`, `orchestrator.py:837`, `daemon.py:173`,
  `types.py:242` all hold their cited symbols).
- No full-suite run (standing orders §16). No `NOT-RUN` claims beyond that and
  the items in §7.

## 1. Per test/suite assessment

Legend: Fb = fails at base `1d7c5bf` [RAN via overlay]. "Pin" = passes at base
by design (negative/back-compat guard).

### 1a. `tests/memory/test_cost_gate_p008.py` (9 tests, all Fb-fail, all pass at HEAD)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_disabled_emits_no_suggestions_and_calls_no_cost` | Yes — empty suggestions + spy zero calls | Yes (Fb + mutation: gate→`if True` fails) | Yes — `db`+`paa_home` (teardown resets settings cache, `tests/conftest.py:30`), monkeypatch env | Spy patches `metrics_mod.total_memory_cost`; works only because curator lazy-imports inside the enabled branch (`curator.py:462`) — correct but fragile coupling, worth a comment | Strong disabled-gate proof |
| `test_default_disabled_is_inert` | Yes | Yes (default→enabled would suggest `inf`) | Yes | No | Pin of `CostGateSettings.enabled=False` default |
| `test_zero_utility_suggests_and_fact_remains_hot` | Yes — `fact_id`, `total_cost==inf`, threshold, log line, row still in hot set | Yes | Yes | No (real DB + real curator) | The advisory-only proof; auto-delete mutation caught by this + 2 others |
| `test_analyze_only_suggests` | Yes — `start_phase=ANALYZE` still suggests | Yes | Yes | No | Note: it does not prove *other* phases do NOT suggest (no DECAY-only negative). Minor |
| `test_finite_total_above_threshold_suggests` | Yes — hand-computed total 2 ≥ 1.0 | Yes | Yes | No | Derivation arithmetic cross-checked by hand: storage 1/N=1, retrieval 1/(1+0)=1, noise 0, maintenance 0, utility 1 → 2 |
| `test_finite_total_below_threshold_is_quiet` | Yes — 2 < 3.0 quiet | Yes | Yes | No | Above/below pair brackets the threshold but never hits equality (R-W5-007) |
| `test_empty_hot_set_is_quiet` | Yes | Yes | Yes | No | |
| `test_same_cost_twice_same_suggestion_no_delete` | Yes — equal lists, count still 1 | Yes | Yes | No | Idempotency / no accumulation |
| `test_dry_run_still_suggests_writes_nothing` | Yes — suggests, row remains | Yes | Yes | No | Pins the documented dry_run DECISION (suggests but writes nothing) |

No test asserts `CurationReport.summary()["cost_gate_suggestions"]` (count-only,
`inf` exclusion is by inspection, exactly as W5-IMPL-01 §3 admits) and no test
exercises hostile DB rows or >`_batch` pagination (see §2/§6).

### 1b. `tests/agents/test_coding_specialist_p011.py` (21 tests)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_coding_specialist_role_value_is_additive` | Yes | Yes (Fb: no member) | Yes | No | |
| `test_specialist_package_exports_only_coding` | Partially — `hasattr(pkg, name)` for 4 gated names | Yes (Fb: package missing) | Yes | No | **Does not catch a stub file** — `specialists/__init__.py` never imports submodules, so creating `specialists/research.py` still passes [RAN mutation]. Claim "no stubs, no empty modules" unenforced (R-W5-005) |
| `test_no_new_event_type_member` | Yes (negative) | Pin — passes at base (correctly) | Yes | No | |
| `test_rollback_event_stays_resumable_not_terminal` | Yes (negative) | Pin — passes at base | Yes | No | |
| `test_registered_name_is_coding` | Yes — one name only | Yes | Yes | No | Duplicate assert (lines 131–132, NIT) |
| `test_coding_specialist_is_leaf` | Yes — role/name/`can_delegate` | Yes | Yes | No | |
| `test_every_mutating_tool_is_covered_by_policy` | **No — tautology** (for each name: if mutating then assert mutating) | **No — survives adding `"delete_everything"` to `CODING_TOOL_NAMES` [RAN]** | Yes | No | R-W5-001 (MAJOR). Only non-vacuous line is `assert covered` |
| `test_tool_names_are_in_constrained_enum` | Yes — enum == surface | Yes | Yes | No | Real lock on the JSON-enum seam |
| `test_fifty_scripted_tasks_route_through_mediation` | Partially — 50/50 `mediate_delegation` succeed | Yes (Fb) | Yes — fresh lineage per task (correctly avoids node ceiling) | Scripted model always succeeds; **target hardcoded `"coding"`, router never involved** | R-W5-002 (MAJOR): measures delegation success, not routing precision/classification |
| `test_unknown_target_is_failure_not_crash` | Yes | Yes | Yes | No | Good failure-shape pin |
| `test_disabled_coding_is_no_agent_failure` | Yes | Yes | Yes | No | Correct absence semantics |
| `test_empty_payload_does_not_crash_specialist` | Yes — `ok is True` | Yes | Yes | No | Does not assert result body (goal/text/tool_calls shape) |
| `test_depth_above_ceiling_blocked_and_emits_rollback` | Yes — error fields + event payload (reason/depth/ceiling/target) + correlation match | Yes (Fb + emit-deletion mutation fails) | Yes | No | Strong. Does not assert `sender`/`cycle` payload fields |
| `test_replay_rebuilds_rolled_back_and_rollback_count` | Yes — `ROLLED_BACK`, count 1 | Yes | Yes | No | Real ledger + real `project()` |
| `test_redelivered_rollback_suppressed_once` | Yes — 2 emits → 1 event, count 1 | Yes | Yes | No | Real store dedupe by full ADR-0008 key |
| `test_self_delegation_is_cycle_block` | Yes — error shape | Yes | Yes | No | **Does not assert the rollback event was emitted** — emit-deletion mutation passes this test [RAN]. Asymmetric with the depth test |
| `test_runtime_registers_coding_only_when_enabled` | Yes | Yes | **Weak** — builds `Settings(home=tmp_path/…)` directly, no `paa_home` fixture, no `reset_settings_cache`, relies on explicit settings object | No | Isolation nit (R-W5-011); passed in both orders [RAN], but a future env-reading change in `Runtime.build` could leak |
| `test_runtime_default_does_not_register_coding` | Yes | Pin — passes at base | Same as above | No | Disclosed pin, matches W5-IMPL-02 §Phase 1 |
| `test_coding_never_delegates_out[0/1/2]` | Only `can_delegate is False` | Yes (Fb: import fails) | Yes | No | Name overstates — it never delegates at any depth (NIT) |

`_ScriptedTurnModel` faithfully follows the `tests/integration/conftest.py`
pattern (answers by schema shape, records calls) and is *not* mocking the unit
under test — the specialist, orchestrator, ledger and replay are all real.
This is the right seam.

### 1c. `tests/context/test_allocator_scheduling_p007b.py` (8 tests)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_enabled_tick_allocates_and_notifies` | Yes — outcome, exact claim tuple `(WORKERS_ALLOC, "allocator", 25)`, 2 notifies, 2 ledger events, acks | Yes (Fb: no `_run_allocation_tick`) | Yes — FakeQueue/FakeStore local, real allocator via `FakeOrchestrator` | **FakeStore re-implements dedupe** — verified faithful: `LedgerEvent.idempotency_key` is `SHA256(corr\|type\|attempt\|discriminator)` (`events.py:101-102,185-193`), which is exactly what `FakeStore` keys on. It does *not* exercise the real store's UNIQUE/transaction path, but the key contract is not assumed blindly | Strong wiring test for the tick |
| `test_disabled_tick_never_touches_queue` | Yes — `None`, zero claims/appends | Yes | Yes | No | Disabled-gate proof for the daemon |
| `test_enabled_tick_empty_queue_underfed_no_notify` | Yes — `underfed`, no enqueues/events | Yes | Yes | No | Honest-underfed pin |
| `test_allocator_module_documents_operator_fed_producer` | Docstring substrings only | Yes (Fb: phrase absent) | Yes | No | R-W5-006 (MINOR): adding a synthetic producer elsewhere does **not** break this; test header/W5-IMPL-03 §5 claim otherwise is inaccurate |
| `test_breach_emits_one_event_with_threshold_payload` | Yes — type, attempt 0, payload keys, discriminator prefix | Yes | Yes — window pinned via private attrs (white-box but deterministic, no wall-clock) | FakeStore (see above) | Only the **full-defer** breach branch (`budget <= 0`, `creator.py:1567`) |
| `test_second_breach_same_window_dedupes` | Yes — 2 appended, 1 stored | Yes | Yes | FakeStore | Same branch; dedupe key uses `int(window_started*1000)` — window pinned, so deterministic |
| `test_no_breach_no_event` | Yes — cap 100, no appends | Yes (Fb: `ledger=` kwarg → `TypeError`) | Yes | No | Weak negative: drains an *empty* queue, so it cannot distinguish "no breach" from "idle" (see §6 P2) |
| `test_no_handle_stays_log_only` | Yes — no raise, returns 0 | **Pin — passes at base** (correct back-compat) | Yes | No | Does not assert the throttle *log* line was emitted |

Daemon `_allocation_loop`, `start()` registration, `_supervise("allocation")`,
curation-lock ordering and tick-failure propagation have **no test at all**
(coverage: `daemon.py:272-277` unexecuted [RAN]; deleting the `start()` task
survives [RAN mutation]) — R-W5-003 (MAJOR).

### 1d. Appended `tests/context/test_metrics.py` (18 tests, all Fb-fail)

Known values (4), `utility=0 → inf` (2), boundary zero terms (2),
out-of-domain parametrize (12: every term negative/NaN/inf → `ValueError`),
huge-input overflow (never NaN), and 2 Hypothesis properties
(finite-or-inf/never-NaN; non-increasing in utility) over
`st.floats(0.0, 1e6, allow_nan=False, allow_infinity=False)`
(`test_metrics.py:48`). All assert behavior against hand-computed values;
`_finite`/`_non_negative` (`metrics.py:81-101`) make the hostile table exact,
not approximate. The Hypothesis monotonicity test correctly `assume(utility > 0)`.
These are the strongest tests in the wave. Gap: no exact-threshold test can
exist here (pure function has no threshold); the threshold lives in the curator
and equality is untested there (R-W5-007).

### 1e. Appended `tests/tui/test_dashboard.py` (2 tests)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_attention_top_ranked_from_allocator_events` | Yes — real ledger seeded with 3 `ATTENTION_ALLOCATED` events, top-2 by score under `max_results=2`, `correlation_id` carried | Yes (Fb: base returns `None`; sort-flip and return-`None` mutations both caught [RAN]) | Yes — `db`+`ledger` fixtures, `SimpleNamespace` rt/settings | No — real `build_panel_fields` + real ledger | Strong happy path. Nothing else about `_attention_top` is pinned (R-W5-008) |
| `test_attention_top_empty_allocator_stays_null` | Yes — `None` | Pin — passes at base (correct honest-null) | Yes | No | |

## 2. Coverage of changed lines/branches, error paths, boundaries

[RAN coverage over the 228 selected tests; W5-relevant lines only.]

| Changed area | Covered | Uncovered (W5 lines) | Risk |
|---|---|---|---|
| `metrics.py::total_memory_cost` (740-750) | 100% of the function | — | None |
| `curator.py` cost-gate block (461-519) | Gate on/off, above/below, empty, dry_run, ANALYZE | `486` empty-batch `break` (needs >`_batch` rows), `494` `use_count<0` raise, `502-504` per-row `errors+=1` skip (hostile DB rows); `summary()` count key (no test anywhere asserts it) | Hostile stored values (`importance>1` → negative noise → `ValueError`; text columns) are handled in code but unpinned; pagination loop's second iteration unpinned |
| `specialists/coding.py` | 100% (27/27) | — | `handle()` with model returning non-list `tool_calls` (code degrades to `[]`, impl-report claim) and with `model=None` are *executed*? 100% coverage says the lines run — via the 50-task/empty-payload paths — but no test asserts the degraded *output* for the non-list input |
| `orchestrator.py` block emit (880-897) | Depth + cycle error paths, real replay | **Node-ceiling** block (`max_nodes`) emit; `sender`/`cycle` payload fields; self-cycle event presence | A node-limit-specific emit regression would pass |
| `daemon.py` allocation (266-309) | `_run_allocation_tick` (279-309) via 3 tests | `_allocation_loop` body (272-277), `start()` 5th task, `_supervise`, lock contention, tick exception → `allocation_failed` log | The scheduling half of "scheduling" is untested (R-W5-003) |
| `creator.py` DRAIN_THROTTLED (1576-1603, 2009+) | Full-defer branch (1567-1584) + dedupe + no-handle | **Partial-cap branch (1586-1603, coverage 1587-1596 missing)**, emit-failure swallow log (`2058-2059`) | Second emit site is dead weight as far as tests know (R-W5-004) |
| `tui/runner.py::_attention_top` (171-219) | Happy path + empty | Per-lineage read failure (194-198), non-dict payload (203-204), non-numeric/bool score (206-207), invalid `max_results` fallback (187), `_PANEL_SCAN_LIMIT` bound (190), panel-isolation `except` in `build_panel_fields` (330-331), tie-break (218) | Malformed ledger payloads and bound edge cases unpinned (R-W5-008) |
| `runtime.py` coding registration | Both on/off via `Runtime.build` | — | Covered |
| `core/types.py` enum | Value + additivity | — | Covered |

Boundary coverage is good where it matters most: utility 0, storage/maintenance 0,
negative/NaN/inf per term, huge overflow, empty hot set, empty queue, depth
exactly at/above ceiling (depth 2 with child 3), `max_results=2` truncation.
Missing boundaries: `total == threshold` exactly, `max_results` 0/negative/non-int,
score ties, window rollover (`now - started >= 1.0` resetting), `limit <= 0`
drain (pre-existing, untouched).

## 3. Mutation thinking

All mutations [RAN] against a TEMP copy of HEAD `src/`; the repo was never edited.

| Plausible bug | Caught? | By which test |
|---|---|---|
| Cost gate: delete fact on suggestion (auto-delete flip) | **Caught** (3 fail) | `test_zero_utility_suggests_and_fact_remains_hot`, `test_same_cost_twice_same_suggestion_no_delete`, `test_dry_run_still_suggests_writes_nothing` |
| Cost gate: `if gate.enabled:` → `if True:` (disabled gate) | **Caught** (2 fail) | `test_disabled_emits_no_suggestions_and_calls_no_cost`, `test_default_disabled_is_inert` |
| Cost gate: `total >= threshold` → `total > threshold` | **Survives** (9 pass) | — (no exact-equality case; R-W5-007) |
| Cost gate: `utility==0 → inf` → epsilon guard `sum/(utility+eps)` | Caught (by reading + Fb) | `test_total_memory_cost_zero_utility_is_infinite` asserts `== math.inf` exactly; `inf` vs large-finite is distinguished |
| Cost gate: `summary()` leaks full list incl. `inf` | Survives (no test) | — (R-W5-009; impl report discloses inspection-only) |
| Coding: delete rollback emit block | **Caught** (3 fail) | depth-block, replay, idempotency tests. (Self-cycle test does *not* catch it — see §1b) |
| Coding: add uncovered mutating tool `"delete_everything"` to `CODING_TOOL_NAMES` | **Survives** (21 pass) | — (R-W5-001 tautology) |
| Coding: add stub `specialists/research.py` | **Survives** (target test passes) | — (R-W5-005) |
| Scheduling: delete `start()` allocation task | **Survives** (8 pass) | — (R-W5-003) |
| Scheduling: delete partial-cap (2nd) drain emit | **Survives** (8 pass) | — (R-W5-004) |
| Attention: sort ascending instead of descending | **Caught** | `test_attention_top_ranked_from_allocator_events` |
| Attention: always return `None` | **Caught** | same |

**6 caught / 5 survived (of 11).** The survived set is exactly the changed
behavior listed in the digest. The headline brief questions, answered:
auto-delete flip → **caught**; disabled gate → **caught**; block emit
pre-failure → **caught** (emit deletion fails 3 tests; an emit moved after
`return` is unreachable and also fails); attention stale → **not caught as a
class** (sort/none are caught, but closed-lineage scan, age/staleness,
malformed-payload and bound mutations have no test to catch them — there is
no freshness concept in the code at all, which is itself worth a P2 pin).

## 4. Flakiness evidence (repeated runs) and test-suite health

- 3 consecutive runs of the 38 new-file tests: all green [RAN]. Reverse file
  order: green [RAN]. No timing-sensitive asserts: the drain window is pinned
  by setting `_drain_window_started`/`_drain_window_indexed` directly
  (white-box, deterministic — acceptable here because the alternative is a
  real 1 s sleep, which would be worse).
- Hypothesis properties use finite non-negative strategies and hold for all
  inputs in domain; `assume(utility > 0)` correctly draws the adversarial
  value class rather than hoping for it (per AGENTS.md testing convention).
- Isolation: cost-gate tests take `paa_home` (env + settings-cache reset on
  teardown, `conftest.py:22-30`), `db` is a tmp file per test, ledger is
  function-scoped. Coding runtime tests are the only isolation nit
  (explicit `Settings(home=tmp_path/…)` without the fixture — green in both
  orders, but not hardened). Dashboard attention tests use the shared
  `db`/`ledger` fixtures correctly.
- No shared-state, ordering, or slow-suite suspects in the W5 files. The only
  red test in the selected set is the pre-existing `test_ledger_with_events`
  typo, identical at base.
- Suite health note: `test_coding_specialist_p011.py` builds a full `Runtime`
  twice (on/off) — the slowest W5 tests, but bounded and tmp-isolated.

## 5. Findings (R-W5-nnn, severity, location, evidence, recommended fix)

### MAJOR

- **R-W5-001 — Tool-policy coverage test is a tautology; the "pins the coverage"
  claim in `coding.py` is false.** `tests/agents/test_coding_specialist_p011.py:145-163`:
  for each tool name it asserts `mutating → (glob-matched OR in MUTATING_RUNTIME_TOOLS)`,
  which is the definition of the antecedent — it cannot fail for a new tool.
  [RAN mutation: adding `"delete_everything"` to `CODING_TOOL_NAMES`
  (`coding.py:55-65`) leaves all 21 tests green.] **Fix:** assert the actual
  property with an explicit expectation, e.g.
  `assert set(CODING_TOOL_NAMES) - {names matched by DEFAULT_MUTATING_TOOLS ∪ MUTATING_RUNTIME_TOOLS} == set()`
  — or better, pin the exact mutating set
  (`{"submit_task","run_task","remember_fact"}`) so a *new* mutating tool
  fails loudly until a human classifies it. One-line fix; the test as written
  gives false confidence on the read-only-by-accident invariant.
- **R-W5-002 — The 50-task "routing precision" test does not test routing.**
  `test_fifty_scripted_tasks_route_through_mediation` hardcodes
  `target="coding"` and the scripted model always succeeds, so `routed == 50`
  is guaranteed by construction; no classifier, router, or task-content
  variation exists anywhere in the path (correctly, per ADR-0041 the router
  is bypassed when the target is named — but then the test cannot be called
  a precision gate). A future router change that misroutes every coding task
  to `research` would leave this green. **Fix:** rename to what it proves
  (delegation success over 50 lineages) and, when/if a router classification
  path exists, add a classifier test with mixed coding/non-coding goals and
  assert the *target chosen*, not just success. Until then the ADR gate is
  50× tautological success.
- **R-W5-003 — Daemon allocation loop and `start()` registration are untested.**
  Tests call `_run_allocation_tick()` directly; `_allocation_loop`
  (`daemon.py:266-277`, coverage-missing [RAN]), the 5th supervised task in
  `start()`, curation-lock serialization, and tick-exception →
  `daemon.allocation_failed` + loop-survives have no test. [RAN mutation:
  deleting the `start()` task leaves all 8 scheduling tests green.] This is
  precisely the R-W4-002 disease (callable but unscheduled) recurring one
  level up. **Fix:** a test that `Daemon.start()` creates 5 tasks / the
  allocation task exists, plus a tick-raises test asserting the loop logs
  `allocation_failed` and a second tick still runs (fake clock/sleep or
  direct `_allocation_loop` iteration with `_stop` set after one pass).
- **R-W5-004 — Partial-cap drain emit (2nd site) is untested.**
  `creator.py:1596-1603` (coverage 1587-1596 missing [RAN]); [RAN mutation:
  deleting that call leaves all 8 scheduling tests green]. Only the full-defer
  branch is exercised. **Fix:** a test with `drain_max_per_second=2`,
  `_drain_window_indexed=1`, `limit=100` (budget 1 < limit) asserting one
  `DRAIN_THROTTLED` with `effective_limit==1`, using a pending fact or the
  existing empty-queue path (emit precedes the pending fetch, so empty is fine).

### MINOR

- **R-W5-005 — "No gated stubs" is not enforced.** `test_specialist_package_exports_only_coding`
  checks `hasattr(pkg, "research")` etc., but `specialists/__init__.py` never
  imports submodules. [RAN mutation: creating `specialists/research.py`
  leaves the test green.] **Fix:** assert the package file set, e.g.
  `sorted(p.name for p in Path(pkg.__file__).parent.glob("*.py")) == ["__init__.py", "coding.py"]`.
- **R-W5-006 — Producer-decision pin cannot detect a synthetic producer.**
  `test_allocator_module_documents_operator_fed_producer` checks two
  docstring substrings; a poll-loop producer added anywhere else passes.
  The test-file header and W5-IMPL-03 §5 claim a synthetic producer "would
  break the docstring pin" — inaccurate. **Fix:** either soften the claim
  (docstring is documentation, not a lock) or add a grep-style test over
  `src/` asserting no `WORKERS_ALLOC` enqueue outside the queue docstring
  example. The latter is the honest lock for "operator-fed".
- **R-W5-007 — Exact-threshold boundary survives.** `total >= threshold`
  (`curator.py:505`) vs `>` is indistinguishable: above uses total 2 ≥ 1.0,
  below uses 2 < 3.0. [RAN mutation: `>=`→`>` green.] W5-IMPL-01 §3
  self-discloses this. **Fix:** one test with threshold exactly `2.0`
  (the hand-computed total) asserting a suggestion — pins the documented
  "crosses" semantics.
- **R-W5-008 — `attention_top` branches beyond the happy path are unpinned.**
  Tie-break (`item_id` asc on equal scores, `runner.py:218`), malformed
  payloads (non-dict, non-numeric/bool score — lines 203-207), invalid
  `max_results` (0/negative/non-int → 5, line 187), per-lineage read failure
  skip (194-198), `_PANEL_SCAN_LIMIT` truncation (190), closed-lineage
  exclusion (only `open_correlations()` is scanned — the "stale" question:
  decisions from closed lineages vanish with no pin), and panel-isolation
  failure (`build_panel_fields` except at 330-331) have no test. **Fix:**
  table-driven tests for malformed/tie/bound + a closed-lineage test
  deciding the intended semantics explicitly.
- **R-W5-009 — Cost-gate hostile-row and pagination paths unpinned.**
  `curator.py:486` (empty-batch break), `494` (`use_count<0`), `502-504`
  (`errors+=1` skip) uncovered [RAN]; no test inserts `importance>1`,
  `confidence>1`, text in numeric columns, or >`_batch` facts; no test
  asserts `summary()` count-only exclusion of `inf`. **Fix:** insert hostile
  rows directly via SQL and assert `errors` increments + phase completes;
  insert `2*_batch` facts and assert all are scanned; assert
  `summary()["cost_gate_suggestions"]` is an int with no `inf` in the
  summary dict.
- **R-W5-010 — Block-emit asymmetries untested.** Self-cycle test does not
  assert the event (emit-deletion mutation passes it [RAN]); node-ceiling
  block emit untested; discriminator `delegation-block:{sender}->{target}:depth{d}`
  (`orchestrator.py:895`) collides for distinct cycles/node-blocks at the
  same depth on one lineage (redelivery vs distinct-block ambiguity — the
  code comment at 878-879 claims full-key idempotency, but the key does not
  include `cycle`/`ceiling`). **Fix:** assert the event in the cycle test;
  add a node-ceiling block test; decide+pin whether same-depth distinct
  blocks should dedupe.
- **R-W5-011 — Runtime-registration isolation nit + duplicate assert.**
  `test_runtime_registers_coding_only_when_enabled` /
  `test_runtime_default_does_not_register_coding` bypass `paa_home` /
  `reset_settings_cache` (explicit `Settings` object only). Duplicate
  `assert orch.get("coding") is not None` (`test_coding_specialist_p011.py:131-132`).
  Green in both orders [RAN], but harden with the fixture and drop the dupe.

### NIT

- `test_coding_never_delegates_out` never delegates (name overstates; it
  pins `can_delegate is False` at 3 depths, which is still useful).
- `test_empty_payload_does_not_crash_specialist` asserts only `ok`;
  asserting `goal == ""` / `tool_calls == []` shape would pin `handle()`'s
  tolerance contract.
- `test_no_handle_stays_log_only` does not capture the throttle log line;
  `structlog.testing.capture_logs` (already used in the cost-gate file)
  would pin log-only *with* the log, not just without a raise.

### Docs sweep (1268d54) — claims audit

- W5-DOCS claims lock 5 passed at final HEAD: **confirmed** [RAN
  `test_docs_references.py` → 5 passed]. Spot re-points verified by reading
  target lines (`curator.py:160`, `orchestrator.py:837`, `daemon.py:173`,
  `types.py:242` — all hold their cited symbols). The one disclosed
  best-holder (`persistence-and-storage.md:178 → orchestrator.py:1131`,
  `def boot`) is plausible and honestly flagged. ADR-0044 present and
  indexed. No test-quality findings on the sweep itself; the lock does what
  it can (non-blank + exists) and W5-DOCS's sentence-reading method is the
  right one. Residual risk is the disclosed one: wrong-but-non-blank cites
  in files untouched this wave pass undetected — same exposure as R-W4b-003,
  reduced but not eliminated. Not a W5 test defect.

## 6. Missing edge cases and tests (prioritized)

### P1 (must add — survivors on changed behavior)

1. **Partial-cap drain emit.** Scenario: `drain_max_per_second=2`,
   window indexed=1, `limit=100`, ledger=FakeStore, empty queue (emit
   precedes fetch). Expected: 1 `DRAIN_THROTTLED`, `effective_limit==1`,
   `threshold==2`, returns 0. Belongs in
   `test_allocator_scheduling_p007b.py::TestDrainThrottledEmit`. Sketch:
   mirror `test_breach_emits_one_event_with_threshold_payload` with
   `indexed=1, cap=2` and assert `payload["effective_limit"] == 1`.
2. **Daemon loop scheduling.** Scenario: `Daemon.start()` with a fake rt;
   assert 5 supervised tasks incl. `"allocation"`; tick-raises scenario:
   orchestrator raises → `daemon.allocation_failed` logged, loop continues.
   Belongs in `test_allocator_scheduling_p007b.py` (needs a `Daemon`-with-
   fake-rt harness + `_stop` set after one iteration, or factor the loop
   body for a single-pass test).
3. **Real tool-policy assertion.** Replace the tautology with
   `uncovered = {n for n in CODING_TOOL_NAMES if n not in globbed and n not in MUTATING_RUNTIME_TOOLS}; assert uncovered == set()`
   plus an exact mutating-set pin. Belongs in
   `test_coding_specialist_p011.py::TestToolSurfacePolicy`.
4. **Exact threshold.** Threshold `2.0` with the hand-computed total-2 fact
   → exactly 1 suggestion. Belongs in `test_cost_gate_p008.py::TestCostGateThreshold`.

### P2 (should add)

5. **Attention malformed/tie/bound/closed-lineage.** Table: bool score,
   string score, non-dict payload, missing `item_id`, equal scores (tie →
   `item_id` asc), `max_results` 0/-1/`"x"` → 5, closed lineage (decide:
   excluded — pin it), one lineage raising on `read_correlation` (others
   still rank), `open_correlations` raising (panel `None`, others intact).
6. **Cost hostile rows + pagination + summary.** SQL-insert hostile rows
   (`importance=2.0`, `initial_confidence=2.0`, `use_count=-1`, text),
   assert `errors>=1`, valid facts still suggested, phase completes;
   `2*_batch+1` facts all scanned; `summary()` has int count, no `inf`.
7. **Node-ceiling block emit + cycle event assert.** Exceed `max_plan_nodes`
   via repeated edges on one lineage; assert `STATE_ROLLBACK_TRIGGERED`
   with `reason=delegation_recursion_block`. Assert event in self-cycle test.
8. **Gated-stub file lock + producer grep lock.** `glob("*.py")` on the
   specialists package; `grep WORKERS_ALLOC enqueue` over `src/` limited
   to the queue docstring example.
9. **No-breach with pending work.** `cap=100`, one pending fact with a fake
   sink → drain indexes it, zero `DRAIN_THROTTLED` (distinguishes idle
   from no-breach; current test drains nothing).
10. **`handle()` output shapes.** Model returning non-list `tool_calls`
    → `[]`; model=None → deterministic text with `goal` echo; assert
    result dict keys (`agent/role/goal/text/tool_calls`).
11. **Emit-failure swallow.** Ledger whose `append` raises → drain still
    returns normally, `memory.drain_throttled_emit_failed` logged
    (`creator.py:2058-2059` uncovered).

### P3 (nice to have)

12. Router classification precision (when a learning router exists): mixed
    goals, assert chosen target ≥90% — the honest form of the T12 gate.
13. `threshold=inf` (only `inf` totals suggest), superseded facts excluded
    from the cost scan, duplicate `item_id` across lineages in
    `attention_top`, window rollover after 1 s (second window emits anew).
14. Tick-vs-maintenance lock contention: allocation tick waits for the
    curation lock (no parallel prune) — hard to test without hooks; a
    lock-held flag fake would do it.
15. `test_metrics` Hypothesis with `utility` drawn near 0 (subnormal) —
    monotonicity already covers `>0` via assume; extreme-subnormal
    division overflow to `inf` is worth one explicit case.

Categories considered and judged N/A with reason: unicode/encoding and
ReDoS (no regex/pattern input in the diff — the one `LIKE`-adjacent surface
is untouched); oversized payloads (score floats bounded by allocator
upstream; cost terms validated finite); authz bypass (no auth surface
changed; policy globs are *read* by the policy test, not modified);
backward compatibility (default-off flags + optional `ledger=` kwarg +
additive enum/report field — all pinned by the pass-at-base tests);
clock/timezone (drain window uses event-loop monotonic time, correctly
pinned rather than slept).

## 7. What I could not verify

- **Full suite** — not run (standing orders §16). Scoped suites around the
  diff (228 selected) are green; `ruff` 0 / `mypy` 3-baseline [RAN].
- **Fail-before for W5-IMPL-01/03 as originally run** — neither implementer
  ran a true fail-before (W5-IMPL-01: grep absence, honestly disclosed;
  W5-IMPL-03: reversal asserted, not run). I ran it myself against the
  base overlay [RAN §0], which *supersedes* both claims: 53/58 fail at
  base for the right reasons. W5-IMPL-02's "18 failed / 3 passed" is
  exactly reproduced by my overlay run.
- **Production `DRAIN_THROTTLED` activation** — W5-IMPL-03's NEED (one line
  in `runtime.py::_build_memory_creator` passing `ledger=`) is outside the
  diff under review; until it lands, production creators are log-only.
  The creator-side tests prove the emit, not the wire. Flagging as a
  residual, not a test defect.
- **D-FORMULA / D-DERIVATIONS** (ratio shape; row-share/reuse/complement
  proxies) are judgement calls the tests pin as-implemented but cannot
  validate as *intended* — that needs the operator/ADR owner, exactly as
  W5-IMPL-01 flags with `REVIEW_REQUIRED`. My mutation evidence shows the
  tests would catch a *change* of shape, not a *wrong* shape.
- **Docs semantic correctness beyond the lock + spot checks** — I verified
  the lock is green and sampled re-points; I did not re-read all ~120
  re-points sentence-by-sentence (W5-DOCS discloses the same scope delta
  for untouched files).
- **Mutation `utility==0 → epsilon` and `summary()`-leak** — verdicts by
  reading + existing asserts, not executed mutations (the other 9 of 11
  were executed). `>=`→`>`, auto-delete, disabled-gate, emit-deletion,
  tool-add, stub-add, daemon-task-deletion, drain-2nd-emit-deletion,
  attention-sort, attention-none were all executed [RAN].

## COMMS last line read

`2026-11-07T14:38:17Z W5-IMPL-01 -> ORCH | DONE | T-008-P008` (148 lines at
start; re-read before finishing — no new lines relevant to this review).
