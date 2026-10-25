# Brief — W3-IMPL-01 (implementer) · wave 3 · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Add an optional LLM-backed summarizer to `ContextCompactor` in `src/paa/context/compaction.py`. The compactor is intentionally mechanical (deterministic, non-LLM) by default per its docstring; this change adds an optional injected `summarizer` callable that can further compress text after mechanical reduction, with mechanical behavior preserved when no summarizer is provided. Also create `tests/context/test_llm_compaction.py` proving the optional summarizer path and the mechanical default.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: docs/memory-polish/plans/Plan1.md — item P-006 (section E row C06)
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-START / wave-2b close)
8. Source entry points:
   - src/paa/context/compaction.py:72-135 (ContextCompactor class, __init__, compact)
   - src/paa/context/compaction.py:293 (module docstring — explains why LLM summarizer was deliberately excluded)
   - src/paa/context/compaction.py:113-135 (compact method — where to add summarizer call)
   - tests/context/ (may not exist yet — create it)
   - tests/conventions/test_docs_references.py (line-number lock — new code shifts nothing here since no existing lines move)

## 3. Task and acceptance criteria
Task T-006 (P-006): Add optional LLM summarizer to ContextCompactor.

Acceptance criteria (measurable):
- `ContextCompactor.__init__` accepts optional `summarizer` parameter (callable: `str -> str`)
- When `summarizer` is None (default): behavior is unchanged — purely mechanical, deterministic
- When `summarizer` is provided: `compact()` calls it after mechanical fixed-point reduction; result is the summarizer's output
- `__slots__` includes `summarizer`
- Mechanical default still produces idempotent output (`compact(compact(x)) == compact(x)`)
- With summarizer: the summarizer is called exactly once per `compact()` call
- Scoped: pytest tests/context/test_llm_compaction.py -q green; ruff check src/paa/context tests/context → 0; mypy src/paa/context → 0

## 4. Ownership
WRITE-SET (may edit): src/paa/context/compaction.py, tests/context/test_llm_compaction.py
READ-ONLY: src/paa/context/gatherer.py (W3-IMPL-02's file — do NOT modify)
FORBIDDEN: src/paa/context/gatherer.py, all other context files
Need something outside? Post NEED in COMMS.md, finish the safe part, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W3-IMPL-02 | T-007 (P-007) | src/paa/context/gatherer.py | LLM ranking in BoundedContextGatherer |

Known interactions: None — disjoint files (compaction.py vs gatherer.py), no shared symbols. You cannot see W3-IMPL-02's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — new surface.
False leads: Do NOT modify gatherer.py (W3-IMPL-02's task). Do NOT make LLM summarization mandatory — it must be optional with mechanical default.
Flaky/pre-existing failures: None in tests/context (new dir).
Gotchas: The compactor's docstring explicitly says LLM summarization is deliberately not done (compaction.py:3). Your change adds it as an OPTIONAL injected callable, preserving the mechanical default — this is consistent with the design intent (determinism by default, LLM as opt-in upgrade). Do NOT remove or contradict the existing docstring's reasoning; extend it.

## 7. Environment and tests
Scoped commands: pytest tests/context/test_llm_compaction.py -q; ruff check src/paa/context tests/context; mypy src/paa/context
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: No DB needed — compactor works on plain strings. Use tmp_path only if needed.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w3-impl/W3-IMPL-01-T-006.md (start early; ORCH-DIGEST first)
Commits: code commit first (trailers: Task: T-006, Agent: W3-IMPL-01, Wave: 3, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Root causes only; no band-aids.
- Long-run test: Source / Load / Hostile / Operability / Reversal.
- Tests must fail without the fix; cover boundaries, empty input, summarizer that returns longer text.
- Start report file early and append as you work.
