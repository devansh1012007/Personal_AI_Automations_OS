# Brief — W2-IMPR-01 (IMPR) · wave 2 improvement · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Close Wave 2 review findings (T-003+T-004) — every BLOCKER/MAJOR FIXED/REJECTED(DEFERRED with reason), tests strengthened. Improve code AND tests.
Role: improver. Plus: Wave 2 improver for DB/detectors integration is NOT in scope — your integration surface is context_agents/touch + daemon/curator + tests only.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — C03, C04
5. Implementer reports: docs/agent-ops/20261106-memory-remembering-agent/reports/w2-impl/W2-IMPL-03-T-003.md, W2-IMPL-04-T-004.md
6. Reviews in FULL: docs/agent-ops/20261106-memory-remembering-agent/reports/w2-review/REV-code.md, TREV-tests.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-improvement.md
8. COMMS.md from last WAVE-START
9. Source: check whether `MemoryCurator.run_maintenance` has a dry-run param; read curator orphan phase + sweep archive behavior (context: R-W2-002/R-W2-004 fixes)

## 3. Findings to disposition (address EVERY one)
CODE (R-W2-001–007):
- R-W2-001 [MAJOR] touch candidate superset not packet.elements → FIX (root cause): touch only ids in delivered `packet.elements` (planner + worker), not pre-packing retrievals. Keep handle-level placement rationale only if equivalent (prove dropped candidates are not touched). Add/strengthen test: low-confidence fact dropped by packer → use_count stays 0 while kept fact advances.
- R-W2-002 [MAJOR] first curator tick deletes permanently (no preview/archive for prune/consolidate/orphans) → FIX by the smallest honest mechanism: first maintenance tick in dry-run/preview mode (exact mechanism per curator API — read it; log preview counts, skip deletes), subsequent ticks full; OR phase-limited first tick (decay+analyze only) + warning. If curator has no dry-run, implement the limiter + document the runbook. If nothing safe fits, ESCALATE (do not fake-archive). Archive_writer for decay phase already exists — verify it is wired (W2-IMPL-04 used None).
- R-W2-003 [MINOR] MemoryCurator import outside fallback try → FIX: move inside construction try so import failure still falls back to sweep.
- R-W2-004 [MINOR] orphan purge wipes all facts if entity_index empty → FIX guard in curator.py (if entity table empty, skip orphan purge + warn). Include a regression test.
- R-W2-005 [MINOR] budget 7200s claim false (between-phase checks only) + in-memory resume wedge → FIX: correct log/comment wording to state between-phase budget; add wedge recovery (reset start_phase on repeated failure or document in-memory limitation honestly in code comment). Do not claim worst-case cap.
- R-W2-006 [MINOR] raw str(exc) in 3 touch helpers + one missing db-None guard → FIX: redact like API helper (use `redact` per models/base.py; verify convention), add the missing guard. Consistent behavior, not just cosmetic.
- R-W2-007 [NIT] 4 near-identical helpers → DEFER (refactor, not correctness) OR FIX by consolidating if it is truly minimal — choose one, record reason.
TESTS (TREV R-W2-001–007 + P1 sketches in TREV §6 — read them):
- [MAJOR×3] Failed-recall no-touch tests (worker hydration/leak, API 400/429); daemon loop-survival test (tick raises → loop retries, not dies); batching test (counting wrapper, one touch_many with deduped ids).
- [MINOR×4] Coder keyword-fallback touch test; no-`retain_only` fake test + docstring; empty-recall API/CLI symmetry; real 0.0-budget second-tick resume test.
Do P1 items from TREV §6; defer P3 unless trivial (list deferred).

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/agents/context_agents.py
- src/paa/coder/runtime_tools.py
- src/paa/api/app.py
- src/paa/cli/main.py
- src/paa/daemon.py
- src/paa/memory/curator.py
- tests/agents/test_context_agents.py
- tests/coder/test_runtime_tools.py
- tests/api/test_memory.py
- tests/cli/test_memory.py
- tests/daemon/test_drain_e2e.py
- tests/daemon/test_maintenance_loop.py
- docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-01.md
READ-ONLY: everything else (including docs/, src/paa/memory/facts.py, decay.py, config.py, schema).
FORBIDDEN: hotspot schema, pyproject, CI. Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/agents/test_context_agents.py tests/coder/test_runtime_tools.py tests/daemon/ tests/api/test_memory.py tests/cli/test_memory.py -q`
`.venv\Scripts\python.exe -m ruff check src tests` · `.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
New/strengthened tests must fail without the corresponding fix; note reversal evidence.

## 6. Deliverables
Improvement report at docs/agent-ops/20261106-memory-remembering-agent/reports/w2-improve/IMPR-01.md (ORCH-DIGEST first; finding-by-finding FIXED/REJECTED/DEFERRED + evidence + commit).
Commits: code, then report, trailers Task: T-003/T-004 / Agent: W2-IMPR-01 / Wave: 2 / Report: <path>. Final ≤8 lines.
