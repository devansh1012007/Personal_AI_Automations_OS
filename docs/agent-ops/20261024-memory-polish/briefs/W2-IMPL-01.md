# Brief — W2-IMPL-01 (implementer) · wave 2a · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Add extraction_model / extraction_model_provider config fields to MemorySettings so the LLM-backed fact extractor can be configured via PAA_MODELS__*. Also create the test file for the LLM extractor (tests/memory/test_llm_extractor.py) proving triples→facts and malformed→quarantine. Failure looks like: no way to configure a small LLM model for fact extraction, and no tests proving the LLM extractor respects containment.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: docs/memory-polish/plans/Plan1.md — items P-004 and P-011, section E rows C04/C11
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table, wave 2)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-START / wave-1 close)
8. Source entry points:
   - src/paa/config.py around MemorySettings (line ~531 area) for where to add fields
   - src/paa/memory/creator.py FactExtractor protocol (~line 238) and RuleBasedExtractor (~line 354) for the seam shape
   - tests/memory/test_creator.py for test conventions in the memory suite
   - src/paa/models/__init__.py for get_embedder / model router patterns (config-driven model selection)

## 3. Task and acceptance criteria
Task T-004 (P-004): Add MemorySettings.extraction_model: str and extraction_model_provider: str fields.

Acceptance criteria (measurable):
- MemorySettings has extraction_model (default "") and extraction_model_provider (default "")
- PAA_MODELS__EXTRACTION_MODEL and PAA_MODELS__EXTRACTION_PROVIDER env vars map to these fields
- Default "" means rule-based extractor (unchanged behavior)
- Scoped: pytest tests/memory/test_llm_extractor.py -q green; ruff check src/paa/config tests/memory → 0; mypy src/paa/config → 0

Task T-011 (P-011): Create tests/memory/test_llm_extractor.py with scripted model tests.

Acceptance criteria (measurable):
- Scripted model returning structured triples → facts written to hot_serving_active_facts, signal marked processed
- Scripted model returning malformed/invalid → MalformedSignalError, signal quarantined, zero facts written
- Containment: NUL byte, oversized value, suspicious pattern → zero facts, signal malformed
- One transaction per signal: crash mid-signal leaves processing + zero facts
- Each test FAILS without the LLMFactExtractor implementation and PASSES with it

## 4. Ownership
WRITE-SET (may edit): src/paa/config.py, tests/memory/test_llm_extractor.py
READ-ONLY: src/paa/memory/creator.py (read FactExtractor protocol shape, do NOT modify), src/paa/models/__init__.py (read patterns)
FORBIDDEN: src/paa/runtime.py (W2-IMPL-02's file), src/paa/daemon.py, other memory files
Need something outside? Post NEED in COMMS.md, finish the safe part, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W2-IMPL-02 | T-005 (P-005) + T-010 (P-010) | src/paa/runtime.py, tests/daemon/test_drain_e2e.py | MemoryCreator construction wiring; e2e drain test |

Known interactions: W2-IMPL-02 needs the extraction_model fields (from your T-004) before wiring MemoryCreator in runtime.py. You go first; they wait on your config fields. You cannot see their uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — new surface.
False leads: Do NOT modify creator.py to add LLMFactExtractor (that's T-003, next wave). Do NOT modify runtime.py (W2-IMPL-02).
Flaky/pre-existing failures: None in tests/memory.
Gotchas: MemorySettings is a pydantic BaseSettings subclass; fields with PAA_MODELS__ prefix. Follow existing embedding_model / allow_hash_embedder_fallback pattern at config.py:531-537.

## 7. Environment and tests
Scoped commands: pytest tests/memory/test_llm_extractor.py -q; ruff check src/paa/config tests/memory; mypy src/paa/config
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: tmp_path fixtures; tests write to temp DB.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w2-impl/W2-IMPL-01-T-004.md (start early; ORCH-DIGEST first)
Commits: code commit first (trailers: Task: T-004, Agent: W2-IMPL-01, Wave: 2, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Root causes only; no band-aids.
- Long-run test: Source / Load / Hostile / Operability / Reversal.
- Tests must fail without the fix.
- Start report file early and append as you work.
