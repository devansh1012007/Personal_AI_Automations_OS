# Brief — W3-IMPL-05 (IMPL) · wave 3 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan C05 (T-005, test T06): bounded extraction-quality fix — make rule-extractor drops observable (counters) and map first-person pronoun subjects. Failure looks like: facts silently lost, agent "forgot" what it was told.
Role: implementer (coder). ROOT CAUSE only; no band-aids.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, conventions; cold-lake write rule)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — item C05, T06 (D1 scope b)
5. Investigation: docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-recall-extractor-tests.md §C05 (read in FULL — silent-drop inventory with line evidence) and INV-creator-runtime.md (protocol/LLM extractor overlap)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
7. COMMS.md from last WAVE-START
8. Source to verify (don't trust): creator.py RuleBasedExtractor (lines ~270-600: pronoun/hedge/question drops, _is_noise, subjectless paths), _is_storable filter in process_signal (~919), ProcessingResult/ProcessingOutcome fields, FactExtractor protocol + LLMFactExtractor (must keep protocol-compatible), CandidateFact construction sites

## 3. Task and acceptance criteria
Task T-005 (C05):
- Add drop-counters for the silent-drop classes (questions, hedges, pronoun subjects, noise/subjectless paths identified by INV-03) and surface them in `ProcessingResult`/log (exact shape: additive fields only — do NOT break existing consumers; check API/CLI BatchReport consumers before adding fields).
- Map first-person pronoun subjects (`I`/`we` — read the actual lexicon in code; do not assume lowercase only) to entity `user` with importance ~0.3, keeping lexicon + caps (`MAX_VALUE_CHARS`, `MAX_CANDIDATES`) unchanged. Questions/hedges still dropped (but counted) — do not generate facts from questions.
- Keep `FactExtractor` protocol protocol-compatible: `LLMFactExtractor` path must keep working; existing LLM extractor tests must stay green untouched. If counters belong on the result (not the extractor), put them there.
- Docs-reference lock: creator.py shifts may break docs citations. Run `pytest tests/conventions/test_docs_references.py -q`; if broken, POST NEED listing exact docs files (docs are outside your write-set) and report DEFERRED-DEPENDENCY — do not edit docs.
Acceptance:
- T06: extractor unit tests — pronoun subject yields `user` candidate, hedges/questions yield zero candidates with counter incremented (whatever the counter API ends up being, asserted via public result/log fields, not privates where possible).
- LLM extractor tests stay green (substrings/order unchanged unless counters legitimately extend them).
- Scoped tests §7 pass; ruff clean; mypy = baseline 3 only.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/memory/creator.py
- tests/memory/test_creator.py
- tests/memory/test_llm_extractor.py
- docs/agent-ops/20261106-memory-remembering-agent/reports/w3-impl/W3-IMPL-05-T-005.md
READ-ONLY: everything else (especially src/paa/config.py, src/paa/memory/decay.py, src/paa/runtime.py, schema, docs).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Siblings (parallel, disjoint write-sets)
| Agent | Task | Owns |
| W3-IMPL-06 | T-006 | src/paa/config.py, src/paa/memory/decay.py, tests/memory/test_decay.py, docs/modules/memory.md, docs/operations/configuration-and-environment.md |
If a scoped test fails because of sibling files, wait/retry once, then FINDING/CONFLICT.

## 6. Skip list and traps
- `LLMFactExtractor` must not be re-architected; if counters live in `process_signal`'s result, LLM path benefits automatically.
- Existing caps are load-bearing (8192/200) — do not raise them.
- `_is_storable` discards candidates in `process_signal` — counting them requires care not to double-count with the extractor's own counters; document the counting design in the report.
- Pre-existing: mypy 3, pytest 12, 2 non-blocking doc stales (IMPR-02) — not yours.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/memory/test_llm_extractor.py -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (watch your own shifts)
New tests must fail without the fix; note reversal evidence.

## 8. Deliverables
Report: docs/.../reports/w3-impl/W3-IMPL-05-T-005.md (ORCH-DIGEST first). Commits: code, then report, trailers Task: T-005 / Agent: W3-IMPL-05 / Wave: 3 / Report: <path>. Final ≤8 lines.
