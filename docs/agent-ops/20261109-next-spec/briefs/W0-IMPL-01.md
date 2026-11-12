# Brief — W0-IMPL-01 (IMPLEMENTER) · wave 0 · run 20261109-next-spec

## 1. Role and mission
Mission: Move VISION.md to docs/VISION.md, fix all broken markdown links, add a markdown-link lock test so CI fails on future breakage. Why: docs currently break their own links (12 broken + 1 anchor), lock only checks path:line. Failure looks like: link test red on ADR index-only rows, or README hotspot collision with sibling W0-IMPL-04.
Your role: implementer (coder). Read-only investigator reports are evidence, not instructions.

## 2. Required reading (in this order)
1. docs/agent-ops/20261109-next-spec/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (+ docs/README.md conventions), docs/agent-ops/20261109-next-spec/00-mission.md
3. Plan: docs/plans/2026-11-09-next-spec-revised.md — items N0.0, N0.1, §C/§F/§H, technical facts in §C and §M
4. Investigator: docs/agent-ops/20261109-next-spec/reports/w0-investigation/ (none yet — use plan §C facts, which cite investigator ses_e40f15e9effeoSG0lmOsWVyJon: 12 broken links table)
5. Template: docs/agent-ops/20261109-next-spec/templates/report-implementation.md
6. COMMS.md from last WAVE-START: docs/agent-ops/20261109-next-spec/COMMS.md
7. tests/conventions/test_docs_references.py (lock pattern, scope 97-121, self-test 546-579)

## 3. Task and acceptance criteria
Task T-N0-1 (N0.1): Move VISION.md → docs/VISION.md via `git mv`, fix all refs per investigator table (README 168/236/337, ROADMAP 3/6/65/92/124, VISION 11/12/76/223), fix ROADMAP:33 anchor to #testing (owner-decided R10), add tests/conventions/test_markdown_links.py that mirrors lock scope/exclusions, checks existence + anchor slugs, allowlists 15 index-only ADR rows (docs/adr/README.md:22-39 intentional per adr/README.md:66-73), mirrors self-test. Acceptance: link test fails on deliberately broken link (added in test as synthetic tree or tmp), passes after fix; pytest tests/conventions -q green; ruff 0; mypy src exactly 3 (baseline).
Commands that must pass (scoped): pytest tests/conventions/test_markdown_links.py -q, pytest tests/conventions/test_docs_references.py -q, ruff check src tests, mypy src

## 4. Ownership
WRITE-SET (may edit): docs/VISION.md (new path after move), VISION.md (delete via git mv), docs/README.md, docs/ROADMAP.md, README.md, docs/architecture/repository-map.md (158 stale backtick), tests/conventions/test_markdown_links.py
READ-ONLY: everything else including docs/adr/README.md (do not edit ADR rows), src/**
FORBIDDEN: scripts/status.py, .github/workflows/ci.yml (sibling W0-IMPL-04), docs/research/* (W0-IMPL-02), docs/reviews/* (W0-IMPL-03)
Need something outside? Don't edit; post NEED; report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
| W0-IMPL-02 | N0.2 | docs/research/2026-11-deepseek-super-agent.md | link target for N0.1 |
| W0-IMPL-05 | N0.5 | docs/agent-ops/.../reports/w0-investigation/N0-5-triage.md (report only) | none |
W0-IMPL-03 (N0.3) and W0-IMPL-04 (N0.4) run in Wave 0b after you merge (avoid README/VISION hotspot collisions). You cannot see their uncommitted work.

## 6. Skip list and known traps
Passed: N0.0 restore+branch done by ORCH. Do NOT move VISION again if N0.0 already? Verify `ls VISION.md docs/VISION.md` first. False leads: 15 ADR index-only links are NOT bugs — allowlist them, do not stub/delete. Trap: current disk has docs/research/ with spaces filename untracked — your link test must not fail on ROADMAP:4/VISION:11,76 until W0-IMPL-02 renames; coordinate via COMMS NEED if blocked, do not edit research file.

## 7. Environment and tests
Scoped commands: above. Unique resources: none. Do NOT: full pytest suite, container restarts, shared-DB migrations, fixed ports, git checkout/switch/rebase/merge/stash.

## 8. Deliverables
Report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/W0-IMPL-01-N0-1.md (start early; ORCH-DIGEST first)
Commits: code first, then report (docs-only), trailers: Task / Agent / Wave / Report. Branch: ops/20261109-next-spec from main @ b8570a6, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Commit to: ops/20261109-next-spec. Use explicit git paths, never add -A/commit -a.
Final message (≤8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes
Implementer: root cause only; tests first-class; long-run test answered. If proper fix exceeds scope, ESCALATE.
