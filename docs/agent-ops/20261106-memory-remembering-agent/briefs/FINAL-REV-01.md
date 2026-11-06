# Brief — FINAL-REV-01 (FINAL) · final integration review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Final whole-branch correctness/design review of ALL Waves 1–4 combined (48 files, +5933/−471 vs base, excluding run folder). Catch cross-wave interactions per-wave reviews missed.
Role: final reviewer (READ-ONLY on source). You own the whole branch diff; another reviewer owns operability/security.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: ruff 0 findings, mypy 3 win32 pre-existing, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — success criteria (A) + D1–D6
5. docs/agent-ops/20261106-memory-remembering-agent/01-work-ledger.md (wave dispositions)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md (digest verdict required; scope = whole branch)
7. COMMS.md (read at start)

## 3. Task
Review: `git diff eb6606e24d33f56b1dd7d2d811110b77940b8318..HEAD -- src/ tests/ docs/` excluding docs/agent-ops (48 source/test/docs-citation files). Focus on CROSS-WAVE issues: touch + curator + sink/drain + extractor + config interplay (e.g. a Wave 1 drain signature vs Wave 4 preview flag, Wave 3 counters vs Wave 4 IGNORED event, Wave 2 delivered-only touch vs Wave 4 e2e). List merge artifacts, duplicated helpers, inconsistent conventions, dead code, and test gaps that let a whole-branch regression pass. Check docs citations accurate (conventions green at gate-w4-2).
Output: verdict APPROVE/APPROVE-WITH-CHANGES/REJECT with findings by severity (BLOCKER/MAJOR/MINOR/NIT), evidence file:line, fixes. Run scoped tests only (pick the relevant files; do not run full suite).

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/final/FINAL-01-correctness.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings counts. Final ≤8 lines.
