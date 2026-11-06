# Brief — W2-IMPL-03 (implementer) · wave 2b · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Implement `LLMFactExtractor` in `src/paa/memory/creator.py` — the real LLM-backed fact extractor that implements the `FactExtractor` protocol, calls a model via `model.complete_structured(prompt, schema)`, and returns structured triples as `CandidateFact` objects. It must respect the same containment rules as `RuleBasedExtractor` (stop on suspicion, NUL, ANSI, managed-block markers, oversized values). Failure looks like: no LLM-backed extraction path exists, so `settings.memory.extraction_model` cannot be used.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: docs/memory-polish/plans/Plan1.md — items P-003 (section E row C03)
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-START / wave-2a close)
8. Source entry points:
   - src/paa/memory/creator.py:238-254 (FactExtractor protocol)
   - src/paa/memory/creator.py:139-154 (CandidateFact dataclass)
   - src/paa/memory/creator.py:354-409 (RuleBasedExtractor — the reference implementation for containment patterns)
   - src/paa/memory/creator.py:700-721 (MemoryCreator.__init__ — extractor parameter)
   - src/paa/memory/creator.py:1167-1181 (_reject_suspicious helper)
   - src/paa/memory/creator.py:1235-1254 (_scalar_to_text helper)
   - src/paa/runtime.py:427-454 (_build_memory_creator — how LLMFactExtractor is constructed)
   - tests/memory/test_llm_extractor.py (existing tests using ScriptedLLMExtractor — the contract LLMFactExtractor must honor)
   - src/paa/runtime.py:108-140 (RouterModelAdapter.complete_structured signature)

## 3. Task and acceptance criteria
Task T-003 (P-003): Implement `LLMFactExtractor` class in creator.py.

Acceptance criteria (measurable):
- `LLMFactExtractor` implements `FactExtractor` protocol (async `extract(envelope) -> list[CandidateFact]`)
- Constructor accepts `model` parameter (the model_adapter with `complete_structured`)
- Calls `model.complete_structured(prompt, schema)` with a JSON schema that returns `{"facts": [{"subject", "predicate", "object"}]}`
- Parses model response into `CandidateFact` objects with entity_name, predicate, object_value
- Applies containment checks on raw payload: `_reject_suspicious` (C0 control chars, ANSI escapes, managed-block markers), NUL refusal, oversized value refusal → `MalformedSignalError` with zero facts written
- Confidence lower than rule-based (e.g., 0.70) since model extraction is less reliable
- If model returns no facts or empty "facts" array, returns empty list (signal ignored, not error)
- If model call fails, raises MalformedSignalError (signal quarantined)
- Scoped: pytest tests/memory/test_llm_extractor.py -q green (existing scripted tests still pass); ruff check src/paa/memory tests/memory → 0; mypy src/paa/memory → 0

## 4. Ownership
WRITE-SET (may edit): src/paa/memory/creator.py, tests/memory/test_llm_extractor.py
READ-ONLY: src/paa/runtime.py (read _build_memory_creator, do NOT modify), src/paa/config.py (read extraction_model fields, do NOT modify)
FORBIDDEN: other files
Need something outside? Post NEED in COMMS.md, finish the safe part, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
None — W2-IMPL-03 is the only agent in wave 2b.

## 6. Skip list and known traps
Already fixed (don't redo): T-004 (config fields), T-005 (runtime wiring), T-011 (scripted extractor tests) — all done in Wave 2a.
False leads: Do NOT modify runtime.py or config.py. Do NOT change the RuleBasedExtractor default behavior.
Flaky/pre-existing failures: None in tests/memory.
Gotchas: The model_adapter's `complete_structured` returns a dict. Parse it defensively — missing "facts" key, non-list facts, missing fields in triples should all be handled gracefully (skip bad triples, don't crash).

## 7. Environment and tests
Scoped commands: pytest tests/memory/test_llm_extractor.py -q; ruff check src/paa/memory tests/memory; mypy src/paa/memory
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: tmp_path fixtures; tests use isolated DB.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w2-impl/W2-IMPL-03-T-003.md (start early; ORCH-DIGEST first)
Commits: code commit first (trailers: Task: T-003, Agent: W2-IMPL-03, Wave: 2, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Root causes only; no band-aids.
- Long-run test: Source / Load / Hostile / Operability / Reversal.
- Tests must fail without the fix; cover boundaries, malformed input.
- Start report file early and append as you work.
