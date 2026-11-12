# Brief — W1-IMPL-07 (IMPLEMENTER) · wave 1c · run 20261109-next-spec

## 1. Role and mission
Mission: N1.5 registration + permissions (build_work_tools, --workspace, READ_ONLY_TOOLS, DEFAULT_MUTATING_TOOLS git_stage*, specialist pin). Why: tools exist but are not wired; git_stage is PLAN-exempt today (MAJOR-3 BLOCKER for wiring). Failure looks like: N1.5 merges without glob -> git_stage PLAN-allowed, mutating tool exempt by accident.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N1.5 + D-10/D-11 owner (+git_stage rename) + Wave-1 permissions tests
3. src/paa/coder/agent.py (build, tools dict merge, MUTATING_RUNTIME_TOOLS), runtime_tools.py READ_ONLY_TOOLS, coder/permissions.py:127 _MUTATING_TOOLS, cli/coder.py (--workspace, NOT cli/main.py per planner B-3), __init__.py export pattern, specialists/coding.py CODING_TOOL_NAMES + tests/agents/test_coding_specialist_p011.py coverage pin
4. W1-REV-03 MAJOR-3 (git_stage PLAN-allowed today) + W1-REV-02 §8 (FS_READ_TOOLS stand-allow) + W1-IMPL-03/04/05/06 reports (factory names)
5. Template report-implementation.md, COMMS.md

## 3. Task and acceptance criteria
Task T-N1-5 (N1.5 serial, integration): build_work_tools(runtime, workspace) merges runtime+fs_read+fs_write+shell+git into single tools dict; cli/coder.py --workspace threads to CoderAgent.build(workspace=...) → build_work_tools; extend READ_ONLY_TOOLS with git_status/git_diff/read_file/glob/grep + stand-allow git read-only + fs reads (ASK never prompts for reads); add git_stage* to DEFAULT_MUTATING_TOOLS tuple (literal) so git_stage is mutating (PLAN denies, ASK prompts), verify every mutating tool name matches a DEFAULT_MUTATING_TOOLS pattern via new test (fail if any mutating escapes); update coding-specialist coverage pin deliberately (CODING_TOOL_NAMES/list + test pin) with reason recorded. End with agent.py frozen except explicit journal=None kwarg slot for Wave 2 (N2.2).
Acceptance: mutating-coverage test fails if a mutating tool matches no pattern (prove by adding a probe mutating tool name and showing red, then removing); PLAN/ASK/LOCKDOWN matrix per tool passes; pytest tests/coder/test_fs_read_tools.py tests/coder/test_fs_write_tools.py tests/coder/test_git_tools.py tests/coder/test_shell_tools.py tests/agents/test_coding_specialist_p011.py -q passes (or scoped subset); ruff 0; mypy 3; conventions 34.
Also update ADR README index/overview staleness? MINOR-4 overview:206 is ORCH sweep, not you unless trivial — note deferred.
Commands: above pytest scoped, ruff check src tests, mypy src.

## 4. Ownership
WRITE-SET: src/paa/coder/agent.py, src/paa/coder/runtime_tools.py, src/paa/cli/coder.py, src/paa/coder/__init__.py, src/paa/coder/permissions.py (git_stage* glob only), src/paa/agents/specialists/coding.py (pin update), tests/agents/test_coding_specialist_p011.py, tests/coder/test_new_integration.py (mutating coverage only)
READ-ONLY: src/paa/coder/fs_*_tools.py, workspace.py, validation/patch.py, sandbox/*
FORBIDDEN: writing new tool implementations, ledger files, validation engine/ast scanner (TASK-3/4), policy decision (TASK-5), scripts/status.py, .github/workflows/ci.yml

## 5. Siblings
No parallel implementers (serial integration). Wave 0b/1b done; Wave 2a N2.1 next (frozen ToolJournal) after you.

## 6. Skip list
Do NOT implement ToolJournal (Wave 2a). Do NOT fix MAJOR-1 (grep) / MAJOR-2 (walk, already fixed) / BLOCKER-1 (paths, already fixed). Do NOT touch docs/architecture/overview.md (ORCH sweep, MINOR-4).

## 7. Environment
Scoped pytest only. No full suite, docker.

## 8. Deliverables
Report: reports/w1-impl/W1-IMPL-07-N1-5.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
This is the BLOCKER gate for wiring: glob must land in same commit as tools wiring.
