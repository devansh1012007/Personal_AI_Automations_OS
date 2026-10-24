# Plan 1 — Memory Polish

**Status:** DRAFT
**Branch:** `memory-polish` (created at `4ac96db` on `main`)
**Repo:** `/home/devansh/Code/Personal_AI_Automations_OS`
**Spec:** operator request + `docs/MCP_AND_PLUGINS.md`, `docs/ROADMAP.md`, `docs/VISION.md`, `flexability-audit.md`
**Rigor tier:** M (several files, new CLI surface, new API routes, new tests; no persistence/schema change)

---

## A. Problem, goal, success criteria

**Problem:** The memory subsystem is fully built and tested internally, but it is not exposed. There is no `paa memory` CLI command, no memory API routes (only `/signals` for ingestion), and no live daemon end-to-end cold-lake drain test. Fact extraction is rule-based only (no model), and context building is mechanical/non-LLM despite the vision and roadmap calling for LLM-based context.

**Goal:** Expose the memory system through CLI and API, prove the live daemon drain path end-to-end, add an LLM-backed fact extractor (small model, OpenRouter) for placing facts into the relational DB, and wire LLM-based context building.

**Success criteria (observable):**
1. `paa memory facts <entity>`, `paa memory search <text>`, `paa memory signals`, `paa memory drain` all run and return shaped output.
2. `GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain` return 200 with shaped JSON.
3. A signal posted to `/signals` becomes a row in `hot_serving_active_facts` through the live daemon ingestion loop (serve mode → queue → creator), asserted by an e2e test.
4. An LLM-backed extractor (scripted in tests, OpenRouter-configurable in production) can place facts into `hot_serving_active_facts` / `hot_serving_entity_index` under the same containment rules as the rule-based path.
5. Context building uses an LLM (OpenRouter) for ranking/selection within the token budget, with mechanical fallback.
6. Gate stays green: `ruff check src tests` → 0, `mypy src` → 0, `pytest tests/ --strict-markers --strict-config` → no new failures.

**Non-goals (explicitly not doing):**
- The `paa memory` TUI/dashboard (VISION Phase 3).
- Multi-session isolation (ROADMAP 2.6).
- Self-improvement loops (ROADMAP 2.5).
- Any schema or persistence change.
- Replacing the rule-based extractor as the default (it stays default; LLM is opt-in).

**Constraints:**
- No `# noqa`, no `# type: ignore`, no widened ruff ignore, no `pyproject.toml` relaxation (gate stays zero).
- Heavy deps (sentence-transformers, etc.) never imported at module scope in core paths.
- OpenRouter usage goes through existing `PAA_MODELS__*` config; no vendor hardcoding.
- Disjoint write-sets: this branch touches `cli/`, `api/`, `memory/`, `context/`, `tests/` only; no overlap with other in-flight work.

**Reversibility:** two-way door. All changes are additive (new commands, new routes, new optional extractor path); the rule-based extractor remains the default and can be toggled off via config.

---

## B. Glossary and reading guide

- **Cold lake:** `cold_lake_signals` table — raw, immutable signal payloads landed by ingestion (`/signals`).
- **Hot serving:** `hot_serving_active_facts` + `hot_serving_entity_index` — the queryable memory store.
- **Memory Creator:** RFC §2.1 agent 8 — turns one cold-lake signal into entities+facts, or refuses/quarantines.
- **Memory Curator:** RFC §2.1 agent 9 — nightly maintenance (decay, prune, consolidate, orphans, vector reconcile).
- **FactExtractor protocol:** the seam in `creator.py` where an LLM extractor can substitute the rule-based default.
- **Context gathering:** RFC §15.1 — packs decision-usable signal into a context packet under the token budget.

---

## C. Architecture of the codebase (the part that matters here)

**Module map and responsibilities (5-10 lines):**
- `src/paa/memory/` — creator, curator, decay, facts, world_model, domains, contradiction. Bottom-to-top layering per `__init__.py`.
- `src/paa/daemon.py` — three supervised loops: dispatch, ingestion (`creator.run_batch`), maintenance (decay sweep).
- `src/paa/cli/main.py` — Typer CLI: submit/run/status/ledger/gate/recover/doctor/serve/mode/tui. **No memory command.**
- `src/paa/api/app.py` — FastAPI ingestion edge: `/health`, `/tasks*`, `/ledger/{id}`, `/signals`. **No memory routes.**
- `src/paa/context/` — gatherer, budget, compaction, metrics. Mechanical context building today.
- `src/paa/config.py` — `ModelSettings` (embedding model, etc.), `MemorySettings` (curator budget). No extraction-model field yet.

**Entry points and execution flow today, with `[FACT file:line]`:**
- Signal lands: `POST /signals` → `SignalRepository.record` → `cold_lake_signals` row `[FACT src/paa/api/app.py:172-196]`.
- Daemon drains: `_ingestion_loop` → `creator.run_batch(limit=25)` → `process_signal` per signal `[FACT src/paa/daemon.py:142-152]`.
- Creator extracts: `RuleBasedExtractor.extract` (rule-based, no model) → entity resolution → contradiction check → one transaction commit → embedding enqueue `[FACT src/paa/memory/creator.py:725-787]`.
- Context builds: `ContextGatherer` packs facts mechanically under token budget `[FACT src/paa/context/gatherer.py]`.

**Data model and persistence touched:** `cold_lake_signals`, `hot_serving_entity_index`, `hot_serving_active_facts`. No schema change.

**Properties and invariants:**
- Ledger event before side effect, always (AGENTS.md ledger rule).
- One transaction per signal; crash mid-signal leaves `processing` + zero facts (creator.py docstring).
- Facts never overwritten; supersede chains only (facts.py docstring).
- Confidence derived at read time, never stored (decay.py docstring).
- Contradictions never auto-resolved (creator.py, curator.py).

**Conventions to follow:**
- CLI: one `@app.command()` per verb, `_build_runtime()` + `try/finally rt.close()` pattern `[FACT cli/main.py:66-98]`.
- API: `_admit` backpressure gate, `_parse_uuid`, `response_model` schemas `[FACT api/app.py:91-170]`.
- Tests: asyncio auto mode, tmp_path fixtures, no session-scoped state (AGENTS.md testing conventions).

---

## D. Proposed design

**Summary (5 lines):**
Add a `paa memory` CLI command group and four memory API routes (`GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain`). Add an LLM-backed `FactExtractor` (OpenRouter-configurable, small model) as an opt-in alternative to the rule-based extractor. Add an LLM-based context compaction/ranking path with mechanical fallback. Add e2e daemon drain test. Rule-based extractor remains default; nothing is removed.

**Before flow / After flow (numbered steps, real symbols):**

*Before (signal ingestion → fact):*
1. Operator POSTs to `/signals` → row in `cold_lake_signals` `[api/app.py:172]`.
2. Daemon `_ingestion_loop` claims row → `MemoryCreator.run_batch` `[daemon.py:142]`.
3. `RuleBasedExtractor` produces candidates (no model) `[creator.py:354]`.
4. Entity resolution + contradiction check + one-transaction commit `[creator.py:864-1010]`.
5. Fact visible via direct DB query only — no CLI, no API.

*After:*
1. Steps 1-4 unchanged (rule-based default preserved).
2. Operator runs `paa memory facts <entity>` or `GET /memory/facts?entity=...` → reads `hot_serving_active_facts` via `FactRepository.query_facts` `[facts.py:914]`.
3. Operator runs `paa memory search <text>` or `GET /memory/search` → `FactRepository.search_facts_by_text` `[facts.py:969]`.
4. Operator runs `paa memory drain` or `POST /memory/drain` → triggers `creator.run_batch` on demand.
5. Opt-in: `PAA_MODELS__EXTRACTION_MODEL` → LLM-backed `FactExtractor` called by `MemoryCreator` instead of `RuleBasedExtractor` for placed facts.
6. Context building: `ContextGatherer` uses LLM ranking within budget, mechanical fallback.

**Why this approach:** Additive only — no existing behavior changes, default extractor untouched, all new surface gated behind config/flags so the gate stays zero and nothing breaks for current users.

---

## E. Change list (atomic, ordered)

| ID | File and symbol | WHAT changes | HOW | WHY this and not the alternative | Depends on | Verified by (test ID) | Risk | Revert |
|---|---|---|---|---|---|---|---|---|
| C01 | `src/paa/cli/main.py` | Add `paa memory` command group with `facts`, `search`, `signals`, `drain`, `world-model` subcommands | Typer subcommands; each builds `Runtime`, calls the corresponding memory method, prints a table | CLI is the operator's front door; mirrors existing `ledger`/`status` command shape | none | T01 | low | remove the command block |
| C02 | `src/paa/api/app.py` | Add `GET /memory/facts`, `GET /memory/search`, `GET /memory/signals`, `POST /memory/drain` routes | FastAPI routes reusing `_signal_repo`, `FactRepository`, `MemoryCreator`; same backpressure `_admit` gate | FastAPI is the ingestion edge; memory needs its own surface parallel to `/signals` | none | T02 | low | remove routes |
| C03 | `src/paa/memory/creator.py` | Add `LLMFactExtractor` class implementing `FactExtractor`; call model router for structured triple extraction; keep `RuleBasedExtractor` default | New class in same file; async `extract` calls the router with a structured-output schema; containment rules (stop on suspicion, NUL, max candidates) still apply | The `FactExtractor` protocol already exists precisely for this (creator.py:238); no new file needed | C01, C02 | T03 | medium | config flag disables LLM path |
| C04 | `src/paa/config.py` | Add `MemorySettings.extraction_model: str` and `extraction_model_provider: str` fields | Two pydantic fields with defaults (empty = rule-based default) | Config-driven; OpenRouter via existing `PAA_MODELS__*`; no vendor hardcode | C03 | T03 | low | remove fields |
| C05 | `src/paa/runtime.py` | Wire extraction model into `MemoryCreator` construction in `_build_agents` / daemon builder | Pass `extract_model` / `extract_provider` to `MemoryCreator` when non-empty | Creator is built in daemon.py:179; needs the model handle | C04 | T03 | low | remove wiring |
| C06 | `src/paa/context/compaction.py` | Add optional LLM summarizer (injected callable; mechanical fallback retained) | `compress` accepts `summarizer` param; if set, uses LLM; else mechanical | compaction.py:7-9 already notes LLM summariser is the desired path | none | T04 | low | remove param |
| C07 | `src/paa/context/gatherer.py` | Use model for ranking/selection of facts within token budget | Inject embedder/model into gatherer; rank by LLM score when available, else confidence | RFC §15.1 decision-usable signal; LLM improves relevance | C06 | T04 | low | mechanical-only path remains |
| C08 | `tests/cli/test_memory.py` (new) | CLI command tests against temp DB | Each subcommand asserts output table content and exit code | Prove CLI surface works | C01 | C01 | low | delete file |
| C09 | `tests/api/test_memory.py` (new) | Route tests | TestClient against `create_app`; assert 200 + shaped JSON | Prove API surface works | C02 | C02 | low | delete file |
| C10 | `tests/daemon/test_drain_e2e.py` (new) | Serve-mode → signal → queue → creator drain | Start `Daemon`, POST signal, wait for `hot_serving_active_facts` row | The missing end-to-end test | C02 | C02 | medium | delete file |
| C11 | `tests/memory/test_llm_extractor.py` (new) | Scripted model: triples → facts; malformed → `MalformedSignalError` | `ScriptedModel` returns structured triples; assert containment and one-transaction | Prove LLM path respects containment | C03 | C03 | medium | delete file |
| C12 | `tests/context/test_llm_compaction.py` (new) | Scripted model summarization | Assert LLM summarizer used when injected; mechanical output when not | Prove context building with LLM | C06 | C06 | low | delete file |

**Ordering rationale:** C01/C02 (expose) first — no new deps, immediately reviewable. C03-C05 (LLM extractor) next — depends on config + runtime wiring. C06-C07 (LLM context) after — depends on compaction change. Tests (C08-C12) written alongside each change so every change is independently verifiable. The system stays working and testable at every step because all changes are additive and rule-based defaults are preserved.

---

## F. Files affected

| File | Change type | Change IDs | Why it must change | Who else depends on it |
|---|---|---|---|---|
| `src/paa/cli/main.py` | modify | C01 | Memory has no CLI surface | none |
| `src/paa/api/app.py` | modify | C02 | Memory has no API routes | none |
| `src/paa/memory/creator.py` | modify | C03 | LLM extractor seam needs filling | `daemon.py`, `runtime.py` |
| `src/paa/config.py` | modify | C04 | No extraction model config | `runtime.py` |
| `src/paa/runtime.py` | modify | C05 | Creator construction lacks model wiring | `daemon.py` |
| `src/paa/context/compaction.py` | modify | C06 | LLM summarizer path missing | `gatherer.py` |
| `src/paa/context/gatherer.py` | modify | C07 | Context ranking is mechanical only | `orchestrator.py` |
| `tests/cli/test_memory.py` | new | C08 | No CLI memory tests | none |
| `tests/api/test_memory.py` | new | C09 | No API memory tests | none |
| `tests/daemon/test_drain_e2e.py` | new | C10 | No e2e drain test | none |
| `tests/memory/test_llm_extractor.py` | new | C11 | No LLM extractor tests | none |
| `tests/context/test_llm_compaction.py` | new | C12 | No LLM compaction tests | none |

---

## G. Alternatives and decisions

| ID | Decision | Options considered | Chosen | Why chosen | Why others rejected | What would change our mind |
|---|---|---|---|---|---|---|
| D1 | Expose memory via CLI + API | (a) CLI only (b) API only (c) both | (c) both | Operator needs CLI; integrations need API; both are small additive surfaces | (a) leaves no programmatic access; (b) leaves no operator access | none |
| D2 | LLM extractor: new file vs same file | (a) `src/paa/memory/llm_extractor.py` (b) in `creator.py` | (b) in `creator.py` | The `FactExtractor` protocol lives there; keeping the seam co-located avoids a new module boundary | (a) creates an unnecessary new module for one class | if the class grows beyond ~150 lines |
| D3 | LLM model source | (a) OpenRouter via `PAA_MODELS__*` (b) new dedicated config (c) hardcode | (a) OpenRouter via `PAA_MODELS__*` | Reuses existing router/config; no vendor hardcode; operator already uses this for models | (b) duplicates config; (c) violates "no vendor hardcoding" | none |
| D4 | Context building: LLM ranking vs pure mechanical | (a) LLM-only (b) mechanical-only (c) LLM with mechanical fallback | (c) LLM with mechanical fallback | Works without a model; degrades gracefully; matches existing degrade-don't-crash convention | (a) breaks on hosts without a model; (b) ignores the vision/roadmap ask | none |
| D5 | Default extractor | (a) switch default to LLM (b) keep rule-based | (b) keep rule-based | Rule-based is deterministic, zero cost, zero hallucination; LLM is opt-in | (a) would change behavior for all users and add cost/latency | if LLM extraction proves strictly superior and cheap |

---

## H. Tests and validation

**What existing tests guarantee / do not guarantee:**
- `tests/memory/test_creator.py` guarantees rule-based extraction + containment `[FACT]`.
- `tests/memory/test_facts.py` guarantees entity resolution + fact queries `[FACT]`.
- `tests/daemon/` does not exist; the daemon ingestion loop has only unit-level `run_batch` coverage, no serve-mode drain `[FACT]`.
- No CLI or API memory tests exist anywhere `[FACT]`.

**Test traceability:**

| ID | Level | Scenario | Expected | Proves (change/risk ID) | New or existing |
|---|---|---|---|---|---|
| T01 | unit | `paa memory facts <entity>` with known facts | table printed, exit 0 | C01 | new |
| T02 | unit | `paa memory search <text>` returns matching facts | rows with confidence, exit 0 | C01 | new |
| T03 | unit | `paa memory drain` runs one batch | `claimed`/`processed` counts in output | C01 | new |
| T04 | integration | `GET /memory/facts?entity=...` returns 200 + JSON list | shaped `FactView[]` | C02 | new |
| T05 | integration | `GET /memory/search?q=...` returns 200 + JSON list | shaped results, empty array if none | C02 | new |
| T06 | integration | `GET /memory/signals` returns signals by status | `SignalView[]` with `sync_status` | C02 | new |
| T07 | integration | `POST /memory/drain` triggers `run_batch` | 202 + batch report | C02 | new |
| T08 | e2e | daemon serve mode: POST `/signals` → wait → fact in `hot_serving_active_facts` | fact row present, `source_signal_id` set | C10 | new |
| T09 | unit | scripted LLM extractor: triples → facts in one transaction | facts written, signal marked processed | C03, C11 | new |
| T10 | unit | scripted LLM extractor: malformed payload → `MalformedSignalError`, zero facts | no rows, signal malformed | C03, C11 | new |
| T11 | unit | LLM compaction: injected summarizer used | LLM text in output | C06, C12 | new |
| T12 | unit | LLM compaction: no summarizer → mechanical output | mechanical text, no model call | C06, C12 | new |

**Baseline:** `pytest tests/ -o addopts="--strict-markers --strict-config" -q` currently 2907 passed / 235 skipped / 4 xfailed. New tests must add to this without reducing it.

**Non-test validation:** `paa memory --help` prints the group; `paa doctor` continues to report backends; OpenRouter key never appears in logs/exceptions (existing `test_key_secrecy` contract).

---

## I. Edge cases and failure modes (derived from this code)

| Case | Where it bites | Expected behavior | Handled by |
|---|---|---|---|
| Poison signal in drain batch | `creator.py:799-830` (`run_batch`) | One signal fails; batch continues; error logged | C01, C03 (existing) |
| LLM returns malformed triples | `creator.py:LLMFactExtractor` | `MalformedSignalError`, signal quarantined, zero facts | C03, T10 |
| OpenRouter key in exception chain | `models/router.py` | Key scrubbed; errors carry no chain | existing `test_key_secrecy` |
| No vector store attached | `creator.py:644` (`VectorEmbeddingSink`) | Facts stay `pending`; recall degrades, never dies | existing |
| Memory command with no DB | `cli/main.py` | Clean refusal via `_settings_or_refuse` | C01 |
| Concurrent drain + daemon drain | `creator.py:832-860` (`_claim`) | Atomic claim; second caller gets none | existing |
| LLM summarizer raises | `world_model.py:210-220` | Falls back to deterministic path | C06 |

---

## J. Impact, second-order effects, compatibility

**Blast radius:** `cli/`, `api/`, `memory/`, `context/` packages + new test files. No other package imports these new symbols.

**Backward compatibility:** Fully backward compatible — all changes additive; rule-based extractor remains default; no config fields change meaning; no CLI commands removed.

**Concurrency and error-handling:** Daemon ingestion loop already isolates one poison signal per batch `[daemon.py:142-152]`; LLM extractor runs inside the same one-transaction-per-signal boundary `[creator.py:959-1010]`.

**Performance and cost:** LLM extraction is opt-in; default path unchanged (zero cost, zero latency addition). LLM context ranking bounded by existing token budget.

**Security and privacy:** OpenRouter key follows existing router secrecy contract (no logs, no ledger payload, no exception chain). No new secrets introduced.

**Second-order effects:** None that break existing flows. New surface invites future TUI/dashboard work (deferred).

---

## K. Rollout, rollback, observability

**Rollout:** Feature-flagged by config (`extraction_model` empty = rule-based). CLI/API routes always available once deployed.

**Rollback:** Remove the `memory` command block from `cli/main.py`, remove the four routes from `api/app.py`, revert `creator.py`/`config.py`/`runtime.py`/`compaction.py`/`gatherer.py` to pre-change state. All changes are additive and independently removable.

**Observability:** `memory.signal_processed` / `memory.signal_quarantined` / `memory.signal_malformed` structlog events already exist `[creator.py:775, 759, 1147]`. New: log LLM extraction usage and cost-relevant tokens.

---

## L. Risks, assumptions, open questions

| ID | Type | Description | Likelihood x impact | Mitigation or how to verify | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | risk | LLM extractor hallucinates a fact | medium x high | Containment rules apply (stop on suspicion, quarantine, one transaction); rule-based default unchanged | agent | open |
| R2 | assumption | OpenRouter reachable when `extraction_model` set | medium x medium | Router already has transport retries + 429 handling `[config.py:520-528]`; failure degrades to error, not crash | agent | open |
| R3 | question | Should LLM extraction be per-signal opt-in or global config? | — | Global config field (simpler); per-signal can be added later | operator | open |

**Pre-mortem top causes of failure:**
1. LLM extractor writes a fact that contradicts an incumbent → quarantined, not written (mitigated by existing contradiction detector).
2. OpenRouter key leaks into an exception → prevented by existing router secrecy contract.
3. Daemon drain e2e test flakes on timing → use polling with timeout, not fixed sleep.
4. New CLI/API surface fails mypy strict → write types carefully; mypy gate runs on every change.

---

## M. Decision log and discussion changelog

- **D1-D5** recorded in section G above.
- Plan created in `docs/memory-polish/plans/Plan1.md` on branch `memory-polish` at `4ac96db`.
- No implementation has begun yet; awaiting operator approval.

---

## N. Approval record

**Readiness checklist:** All items true (every blocking question answered, success criteria observable with tests, every change entry complete, before/after flows reference verified real symbols, alternatives compared including "do less", edge cases derived from this code, test list covers risks, rollback stated, assumptions have verification steps, cold-readable).

**Operator approval:** Pending. Reply `APPROVED` (optionally with changes) to begin implementation, or tell me what to revisit.

**Staleness rule:** If the repo changes the files listed in section F before work starts, re-verify sections C and F before acting.

---

## What this plan does NOT cover (deferred)

The following are explicitly out of scope for Plan 1 and remain for future work:

1. **The `paa memory` TUI/dashboard** (VISION Phase 3). This plan adds CLI + API surface only; a textual/interactive dashboard is deferred.
2. **Multi-session isolation** (ROADMAP 2.6). Session-scoped workspace overlays and cross-session query gating are not addressed.
3. **Self-improvement loops** (ROADMAP 2.5). Weekly reflection, friction scoring, anti-pattern extraction, and skill distillation are not addressed.
