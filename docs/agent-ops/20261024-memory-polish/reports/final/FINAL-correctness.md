# ORCH-DIGEST

run: 20261024-memory-polish | branch: memory-polish | base: 4ac96db | role: FINAL REVIEWER (read-only)
scope: src/paa/api/app.py, src/paa/api/schemas.py, src/paa/cli/main.py, src/paa/config.py, src/paa/context/compaction.py, src/paa/context/gatherer.py, src/paa/memory/creator.py, src/paa/runtime.py + tests/ + docs/memory-polish/plans/Plan1.md
gate verified: ruff check src tests -> 0 findings; mypy src -> 0 errors; affected tests -> 68 passed (cli 14, api 12, daemon 2, memory 16, context 23) + full suite green
agent reports audited: W1-IMPL-01 (T-001 CLI), W1-IMPL-02 (T-002 API), W2-IMPL-01 (T-004 config), W2-IMPL-02 (T-005 wiring + T-010 e2e), W2-IMPL-03 (T-003 LLMFactExtractor), W3-IMPL-01 (T-006 compactor), W3-IMPL-02 (T-007 gatherer)

Verdict: APPROVE-WITH-CHANGES. The branch is clean, additive, gate-green, and well-tested. One cross-wave integration defect (F1) makes the Wave-2 extraction-model config unreachable through the Wave-1 CLI/API drain surface; two correctness nits (F2, F3) and one report-accuracy nit (F4). All are fixable without changing behavior of the default rule-based path.

---

# Findings

## F1 — MAJOR: Wave-2 extraction-model wiring never reaches the Wave-1 drain surface

**Files:** `src/paa/runtime.py:181,333,375,430-454`; `src/paa/cli/main.py:626-630`; `src/paa/api/app.py:200,277-280`

**Evidence:**
- `Runtime.build()` builds `memory_creator = _build_memory_creator(settings, db, model_adapter)` (runtime.py:333), stores it as `Runtime.memory_creator` (runtime.py:181), and passes it to the Runtime constructor (runtime.py:375). `_build_memory_creator` reads `settings.memory.extraction_model` and, when non-empty, resolves `LLMFactExtractor(model=model_adapter)`; otherwise `RuleBasedExtractor`.
- But the CLI `drain` command builds its own `MemoryCreator(rt.db)` directly (cli/main.py:630), and `POST /memory/drain` builds `MemoryCreator(runtime.db)` directly (api/app.py:280). Neither reads `rt.memory_creator` / `runtime.memory_creator`.
- `_build_runtime()` (cli/main.py:51-60) calls `Runtime.build()`, so `rt.memory_creator` is always populated for CLI commands.

**Why it matters:** `PAA_MEMORY__EXTRACTION_MODEL` is a no-op for `paa memory drain` and `POST /memory/drain` — both always use the rule-based extractor. Wave 2's headline feature (opt-in LLM extraction) is unreachable through Wave 1's new surface, and the `memory_creator` field is dead code from the CLI/API perspective. An operator setting the flag expecting LLM extraction via `paa memory drain` gets rule-based extraction silently.

**Recommended fix:** Make the drain paths consume the runtime-wired creator:
- cli/main.py `drain`: `creator = rt.memory_creator or MemoryCreator(rt.db)`
- api/app.py `drain_memory`: `creator = runtime.memory_creator or MemoryCreator(runtime.db)`
Since `Runtime.build()` always populates `memory_creator`, the `or` fallback is defensive only. This routes `extraction_model` through the new surface with zero change to the default path.

**Audit vs agent report:** W2-IMPL-02's T-005 report claims the wiring is complete ("`MemoryCreator` is constructed with extraction model/provider when config fields are non-empty"). That's true for `Runtime.build()`, but the report never verifies the CLI/API drain call sites consume `rt.memory_ctor`. The e2e drain test (T-010) uses the default rule-based path, so it doesn't catch this. Cross-wave integration was not verified.

---

## F2 — MINOR: `extraction_model_provider` config field is declared but never read

**Files:** `src/paa/config.py:258-264`; `src/paa/memory/creator.py:617-621` (`LLMFactExtractor.__init__(*, model)`); `src/paa/runtime.py:_build_memory_creator`

**Evidence:** `MemorySettings` gains `extraction_model_provider: str = ""` (config.py). Nothing reads it: `_build_memory_creator` only checks `settings.memory.extraction_model`; `LLMFactExtractor.__init__` accepts only `model=`, no provider argument. The provider string is inert.

**Why it matters:** Misleads operators into believing provider selection (e.g. openrouter vs ollama) is wired. It is not; `LLMFactExtractor` receives `model_adapter` from the runtime router, which already resolves the provider via `PAA_MODELS__*` config. The field is redundant dead config.

**Recommended fix:** Either (a) wire `extraction_model_provider` into `LLMFactExtractor` construction so it can select a provider-specific model, or (b) remove the field to avoid dead config. Given `model_adapter` already carries the router-resolved provider, (b) is simpler unless the roadmap plans per-extractor provider selection.

---

## F3 — MINOR: `/memory/signals` API route and CLI `signals` command select inconsistent column sets vs `SignalView`

**Files:** `src/paa/api/app.py:250-266`; `src/paa/api/schemas.py:113-122`; `src/paa/cli/main.py:493-518`

**Evidence:** `SignalView` declares `id, channel, external_id, sync_status, received_at, processed_at, error_detail`. The API route SQL selects only `id, channel, external_id, sync_status, received_at` — `processed_at` and `error_detail` are never selected and are emitted as `null` (they have `= None` defaults). The CLI route selects `processed_at` but not `error_detail`. CLI and API disagree, and the API's `SignalView` documents fields that are always null.

**Why it matters:** API consumers see `processed_at: null, error_detail: null` for every signal, which is misleading given the schema implies they may be populated. Inconsistent shape between CLI and API for the same table.

**Recommended fix:** In the API route, select `processed_at, error_detail` too and pass them into `SignalView` (matching the CLI). Or drop the always-null fields from `SignalView`. Prefer selecting all columns for parity.

---

## F4 — NIT: W1-IMPL-01 report §7 lists test names that do not match the test file

**File:** `docs/agent-ops/20261024-memory-polish/reports/w1-impl/W1-IMPL-01-T-001.md` (§7)

**Evidence:** Report lists `test_facts_prints_table`, `test_drain_prints_batch_counters`, `test_drain_processes_seeded_signal`, `test_facts_no_facts_graceful`, `test_search_no_matches_graceful`, `test_world_model_prints_managed_section`, `test_world_model_empty_vault`, `test_world_model_unknown_doc`, `test_search_nul_byte_refused`. The actual file defines `test_prints_table_for_known_entity`, `test_prints_all_counters_on_empty_backlog`, `test_processes_a_seeded_signal`, `test_prints_no_facts_when_entity_unknown`, `test_prints_no_facts_when_entity_has_none`, `test_prints_matching_facts`, `test_prints_no_matches_gracefully`, `test_prints_managed_section`, `test_prints_no_managed_section_on_empty_vault`, `test_unknown_document_is_refused_cleanly`, `test_nul_byte_is_refused_cleanly`, `test_filters_by_status`, `test_help_lists_all_subcommands`, `test_prints_no_signals_gracefully`.

**Why it matters:** Cosmetic report inaccuracy only; no code impact. The tests themselves are correct and green.

**Recommended fix:** Align report §7 with actual test names if reports are treated as auditable artifacts.

---

# Long-run test (source / load / hostile / operability / reversal)

- **Source:** All new surface derives from existing repositories/creator (FactRepository.query_facts/search_facts_by_text, MemoryCreator.run_batch, WorldModel.read_managed). No new state; fully replayable/derivable. LLM path is additive behind `extraction_model`, rule-based default untouched. LLM extractor applies containment (`_reject_suspicious`, `_scalar_to_text`, `MAX_CANDIDATES`) before inference and quarantines on model failure — same contract as rule-based.
- **Load:** Drain bounded by `limit` (default 25 CLI / 25 API); facts/search capped by `max_scan=10_000` (existing); `_admit` backpressure gate (429) on all four routes; ranker/summarizer are single synchronous calls with `None` short-circuit. No unbounded loops/queues added.
- **Hostile input:** `search` NUL byte → `ValueError` → HTTP 400 / CLI exit 1 (refused, not swallowed). `LLMFactExtractor` runs `_reject_suspicious` (C0 controls, ANSI, managed-block markers) before the model call; oversized object (>8 KiB) → `MalformedSignalError`, zero facts; bad triples skipped; candidate ceiling breach → `MalformedSignalError`. Signals SQL uses bound params (no injection).
- **Operability:** Empty DB/vault/signal backlog handled gracefully (empty tables, zero counters, "no facts"/"no matches"/"no signals"); clean refusals with exit codes; no tracebacks on operator error. Degrade-don't-crash: `LLMFactExtractor` absent/fails → rule-based fallback, logged warning, boot continues. Summarizer/ranker exceptions propagate (fail loud), no swallowing.
- **Reversal:** Reverting the `memory` command block → `paa memory` exit 2, all CLI tests fail. Reverting the four routes → 404, all API tests fail. `test_drain_requires_daemon_loop` proves no writes without the ingestion loop. `test_llm_compaction` injected-path tests fail without the summarizer/ranker hooks.

# Cross-wave interactions

- **Wave 1 (CLI/API) vs Wave 2 (config/runtime wiring):** Defect F1 — the drain surface bypasses `rt.memory_creator`, so Wave 2's `extraction_model` flag has no effect through Wave 1's routes/commands. Fix F1 resolves this; no other conflict.
- **Wave 1 vs Wave 3 (compactor/gatherer):** Disjoint files (`compaction.py` vs `gatherer.py`), both additive optional parameters with `None` defaults. Shared test file `tests/context/test_llm_compaction.py` was co-authored (W3-IMPL-01 created it, W3-IMPL-02 appended ranker tests) with no edit-region overlap; 23 tests pass together. No conflict.
- **Schema/persistence:** None (Plan1 A-004). No migration. Relational schema parity untouched.

# Band-aid check

- No swallowed errors: `LLMFactExtractor` wraps model failure in `MalformedSignalError`; ranker/summarizer exceptions propagate; NUL/hostile input refused, not truncated or coerced.
- No special-cased inputs beyond documented containment (NUL/ANSI/marker/oversized) — these are invariant enforcement, not band-aids.
- No suppressed types: no `# noqa`, no `# type: ignore`, no `Any` widening beyond the structural model-adapter parameter (matches existing convention), no ruff/mypy relaxations.
- No weakened tests: reversal tests present for CLI, API, daemon drain, LLM extractor, compactor, ranker; deterministic (polling with deadline, no fixed sleeps); isolated tmp_path fixtures.

# Test quality

- **Fail without fix?** Yes — each new test asserts new surface; reverting the implementation makes them fail (404/exit-2/timeout).
- **Deterministic?** Yes — bounded polling loops (`_wait_for`), no fixed long sleeps; isolation via `tmp_path` + `reset_settings_cache()`.
- **Over-mocked?** No — tests drive real `Runtime`, real `Database`, real `Daemon`, real `FactRepository`/`MemoryCreator`; models are scripted (appropriate for unit-level contract tests).
- **Edge coverage:** Strong on containment (NUL, ANSI, marker, oversized, malformed triples, model failure, candidate ceiling, empty/missing/non-list `facts`), batch isolation (crashing signal spares siblings), ranker (empty pool, subset, reorder, ceiling post-rank, failure propagates, worker packet), summarizer (exactly once, fixed-point input, longer output passthrough, empty input).
- **Missing edge:** No test verifies `extraction_model` config actually routes to `LLMFactExtractor` through `Runtime.build()` (covered only implicitly by `test_drain_e2e` which uses rule-based default). This is subsumed by F1 — once drain paths use `rt.memory_creator`, an integration test setting `PAA_MEMORY__EXTRACTION_MODEL` and asserting `LLMFactExtractor` is consulted would close the gap.

# Optimization opportunities

| Effort | Item | Why |
|--------|------|-----|
| DO NOW | Fix F1: drain CLI/API use `rt.memory_creator`/`runtime.memory_creator` | Makes Wave-2 config effective through Wave-1 surface; highest correctness impact |
| DO LATER | Resolve F2: wire or remove `extraction_model_provider` | Removes dead config; prevents operator confusion |
| DO LATER | Resolve F3: select `processed_at`/`error_detail` in API `/memory/signals` | Schema honesty; CLI/API parity |
| DO LATER | Add integration test: `PAA_MEMORY__EXTRACTION_MODEL` → `LLMFactExtractor` consulted via `rt.memory_creator` | Closes the config-to-extractor integration gap exposed by F1 |
| NOT WORTH IT | F4 report name nit | Cosmetic; no code impact |
| NOT WORTH IT | Pagination on `/memory/signals` | Consciously out of scope (fixed 50-row cap per Plan1) |

# Verdict

**APPROVE-WITH-CHANGES**

The branch is additive, gate-green (ruff 0, mypy 0, 68 affected tests pass, full suite green), well-tested with strong containment and reversal coverage, and follows AGENTS.md conventions (no `# noqa`/`# type: ignore`, degrade-don't-crash, bound params, optional `None` defaults preserving existing behavior). The one defect that blocks unconditional approval is F1: the Wave-2 `extraction_model`/`memory_creator` wiring never reaches the Wave-1 `paa memory drain` / `POST /memory/drain` surface, so the opt-in LLM extraction flag is silently a no-op through the new CLI/API. Fix F1 (and optionally F2/F3) and re-verify the gate; the branch is otherwise sound.
