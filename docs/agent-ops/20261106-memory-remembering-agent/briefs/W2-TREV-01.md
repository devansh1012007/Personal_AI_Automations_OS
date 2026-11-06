# Brief — W2-TREV-01 (TREV) · wave 2 test review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Audit Wave 2 tests (T-003 T04, T-004 T05) as work product — would they catch touch not happening, curator not running, or daemon dying?
Role: test reviewer (READ-ONLY on source). Write only your report file.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (testing conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: 12 Windows failures, mypy 3)
4. Plan items C03/C04/T04/T05 (plan path as in REV brief)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-tests.md
6. COMMS.md (read at start)

## 3. Task
Review new tests: T-003 (14 added per digest — verify count in tests/agents, tests/coder, tests/api, tests/cli) and T-004 (5 added in tests/daemon/test_maintenance_loop.py).
Judge behavior vs detail, determinism, mocking matches contracts, fail-before (temp worktree if possible), mutation thinking: (a) touch removed → use_count stays 0 — caught? (b) touch raises → recall still returns — caught? (c) worker pre-filter superset touched — caught? (d) coder touch uses serialized dict (no id) → crash or silent no-op — caught? (e) daemon curator call removed → sweep-only — caught? (f) curator exception propagates → daemon dies — caught? (g) budget ignored → long run — caught?
Produce prioritized missing edge cases P1/P2/P3 with sketches: touch on empty result, superseded semantic hits, concurrent recalls, curator with empty DB, curator phase failure mid-run, maintenance during ingestion load, archive_writer=None, vector_store=None degrade, `use_count` reset after hot-replay (documented derived state).
Run scoped tests only; repeat for flakiness.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w2-review/TREV-tests.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict, per-test table, mutation table, missing cases with sketches.
Final ≤8 lines.
