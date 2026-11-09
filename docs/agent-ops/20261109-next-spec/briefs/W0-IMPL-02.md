# Brief — W0-IMPL-02 (IMPLEMENTER) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Rename research note from spaces filename to spec'd slug and commit it so N0.1 link test passes. Why: N0.1 links point at docs/research/2026-11-deepseek-super-agent.md which does not exist; on disk is "deep seek super agent.md" (spaces, 2026-11 name differs). Failure looks like: ROADMAP:4 + VISION:11,76 stay red, or you author content and fabricate provenance.
Your role: implementer (coder).

## 2. Required reading (in this order)
1. docs/agent-ops/20261109-next-spec/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md, docs/agent-ops/20261109-next-spec/00-mission.md
3. Plan: docs/plans/2026-11-09-next-spec-revised.md — items N0.0, N0.2, §M Q-research (owner: rename+commit)
4. Template: docs/agent-ops/20261109-next-spec/templates/report-implementation.md
5. COMMS.md: docs/agent-ops/20261109-next-spec/COMMS.md
6. Check on disk: docs/research/deep seek super agent.md (spaces) existence; git ls-files docs/research/ (untracked)

## 3. Task and acceptance criteria
Task T-N0-2 (N0.2): Rename `docs/research/deep seek super agent.md` → `docs/research/2026-11-deepseek-super-agent.md` (filesystem rename, no content edit), `git add` + commit to ops branch. Acceptance: `ls docs/research/2026-11-deepseek-super-agent.md` exists; path is exactly spec'd slug; link test (W0-IMPL-01) can resolve ROADMAP:4 + VISION:11,76 after VISION move (coordinate via COMMS CONTRACT after commit); content byte-identical to pre-rename (verify via hash or git diff --no-index if needed). Do NOT touch VISION/README/ROADMAP links (sibling W0-IMPL-01 owns those).
Commands that must pass (scoped): `git ls-files docs/research/` shows new path after add, `git status --porcelain` clean for that path after commit.

## 4. Ownership
WRITE-SET: docs/research/2026-11-deepseek-super-agent.md (new), docs/research/deep seek super agent.md (delete via rename)
READ-ONLY: docs/README.md, docs/ROADMAP.md, VISION.md, tests/conventions/*
FORBIDDEN: docs/VISION.md, README.md, scripts/status.py, docs/reviews/*
Need outside? Post NEED, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings
| W0-IMPL-01 | N0.1 | docs/VISION.md, docs/README.md, docs/ROADMAP.md, README.md, tests/conventions/test_markdown_links.py | link-target semantics |
| W0-IMPL-05 | N0.5 | docs/agent-ops/.../reports/w0-investigation/N0-5-triage.md | none |

## 6. Skip list and known traps
Do NOT author research content, do NOT "tidy" to new text. If file does not exist at spaces path, ESCALATE with ls output (possible pre-existing state difference). If path is already renamed by ORCH N0.0, verify and report ALREADY-FIXED.

## 7. Environment and tests
Scoped commands: git ls-files, git add -- path, git status --porcelain. Do NOT run full pytest, docker, etc.

## 8. Deliverables
Report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/W0-IMPL-02-N0-2.md
Commits: code first (research rename), then report. Trailers Task/Agent/Wave/Report. Branch ops/20261109-next-spec from main @ b8570a6, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit git paths only.
Final message ≤8 lines: STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes
Implementer: verify rename preserves bytes.
