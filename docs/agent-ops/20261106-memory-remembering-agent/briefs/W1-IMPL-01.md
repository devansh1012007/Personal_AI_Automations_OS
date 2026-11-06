# Brief — W1-IMPL-01 (IMPL) · wave 1 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan C01+C01b+C02 (T-001, T-002, tests T01–T03): wire `VectorEmbeddingSink` in `Runtime._build_memory_creator`, align the sink payload keys with the `_semantic` reader, and add `drain_pending_embeddings` called from the 3 drain entry points. Failure looks like: new facts never get vector rows, or semantic hits return empty content.
Your role: implementer (coder). ROOT CAUSE only; no band-aids (see STANDING-ORDERS).

## 2. Required reading (in this order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (+ docs/adr/README.md if needed) — gate, ledger invariant, LIKE rule, conventions
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (commands, baseline, hotspots)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C01, C02, T01–T03 (verbatim in §E)
5. Investigation: docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-creator-runtime.md (read in FULL; it is your primary evidence)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
7. COMMS.md from last WAVE-START (read before first edit)
8. Source (verify, don't trust): src/paa/runtime.py:398-460, src/paa/memory/creator.py:799-880,1182-1210, src/paa/memory/facts.py:1057-1087, src/paa/agents/context_agents.py:241-262, src/paa/daemon.py:144-184, src/paa/api/app.py:270-277, src/paa/cli/main.py (drain command), src/paa/config.py:537-544

## 3. Task and acceptance criteria
Task T-001 (C01): `Runtime._build_memory_creator` constructs `VectorEmbeddingSink` when vector store + embedder available, else degrades to None + warning (same pattern as `_try_vector/_try_graph/_try_queue`). Reuse `_try_vector` result; embedder from existing factory (`settings.models` embedding fields, hash fallback allowed). Log `memory.embedding_sink` wired/degraded with embedder name. Do NOT require `sentence-transformers`, do NOT change taxonomy/schema.
Task T-001b (C01b payload alignment): producer (creator.py:842-848 keys entity_id/predicate/memory_domain/memory_scope/statement) vs reader (context_agents.py:251-262 reads object_value/confidence/importance). Pick ONE (recommend: extend payload to include object_value, confidence, importance — reader keeps working and old points still read with defaults) and document it in the report. Payload must round-trip through `_semantic` with non-empty content (test required, see §7).
Task T-002 (C02): add `MemoryCreator.drain_pending_embeddings` (or equivalent name — document choice): `pending_embeddings(limit)` → `sink.enqueue` → `mark_embedded`; failures leave `pending` (never flip to terminal `failed`), log errors, idempotent second drain = 0. Call it from `daemon._ingestion_loop` (after `run_batch`), `api.drain_memory`, and `cli.drain`. Log pending/indexed counts per drain.
Acceptance (measurable):
- With backends available, `Runtime.build(...).memory_creator` has a non-None sink; without, boot succeeds with warning and sink is None.
- A triple-payload `process_signal` leaves zero `pending` rows and one point in `active_facts`, and `_semantic` round-trip returns non-empty content for that fact's object_value.
- Failing embedder: fact stays `pending`; next successful drain flips to `indexed`.
- C02 drain is idempotent (second run returns 0) and never loses a fact.
- Scoped test commands in §7 pass; `ruff check src tests` clean; `mypy src` shows ONLY the 3 pre-existing win32 errors (baseline, see 00-mission).

## 4. Ownership
WRITE-SET (may edit only these):
- src/paa/runtime.py
- src/paa/memory/creator.py
- src/paa/daemon.py
- src/paa/api/app.py
- src/paa/cli/main.py
- tests/memory/test_creator.py
- tests/runtime/test_memory_creator.py
- tests/api/test_memory.py
- tests/cli/test_memory.py
- docs/agent-ops/20261106-memory-remembering-agent/reports/w1-impl/W1-IMPL-01-T-001.md
READ-ONLY: everything else, especially src/paa/agents/context_agents.py (align via producer side; do NOT edit reader in this wave), src/paa/memory/facts.py, src/paa/config.py, schema files, docs (except your report).
FORBIDDEN: hotspot schema files, pyproject.toml, CI config.
Need something outside? Don't edit; post NEED in COMMS.md, finish safe part, report DEFERRED-DEPENDENCY.

## 5. Parallel siblings this wave
None (single implementer by design — no parallel file conflicts).

## 6. Skip list and known traps
- Pre-existing baseline failures, NOT yours: mypy 3 win32 errors (sandbox/subprocess_backend.py:548), pytest 12 Windows failures (sandbox/docker ×9, tui/dashboard strftime bug, validation/workspace ×2) — see 00-mission. Do not fix.
- `VectorEmbeddingSink` is decorated? No — verify decorator status in code before changing payload (investigation noted @runtime_checkable on Protocol, not sink).
- `Runtime._build_memory_creator` signature is `(settings, db, model_adapter)` — the vector store is built elsewhere in `Runtime.build` BEFORE this call; thread it through, keep signature backward-compatible (default params or build inside function via existing helpers — document choice and why).
- `embedding_status='pending'` partial index exists; drain must use `pending_embeddings` order (created_at, id), not OFFSET.
- SentenceTransformer's lock serialises embeds — don't spam parallel embeds; batch per existing pattern.
- Investigator assumed Runtime.build test exists for sink wiring — verify; create it in `tests/runtime/test_memory_creator.py` if absent.

## 7. Environment and tests
Scoped commands (run from repo root, Windows):
- `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py -q`
- `.venv\Scripts\python.exe -m pytest tests/runtime/test_memory_creator.py -q`
- `.venv\Scripts\python.exe -m pytest tests/api/test_memory.py tests/cli/test_memory.py -q`
- `.venv\Scripts\python.exe -m ruff check src/paa/memory/creator.py src/paa/runtime.py src/paa/daemon.py src/paa/api/app.py src/paa/cli/main.py`
- `.venv\Scripts\python.exe -m mypy src` (expect only baseline 3)
Do NOT: full suite, container restarts, shared-DB migrations, fixed ports.
New tests must be confirmed to FAIL without your fix (revert/stash test, note result) per TEST QUALITY rules.

## 8. Deliverables
Report: docs/agent-ops/20261106-memory-remembering-agent/reports/w1-impl/W1-IMPL-01-T-001.md (start early; ORCH-DIGEST first; use templates/report-implementation.md)
Commits: code first, then report (docs-only), trailers `Task: T-001` / `Agent: W1-IMPL-01` / `Wave: 1` / `Report: <path>`. Explicit paths only. Stay on `ops/20261106-memory-remembering-agent`.
Final message (≤8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
Root cause only. Answer the long-run test in the report (Source/Load/Hostile/Operability/Reversal). Challenge the plan with evidence if it is wrong — do not implement a wrong fix silently. If proper fix exceeds write-set, stop and return ESCALATE.
