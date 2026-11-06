# Brief — W3-IMPL-06 (IMPL) · wave 3 · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Implement plan C06 (T-006, test T07): config hygiene — make `prune_confidence_floor` effective (explicit-set only) and deprecate duplicate `decay_lambda`; sweep behavior unchanged for unset deployments.
Role: implementer (coder). ROOT CAUSE only; no band-aids. (Installing/launching is NOT part of this wave; other waves handle the final launch smoke test.)

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, config rules: secret names never values; `get_settings` cache; `reset_settings_cache` for tests)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12 pre-existing)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — item C06, T07
5. Investigation: docs/agent-ops/20261106-memory-remembering-agent/reports/w0-investigation/INV-decay-curator-config.md §C06 (read in FULL — floor conflict analysis, model_fields_set recommendation, seed check)
6. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/report-implementation.md
7. COMMS.md from last WAVE-START
8. Source to verify (don't trust): config.py MemorySettings (~214-260, prune/decay fields), decay.py sweep/inline floor comparison + is_stale + half_life_days, domains.py domain_policy/DOMAINS (READ-ONLY — do NOT change DOMAINS values), schema_sqlite.sql:496 + schema_postgres.sql:441 hot_serving_memory_registry.decay_lambda seed (find the seed; if seeded from Settings, record it — do not change schema/seed without escalating)

## 3. Task and acceptance criteria
Task T-006 (C06):
- Wire `prune_confidence_floor` into `DecaySweeper` ONLY when explicitly set by the operator (detect via `model_fields_set` — per INV-02 recommendation); otherwise per-domain `prune_floor` from `domain_policy` applies (no behavior change for unset deployments). Document the choice.
- Deprecate `MemorySettings.decay_lambda` (keep field for compat, warn-on-use naming `domain_policy` as replacement; `domain_policy` stays sole source). Stored `.env` keys keep parsing.
- Keep `DecaySweeper.sweep()` signature unchanged (floor via constructor or settings read — decide to avoid a settings→decay import cycle; document).
- Docs-reference lock: config.py/decay.py shifts may break docs citations. Run conventions test; docs/modules/memory.md and docs/operations/configuration-and-environment.md ARE in your write-set (update stale text there too, checked against code). Other docs files are not — POST NEED + DEFERRED-DEPENDENCY for those.
Acceptance:
- T07: with `prune_confidence_floor=0.5` explicitly set, a semantic fact at 0.4 evicts (fails pre-fix); with defaults, eviction set is byte-identical to today (existing test_decay.py green unmodified); explicit `decay_lambda` emits a deprecation warning and sweep behavior still follows DOMAINS (settings λ changes do not alter decay).
- Unknown-domain keys in dict must not crash settings load (warn-and-ignore or documented reject — decide, test it).
- Scoped tests §7 pass; ruff clean; mypy = baseline 3 only.

## 4. Ownership
WRITE-SET (may edit ONLY these):
- src/paa/config.py
- src/paa/memory/decay.py
- tests/memory/test_decay.py
- docs/modules/memory.md
- docs/operations/configuration-and-environment.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/w3-impl/W3-IMPL-06-T-006.md
READ-ONLY: everything else (especially src/paa/memory/domains.py — DO NOT change DOMAINS values; src/paa/memory/creator.py; schema files).
FORBIDDEN: hotspot schema (sqlite/postgres parity), pyproject, CI.
Need outside write-set? Don't edit; post NEED; DEFERRED-DEPENDENCY.

## 5. Siblings (parallel, disjoint write-sets)
| Agent | Task | Owns |
| W3-IMPL-05 | T-005 | src/paa/memory/creator.py, tests/memory/test_creator.py, tests/memory/test_llm_extractor.py |
Disjoint. Do not touch creator.py even for docstring polish.

## 6. Skip list and traps
- Do NOT change `DOMAINS` values (touches decay/facts/context/tests everywhere) — sole-source means settings defers to it, not the reverse.
- `DecaySweeper.sweep` currently inlines the floor comparison (does not call `is_stale`) — wiring must preserve inlined behavior except the explicit-set case.
- Universal env name is `PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR` (nested) — never write secret values; floors/lambdas are not secrets.
- Pre-existing: mypy 3, pytest 12, 2 non-blocking doc stales (IMPR-02) — not yours.

## 7. Commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/memory/test_decay.py -q`
`.venv\Scripts\python.exe -m ruff check src tests`
`.venv\Scripts\python.exe -m mypy src` (expect baseline 3)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` (incl. your two docs files)
New tests must fail without the fix; note reversal evidence.

## 8. Deliverables
Report: docs/.../reports/w3-impl/W3-IMPL-06-T-006.md (ORCH-DIGEST first; include seed finding for registry column). Commits: code, then report, trailers Task: T-006 / Agent: W3-IMPL-06 / Wave: 3 / Report: <path>. Final ≤8 lines.
