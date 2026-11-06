# Brief — W3-IMPR-01 (IMPR) · wave 3 improvement · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Close Wave 3 review findings + fix the two gate regressions (orphan guard, docs citations). Improve code AND tests.
Role: improver.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, testing conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — C05, C06
5. Implementer reports: reports/w3-impl/W3-IMPL-05-T-005.md, W3-IMPL-06-T-006.md
6. Reviews in FULL: reports/w3-review/REV-code.md, TREV-tests.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-improvement.md
8. COMMS.md from last WAVE-START + gates/gate-w3-1.md

## 3. Findings to disposition (address EVERY one)

### Gate regressions (already triaged — FIX)
- R-W3-REG1 [BLOCKER-class] Wave 2 orphan guard (curator.py empty-index skip) breaks `test_curator.TestOrphans::test_orphan_sweep_removes_danglers` (legit empty-entity case). FIX root cause: refine the guard so genuinely-empty entity index with dangling facts still sweeps, while the catastrophic case the reviewer feared is prevented. Read the guard + test + reviewer reasoning (Wave 2 REV R-W2-004) before choosing — do not delete the guard, do not relax the test. Record the reasoning.
- R-W3-DOCS [MAJOR] 8 broken citations (7 × config.py shifts, 1 × Plan1.md creator.py:799-830). FIX all 8 (read each, verify). Include both docs files in your write-set below. If you shift lines further, re-run conventions until green.

### CODE (R-W3-001–007)
- R-W3-001 [MINOR] DeprecationWarning attributed to pydantic/main.py → silent everywhere → FIX: correct stacklevel so it attributes to paa code (and matches the `paa.*` error filter — verify with a test that asserts the warning fires for explicit decay_lambda and is not swallowed). Do NOT use `# noqa`/`filterwarnings` silencing.
- R-W3-002 [MINOR] Injected-curator settings floor never reaches sweep → FIX: thread explicit floor (or the settings-derived floor) into `MemoryCurator`'s sweeper so injected settings behave like global settings. If this needs a curator.py change, it is in your write-set (add it).
- R-W3-003 [MINOR] LLM path still creates I/we entities → FIX: apply first-person mapping on the LLM extractor path too (or document why not, with evidence — do not leave inconsistency silent).
- R-W3-004 [MINOR] Concurrent drain counter interleaving → FIX: make drop counters atomic / per-call local (or thread-safe), or document + test the race. At minimum do not share mutable counters across concurrent calls.
- R-W3-005 [MINOR] Drop counts DEBUG-only → FIX (smallest honest change): include drop counts in the `signal_processed` log event (or equivalent info-level observable). Keep BatchReport.summary() shape unless API consumer check shows additive is safe.
- R-W3-006 [NIT] NaN floor unpinned → FIX with 1 test.
- R-W3-007 [NIT] `user` entity merge semantics → FIX by documenting + 1 test pinning max-importance merge (or choose a distinct entity only if trivially safe — prefer document+pin).

### TESTS (TREV M1–M4 + N1–N2 + P1 sketches in TREV §6)
- M1: valid-LLM-input → PROCESSED + total==0 test (mutation survivor (c) must die).
- M2: key-value + `_walk` path tests for first-person mapping.
- M3: exact-set assert for `BatchReport.summary()` keys (update test to fail on additive keys; if additive is intentionally allowed, assert that explicitly and say why — do not leave the digest claim false).
- M4: `monkeypatch.delenv(..., raising=False)` in unset-env test.
- N1: contraction variant pinning test ("I'm" unmapped — pin current behavior).
- P1 (§6): implement all P1; defer P2/P3 with reasons.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/memory/creator.py
- src/paa/config.py
- src/paa/memory/decay.py
- src/paa/memory/curator.py
- tests/memory/test_creator.py
- tests/memory/test_llm_extractor.py
- tests/memory/test_decay.py
- tests/memory/test_curator.py
- docs/architecture/overview.md
- docs/data/persistence-and-storage.md
- docs/development/developer-workflow.md
- docs/interfaces/integrations-and-communication.md
- docs/modules/cli.md
- docs/runtime/background-jobs-and-events.md
- docs/memory-polish/plans/Plan1.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/w3-improve/IMPR-01.md
READ-ONLY: everything else (including docs/modules/memory.md, docs/operations/configuration-and-environment.md — do NOT edit those; if needed, POST NEED).
FORBIDDEN: hotspot schema, pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/ -q`
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
New/strengthened tests must fail without the corresponding fix; note reversal evidence.

## 6. Deliverables
Improvement report at docs/agent-ops/20261106-memory-remembering-agent/reports/w3-improve/IMPR-01.md (ORCH-DIGEST first; finding-by-finding FIXED/REJECTED/DEFERRED + evidence + commit).
Commits: code, then docs/report, trailers Task: T-005/T-006 / Agent: W3-IMPR-01 / Wave: 3 / Report: <path>. Final ≤8 lines.
