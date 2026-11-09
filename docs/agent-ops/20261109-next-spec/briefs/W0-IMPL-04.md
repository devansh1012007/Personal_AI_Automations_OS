# Brief — W0-IMPL-04 (IMPLEMENTER) · wave 0b · run 20261109-next-spec

## 1. Role and mission
Mission: Create scripts/status.py markdown-block generator + README markers + CI --check job. Why: README numbers are hand-typed 1,185 stale vs real 3,754 collected; ROADMAP expects script per H1.7. Failure looks like: CI flaps per host (Docker-dependent counts) or method drift (physical vs non-blank lines).
Your role: implementer (coder).

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.4 + §H/N0.4 method pin (cloc-equivalent physical src lines + pytest --collect-only -q -o addopts="" count; note INV counted 48,879 vs spec 57,300 method unstated — pin method)
3. README.md status section (hand-typed), .github/workflows/ci.yml gate, docs/ROADMAP.md:30-33, docs/ROADMAP.md:82 H1.7
4. Template report-implementation.md
5. COMMS.md

## 3. Task and acceptance criteria
Task T-N0-4 (N0.4): scripts/status.py prints markdown block (source lines, tests collected, outcome counts), README status section between <!-- STATUS:BEGIN/END --> markers, CI job runs `python scripts/status.py --check` (fail if hand-edited). Acceptance: --check fails if block hand-edited, passes after regeneration; method pinned in script docstring + plan addendum; `python scripts/status.py` output stable on same checkout; ruff 0, mypy src exactly 3 (script must be mypy-clean if in src? scripts/ is outside src mypy gate — ensure ruff passes on script); README numbers updated to real counts (not 1,185). Depends on N0.1 (README hotspot serial — you run in Wave 0b, do not parallel-edit README with W0-IMPL-01; if W0-IMPL-01 still owns README, post NEED and deliver script + ci.yml + report with exact marker diff for ORCH to apply).
Commands: python scripts/status.py --check, pytest tests/conventions -q, ruff check src tests, mypy src

## 4. Ownership
WRITE-SET: scripts/status.py (new), .github/workflows/ci.yml, README.md (status block only — coordinate; if conflict with W0-IMPL-01, defer README edit to ORCH)
READ-ONLY: src/**
FORBIDDEN: docs/VISION.md, docs/reviews/*, docs/research/*

## 5. Siblings
W0-IMPL-03 (N0.3) parallel Wave 0b; owns docs/reviews/* + VISION banner.

## 6. Traps
Do NOT implement CI-fail-as-pytest-that-flaps — use --check job. Do NOT pick count method silently — document it (physical lines via Get-Content? use python reading files). Do NOT update ROADMAP numbers beyond README block (leave ROADMAP text, note drift).

## 7. Scoped commands
python scripts/status.py, --check, pytest tests/conventions -q, ruff, mypy

## 8. Deliverables
Report: docs/agent-ops/20261109-next-spec/reports/w0-investigation/W0-IMPL-04-N0-4.md, commits code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS, explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: pin method, avoid host-flap CI.
