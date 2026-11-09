# Brief — W0-REV-05 (CODE-REVIEWER) · wave 0b · run 20261109-next-spec

## 1. Role and mission
Mission: Audit T-N0-4 status generator (c09b0f1). Why: hand-typed numbers (1,185) stale vs real 3,754 collected; CI must fail if block hand-edited without flapping per host. Failure looks like: method drift (physical vs non-blank), --check vacuous, CI job flaps.
Your role: code-reviewer (read-only).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.4 + §H method pin + reviewer M-6 (CI-fail flap)
3. W0-IMPL-04 report: reports/w0-investigation/W0-IMPL-04-N0-4.md (after blind pass)
4. templates/review-code.md
5. COMMS.md
6. Diff: git show c09b0f1 --stat; scripts/status.py, README.md markers, .github/workflows/ci.yml status job

## 3. Task and acceptance criteria
Verify: (a) method pinned and matches plan (physical src lines + collect-only collected; agent reports 57,326 lines + 3,762 collected — note INV counted 48,879 physical earlier; method drift? Check script's counting code); (b) --check fails on hand-edit, passes after regen (run it); (c) outcome counts excluded deviation is justified (M-6 host-flap) or a gap (plan said outcome counts in block — agent excluded); (d) README numbers updated from 1,185 (not stale), ROADMAP drift left untouched per brief is acceptable; (e) CI job uses --check not flaky pytest. Red-team: run --check, hand-edit a copy, verify failure; check script handles no Docker env (counts vary). Run pytest tests/conventions -q, ruff, mypy, and scripts/status.py --check.
Report BLOCKER/MAJOR/MINOR/NIT, verdict.

## 4. Ownership
WRITE-SET: reports/w0-review/W0-REV-05-review.md
READ-ONLY: scripts/status.py, README.md, ci.yml, all source
FORBIDDEN: editing code/tests/ci

## 5. Siblings
W0-REV-04 reviews N0.3.

## 6. Skip list
Do not implement fixes. Do not re-run full pytest suite.

## 7. Environment
Scoped: python scripts/status.py --check, pytest tests/conventions -q, ruff, mypy.

## 8. Deliverables
Report to W0-REV-05-review.md. Report-only commit. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Reviewer notes
Blind pass on diff, then claims audit, then --check probes.
