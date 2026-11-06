# Brief — FINAL-REV-02 (FINAL) · final operability review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Final whole-branch operability/security/scale review (failure modes, rollout/rollback, load/abuse, migrations). Complement the correctness reviewer — do NOT duplicate correctness unless you find a security/data-loss hole.
Role: final reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, security conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: ruff 0, mypy 3, pytest 12)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — J (impact), K (rollout/rollback), L (risks R1–R3)
5. docs/agent-ops/20261106-memory-remembering-agent/01-work-ledger.md (wage dispositions)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md (digest verdict; scope = whole branch operability)
7. COMMS.md (read at start)

## 3. Task
Review: same whole-branch diff as FINAL-REV-01. Lens: (a) load/abuse — 100x backlog drain, concurrent drains/recalls, oversized/hostile payloads, DoS via pending COUNT frequency; (b) secret/PII logging (redact audit); (c) first-production-curator-run safety (preview, snapshot guidance); (d) rollback per change (docs accurate?); (e) Docker/deploy gaps (embeddings extra excluded — production hash embedder caveat documented?); (f) migration/compat (no schema change — true? print the diff proves it); (g) residual risks R1–R3: honest? mitigation complete?
Output: verdict + findings by severity + deploy/operator notes.
Run scoped tests only.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/final/FINAL-02-operability.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings counts. Final ≤8 lines.
