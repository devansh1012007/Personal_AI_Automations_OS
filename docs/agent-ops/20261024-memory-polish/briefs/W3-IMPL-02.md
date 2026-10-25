# Brief — W3-IMPL-02 (implementer) · wave 3 · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Add optional LLM-based ranking to `BoundedContextGatherer` in `src/paa/context/gatherer.py`. Ranking is currently purely mechanical (tier → relevance×confidence → importance → id). This change adds an optional injected `ranker` callable that can re-rank candidates after the mechanical sort, with mechanical behavior preserved when no ranker is provided. Also ensure tests/context/test_llm_compaction.py covers the ranker path.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: docs/memory-polish/plans/Plan1.md — item P-007 (section E row C07)
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-START / wave-2b close)
8. Source entry points:
   - src/paa/context/gatherer.py:286-316 (BoundedContextGatherer class, __init__)
   - src/paa/context/gatherer.py:242-278 (_rank_key — mechanical ranking)
   - src/paa/context/gatherer.py:498-554 (_compile — where ranking is applied at line 516)
   - src/paa/context/gatherer.py:103-170 (ContextElement, ContextPacket models)
   - src/paa/context/gatherer.py:1-30 (imports — what types are available)
   - tests/context/ (may not exist yet — W3-IMPL-01 creates it; you add ranker tests to same file)

## 3. Task and acceptance criteria
Task T-007 (P-007): Add optional LLM ranking to BoundedContextGatherer.

Acceptance criteria (measurable):
- `BoundedContextGatherer.__init__` accepts optional `ranker` parameter (callable: `tuple[list[ContextElement], frozenset[str], float] -> list[ContextElement]`)
- When `ranker` is None (default): behavior is unchanged — purely mechanical ranking
- When `ranker` is provided: `_compile()` calls it after mechanical sort at line 516; the ranker's output order is used for packing
- Mechanical default still produces deterministic total ordering (tier, relevance×confidence, importance, id)
- With ranker: the ranker is called exactly once per `compile()` call with the mechanically-sorted pool
- Scoped: pytest tests/context/test_llm_compaction.py -q green (includes ranker tests); ruff check src/paa/context tests/context → 0; mypy src/paa/context → 0

## 4. Ownership
WRITE-SET (may edit): src/paa/context/gatherer.py, tests/context/test_llm_compaction.py
READ-ONLY: src/paa/context/compaction.py (W3-IMPL-01's file — do NOT modify)
FORBIDDEN: src/paa/context/compaction.py, all other context files
Need something outside? Post NEED in COMMS.md, finish the safe part, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W3-IMPL-01 | T-006 (P-006) | src/paa/context/compaction.py | LLM summarizer in ContextCompactor |

Known interactions: None — disjoint files (gatherer.py vs compaction.py), no shared symbols. Both agents write tests to the same file `tests/context/test_llm_compaction.py` — coordinate via COMMS.md to avoid conflicts. W3-IMPL-01 creates the file first; W3-IMPL-02 appends ranker tests. You cannot see W3-IMPL-01's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — new surface.
False leads: Do NOT modify compaction.py (W3-IMPL-01's task). Do NOT make LLM ranking mandatory — it must be optional with mechanical default.
Flaky/pre-existing failures: None in tests/context (new dir).
Gotchas: **Both agents write to tests/context/test_llm_compaction.py.** W3-IMPL-01 creates the file; W3-IMPL-02 must coordinate via COMMS.md to append ranker tests without conflicting. Use explicit-path git commits only.

## 7. Environment and tests
Scoped commands: pytest tests/context/test_llm_compaction.py -q; ruff check src/paa/context tests/context; mypy src/paa/context
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: No DB needed — gatherer works on ContextElement objects. Construct them directly in tests.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w3-impl/W3-IMPL-02-T-007.md (start early; ORCH-DIGEST first)
Commits: code commit first (trailers: Task: T-007, Agent: W3-IMPL-02, Wave: 3, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Root causes only; no band-aids.
- Long-run test: Source / Load / Hostile / Operability / Reversal.
- Tests must fail without the fix; cover boundaries, empty pool, ranker that returns different order.
- Coordinate test file writes with W3-IMPL-01 via COMMS.md.
- Start report file early and append as you work.
