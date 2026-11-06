# Brief — W3-IMPR-02 (improver) · wave-impr · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Fix the MAJOR dead-wiring defect: `Runtime.memory_creator` is wired with the LLM extractor from `settings.memory.extraction_model`, but CLI `paa memory drain`, `POST /memory/drain`, and `daemon.py:_memory_creator` all construct `MemoryCreator(db)` directly, bypassing `rt.memory_creator`. Make the drain paths consume the runtime-wired creator so `PAA_MEMORY__EXTRACTION_MODEL` takes effect through the operator surface. Your role: improver.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. docs/agent-ops/20261024-memory-polish/reports/final/FINAL-operability.md (MAJOR dead-wiring finding, full detail)
5. docs/agent-ops/20261024-memory-polish/reports/final/FINAL-correctness.md (F1 cross-wave dead wiring, full detail)
6. docs/agent-ops/20261024-memory-polish/templates/report-improvement.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-IMPR-START)
8. Source entry points:
   - src/paa/cli/main.py:318-357 (drain command — builds MemoryCreator(rt.db) at ~630)
   - src/paa/api/app.py:86-93 (POST /memory/drain — builds MemoryCreator(runtime.db) at ~280)
   - src/paa/daemon.py:170-179 (_memory_creator — returns MemoryCreator(self._rt.db) at ~179)
   - src/paa/runtime.py:181,333,375 (memory_creator field, build, constructor pass)
   - src/paa/memory/creator.py:700-721 (MemoryCreator.__init__ — extractor parameter)

## 3. Task and acceptance criteria
Fix this finding:

**MAJOR (dead wiring):** Runtime.memory_creator wired but never exposed
- CLI `paa memory drain`: use `rt.memory_creator or MemoryCreator(rt.db)` instead of `MemoryCreator(rt.db)` directly
- API `POST /memory/drain`: use `runtime.memory_creator or MemoryCreator(runtime.db)` instead of `MemoryCreator(runtime.db)` directly
- Daemon `_memory_creator()`: return `self._rt.memory_creator or MemoryCreator(self._rt.db)` instead of `MemoryCreator(self._rt.db)`
- The `or` fallback is defensive only (Runtime.build() always populates memory_creator)

Acceptance criteria (measurable):
- `paa memory drain` uses `rt.memory_creator` when available (LLM extractor active when extraction_model set)
- `POST /memory/drain` uses `runtime.memory_creator` when available
- Daemon ingestion loop uses `self._rt.memory_creator` when available
- Default path unchanged when `extraction_model=""` (RuleBasedExtractor)
- Scoped: pytest tests/cli/test_memory.py tests/api/test_memory.py tests/daemon/test_drain_e2e.py -q green; ruff check src/paa/cli src/paa/api src/paa/daemon → 0; mypy src/paa/cli src/paa/api src/paa/daemon → 0

## 4. Ownership
WRITE-SET (may edit): src/paa/cli/main.py, src/paa/api/app.py, src/paa/daemon.py
READ-ONLY: src/paa/runtime.py (read memory_creator field), src/paa/memory/creator.py (read MemoryCreator constructor)
FORBIDDEN: src/paa/memory/creator.py, src/paa/runtime.py (IMPR-01's files)
Need something outside? Post NEED in COMMS.md.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W3-IMPR-01 | IMPR-01 (redaction + timeout/retry) | src/paa/memory/creator.py, src/paa/runtime.py | LLM extractor security + scale |

Known interactions: None — disjoint files. You cannot see IMPR-01's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — new finding from final review.
False leads: Do NOT modify creator.py or runtime.py (IMPR-01's files). Do NOT add observability/logging beyond what's needed (reviewer marked DO LATER). Do NOT change the rule-based default behavior.
Flaky/pre-existing failures: The daemon drain e2e test may be timing-sensitive — use polling with deadline, not fixed sleeps.
Gotchas: `rt.memory_creator` is set in `Runtime.build()` — `_build_runtime()` calls `Runtime.build()`, so it's always populated for CLI/API. The `or` fallback is defensive only.

## 7. Environment and tests
Scoped commands: pytest tests/cli/test_memory.py tests/api/test_memory.py tests/daemon/test_drain_e2e.py -q; ruff check src/paa/cli src/paa/api src/paa/daemon; mypy src/paa/cli src/paa/api src/paa/daemon
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: tmp_path fixtures; daemon test uses isolated DB.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w3-improve/W3-IMPR-02-DEAD-WIRING.md (ORCH-DIGEST first)
Commits: code commit first (trailers: Task: IMPR-02, Agent: W3-IMPR-02, Wave: impr, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (improver)
- Address every MAJOR finding: FIXED or REJECTED-with-evidence or DEFERRED-with-reason.
- Improve code AND tests (add test proving drain uses rt.memory_creator when LLM extractor configured).
- No band-aids; fix at source.
- Start report file early and append as you work.
