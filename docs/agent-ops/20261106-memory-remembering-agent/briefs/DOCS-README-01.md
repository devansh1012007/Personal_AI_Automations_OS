# Brief — DOCS-README-01 (IMPR) · /docs README update · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Bring `docs/README.md` (PAA Documentation Index) up to date with the memory-remembering-agent run (C01–C07, final reviews, SUMMARY) and the two RFC docs at `docs/` root, WITHOUT rewriting unrelated docs content.
Role: improver (docs only).
Branch/worktree: work in the current working directory on branch `ops/20261106-memory-remembering-agent` and commit to that branch. Do NOT create a new branch or worktree.

## 2. Context sources (read in FULL before editing)
1. docs/README.md (full — this is the file you own)
2. docs/agent-ops/20261106-memory-remembering-agent/SUMMARY.md (the run's outcome — index it, do not duplicate it)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3 win32, pytest 12 pre-existing)
4. docs/agent-ops/20261106-memory-remembering-agent/01-work-ledger.md (C01–C07 dispositions)
5. docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md (F-01 runbook — point the index at `docs/operations/deployment-and-runtime.md` *Memory curation*)
6. AGENTS.md (docs conventions; path:line lock — `tests/conventions/test_docs_references.py`)
7. docs/agent-ops/20261106-memory-remembering-agent/COMMS.md (read at start; do NOT append — the parallel install agent owns COMMS)

## 3. Task
1. Audit what `docs/README.md` currently indexes vs what exists under `docs/` (module docs, operations, architecture, adr/README, memory-polish plans, `docs/modules/memory.md` post-C01–C07 state, run folder `docs/agent-ops/20261106-memory-remembering-agent/`, and the two RFC docs at `docs/` root: `A Disciplined System for Computational Attention Allocation.md`, `Core Architecture Specification and Production Runtime RFC.md`).
2. Update ONLY the index/summary text of `docs/README.md`:
   - Memory section: reflect post-run state — sink/drain wired, touch on recall, curator in daemon, extractor counters, floor/deprecation, e2e proof (`tests/memory/test_e2e_remember_recall.py`), and point to SUMMARY + runbook (do not copy SUMMARY content).
   - `/docs` root RFC pair: list both with HONEST status (aspirational/vision docs, not ADR-adopted build specs; Doc A formula deviates per ADR-0012; Doc B 21 specialists empty, vault 11/13 missing) — one line each + pointer to SUMMARY §8. Do NOT claim them as normative.
   - Run folder: index `docs/agent-ops/20261106-memory-remembering-agent/SUMMARY.md` + ledger + gates (excluded from conventions lock per run convention — note it).
   - Fix any stale links/counts you find in the index (read, don't guess). Leave sub-docs' content untouched.
3. Verify: `tests/conventions/test_docs_references.py` passes (every `path:line` you add/keep must name exactly one file, line exists, non-blank, and holds claimed content — read targets, don't re-number). Ruff is N/A for markdown; run scoped pytest only.

## 4. Ownership (disjoint from parallel FINAL-INSTALL-01)
WRITE-SET (may edit ONLY these):
- docs/README.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-README-01.md (create; ORCH-DIGEST first)
READ-ONLY: everything else, ESPECIALLY `src/`, `tests/` (run-only), `docs/modules/*`, `docs/operations/*`, `docs/agent-ops/**/COMMS.md`, `reports/final/INSTALL-01.md` (parallel install agent's). FORBIDDEN: editing source/tests, deleting the RFC pair, tracking/moving the RFC pair.
Need something outside write-set? Don't edit; note as DEFERRED in your report.

## 5. Deliverables
Report at docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-README-01.md (what you indexed/fixed, conventions result with real output). Commits: docs, then report, trailers Task: DOCS-README / Agent: DOCS-README-01 / Wave: FINAL / Report: <path>. Explicit `git add -- <paths>` only, never `git add -A`. Do NOT touch COMMS.md. Final ≤8 lines.
