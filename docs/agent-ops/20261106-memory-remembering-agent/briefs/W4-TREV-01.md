# Brief — W4-TREV-01 (TREV) · wave 4 test review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Audit Wave 4 tests (10 new e2e + observability/minor regression tests) as work product — would they catch a missing paraphrase, a stuck pending backlog, or a preview bypass?
Role: test reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (testing conventions; honesty about fakes)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline; gate-w4-1 stale tests excluded)
4. Plan item T08 (re-scoped) + C07 (plan path in REV brief)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-tests.md
6. COMMS.md (read at start)

## 3. Task
Review the 10 new tests in `tests/memory/test_e2e_remember_recall.py` (count yourself) plus coverage of the 3 MINOR fixes. Verify: fail-before (temp worktree if possible), determinism, mocking matches real contracts (fake store semantics — is `search` ranking real?), mutation thinking: (a) sink removed → e2e fails? (b) payload object_value removed → fails? (c) dry_run fallback removed → sibling/stale test would catch? (d) preview flag always-set → partially-preview stays? (e) IGNORED INFO removed → fails? (f) pending_count removed → capture_logs fails? Produce P1/P2/P3 missing cases with sketches.
Run scoped tests only; repeat for flakiness.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w4-review/TREV-tests.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict, per-test table, mutation table, missing cases.
Final ≤8 lines.
