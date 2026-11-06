# Brief — W3-TREV-01 (TREV) · wave 3 test review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Audit Wave 3 tests (T06 extractor, T07 config) as work product. Confirm each new test fails without its fix and covers the regression risk, not just happy path.
Role: test reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (testing conventions; Hypothesis guidance is relevant for decay invariants)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline; gate-w3-1 excluded)
4. Plan items C05/C06/T06/T07 (plan path as in REV brief)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-tests.md
6. COMMS.md (read at start)

## 3. Task
Review new tests: T-005 (18 added in test_creator.py/test_llm_extractor.py per digest — verify) and T-006 (16 added in test_decay.py per digest — verify). Count yourself via diff.
Judge behavior vs detail, determinism, mocking fidelity, fail-before (temp worktree if possible), mutation thinking: (a) pronoun map removed → user fact lost/still drops silently — caught? (b) counters removed → drop uncounted — caught? (c) LLM extractor changed to always-return-empty — caught? (d) explicit floor ignored (always per-domain) — caught? (e) floor always-on (default 0.15 overrides per-domain) — caught? (f) decay_lambda warn missing — caught? (g) unknown key crash — caught?
Produce P1/P2/P3 missing cases with sketches: pronoun variants (I'M, WE'RE, uppercase), "user" as real name collision, empty/pronoun-only text, counter under LLM path, .env-file explicit-set, multiple decay_lambda domains, DOMAINS values drift, registry column dormant.
Note gate-w3-1 orphan-guard regression is already known — do not re-report; note it as context.
Run scoped tests only; repeat for flakiness.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w3-review/TREV-tests.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict, per-test table, mutation table, missing cases with sketches.
Final ≤8 lines.
