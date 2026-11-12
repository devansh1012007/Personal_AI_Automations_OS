# Brief — W1-IMPR-04 (IMPLEMENTER) · wave 1b improve · run 20261109-next-spec

## 1. Role and mission
Mission: Fix W1-REV-03 BLOCKER-1 (git_stage path expansion) + MAJOR-1 (write MODIFY trailing newline) + MAJOR-2 (edit_file result-cap bypass). Why: secrets staged via pathspec expansion; write_file exact-bytes contract false for MODIFY; edit past 256KiB breaks memory bound. Failure looks like: .env committed by git_stage([sub]) even though fs tools refuse it.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. W1-REV-03 review: reports/w1-review/W1-REV-03-review.md §4 BLOCKER-1/MAJOR-1/MAJOR-2, §5 P1-P7 (exact probes + fix shape)
3. src/paa/coder/git_tools.py:314-333 _jail_paths, :516 argv; fs_write_tools.py:489,498; validation/patch.py:588 wants_trailing, :335 patch-size, fs_write_tools.py:81-86 MAX_CONTENT_BYTES
4. W1-IMPL-04 + W1-IMPL-06 reports, tests/coder/test_fs_write_tools.py, test_git_tools.py
5. Template: report-implementation.md, COMMS.md
6. Workspace jail invariant: jail-then-protect per path + expansion (see REV-03 fix shape: refuse dir/./magic/glob OR expand via git ls-files + is_protected on every expansion all-or-nothing)

## 3. Task and acceptance criteria
Task T-N1-2/4-IMPR (3 fixes, one agent to keep tool files coherent):
- BLOCKER-1 (git stage/diff expansion): either refuse directory, ".", and any path containing pathspec-magic/glob metacharacters `* ? [ :` (documented refusal), OR expand each pathspec against worktree (`git ls-files`) and run is_protected on EVERY expansion, all-or-nothing (stage nothing if any protected). Apply to git_stage AND git_diff (reviewer: same expansion leaks). Add tests for all 4 probe shapes (sub, ., :/, *.txt) that fail before (staged .env before) and pass after (error + stage nothing). No push/force/config/hooks regression.
- MAJOR-1 (MODIFY trailing newline): fix in planner (_plan_file wants_trailing: treat absence of no-newline marker on new side as new trailer, not prior's) or in synthesis (encode new trailer explicitly). Overwrite-empty and overwrite-no-trailing tests that fail before, pass after. Keep CREATE behaviour (TASK-2 test still green).
- MAJOR-2 (edit result cap): in EditFileTool.run compute resulting body UTF-8 length, refuse with limit_exceeded payload shape if >MAX_CONTENT_BYTES before synthesis. Add test (1 char -> 300KiB) that fails before, passes after.
Acceptance: 3 new test groups fail-before/pass-after; pytest tests/coder/test_fs_write_tools.py tests/coder/test_git_tools.py tests/validation/test_patch.py -q passes; ruff 0; mypy 3; conventions 34.
Do NOT attempt MAJOR-3 (git_stage PLAN glob — N1.5-owned, must land before wiring; add NEED note), MINOR/NITs, hand-built-plan revalidation (IMPL-04 §13.3 defence-in-depth, note deferred).
Commands: scoped pytest above, ruff, mypy.

## 4. Ownership
WRITE-SET: src/paa/coder/git_tools.py, src/paa/coder/fs_write_tools.py, validation/patch.py (trailing-newline fix only), tests/coder/test_git_tools.py, tests/coder/test_fs_write_tools.py, tests/validation/test_patch.py (new tests only for this task)
READ-ONLY: workspace.py, permissions.py (do not edit for MAJOR-3), ADR-0045
FORBIDDEN: fs_read/shell tools, agent.py, permissions.py, validation/engine.py, ast_scanner.py

## 5. Siblings
W1-IMPR-03 (read MAJOR-1/2) parallel — disjoint files, do NOT touch fs_read_tools.

## 6. Skip list
MAJOR-3 git_stage glob is N1.5-owned (BLOCKER for N1.5 wiring, not 1b lock). Hand-built plan revalidation noted as defence-in-depth, not required now.

## 7. Environment
Scoped pytest single-file/batched only.

## 8. Deliverables
Report: reports/w1-review/W1-IMPR-04-improve.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
BLOCKER is gating: no N1.5 wiring until this lands.
