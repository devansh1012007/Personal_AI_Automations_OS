# Brief — FINAL-DELTA-REV (DELTA) · final delta review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Independent delta review of FINAL-IMPR-02 (code 1889808 + docs 91da8c8) — did the F-01/FINAL-01..03 fixes land without new defects? Verdict gates SUMMARY.md and install/launch.
Role: code reviewer (READ-ONLY on source). You did not write this change.
Branch/worktree: current working directory on branch `ops/20261106-memory-remembering-agent`, commit any report to that branch. Do NOT create a new branch or worktree.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, docs conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: ruff 0, mypy 3, pytest 12 pre-existing)
4. Plan §A/K: `C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md`
5. docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (dispositions: FIXED 4 / PARTIAL 1 / REJECTED 1 / DEFERRED 4)
6. Findings: FINAL-01..05 in reports/final/FINAL-01-correctness.md; F-01..05 in FINAL-02-operability.md
7. COMMS.md (read at start)

## 3. Task
Review ONLY: `git diff 2f7b083..91da8c8 -- src/ tests/ docs/` (code 1889808 + docs 91da8c8). Check: (a) coder-note rewrite (FINAL-01) — is the keyword-vs-planner truthful vs C01? (b) fallback floor forwarding (FINAL-02) — `explicit_prune_floor()` same source as curator, defaults byte-identical? (c) `redact()` on drain error (F-02 in-set) — correct helper/import, and confirm the 2 out-of-set lines (runtime.py:481, cli/main.py:649) are truly untouched? (d) new handle-touch e2e (FINAL-03) — asserts real retention effect via handle()? (e) F-01 runbook — commands/paths exist, snapshot/rollback truthful (cold-lake replay vs code revert)? (f) docs path:line cites hold claimed content (conventions green).
Output: verdict APPROVE / APPROVE-WITH-CHANGES / REJECT with BLOCKER/MAJOR/MINOR/NIT findings (file:line evidence). Run scoped tests only (e2e + maintenance + conventions).

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/final/DELTA-review.md
READ-ONLY: everything else. FORBIDDEN: editing source/tests/docs (except your report).

## 5. Deliverables
Report with ORCH-DIGEST verdict + findings counts. Commit ONLY your report + COMMS line: explicit `git add -- <paths>`. Final ≤8 lines: STATUS|agent|task|report|SHAs|blockers.
