# Brief — W0-REV-01 (CODE-REVIEWER) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Independently audit T-N0-1 (VISION move + link lock, commit d22f0d4) and try to break it. Why: new docs lock raises CI bar; slug algorithm or allowlist errors would silently pass broken links or fail sanctioned ADR rows. Failure looks like: broken anchor reported as OK, or valid ADR link flagged.
Your role: code-reviewer (read-only on source, write only your report).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N0.1
3. W0-IMPL-01 report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/W0-IMPL-01-N0-1.md (read after blind pass)
4. templates/review-code.md
5. COMMS.md from WAVE-START
6. Diff: git diff b8570a6..d22f0d4 -- docs/README.md docs/ROADMAP.md tests/conventions/test_markdown_links.py; git show d22f0d4 --stat

## 3. Task and acceptance criteria
Audit T-N0-1 diff + surrounding code. Check: bugs, regressions, flawed assumptions (slug algorithm vs GitHub, anchor dedup, path resolution doc-relative vs root, allowlist correctness for 15 ADR rows, synthetic self-test vacuity), integration (test_docs_references still green, docs/README link correctness), security (no network/subprocess writes in test), edge cases, conventions. Red-team: construct adversarial inputs (malformed links, heading slugs with punctuation, dir links, missing file, allowlisted name with fragment, external URLs, code-span links) that make lock fail to detect or crash; attempt to write failing tests if possible. Audit tests (fail without fix? pass for wrong reason?). Run ruff/mypy/conventions yourself, report verbatim.
Output: findings BLOCKER/MAJOR/MINOR/NIT with file:line evidence, suggested fix. Digest verdict APPROVE/APPROVE-WITH-CHANGES/REJECT.

## 4. Ownership
WRITE-SET: docs/agent-ops/20261109-next-spec/reports/w0-review/W0-REV-01-review.md (report only)
READ-ONLY: all source/docs/tests
FORBIDDEN: editing code, editing tests/conventions/*, committing code commits

## 5. Siblings
W0-REV-02 (separate agent) reviews T-N0-2+T-N0-5. You cannot see their uncommitted work.

## 6. Skip list
Do not review waves 1+ (not implemented). Do not edit N0.1 files.

## 7. Environment
Scoped: pytest tests/conventions -q, ruff, mypy allowed. Do NOT run full pytest, docker restarts.

## 8. Deliverables
Report to W0-REV-01-review.md (ORCH-DIGEST first). Report-only commit with trailers to ops/20261109-next-spec (docs-only). Branch ops/20261109-next-spec from main @ b8570a6, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit git paths only.
Final message ≤8 lines: STATUS | reviewer | scope | report path | verdict | blockers

## 9. Reviewer notes
Blind pass first, then claims audit. Diff: git diff b8570a6..d22f0d4. Implementer report path above.
