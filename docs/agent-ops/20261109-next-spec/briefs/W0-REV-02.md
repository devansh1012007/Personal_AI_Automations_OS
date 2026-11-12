# Brief — W0-REV-02 (CODE-REVIEWER) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Audit T-N0-2 rename (be51bba) + T-N0-5 triage (c90d835) claims and try to break them. Why: rename must be byte-identical (provenance); triage must not mislabel real bugs as env. Failure looks like: content silently edited, or TASK-2/3/4 dismissed as env (patch newline/empty PASS/AST walk are real hostile-input bugs).
Your role: code-reviewer (read-only).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.2 + N0.5 + §M Q-research
3. Reports: W0-IMPL-02-N0-2.md, N0-5-triage.md (after blind pass)
4. templates/review-code.md
5. COMMS.md
6. Diff: git show be51bba --stat; git show c90d835 --stat

## 3. Task and acceptance criteria
Verify claims: rename hash byte-identical (pre/post SHA256 + git blob), no content edit, correct slug path, VISION/ROADMAP links resolvable after move; triage verdict env vs real for 1 failed + 245 skips + 4 xfails with evidence (path:line, guard, backend rule). Red-team: attempt to find a real bug hidden as env (patch/validation bugs are real per N1.2 affinity); check TASK-1..5 filing quality (issue-ready? owner?). Run no code edits. Run scoped: git ls-files, git hash-object, focused pytest for 4 xfails (pytest ... -q -o addopts="" for nodeids) if needed. Audit tests: does triage test fail-before/pass-after logic hold? Report findings BLOCKER/MAJOR/MINOR/NIT with evidence, verdict.

## 4. Ownership
WRITE-SET: docs/agent-ops/20261109-next-spec/reports/w0-review/W0-REV-02-review.md
READ-ONLY: all source/docs/tests, research file content (hash only if measible, do not invent)
FORBIDDEN: editing code/docs, fixing triage, committing code commits

## 5. Siblings
W0-REV-01 reviews N0.1. No overlap.

## 6. Skip list
Do not implement fixes for TASK-1..5 (those are Wave 1+ tasks, reported as findings if mislabeled).

## 7. Environment
Scoped pytest single-file/nodeids only. Do NOT run full suite, docker, migrations.

## 8. Deliverables
Report to W0-REV-02-review.md (ORCH-DIGEST). Report-only commit. Branch ops/20261109-next-spec from main @ b8570a6, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Reviewer notes
Blind pass first, then claims audit against reports and code.
