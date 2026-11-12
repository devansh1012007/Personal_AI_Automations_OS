# Brief — W1-IMPR-03 (IMPLEMENTER) · wave 1b improve · run 20261109-next-spec

## 1. Role and mission
Mission: Fix W1-REV-02 MAJOR-1 (grep single-file silent empty) + MAJOR-2 (10k walk cap silent truncation) in fs_read_tools.py. Why: incomplete grep presented as complete violates repo "truncation marked, never silent" convention. Failure looks like: model trusts empty grep over a file that contains the pattern.
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. W1-REV-02 review: docs/agent-ops/20261109-next-spec/reports/w1-review/W1-REV-02-review.md §4 MAJOR-1/MAJOR-2, §5 P1/P3
3. src/paa/coder/fs_read_tools.py:523, :613-621, :434-436, :565-567, :546-551
4. W1-IMPL-03 report + tests/coder/test_fs_read_tools.py
5. Template: report-implementation.md
6. COMMS.md

## 3. Task and acceptance criteria
Task T-N1-1-IMPR: (1) grep single-file: if jailed root is_file(), search that one file (files_scanned=1, matches counted) — do NOT silently return 0/0; add test with path:"only.txt" that FAILS before fix (count 0 before, >0 after). (2) Walk cap: thread cap-hit signal from _walk to glob/grep payloads: truncated:true + note saying walk capped at 10000 entries, incomplete. Add test with stubbed small cap or generated tree that fails before (truncated false before). (3) While there, optionally count binary/non-UTF-8 skips (MINOR-2: :546-551) if ≤5 lines and testable — only if no scope risk; otherwise defer with note. Acceptance: both new tests fail-before/pass-after; pytest tests/coder/test_fs_read_tools.py -q passes; ruff 0; mypy 3 baseline; conventions 34.
Do NOT attempt MAJOR-3 shell PROCESS (documented), MINOR-1 char-vs-byte, NITs.
Commands: pytest tests/coder/test_fs_read_tools.py -q, pytest tests/conventions -q, ruff, mypy.

## 4. Ownership
WRITE-SET: src/paa/coder/fs_read_tools.py, tests/coder/test_fs_read_tools.py
READ-ONLY: workspace.py, ADR-0045, shell_tools.py, git_tools.py, fs_write_tools.py
FORBIDDEN: shell/git/write tools, workspace, agent, validation/*

## 5. Siblings
W1-IMPR-04 (BLOCKER-1 + MAJOR-1/2 write/git) parallel this sub-wave — disjoint files, do NOT touch theirs.

## 6. Skip list
MAJOR-3 is NOT a code fix here (Wave-2 journalling constraint, documented). MINOR/NITs deferred unless ≤5 lines with test.

## 7. Environment
Scoped pytest single-file only.

## 8. Deliverables
Report: reports/w1-review/W1-IMPR-03-improve.md. Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final ≤8 lines.

## 9. Role notes
Tests-first; prove fail-before.
