# Work ledger — 20261024-memory-polish   (orchestrator-owned; source of truth for resume)

## Plan coverage
| Plan item | Summary | Disposition |
|---|---|---|
| P-001 (C01) | `paa memory` CLI command group (`facts`/`search`/`signals`/`drain`/`world-model`) | T-001 → W1-IMPL-01 |
| P-002 (C02) | Memory API routes (`GET /memory/facts`, `/memory/search`, `/memory/signals`, `POST /memory/drain`) | T-002 → W1-IMPL-02 |
| P-003 (C03) | `LLMFactExtractor` in `memory/creator.py` (opt-in LLM-backed fact extraction) | T-003 → W2 |
| P-004 (C04) | `MemorySettings.extraction_model` / `extraction_model_provider` config fields | T-004 → W2 |
| P-005 (C05) | Wire extraction model into `MemoryCreator` construction in `runtime.py` | T-005 → W2 |
| P-006 (C06) | Optional LLM summarizer in `context/compaction.py` (mechanical fallback retained) | T-006 → W3 |
| P-007 (C07) | LLM-based ranking/selection in `context/gatherer.py` within token budget | T-007 → W3 |
| P-008 (C08) | CLI memory tests (`tests/cli/test_memory.py`) | T-008 → W1-IMPL-01 |
| P-009 (C09) | API memory tests (`tests/api/test_memory.py`) | T-009 → W1-IMPL-02 |
| P-010 (C10) | E2e daemon cold-lake drain test (`tests/daemon/test_drain_e2e.py`) | T-010 → W2 |
| P-011 (C11) | LLM extractor tests (`tests/memory/test_llm_extractor.py`) | T-011 → W2 |
| P-012 (C12) | Context LLM tests (`tests/context/test_llm_compaction.py`) | T-012 → W3 |

## Tasks
| Task | Plan items | Domain | Class | Root cause (1 line) | Write-set | Contract-set | Env-set | Depends on | Conflicts (C#) | Wave | Agent | Status | Code commits | Report |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T-001 | P-001, P-008 | cli | SURFACE | Memory has no CLI command group; operator cannot read/query memory from the terminal | `src/paa/cli/main.py`, `tests/cli/test_memory.py` | new `paa memory` subcommands + `--help` output | none | none | none | 1 | W1-IMPL-01 | open | | |
| T-002 | P-002, P-009 | api | SURFACE | Memory has no API routes; only `/signals` exists for ingestion; no recall/search surface | `src/paa/api/app.py`, `tests/api/test_memory.py` | `GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain` | none | none | none | 1 | W1-IMPL-02 | open | | |
| T-003 | P-003, P-011 | memory | SURFACE | `FactExtractor` seam exists but only `RuleBasedExtractor` is implemented; no LLM-backed placement | `src/paa/memory/creator.py`, `tests/memory/test_llm_extractor.py` | `FactExtractor` protocol unchanged; new `LLMFactExtractor` class | model router + OpenRouter config | P-004 | none | 2 | W2-IMPL-01 | open | | |
| T-004 | P-004 | config | SURFACE | No config field for extraction model/provider; LLM extraction cannot be configured | `src/paa/config.py` | `MemorySettings.extraction_model`, `extraction_model_provider` | none | none | none | 2 | W2-IMPL-02 | open | | |
| T-005 | P-005 | runtime | SURFACE | `MemoryCreator` constructed without an extraction model in `runtime.py` / daemon | `src/paa/runtime.py` | `MemoryCreator` constructor gains optional extract-model params | none | P-004 | none | 2 | W2-IMPL-02 | open | | |
| T-006 | P-006, P-012 | context | SURFACE | Context compaction is purely mechanical; no LLM summarizer option (docstring says deliberately so) | `src/paa/context/compaction.py`, `tests/context/test_llm_compaction.py` | `ContextCompactor.compact` gains optional `summarizer` param | none | none | none | 3 | W3-IMPL-01 | open | | |
| T-007 | P-007 | context | SURFACE | Context gathering ranks purely by confidence/importance/age; no LLM relevance ranking | `src/paa/context/gatherer.py` | gatherer ranking gains optional model-backed scoring | embedder/model | P-004 | none | 3 | W3-IMPL-01 | open | | |
| T-010 | P-010 | daemon/e2e | SURFACE | No end-to-end drain test: signal → queue → creator → hot_serving_active_facts never proven live | `tests/daemon/test_drain_e2e.py` | none | daemon + queue + creator | P-002 | none | 2 | W2-IMPL-03 | open | | |

## Waves
### Wave 1 — surface expose (CLI + API)
- WAVE_BASE_SHA: `1c75a9e`
- Agents: `W1-IMPL-01` (cli + cli tests), `W1-IMPL-02` (api + api tests)
- Ownership file: `ownership/W1.tsv`
- Pre-mortem: Two agents edit different files (`cli/main.py` vs `api/app.py`) and different test dirs; no shared symbols. `api/app.py` imports `_signal_repo` which builds a `SignalRepository` — new `/memory/*` routes reuse `FactRepository` directly, no shared mutable state. Risk: test file dirs `tests/cli/` and `tests/api/` may not exist yet; agents must create them (new files are fine, disjoint).
- Gate results: pending
- Review findings → dispositions: pending
- Review/improve cycles used: 0/2
- Status: READY TO SPAWN

## Deep dives
| Task | Agent | Design approved by | Report | Reviewers |
|---|---|---|---|---|

## Deferred / open items
None at preflight.

## Out-of-plan discoveries
None at preflight.

## Temporary mitigations (user-approved)
None.

## Assumptions → see 00-mission.md (A-001…A-004)
