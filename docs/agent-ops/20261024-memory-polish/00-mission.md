# Mission — 20261024-memory-polish

## Scope
Execute Plan 1 (`docs/memory-polish/plans/Plan1.md`) on branch `memory-polish`:
expose the memory system (CLI + API routes), add an opt-in LLM-backed fact
extractor (OpenRouter via `PAA_MODELS__*`), wire LLM-based context building
with mechanical fallback, and add the e2e daemon cold-lake drain test.

## Baseline (captured 2026-10-24, before any Wave-1 code change)
- `git rev-parse HEAD`: 1c75a9e (`docs(plan1): fix wrong citation in edge-case table`)
- Branch: `memory-polish` (based on `main` at `4ac96db`)
- `ruff check src tests`: 0 findings
- `mypy src`: 0 errors, 138 source files
- `pytest tests/ -o addopts="--strict-markers --strict-config" -q`: 2907 passed / 235 skipped / 4 xfailed
- Gate command order (per AGENTS.md): ruff → mypy → pytest

## Gate commands
- `.venv/bin/python -m ruff check src tests`
- `.venv/bin/python -m mypy src`
- `.venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q`

## Hotspots (single-writer files, section 12 of the wave skill)
| File | Why it is a hotspot |
|---|---|
| `src/paa/cli/main.py` | Typer command registry; only one agent may add the `memory` group |
| `src/paa/api/app.py` | FastAPI route registry; only one agent may add `/memory/*` routes |
| `src/paa/memory/creator.py` | FactExtractor seam; only one agent may add `LLMFactExtractor` |
| `src/paa/config.py` | Settings model; only one agent may add `extraction_model` fields |
| `src/paa/runtime.py` | Composition root; only one agent may wire the extraction model |
| `src/paa/context/compaction.py` | Mechanical compactor; only one agent may add LLM summarizer option |
| `src/paa/context/gatherer.py` | Context builder; only one agent may add LLM ranking |

## Assumptions (A-nnn)
- A-001: All Wave-1 tasks are `SURFACE` (local, one domain, no contract change) except P-003/P-005 which touch shared seams but are still single-writer.
- A-002: OpenRouter key handling follows the existing router secrecy contract (no new secrets).
- A-003: Rule-based extractor remains the default; LLM extraction is opt-in via config.
- A-004: No schema/persistence change; no migration needed.

## Out of scope (explicitly deferred)
- `paa memory` TUI/dashboard (VISION Phase 3)
- Multi-session isolation (ROADMAP 2.6)
- Self-improvement loops (ROADMAP 2.5)

## Isolation mode
`shared-tree` — all agents in one working tree with disjoint write-sets enforced by `ownership/W<N>.tsv` + `uniq -d` check.

## Pre-existing uncommitted changes
None (tree clean at HEAD `1c75a9e`).
