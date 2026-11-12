# Brief — W0-REV-04 (CODE-REVIEWER) · wave 0b · run 20261109-next-spec

## 1. Role and mission
Mission: Audit T-N0-3 (e9e5bbc) composability review vs arXiv 2608.25512. Why: 565-line appendix cites a future paper; owner said keep + mark UNVERIFIED. Failure looks like: unsupported claim marked supported, or abstract-level check presented as full-PDF verification.
Your role: code-reviewer (read-only).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.3 + §M Q-appendix (owner: keep + UNVERIFIED)
3. W0-IMPL-03 report: reports/w0-investigation/W0-IMPL-03-N0-3.md (after blind pass)
4. templates/review-code.md
5. COMMS.md
6. Diff: git show e9e5bbc --stat; docs/VISION.md:402 banner; docs/reviews/2026-11-09-composability-review.md per-claim table

## 3. Task and acceptance criteria
Verify: (a) paper actually fetched and says what appendix attributes (abstract vs body — agent says abstract supports C1–C7, C8–C14 extrapolation unverifiable, no unsupported); (b) banner wording exact, placement correct, link lock green; (c) no trim, appendix preserved; (d) caveat recorded (PDF body not line-checked) follows up correctly. Red-team: can you find a claim marked supported that paper doesn't support? Run pytest tests/conventions -q, ruff/mypy scoped (expect baseline). Report BLOCKER/MAJOR/MINOR/NIT with file:line, verdict.

## 4. Ownership
WRITE-SET: reports/w0-review/W0-REV-04-review.md
READ-ONLY: docs/VISION.md, docs/reviews/*, all source
FORBIDDEN: editing code/docs, fixing findings

## 5. Siblings
W0-REV-05 reviews N0.4. No overlap.

## 6. Skip list
Do not re-review N0.1 move. Do not author new review note.

## 7. Environment
Scoped pytest/ruff/mypy only.

## 8. Deliverables
Report to W0-REV-04-review.md (ORCH-DIGEST). Report-only commit. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Reviewer notes
Blind pass on diff, then claims audit.
