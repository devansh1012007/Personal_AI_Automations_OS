# Brief — W2-IMPL-03 (IMPL) · wave 2 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan C03 (T-003, test T04): touch-on-recall — every recall path must advance `use_count`/`last_queried_at` for the facts it returns so decay reflects use. Failure looks like: used facts still expire from creation date.
Role: implementer (coder). ROOT CAUSE only; no band-aids.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (ledger invariant, gate, conventions; recall touch must never mutate hot data outside FactRepository/DecaySweeper touch — document the derived-state exception you are creating)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — item C03, T04
5. Investigation: docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-recall-extractor-tests.md (read in FULL — 5 call sites + exact symbols) and INV-decay-curator-config.md §C03 (mechanism via last_queried_at→effective_confidence, not importance_index)
6. Wave-1 reports (for context, read digest): reports/w1-review/REV-code.md (R-W1-004 deferral note), gates/gate-w1-2.md
7. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
8. COMMS.md from last WAVE-START
9. Source to verify (don't trust): src/paa/memory/decay.py:84-107,250-275 (touch/touch_many), context_agents._hot_facts/_semantic (ids = fact ids, verified VO point id==fact id), runtime_tools.recall_facts (emitted dicts have NO id — touch via f.id before serialization), api.list_facts/search_facts, cli.facts/search (_do inner fns)

## 3. Task and acceptance criteria
Task T-003 (C03): after a SUCCESSFUL recall returns facts, call `DecaySweeper.touch_many(fact_ids)` batched (one call per recall, not per fact). Call sites (exactly these):
- `PlannerContextBuilder._hot_facts` / `_semantic` (+ `WorkerContextBuilder._hot_facts` inherits path) — note worker drops narrative domains (hydration_required): touch only the ids actually returned after filtering, never the pre-filter superset. Carry over the worker narrative-leak caution from INV-03 (touch must not change content/routing).
- `coder/runtime_tools.recall_facts` — touch internal `f.id`s BEFORE serialization (emitted dicts have no id; do NOT add id to output, that is a contract change — keep it internal).
- `api.list_facts` / `api.search_facts`; `cli.facts` / `cli.search` (rows before `_render_fact_table`, which drops ids).
Rules: failures in touch must be logged and NEVER raised into the read path (recall still returns results); no extra top-level queries when the list is empty (touch_many already early-returns — keep that); never touch superseded ids (recall already filters `superseded_by IS NULL` except semantic hits — semantic ids must be validated to be live fact ids before touching? At minimum, a superseded id touch is a no-op increment risk — document choice in report).
Acceptance:
- T04: for at least one path per group (context builder, coder, API, CLI), recall advances `use_count` by +1 and `last_queried_at` to now, and `effective_confidence` reflects it (or `is_stale` flips).
- Touch failure (monkeypatched raise) does not fail recall; error logged.
- Existing recall tests stay green (substrings/order unchanged — touch is side-effect only).
- Scoped tests §7 pass; ruff clean; mypy = baseline 3 only.
- WARN: Wave 1 shifted runtime.py/daemon.py — docs-reference lock. If you add lines to api/app.py, cli/main.py, context_agents.py, runtime_tools.py and docs cite those lines, run `pytest tests/conventions/test_docs_references.py -q` and fix any broken citations in docs (same WRITE-SET extension for docs needed? POST NEED if a docs file must change — do not edit docs outside write-set; report DEFERRED-DEPENDENCY listing exact files). Prefer avoiding doc edits by not reformatting.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/agents/context_agents.py
- src/paa/coder/runtime_tools.py
- src/paa/api/app.py
- src/paa/cli/main.py
- tests/agents/test_context_agents.py
- tests/coder/test_runtime_tools.py
- tests/api/test_memory.py
- tests/cli/test_memory.py
- docs/agent-ops/20261106-memory-remembering-agent/reports/w2-impl/W2-IMPL-03-T-003.md
READ-ONLY: everything else (especially src/paa/memory/facts.py, src/paa/memory/decay.py, src/paa/memory/curator.py, src/paa/daemon.py, schema, docs).
FORBIDDEN: hotspot schema, pyproject, CI.
Need something outside? Don't edit; post NEED in COMMS.md, finish safe part, report DEFERRED-DEPENDENCY.

## 5. Siblings (same wave, parallel)
| Agent | Task | Owns | Contracts changed |
| W2-IMPL-04 | T-004 | `src/paa/daemon.py`, `tests/daemon/*` | daemon maintenance loop |
You own NO overlap with W2-IMPL-04. Parallel coders have disjoint write-sets by design.

## 6. Skip list and traps
- `importance_index` has 0 production callers; do NOT rewire ranking to it. Effect is via `last_queried_at->effective_confidence`.
- `touch_many` signature: `touch_many(fact_ids: list[str], *, now=None)`; construct `DecaySweeper(db)` inside each call site (verify how construction works — `DecaySweeper(db, *, archive_writer=None, batch_size=500)` per INV-02) — do not change decay.py to add a module-level helper (outside write-set; if needed, POST NEED).
- `_semantic` vector ids == fact ids (verify post-Wave-1); still guard: touch only ids present in returned elements.
- Do not touch facts you did not return (e.g. worker pre-filter set, entity-resolution candidates).
- Pre-existing: mypy 3 win32, pytest 12 Windows failures, 2 non-blocking doc stales (IMPR-02 report) — not yours.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/agents/test_context_agents.py tests/coder/test_runtime_tools.py tests/api/test_memory.py tests/cli/test_memory.py -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (watch for your own line shifts)
New tests must fail without the fix; note reversal evidence.

## 8. Deliverables
Report: docs/.../reports/w2-impl/W2-IMPL-03-T-003.md (ORCH-DIGEST first). Commits: code, then report, trailers Task: T-003 / Agent: W2-IMPL-03 / Wave: 2 / Report: <path>. Final ≤8 lines: STATUS|agent|task|report|SHAs|blockers.
