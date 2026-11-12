# Brief — W1-IMPL-06 (IMPLEMENTER) · wave 1b · run 20261109-next-spec

## 1. Role and mission
Mission: Git tools git_status/git_diff (read-only) + git_stage/git_commit (mutating) via fixed argv, no push/force/config/hooks (N1.4 D-7). Why: agent must commit without reaching remote. Failure looks like: git_add escapes PLAN (matches zero globs), free-form args reach remote, commit with no staged changes crashes.
Your role: implementer (coder). NOTE rename: git_stage not git_add (owner fix).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.4 + D-7 + §G D-7/D-10 owner fix
3. ADR-0045 + src/paa/coder/workspace.py (jail then protect)
4. src/paa/coder/permissions.py:127 _MUTATING_TOOLS globs (git_add matches NONE — must rename to git_stage and add git_stage* to DEFAULT_MUTATING_TOOLS literal so PLAN/ASK classification holds), runtime_tools.py READ_ONLY_TOOLS
5. coder/loop.py Tool protocol
6. Template report-implementation.md, COMMS.md

## 3. Task and acceptance criteria
Task T-N1-4: src/paa/coder/git_tools.py with git_status, git_diff (read-only), git_stage, git_commit (mutating; git_stage is fixed-argv `git add -- <paths>` jail-then-protected). No push/force/config/hooks allowed — any call attempting those args must be rejected at tool boundary. Workspace root = git repo root jailed. Tests tests/coder/test_git_tools.py (slow where needed): git_commit with no staged changes fails cleanly, no remote operations possible, each tool correct in PLAN (mutating refused), ASK (prompt for mutating only), LOCKDOWN. Acceptance: tests pass, ruff 0, mypy 3. The mutating-coverage test (every mutating name matches DEFAULT_MUTATING_TOOLS) may land in N1.5 — coordinate via COMMS CONTRACT after commit; do not edit agent.py/permissions in this task (leave NEED if wanted).
Commands: pytest tests/coder/test_git_tools.py -q, pytest tests/conventions -q.

## 4. Ownership
WRITE-SET: src/paa/coder/git_tools.py, tests/coder/test_git_tools.py
READ-ONLY: workspace.py, permissions.py (do not edit — propose change in report as NEED if needed for N1.5)
FORBIDDEN: fs_read/write/shell tools, agent.py, permissions.py, cli*, workspace.py

## 5. Siblings (parallel 1b)
W1-IMPL-03 (read), W1-IMPL-04 (write), W1-IMPL-05 (shell). Disjoint.

## 6. Skip list
Do NOT implement push, force, config, hooks. Do NOT use free-form git argv from model.

## 7. Environment
Scoped pytest single-file only.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-06-N1-4.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: git_stage rename is owner-required; document PLAN/ASK matrix.
