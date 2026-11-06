# Brief — W2-DELTA-REV (DELTA) · wave 2 delta review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Lightweight delta review of Wave 2 improvement commits (code 2c72f96 + docs 1541e0b) — did MAJOR fixes close without new defects?
Role: code reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
3. docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-01.md (dispositions)
4. docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-DOCS.md (docs fix)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md
6. COMMS.md (read at start)

## 3. Task
Review ONLY improvement commits: `git diff 5c14ed3..HEAD -- src/ tests/ docs/` excluding docs/agent-ops. Verify MAJOR fixes: R-W2-001 delivered-only touch, R-W2-002 curator preview/dry-run (verify mechanism actually prevents first-tick deletes), TREV loop/batch/negative tests actually present, orphan guard (R-W2-004). Spot-check REJECTED/DEFERRED reasoning. Run scoped tests only.
Output: verdict APPROVE / APPROVE-WITH-CHANGES / REJECT with new BLOCKER/MAJOR (or none).

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w2-review/DELTA-review.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings count. Final ≤8 lines.
