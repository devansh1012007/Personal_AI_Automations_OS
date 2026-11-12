# Brief — W0-IMPL-05 (GENERAL) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Triage baseline failures: label 1 failed + 245 skipped + 4 xfailed as environment-specific or real, file real ones as tasks. Why: Wave 1 green gate needs honest baseline (spec §2). Failure looks like: env failure (Docker/non-root user) mistaken for real regression, or real failure buried as env.
Your role: general fixer (report only, no code).

## 2. Required reading
1. docs/agent-ops/20261109-next-spec/templates/STANDING-ORDERS.md (binding)
2. docs/agent-ops/20261109-next-spec/00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N0.5
3. tests/conventions? Use existing baseline evidence: ruff 0, mypy 3, conventions 26, collected 3754 (ops branch). Full suite baseline on Linux: 3504 passed/245 skipped/4 xfailed/1 failed (test_user_is_the_workspace_owner_not_root)
4. Template: docs/agent-ops/20261109-next-spec/templates/report-implementation.md (adapt: this is report-only)
5. COMMS.md

## 3. Task and acceptance criteria
Task T-N0-5 (N0.5): Report each baseline failure/skip/xfail labelled env vs real with evidence (path:line, command output). Acceptance: report lists each of the 1 failed + skip groups + 4 xfailed with label + evidence + if real, filed task description (issue-ready, not code). Do NOT edit src/tests.
Commands you may run (scoped): grep for test_user_is_the_workspace_owner_not_root definition, read its fixture; run focused pytest -k test_user_is_the_workspace_owner_not_root -q on ops branch; check if Docker needed; read tests/ledger/test_recovery, tests/storage that skip.

## 4. Ownership
WRITE-SET: docs/agent-ops/20261109-next-spec/reports/w0-investigation/N0-5-triage.md (new, report only)
READ-ONLY: src/**, tests/**
FORBIDDEN: any code edit, any commit to src/tests, any change to README/CI
Need something outside? Report DEFERRED-DEPENDENCY.

## 5. Parallel siblings
W0-IMPL-01 (N0.1), W0-IMPL-02 (N0.2) — they own docs/tests/conventions, you own only report file.

## 6. Skip list
Do NOT run full pytest suite (ORCH will gate). Do NOT try to fix failures. Do NOT modify docs/plans/*.

## 7. Environment
Scoped: single-test pytest runs only.

## 8. Deliverables
Report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/N0-5-triage.md (ORCH-DIGEST first)
Commits: report-only commit (docs-only) with trailers. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit git paths.
Final message ≤8 lines: STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role notes
General: minor fixes only, but this task is report-only; any code fix goes to coder next wave.
