# Brief — W0-IMPL-03 (IMPLEMENTER) · wave 0b · run 20261109-next-spec

## 1. Role and mission
Mission: Review composability appendix (docs/VISION.md 401-963) vs arXiv 2608.25512, write per-claim review note, add UNVERIFIED banner. Why: 565 lines of unreviewed design claims cite a paper dated 2026 (future/post-cutoff). Failure looks like: unverified claims treated as support, or silent trim without verdicts.
Your role: implementer (coder) — docs only.

## 2. Required reading
1. STANDING-ORDERS (binding)
2. 00-mission.md, plan N0.3 (owner: keep + mark UNVERIFIED)
3. docs/VISION.md (post-N0.1 move), docs/reviews/ dir, docs/ROADMAP.md §Ideas imported (for context, not as substitute)
4. Template report-implementation.md
5. COMMS.md

## 3. Task and acceptance criteria
Task T-N0-3 (N0.3): Try to fetch/paper-verify arXiv 2608.25512 (webfetch if needed; if unavailable, mark UNVERIFIABLE and say why). Write docs/reviews/2026-11-09-composability-review.md listing each appendix claim as supported/unsupported/unverifiable with appendix line refs. Add banner at top of appendix in docs/VISION.md: "UNVERIFIED — paper not fetched or dated 2026, claims subject to review" (exact wording per plan). Acceptance: review note exists with per-claim table; banner present; pytest tests/conventions -q green; docs link test from W0-IMPL-01 still green. Do NOT trim appendix.
Commands: pytest tests/conventions -q, ruff/mypy baseline unaffected (docs only).

## 4. Ownership
WRITE-SET: docs/reviews/2026-11-09-composability-review.md, docs/VISION.md (banner only, small append/edit — coordinate via COMMS CONTRACT if W0-IMPL-01 still owns file; if conflict, do banner as deferred 1-liner and report)
READ-ONLY: src/**
FORBIDDEN: scripts/status.py, ci.yml, docs/research/*

## 5. Siblings
W0-IMPL-04 (N0.4) runs parallel in Wave 0b; owns scripts/status.py + ci.yml (not VISION).

## 6. Traps
Do NOT assert support without fetching paper. If paper unfetchable, say UNVERIFIABLE, do not invent summary. If banner placement would break link anchors, use NEED and defer.

## 7. Scoped commands
pytest tests/conventions -q only.

## 8. Deliverables
Report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/W0-IMPL-03-N0-3.md, commits code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS, explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: docs only, no code.
