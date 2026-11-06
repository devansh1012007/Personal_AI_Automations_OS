# Brief — W1-DELTA-REV (DELTA) · wave 1 delta review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Lightweight delta review of Wave 1 improvement commits — did the fixes close BLOCKER/MAJOR without introducing new defects? Verdict only.
Role: code reviewer (READ-ONLY on source).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
3. docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-01.md (dispositions: 13 FIXED / 2 REJECTED / 1 DEFERRED)
4. docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-02.md (docs-reference fix, 17 citations)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md (digest verdict required)
6. COMMS.md (read at start)

## 3. Task
Review ONLY the improvement commits: code e966b68010bda6ba1eb3cc4b686500e9cfd934ba and docs 5b8d6ee (diff `git diff aadb42f..HEAD -- src/ tests/ docs/` excluding docs/agent-ops). Check each FIXED MAJOR (R-W1-001/01/02 composition tests, R-W1-03 fake fidelity) is actually present and sound; spot-check REJECTED/DEFERRED reasoning; run scoped tests only (single test files). Do NOT re-review the original Wave 1 implementation (separate reviews exist).
Output: verdict APPROVE / APPROVE-WITH-CHANGES / REJECT with any new BLOCKER/MAJOR (or none).

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w1-review/DELTA-review.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings count. Final message ≤8 lines.
