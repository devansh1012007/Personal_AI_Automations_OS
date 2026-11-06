# Brief — W4-DELTA-REV (DELTA) · wave 4 delta review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Lightweight delta review of Wave 4 improvement commits (code d7801a8 + docs 51d5eba) — did MAJOR fixes close without new defects?
Role: code reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
3. docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-01.md (dispositions)
4. docs/agent-ops/20261106-memory-remembering-agent/reports/w4-improve/IMPR-DOCS.md (docs fix)
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md
6. COMMS.md (read at start)

## 3. Task
Review ONLY improvement commits: `git diff c8892df..HEAD -- src/ tests/ docs/` excluding docs/agent-ops. Verify: R-W4-001 fallback preview CONSUMPTION (permanently-broken curator still eventually deletes OR is explicitly preview-forever with loud warning — which did the improver choose and is it safe?), R-W4-002 liveness (one erroring phase outcome), stale daemon tests alignment (preview contract + orphan semantics), R-W4-003/004/005/006 fixes.
Output: verdict APPROVE / APPROVE-WITH-CHANGES / REJECT with new BLOCKER/MAJOR (or none). Run scoped tests only.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w4-review/DELTA-review.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings count. Final ≤8 lines.
