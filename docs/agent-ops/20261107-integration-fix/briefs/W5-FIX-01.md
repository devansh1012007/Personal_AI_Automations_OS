# Brief — W5-FIX-01 (IMPL) · wave 5 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W5 MAJORs R-W5-001 (production DRAIN_THROTTLED unwired), R-W5-002 (PG UUID cursor), R-W5-003 (attention_top stale/dup/terminal) + REV minors R-W5-005/006/007 + TREV-W5 MAJORs R-W5-001..004 + targeted TREV minors (pinned in §3). Why: the three REV majors break production wiring/cross-engine correctness/panel truth; TREV majors are false-confidence pins on the exact invariants this run exists to lock.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; single-owner hotspots; PG dialect types UUID/TIMESTAMPTZ)
3. `docs/agent-ops/20261107-integration-fix/reports/w5-review/REV-W5.md` §§1,4–5 (R-W5-001..010 + D-FORMULA verdict: sound — no action on formula)
4. `docs/agent-ops/20261107-integration-fix/reports/w5-review/TREV-W5.md` §§5–6 (R-W5-001..007 + P1/P2 test sketches)
5. `docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-01.md`, `W5-IMPL-02.md`, `W5-IMPL-03.md` (what W5 built + NEEDS)
6. Code at HEAD (verify line numbers yourself): `src/paa/runtime.py:346,508` (+ `ledger` in scope), `src/paa/memory/curator.py:458-520` (cost-gate scan/cursor + threshold), `src/paa/tui/runner.py:171-219` (attention_top), `src/paa/storage/queue/base.py` (no — verify-only), `src/paa/context/allocator.py` docstring (producer statement), `src/paa/daemon.py` allocation tick + `start()`, `tests/agents/test_coding_specialist_p011.py:145-163` (tautology), existing naming patterns in those files.

## 3. Task and acceptance criteria
Task W5-FIX-01. All items:
- **R-W5-001 (REV, 1-line wiring)**: thread `ledger=` from `runtime.py` into `_build_memory_creator`'s `MemoryCreator(...)` call (the production construction site at runtime.py:508; `ledger` is in scope at :364). No other runtime.py change. Test: real `Runtime.build`-level (or construction-path-level per the codebase's construction-test pattern) asserting a breach in the production-built creator emits `DRAIN_THROTTLED` to the real ledger (not just fakes). If a full Runtime build is impractical, the narrowest construction-path test that proves the kwarg is threaded + an emit on breach through that path.
- **R-W5-002 (REV)**: cursor pagination must work both engines — first page WITHOUT the `id > ?` predicate (or a NULL-sentinel branch), subsequent pages keep the total-order cursor. Test: gate runs across >1 page on SQLite (seed > page size, assert all facts considered, no duplicates) + a PG-shape assertion (cursor logic has no `''`-vs-UUID param — pin by the code shape: first page issues no id param; state the PG-verification limit honestly — PG suite is NOT-RUN here).
- **R-W5-003 (REV)**: attention_top pipeline — dedupe by `item_id` keeping the LATEST score (later events win), scan most-recent-first (or document + implement recency-bounded scan), document the terminal-lineage drop in the function docstring (a terminal lineage vanishing is accepted-by-design ONLY if documented), keep empty→None. Tests: same item twice → one row (latest score); stale oldest-lineage not crowding out fresh (bound holds); terminal lineage after allocation → documented behavior.
- **REV R-W5-005 (TREV-001)**: pin the EXACT mutating set `{"submit_task","run_task","remember_fact"}` (a new mutating tool fails loudly until classified) + pin read-only half == READ_ONLY_TOOLS (direct import assert). `test_every_mutating_tool_is_covered_by_policy` → rewritten to the explicit expectations; keep the audit arrow (which tools are mutating) honest.
- **TREV R-W5-002**: rename/re-document the 50-task test as delegation-success-over-50-lineages (not precision); assert named-target bypass (ADR-0041: router bypass on named target) in the docstring; do NOT invent a classifier.
- **TREV R-W5-003**: daemon tests — `Daemon.start()` creates the 5th allocation task (assert task count/shape per the existing loops' start-test pattern) + tick-raises → `allocation_failed` logged AND a second tick still runs (direct `_allocation_loop` iteration with injected `_stop` after one pass, or fake sleep/clock — follow the reflection tick test pattern).
- **TREV R-W5-004**: partial-cap drain emit test (`drain_max_per_second=2`, window pre-indexed=1, `limit=100` → one `DRAIN_THROTTLED` with `effective_limit==1`; empty queue is fine — emit precedes the pending fetch). If a creator test file needs the ledger wiring from R-W5-001, order your changes so both land together.
- **TREV R-W5-005**: `test_specialist_package_exports_only_coding` → assert the package file set `["__init__.py","coding.py"]` (glob).
- **TREV R-W5-006**: producer-lock — grep-style test over `src/` asserting no `WORKERS_ALLOC` enqueue outside the queue docstring example AND the operator-fed docstring statement present (documentation + lock together; soften the old claim per TREV's fix line).
- **REV R-W5-006**: thread allocator settings through `run_allocation` (tick gate + allocator gate read the same injected source, not global-vs-instance). Test: Runtime with non-global Settings → gates agree (no "disabled logged as completed pass" split).
- **REV R-W5-007**: bound the suggestion output (count + top-K log line per pass, or one summary log line) — cap the list, keep advisory semantics; test: >K facts over threshold → suggestions capped at K + count line present.
- **REV R-W5-008 (NIT-tier but cheap)**: accept CostGateSettings via injection where the curator already accepts injected settings (follow the MemorySettings injection point — read it first; if injection is awkward at the single construction site, record NEED + stop that half).
- **REV R-W5-004**: discriminator wall-clock epoch (or boot/process id) — small change in `_emit_drain_throttled_event`; note the idempotency implication in the module docstring.
- Trace: keep existing `PLAN:integration-fix:P007/P008/P011` tags; no new tags.
- Acceptance: your test files green + W5 scoped suites green + `tests/agents/` + `tests/memory/` + `tests/tui/` green except the known strftime failure (quote counts); ruff 0, mypy total 3; docs untouched (docs sweep stays with W5-DOCS; if your shifts break the lock, record the exact residual for the orchestrator — do NOT edit docs).

## 4. Ownership
WRITE-SET (only these): `src/paa/runtime.py` (ledger= line only), `src/paa/memory/curator.py` (cursor + threshold gate + suggestion cap + injection if cheap), `src/paa/tui/runner.py` (attention_top only), `src/paa/context/allocator.py` (docstring/producer statement only), `src/paa/memory/creator.py` (discriminator epoch only), `src/paa/daemon.py` (none — tests only, unless the tick needs a log-name line; if it does, that ONE line is your only daemon edit), `tests/agents/test_coding_specialist_p011.py`, `tests/context/test_allocator_scheduling_p007b.py`, `tests/memory/test_cost_gate_p008.py`, `tests/memory/test_creator.py` (drain emit tests — append), `tests/tui/test_dashboard.py` (attention_top append only), `tests/agents/test_daemon_allocation_tick_p007b.py` (new) or the daemon test home you find (record which). READ-ONLY: everything else. FORBIDDEN: `docs/**`, `src/paa/core/types.py`, `src/paa/context/metrics.py`, `src/paa/agents/orchestrator.py`, EventTypes.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `runtime.py`, `curator.py`, `runner.py`, `creator.py` (shared files).

## 5. Parallel siblings this fix wave
None running (W5-DOCS landed). Re-review (REV-W5b/TREV-W5b) runs after you. If your line shifts break the docs lock, the orchestrator dispatches W5-FIX-DOCS2 — record the residual list in your report.

## 6. Skip list and known traps
- R-W5-009/010 NITs = seen/recorded (REV §4).
- PG actual run stays NOT-RUN (no DSN) — never claim PG green.
- The allocator tick must not move a real task-queue consumer change beyond §3 (delegate ≠ delegate_target).
- Baseline failures are not yours.

## 7. Environment and tests
Scoped only: your test files + W5 scoped suites + `tests/agents/` + `tests/memory/test_creator.py` + `tests/tui/` + ruff/mypy scoped. Tmp fixtures only. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: W5-FIX-01`, `Agent: W5-FIX-01`, `Wave: W5-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-review/W5-FIX-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-review/W5-FIX-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
