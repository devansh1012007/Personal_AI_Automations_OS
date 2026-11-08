# Plan — 2026-11-08 memory-residuals fix + conformance review (run 20261108)

**Status:** ready for dispatch (investigations done; plan grouped from two investigator reports).
**Branch:** `ops/20261108-memory-residuals-fix` (created from `ops/20261107-integration-fix`).
**Base SHA:** `750d1a0` (HEAD).

## 1. Problem statement and goal

The 20261107-integration-fix run shipped P-001..P-014 and is green at its own gates (ruff 0, mypy 3, docs lock), but five recorded residuals remain open, plus a set of docs-vs-code divergences found by the conformance audit. The goal of this follow-up:

1. Close the five residuals with the minimal correct fix for each (no scope creep, no new dependencies).
2. Fix the docs claims that the conformance audit proved false/stale (major only — allocator "live" over-claim, tier one-rung, P-002 fallback prose, DRAIN_THROTTLED stale comment, architecture/coder-agent staleness).
3. Re-verify: namespace-agnostic memory-system behavior is exactly as documented after the fixes.

**Non-goals:** PG/S3 live suites (still NOT-RUN locally; CI-only), Docker embed build (CI-only), R3 restore drill (operator), enabling allocator/tier/cost in production defaults, any Doc-A formula adoption, the `_PANEL_SCAN_LIMIT` O(N) perf item, satisfying the unpinned `attributes` scope decision for item 4 unless investigated in-task.

## 2. Facts from Step 1 (investigator evidence) vs assumptions

**Facts (from Subagent 1 — conformance audit, session ses_e44e40021ffeJcZDa4cxAzgTIe):**
- P-002 hydrate/derive, P-003 entity v6, P-004 touch+fallback, P-008 cost advisory, P-009 mechanics, P-010 vault enum/fence: **MATCH** docs. Evidence files include `context_agents.py:274-284,299-304`, `migrations.py:1461-1477`, `memory/touch.py:30`, `runtime_tools.py:806-810`, `metrics.py:691-749`, `tier.py:216,242,273-274`, `world_model.py:80-94,189-201`.
- P-007 allocator: mechanics MATCH; "live loop" **DIVERGES** — allocator is gated (`config.py:692` `enabled=False`) and operator-fed (`allocator.py:42-50` docstring: no code enqueues to `workers:alloc`).
- P-009 tier archive: code moves **one rung per tick, never a cascade** (`tier.py:109,274`); docs say "archived" implying direct T2→T4.
- P-002: hydrated hits derive; **hydrated-miss hits fall back to payload confidence** (`context_agents.py:321,325`) — "never ranking truth" prose is over-absolute.
- `docs/architecture/overview.md` (:205-207, :238, :243, :226) and `docs/coder-agent.md:61` are **stale** vs tree (ADR list, "no UI beyond CLI" vs 5 panels, `specialists/` empty-package claim vs `specialists/coding.py` exists, daemon imports allocator, vector fallback row).
- Unredacted sibling sites remain (`context_agents.py:137,252,367` `error=str(exc)`) — ledger FINDING; **in scope only if touched by Item 1** (out of this plan's five items; recorded as tech-debt).
- Test coverage is strong per item (see investigator §5 table); no critical divergence (all memory paths default-safe: allocator off, tier dry-run, cost off).

**Facts (from Subagent 2 — residual specs, session ses_e44e40020ffeglDeKZJNGcGhIn):**
- Item 1 threshold: `curator.py:534` `>=` matches `config.py:728` "at or above"; **equality untested** (mutations M9 `>=→>` survive all 14 cost-gate tests; TREV-W5b§3). Drain `creator.py:1578,1597` boundary similarly unpinned — adjacent, same class.
- Item 2 cap: `curator.py:547-550` sort `(-total_cost, fact_id)` + slice 100; **kept-set content unpinned** (M8b ascending-sort survives; 10 facts all `importance=0` → `inf`).
- Item 3 producer lock: `test_allocator_scheduling_p007b.py:292-319` line-shaped predicate; multi-line enqueue and producer-in-exempt-file both evade. Tree is clean; gap is lock completeness.
- Item 4 v5 wedge: SQLite STRICT + `ck_entity_aliases_json` CHECK (sqlite:235), version 5 shape, corrupt `aliases` row → v6 `ADD COLUMN` re-validates CHECKs → `StorageError`, single-transaction rollback, DB pinned at v5 forever; backfill unreachable. PG does not wedge (no `json_valid` CHECK; ADD COLUMN doesn't re-validate). Existing test pins only crash-mid-step. **Pre-repair step before column adds, same transaction, idempotent, same event name `migration.backfill_alias_corrupt`, SQLite guard via `json_valid(aliases)=0`, PG no-op documented.**
- Item 5 threading: daemon gates on `self._rt.settings.allocator.enabled` (`daemon.py:293-297`) → `run_allocation()` → orchestrator re-reads **global** `get_settings().allocator` (`orchestrator.py:1165-1173`) → non-global-Settings deployments silently no-op. Same shape for cost-gate wire (`daemon.py:418` missing `cost_gate=`). Tier-sweeper precedent: callers pass settings explicitly (`tier.py:112-136`, `curator.py:166-170`).

**Assumptions (explicit):**
- A1: `config.py:728` "at or above" is the controlling intent over ADR-0040's "crosses" (high confidence, per Subagent 2). Items 1–2 are pin-only; no src change.
- A2: Item 4 scopes to `aliases` only (the briefed CHECKs); if attributes scope is needed it will be recorded as residual, not silently widened.
- A3: Item 5's signature change (`run_allocation(settings=...)`, optional) is additive-API-safe; existing callers/tests byte-identical for global deployments.
- A4: PG halves of Items 1/2/4 are gated tests (skipped without `PAA_TEST_POSTGRES_DSN`); CI proves PG.

## 3. Candidate approaches considered

- **A. Monolithic single coder for all five items.** Rejected: items touch disjoint files and could regress each other; review would be un-decomposable; violates atomic-task rule.
- **B. Five parallel coders, one per item.** Rejected (as stated): Items 1+2 share one test file (must be one task); Item 5 and Item 3 both would edit `test_allocator_scheduling_p007b.py` (same-file race). 
- **C. Four atomic tasks with dependency grouping (CHOSEN):** T-01 (Items 1+2, one test file, test-only), T-02 (Item 5, orchestrator+daemon), T-03 (Item 3, producer-lock hardening, sequenced after T-02 to avoid same-file clash), T-04 (Item 4, migrations v6 pre-repair). Overlapping-safe subsets {T-01, T-02, T-04} run in parallel; T-03 runs after T-02. Each leaves the tree working.
- **D. Docs-only fix (reword ADR-0040 "crosses" → "at or above", no tests).** Rejected: leaves M8b/M9 survivors; test pins are the enforcing half and the investigations show pin-only fixes are 1–3 lines each.

## 4. Atomic tasks (dependency graph)

```
T-01 ──┐
T-02 ──┤──► T-03
T-04 ──┘
```
- T-01, T-02, T-04: mutually disjoint files; parallel-safe.
- T-03 depends on T-02 (both would touch `test_allocator_scheduling_p007b.py` — TestProducerLock vs TestDaemonScheduling sections; T-02 also edits the file for the gate-agreement test). T-03 runs after T-02.
- Each task is independently verifiable; tree stays green after each phase.

### T-01 — Pin threshold boundary + cap kept-set (Items 1+2; test-only)
**Files:** `tests/memory/test_cost_gate_p008.py` only. **Src:** none.
**Do:** (a) add `test_finite_total_exactly_at_threshold_suggests` (threshold 2.0 vs the total-2 fact from `test_finite_total_above_threshold_suggests`); assert 1 suggestion (catches M9 `>=→>`). (b) strengthen `test_suggestions_capped_at_k_with_summary_count`: give the 10 facts distinct **finite** totals via varied `importance`/`confidence` (avoid `utility=0 → inf`), assert `report.cost_gate_suggestions` are the **5 highest** totals and the list is sorted `-total_cost` descending; do NOT assert `fact_id` order (random UUIDs). (c) pin empty-`collected` no-summary branch (`if collected:` at curator.py:559). (d) drain boundaries (creator.py:1578) only if the same file covers them — else record as follow-up (drain tests live in `tests/context/test_allocator_scheduling_p007b.py`; DO NOT edit that file in T-01).
**Done:** M8b and M9 mutations each fail ≥1 test; `pytest tests/memory/test_cost_gate_p008.py -q` green; ruff/mypy clean.

### T-02 — Thread allocator/cost settings (Item 5; W5-FIX-02)
**Files:** `src/paa/agents/orchestrator.py`, `src/paa/daemon.py`, `tests/context/test_allocator_scheduling_p007b.py` (gate-agreement test + FakeOrchestrator accepts/records `settings=` kwarg; do NOT touch TestProducerLock section — owned by T-03), `tests/agents/test_allocation_callsite_p007.py` (add injected-path test; existing `_enable` stays for fallback).
**Do:** (1) `run_allocation(self, *, limit=25, settings: AllocatorSettings | None = None)` → `allocate_attention(..., settings=settings or get_settings().allocator, ...)`. Keep `TYPE_CHECKING` + lazy import posture (no new module-scope settings import; `from __future__ import annotations` already present; docs line cites for orchestrator.py must not shift — add methods only at the existing location). (2) `daemon.py::_run_allocation_tick` → `run_allocation(settings=self._rt.settings.allocator)`. (3) `daemon.py:418` `MemoryCurator(..., cost_gate=self._rt.settings.cost_gate)` (same commit). (4) New `test_allocator_gate_agrees_on_non_global_settings`: Runtime/daemon with non-global `Settings(allocator.enabled=True)`, global disabled → tick allocates/underfed (never `"disabled"` logged as completed); inverse (rt disabled, global enabled → `None`, queue untouched). (5) Daemon cost-gate wire test (non-global `cost_gate.enabled=True` vs global disabled → daemon curator suggests; inverse).
**Done:** both new tests fail at HEAD and pass after; `pytest tests/context/test_allocator_scheduling_p007b.py tests/agents/test_allocation_callsite_p007.py tests/memory/test_cost_gate_p008.py -q` green; ruff/mypy clean (mypy still EXACTLY 3).

### T-03 — Producer-lock hardening (Item 3; test-only, sequenced after T-02)
**Files:** `tests/context/test_allocator_scheduling_p007b.py` — TestProducerLock only.
**Do (cheapest-first, option 1 + negative control; option 2 AST lock if the coder can land it cleanly):** scope the `allowed` exemption to the docstring-example line range in `storage/queue/__init__.py:13` instead of the whole file; do not widen. Add a synthetic negative-control pin (in-memory source string with a multi-line enqueue) asserting the chosen predicate would flag it. Update the stale file-header comment (`would break the docstring pin`). If AST hardening is chosen, prove it on the synthetic multi-line string via predicate-executed test (TREV §1b style). Add one sentence to `allocator.py:42-50` docstring noting the multi-line limit only if option 1 is taken (no ADR).
**Done:** multi-line synthetic enqueue is flagged; scoped `pytest tests/context/test_allocator_scheduling_p007b.py -q` green.

### T-04 — v5→v6 pre-repair step (Item 4; largest blast radius, last in parallel group)
**Files:** `src/paa/storage/relational/migrations.py` (new guarded step + python fn, placed **before** `_entity_norm_column_step(...canonical_name_norm)` in the v6 list), `tests/storage/test_tier_migration_p009.py` (new SQLite red pin + idempotency + PG-gated mirror), `docs/adr/0044` one-line amendment note if the step ships as separate v7 (else runbook line with the migration — decide in-task, prefer inside-v6 no ADR). **No schema change.**
**Do:** SQLite guard `SELECT 1 FROM hot_serving_entity_index WHERE json_valid(aliases) = 0 LIMIT 1` → rewrite corrupt blobs to `'[]'` (+ `'{}'` for `attributes` only if investigations show same-wedge CHECK; else record as residual), emit `migration.backfill_alias_corrupt` with count (same event name as backfill), idempotent, **same single migration transaction** (`_run_migration`), re-run is no-op. PG: no-op guard (documented) OR same-event probe — decide in-task with ADR-0029 owner logic per investigation; assert whichever is chosen. New test `test_v5_shape_corrupt_blob_migrates_to_v6` (build v5 shape via existing `_downgrade_to_v5`, write corrupt row via `PRAGMA ignore_check_constraints=ON` **before** column adds, `connect()` → versions 1..6, `aliases=='[]'`, `aliases_norm=='[]'`, real `canonical_name_norm` fold, one corrupt-count log line with `corrupt_rows == 1`). Guard-idempotency re-run test. PG-gated mirror (`requires_postgres`, skipped without DSN).
**Done:** new SQLite test fails at HEAD (CHECK error) and passes after; `pytest tests/storage/test_tier_migration_p009.py tests/storage/test_schema_parity.py -q` green; fresh-DB path unaffected (guard false on empty table); mypy still EXACTLY 3.

### T-05 — Docs conformance fixes (Major divergences, docs-only, after T-01..T-04)
**Files:** `docs/architecture/overview.md`, `docs/coder-agent.md`, `docs/adr/0036-*.md` + `0040-*.md` (one-line boundary phrase), `src/paa/core/types.py` DRAIN_THROTTLED comment (docs-only comment fix), `src/paa/context/allocator.py` "live" docstring qualification, drawer for P-002 fallback prose in ADR-0039.
**Do:** reword allocator "live loop" → "consumer + daemon tick gated off, operator-fed"; document tier "one rung per tick, convergence in 2 ticks" (or change sweeper only if a coder proves direct-archive intent — prefer doc fix, sweeper change out of scope); qualify P-002 "never ranking truth" → "hydrated hits derived; degraded hits payload-snapshot by design"; fix DRAIN_THROTTLED comment to W5 emitter reality; fix architecture/coder-agent stale entries (ADR list 0036-0044, 5 TUI panels + API snapshot fields, specialists/coding.py exists, daemon imports allocator, vector-fallback row in recall description). Docs lock green.
**Done:** `pytest tests/conventions -q` green; no code behavior change.

## 5. Blast radius / risks / rollback

- **Public API:** only additive-optional `run_allocation(settings=None)`; no other signature changes. Ledger payload unchanged (allocator payload shape unchanged; cost suggestions log-only, ADR-0040:75). No hash-chain risk (`canonical_json` untouched).
- **Migrations:** T-04 adds a guarded DML step inside v6 (same transaction/version row) — no new version, no DDL, no separate ADR if kept in v6. Rollback: revert the step; DBs already migrated are unaffected (guard false after repair).
- **Settings:** T-02 changes which Settings instance the allocator/curator read on the daemon path. Global deployments byte-identical; non-global deployments change from silent no-op to working (intended). Threading must preserve the "never silently mix settings sources" rule (curator.py:156-161 precedent).
- **Tests:** T-01/T-03 are test-only; T-02/T-04 change behavior — each ships with a test that fails at HEAD (red pin proof required in report).

## 6. Risks, open questions, second-order effects

- **Operator policy decision** on `>=` vs `>` policy (ADR-owner): plan pins `>=` per A1; flipping would require amending `config.py:728` + ADR-0040 in the same commit.
- **Item 4 `attributes` scope** + **PG probe scope**: coder decides in-task; widening silently is forbidden; unwidened parts become recorded residuals.
- **T-03 AST cost:** test-time `src/` scan ~seconds; acceptable in test suite, but if it slows the suite materially, fall back to option 1 (line-range) + negative control.
- **Docs conformance (T-05)** is human-judgment work the docs lock cannot verify (AGENTS.md: "lock cannot see a reference that is wrong without being broken") — reviewer must read the wording, not just run the lock.
- **Out of scope recorded:** unredacted siblings `context_agents.py:137,252,367` (pre-existing FINDING — tech-debt, not this plan), `_PANEL_SCAN_LIMIT` perf, O(N) `collected` heapq, daemon task-identity, `open_correlations` ordering.

## 7. Verification plan (overall)

- After each task: `ruff check src tests` (0), `mypy src` (exactly 3), task's scoped pytest green, red-pin proof (test fails at HEAD, passes after).
- After all tasks: docs lock green (`tests/conventions`), full `pytest tests/ -q` reconciled against the 14-baseline (expect 12 pre-existing failures, zero new IDs), FINAL-REV fresh reviewer on the combined diff.
- PG/S3/Docker halves stay NOT-RUN locally and are cited as CI-only wherever a task touches them.
