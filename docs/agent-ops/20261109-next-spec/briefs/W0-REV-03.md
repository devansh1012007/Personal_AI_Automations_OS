# Brief — W0-REV-03 (CODE-REVIEWER delta) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Delta-review the MAJOR-1/MAJOR-2 fix (commit 2f26fb8, report 21ef990) and confirm no regression, no scope creep. Why: granular-vs-helper unification can reintroduce divergence or break current corpus. Failure looks like: fix passes CI by skipping a new class or flags valid links.
Your role: code-reviewer delta (read-only on source).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.1
3. W0-IMPR-01-improve.md + W0-REV-01-review.md §4 (MAJOR-1/2 expectations)
4. templates/review-code.md
5. COMMS.md
6. Diff: git diff 1ccf3f0..2f26fb8 -- tests/conventions/test_markdown_links.py (or git show 2f26fb8)

## 3. Task and acceptance criteria
Verify: (a) malformed sentinel now fails existence (probe #1/#2), (b) fragmented allowlist now fails (probe #6), (c) corpus still 0 violations (no false positives), (d) ruff 0, mypy exactly 3, conventions 34 passed (re-run). Check intent: `_existence_defect` unification didn't change behaviour for healthy links, external URLs, dir links, etc. Check MAJOR-3 deferral is recorded (not silently dropped). Check no unrelated files edited. Run pytest tests/conventions -q, ruff, mypy.
Output: verdict APPROVE/APPROVE-WITH-CHANGES/REJECT, findings BLOCKER/MAJOR/MINOR/NIT with file:line, confirm MAJOR-1/2 closed, MAJOR-3 deferred correctly.

## 4. Ownership
WRITE-SET: docs/agent-ops/20261109-next-spec/reports/w0-review/W0-REV-03-delta.md
READ-ONLY: tests/conventions/*, docs/*
FORBIDDEN: editing code/tests

## 5. Siblings
No other reviewers this sub-wave.

## 6. Skip list
Do not re-review T-N0-1 wholesale (only delta 2f26fb8). Do not implement MAJOR-3.

## 7. Environment
Scoped pytest/ruff/mypy only.

## 8. Deliverables
Report to W0-REV-03-delta.md (ORCH-DIGEST). Report-only commit. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Reviewer notes
Blind pass on delta, then claims audit against W0-IMPR-01-improve.md.
