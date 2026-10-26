# Brief — W2-IMPL-02 (implementer) · wave 2a · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Wire the extraction model into MemoryCreator construction in runtime.py, and create the e2e daemon cold-lake drain test proving signal → queue → creator → hot_serving_active_facts works end-to-end. Failure looks like: no live drain path test, and the extraction model config never reaches MemoryCreator.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: docs/memory-polish/plans/Plan1.md — items P-005 and P-010, section E rows C05/C10
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table, wave 2)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-START / wave-1 close)
8. Source entry points:
   - src/paa/runtime.py around _build_agents / MemoryCreator construction (~line 316-324, 460-490)
   - src/paa/daemon.py _memory_creator (~line 170-181) and _ingestion_loop (~line 142-152)
   - src/paa/memory/creator.py MemoryCreator constructor (~line 700)
   - tests/ directory conventions (tests/daemon/ may not exist yet — create it)

## 3. Task and acceptance criteria
Task T-005 (P-005): Wire extraction model into MemoryCreator construction.

Acceptance criteria (measurable):
- MemoryCreator is constructed with extraction model/provider when config fields are non-empty
- When config fields are empty (default), MemoryCreator uses RuleBasedExtractor (unchanged behavior)
- NEED posted to W2-IMPL-01 if config fields not yet available
- Scoped: pytest tests/daemon/test_drain_e2e.py -q green; ruff check src/paa/runtime tests/daemon → 0; mypy src/paa/runtime → 0

Task T-010 (P-010): Create tests/daemon/test_drain_e2e.py.

Acceptance criteria (measurable):
- Start Daemon, POST a signal via /signals (or directly insert cold_lake_signals row), wait for drain
- Assert hot_serving_active_facts row appears with source_signal_id pointing to the signal
- Assert signal sync_status transitions to 'processed'
- Test FAILS without the daemon ingestion loop working and PASSES with it
- Marks NOT-RUN if no shared DB available (document in report)

## 4. Ownership
WRITE-SET (may edit): src/paa/runtime.py, tests/daemon/test_drain_e2e.py
READ-ONLY: src/paa/config.py (W2-IMPL-01's file — read for extraction_model fields, do NOT modify), src/paa/memory/creator.py (read constructor), src/paa/daemon.py (read loops)
FORBIDDEN: src/paa/config.py, src/paa/memory/creator.py (modification)
Need something outside? Post NEED in COMMS.md. If extraction_model fields not yet in config.py, post NEED and finish the drain test first.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W2-IMPL-01 | T-004 (P-004) + T-011 (P-011) | src/paa/config.py, tests/memory/test_llm_extractor.py | Config fields; LLM extractor tests |

Known interactions: You need W2-IMPL-01's extraction_model config fields before wiring MemoryCreator. If not available, post NEED and proceed with drain test only. You cannot see their uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — new surface.
False leads: Do NOT modify config.py (W2-IMPL-01). Do NOT add LLMFactExtractor to creator.py (T-003, next wave).
Flaky/pre-existing failures: E2e drain test may be flaky on timing — use polling with timeout, not fixed sleeps.
Gotchas: Daemon needs a running DB/queue; if unavailable, mark test NOT-RUN(needs shared env) and document. Use tmp_path for isolated DB.

## 7. Environment and tests
Scoped commands: pytest tests/daemon/test_drain_e2e.py -q; ruff check src/paa/runtime tests/daemon; mypy src/paa/runtime
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: tmp_path fixtures; isolated temp DB.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w2-impl/W2-IMPL-02-T-005-T-010.md (start early; ORCH-DIGEST first)
Commits: code commit first (trailers: Task: T-005/T-010, Agent: W2-IMPL-02, Wave: 2, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Root causes only; no band-aids.
- Long-run test: Source / Load / Hostile / Operability / Reversal.
- Tests must fail without the fix.
- E2e drain: poll with timeout instead of fixed sleep to avoid flakiness.
- Start report file early and append as you work.
