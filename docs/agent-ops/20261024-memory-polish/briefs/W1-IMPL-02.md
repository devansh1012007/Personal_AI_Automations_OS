# Brief — W1-IMPL-02 (implementer) · wave 1 · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Add memory API routes (`GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain`) to the FastAPI ingestion edge, and add tests proving each route returns shaped JSON. Failure looks like: memory is ingestible via `/signals` but not queryable or drains cannot be triggered over HTTP.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + any nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions A-001…A-004)
4. Plan: docs/memory-polish/plans/Plan1.md — items P-002 and P-009, quoted verbatim: section E rows C02/C09 and section H rows T02/T05
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table, wave 1)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from the last WAVE-START line)
8. Source entry points: src/paa/api/app.py:172-196 (`/signals` route shape + `_signal_repo`), src/paa/api/app.py:205-223 (`_admit` backpressure gate), src/paa/api/app.py:248-254 (`_parse_uuid`), src/paa/api/schemas.py:72-80 (`SignalRequest`/`SignalResponse` schema shape), src/paa/memory/facts.py:914-1033 (`query_facts`, `search_facts_by_text`), src/paa/memory/creator.py:791-830 (`run_batch`), src/paa/storage/coldlake/signals.py:102-120 (`SignalRepository`)

## 3. Task and acceptance criteria
Task T-002 (P-002 + P-009): Add four memory routes to the FastAPI app and add tests/api/test_memory.py proving each route.

Acceptance criteria (measurable):
- `GET /memory/facts?entity=<id>` returns 200 with a JSON list of fact objects (id, entity_id, predicate, object_value, confidence, domain, source_signal_id)
- `GET /memory/search?q=<text>` returns 200 with a JSON list of matching facts (empty array `[]` when none match)
- `GET /memory/signals` returns 200 with a JSON list of signals including `sync_status`; supports optional `?status=unprocessed|processing|processed|malformed|quarantined|error` filter
- `POST /memory/drain` returns 202 with a batch report (claimed, processed, facts_written, …)
- Routes apply the same `_admit` backpressure gate as `/signals` (return 429 under overload)
- Each route has a test that FAILS without the implementation and PASSES with it (reversal check)
- Scoped run: `pytest tests/api/test_memory.py -q` green; `ruff check src/paa/api tests/api` → 0 findings; `mypy src/paa/api` → 0 errors

## 4. Ownership
WRITE-SET (may edit): `src/paa/api/app.py`, `tests/api/test_memory.py` (create the file if the dir does not exist)
READ-ONLY: `src/paa/memory/creator.py`, `src/paa/memory/facts.py`, `src/paa/storage/coldlake/signals.py`, `src/paa/api/schemas.py`
FORBIDDEN (hotspots / other agents' files): `src/paa/cli/main.py` (W1-IMPL-01's file), `src/paa/memory/*.py` (read-only — do not modify creator/facts internals)
Need something outside? Post `NEED` in COMMS.md, finish the safe part, report `DEFERRED-DEPENDENCY`.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W1-IMPL-01 | T-001 (P-001 + P-008) | src/paa/cli/main.py, tests/cli/test_memory.py | New `paa memory` CLI group |

Known interactions: None — disjoint files, disjoint test dirs, no shared symbols. You cannot see W1-IMPL-01's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — this is new surface.
False leads: Do not add CLI commands (that is W1-IMPL-01's task). Do not modify memory/creator.py internals (read-only).
Flaky/pre-existing failures: None in tests/api (new dir).
Gotchas: New routes must use `response_model` schemas (define them in src/paa/api/schemas.py or inline) and follow the `_admit` + `_parse_uuid` pattern. `SignalRepository` (`_signal_repo`, app.py:226-245) shows how to build repo objects from `runtime.db`. New test dirs may not exist yet — create them (new files are fine).

## 7. Environment and tests
Scoped commands: `pytest tests/api/test_memory.py -q`; `ruff check src/paa/api tests/api`; `mypy src/paa/api`
Do NOT: run the full suite, restart containers, run shared-DB migrations, bind fixed ports.
Unique test resources: FastAPI `TestClient` against `create_app(runtime)` with a temp DB; no shared server needed.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w1-impl/W1-IMPL-02-T-002.md (start it early; ORCH-DIGEST block first; append as you work)
Commits: code commit first (trailers: Task: T-002, Agent: W1-IMPL-02, Wave: 1, Report: <path>), then report commit (docs-only, lists the code SHA)
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Fix root causes only; no band-aids (standing orders §7).
- Answer the long-run test: Source / Load / Hostile / Operability / Reversal.
- If a proper fix exceeds your scope, stop and return `ESCALATE` with analysis — do not ship a stopgap.
- Tests must fail without the fix; cover boundaries, malformed input, empty results, 429 backpressure.
- Create your report file at the start and append as you work.
