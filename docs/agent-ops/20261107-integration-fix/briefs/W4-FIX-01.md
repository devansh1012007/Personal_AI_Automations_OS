# Brief — W4-FIX-01 (IMPL) · wave 4 fix · run 20261107-integration-fix

## 1. Role and mission
Fix REV-W4 MAJORs R-W4-004 (graph second writer), R-W4-003 (TUI mirror dead), MINORs R-W4-005 (false notify comment), R-W4-007 (claim failure propagates), R-W4-008 (backfill corrupt count-log) + casualty owner updates C1–C5 (deliberate, rated stale by REV — you update with justification, never weaken). Why: the graph writer breaks "one normalisation point"; the TUI mirror renders `disabled` forever; claim failure violates degrade-never-raise; casualties red the gate.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; runtime contract import; parity lock if SQL changes — none here)
3. `docs/agent-ops/20261107-integration-fix/reports/w4-review/REV-W4.md` §§4–5 (R-W4-003/004/005/007/008 + casualty ratings C1–C5 in §4 table), §9
4. `docs/agent-ops/20261107-integration-fix/reports/w4-review/TREV-W4.md` §§5–6 (P1/P2 test adds relevant to your files only)
5. `docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-01.md` (allocator call sites), `W4-IMPL-02.md` (TUI mirror NEED + formatter), `W4-IMPL-03.md` (normalisation write path)
6. Code at HEAD (verify line numbers yourself): `src/paa/storage/graph/sqlite_backend.py:79-86,144-149` (second writer), `src/paa/memory/facts.py` upsert (the canonical write path to mirror the fold computation), `src/paa/tui/runner.py` `_build_info` + `src/paa/tui/dashboard.py:362-423` (`format_reflection_health` in `reflection.py`), `src/paa/context/allocator.py` (`queue.claim` ~:254, notify comment ~:332), `src/paa/memory/creator.py` throttle (read-only — casualty C1 is its TEST), `src/paa/storage/relational/migrations.py` `_backfill_entity_norm`, casualty test files (§3).

## 3. Task and acceptance criteria
Task W4-FIX-01. All items:
- **R-W4-004 (MAJOR, root cause)**: include `canonical_name_norm`/`aliases_norm` in `_UPSERT_ENTITY_SQL` (both INSERT and `ON CONFLICT DO UPDATE`), computed via the same `fold_case` the facts path uses (import the canonical function — do not duplicate the fold). New cross-writer test (graph-upsert → facts-resolve finds it; facts-upsert → graph read sees it) in a test file you own (verify `tests/storage/test_graph.py` is the natural home — if so, that file is in your write-set for this addition only). Kuzu backend: verify it does not touch this table (state the verification).
- **R-W4-003 (MAJOR)**: populate the 3 reflection keys in `runner._build_info` from the shared `reflection_health(...)` helper (CLI already uses it — same source) and have the dashboard helper call the SHARED formatter (`format_reflection_health`) instead of re-implementing it. Test: enabled+running → TUI health shows age (assert non-disabled), enabled+stale → stale string; disabled → `disabled`. (Tests in `tests/tui/test_dashboard.py` mirror style or `tests/tui/test_runner_panels.py` — follow the brief's existing mirror-test pattern; hand-built info dicts pinning the RENDER contract already exist.)
- **R-W4-005**: correct the false notify comment in `allocator.py` to state the accepted at-most-once loss (no behavior change; the module docstring already admits it). Do not ack-on-notify-failure (the reclaim cycle is accepted — changing ack semantics is a separate decision; record it).
- **R-W4-007**: wrap `queue.claim` failure → log warning + return underfed/error result (never raise); type `run_allocation`'s return as the allocator's result type (TYPE_CHECKING import if a cycle forces it — if a TYPE_CHECKING block is needed, classify it per the import-conventions lock table in `tests/conventions/test_import_conventions.py` in the SAME commit). Tests: claim-raises → underfed, no raise.
- **R-W4-008**: count-log rows backfilled from unparseable alias blobs (explicit `migration.backfill_alias_corrupt` count line in the migration log; keep the `'[]'` behaviour — honest middle, no wedging). Test: a corrupt blob row → count logged, migration completes.
- **Casualties (deliberate owner updates, each with a report section justifying stale-vs-defect per REV ratings)**:
  - C1 `test_creator.py::test_drain_105_backlog_drained_not_paginated` → throttle-aware expectation (100 indexed, remainder deferred + throttle log) or two-window drain.
  - C2 `test_dialect.py` 7 → update stale `lower(e.canonical_name)` literals to the norm-column + fixture tables gain norm columns; hostile-term behaviour re-pinned.
  - C3a `test_facts.py::TestTheCorruptRow` → corrupt `aliases_norm` (not the raw blob) re-pin, fail-closed.
  - C3b `test_facts.py::TestTheGateModel` → raw-INSERT rows are norm-NULL (foreign-writer class); update the gate model to include norm columns (test the new truth; note the R-W4-004 class).
  - C4 `test_tier_migration_p009.py::test_schema_version_is_five` → version 6.
  - C5 `test_entity_resolution.py` 5 → raw `_insert` helper writes `fold_case` norm columns (fixture fix, not expectation weakening); escaped-blob now resolves via norm (intended fix — assert so).
- **TREV P1 (cheap)**: direct `run_allocation` tests — allocated path + underfed-on-claim-failure (R-W4-007) in `tests/agents/` (glob placement; orchestrator-level). Do NOT schedule it (daemon tick is W5, not here).
- Acceptance: graph cross-writer tests green; TUI mirror 3 states green; claim-failure green; backfill-corrupt count green; casualties green with justification sections; ruff 0, mypy total 3, docs untouched (W4-FIX-DOCS owns docs).

## 4. Ownership
WRITE-SET (only these): `src/paa/storage/graph/sqlite_backend.py`, `src/paa/tui/runner.py` (_build_info keys only), `src/paa/tui/dashboard.py` (shared formatter call only), `src/paa/context/allocator.py` (comment + claim guard only), `src/paa/storage/relational/migrations.py` (count-log only), `tests/memory/test_creator.py`, `tests/storage/test_dialect.py`, `tests/memory/test_facts.py`, `tests/storage/test_tier_migration_p009.py` (version pin), `tests/memory/test_entity_resolution.py`, `tests/storage/test_graph.py` (cross-writer tests only; if the natural home differs, record the actual file in your report), `tests/agents/test_allocation_callsite_p007.py` (new), + mirror tests in `tests/tui/test_dashboard.py` (append). READ-ONLY: everything else. FORBIDDEN: `docs/**`, `src/paa/core/types.py`, `src/paa/ledger/*`, `src/paa/daemon.py` (allocator scheduling is W5), `src/paa/context/metrics.py`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `runner.py` + `allocator.py` (shared files).

## 5. Parallel siblings this fix wave
W4-FIX-DOCS (docs re-point — disjoint: you edit zero docs).

## 6. Skip list and known traps
- R-W4-002 (allocator unscheduled + no producer) + R-W4-006 (DRAIN_THROTTLED emitter) + R-W4-009 NITs = TRACKED RESIDUALS (W5 owns the tick; no code here). Do NOT invent a producer.
- Backfill chunking (DO LATER) — do not chunk in this task.
- The `_DRAIN_WINDOW_SECONDS`-at-EOF placement: leave alone (REV noted the docs task makes mid-file free later; moving constants churns citations).
- Casualty updates must not weaken: each flips a stale pin to the new truth with the REV rating cited.

## 7. Environment and tests
Scoped only: your test files + graph + tui + allocator + facts/dialect/entity suites + ruff/mypy scoped. Tmp fixtures only. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: W4-FIX-01`, `Agent: W4-FIX-01`, `Wave: W4-FIX`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-review/W4-FIX-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
