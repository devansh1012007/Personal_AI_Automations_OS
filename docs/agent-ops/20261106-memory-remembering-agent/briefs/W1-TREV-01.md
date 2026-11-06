# Brief — W1-TREV-01 (TREV) · wave 1 test review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Audit Wave 1 tests as work product — would they catch the real bugs (empty semantic content, stuck pending, drain loss)? Missing tests for behavior changes are automatic MAJOR.
Role: test reviewer (READ-ONLY on source).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (testing conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: 12 pre-existing Windows failures, mypy 3 — not new)
4. Plan items C01, C02, C01b, T01–T03 (plan path as in REV brief)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-tests.md
6. COMMS.md (read at start)

## 3. Task
Review new tests in diff for tests/memory/test_creator.py, tests/runtime/test_memory_creator.py, tests/api/test_memory.py, tests/cli/test_memory.py (14 added per implementer digest; verify count yourself).
Judge: behavior vs implementation detail, determinism/isolation, over-mocking (fakes must match real contracts — check fake store/embedder against `VectorStore`/`Embedder` protocols), fail-before/pass-after (run in temp worktree against pre-fix if possible, else reason it and say so), mutation thinking (name 5+ plausible bugs: payload key missing, drain not called, mark_embedded on failure, pending flipped to failed, wrong collection, sink None in degraded path — would tests catch each?), coverage of error paths and boundaries.
Produce prioritized missing edge cases P1/P2/P3 with ready-to-write sketches: boundaries, empty/null/duplicate, unicode, oversized payloads, malformed/hostile (NUL, injection), concurrency/race (double drain), idempotency, partial failure (embedder raises mid-batch), resource exhaustion, authz bypass, backward compat, clock/timezone.
Run scoped tests only (single files); repeat counts for flakiness.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w1-review/TREV-tests.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict, findings by severity, per-test table, mutation table, missing cases with sketches.
Final message (≤8 lines): STATUS | agent | scope | report path | verdict | missing-edge counts.
