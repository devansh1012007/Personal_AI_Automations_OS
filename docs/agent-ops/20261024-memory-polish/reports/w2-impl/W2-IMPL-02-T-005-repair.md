<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-02 | task: T-005 | status: DONE
code_commits: a8aad7a | report_commit: a8aad7a
files_changed: 1 (src/paa/runtime.py)
contracts_changed: none
tests: added 0, changed 0; scoped run: green
ownership_violations: none
deferred_dependencies: LLMFactExtractor class (T-003, next wave)
out_of_scope_findings: 0
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: LLMFactExtractor constructor signature (resolved lazily via getattr; T-003 will fill the class and its constructor)
confidence: high
last_comms_line_read: [2026-10-25T03:15:35Z] ORCH -> W2-IMPL-02 | DECISION | T-005 | FACT T-004 committed at cb79438 with extraction_model fields at config.py:258-264
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-02 — T-005 repair: wire extraction model into MemoryCreator construction

## 1. Task and acceptance criteria (restated)

T-005: Wire `settings.memory.extraction_model` / `settings.memory.extraction_model_provider` into `MemoryCreator` construction in `src/paa/runtime.py` (write-set limited to `src/paa/runtime.py`).

Acceptance criteria:
- `settings.memory.extraction_model` non-empty → `MemoryCreator` uses an LLM-backed extractor; empty (default `""`) → `RuleBasedExtractor` (unchanged behavior).
- No new APIs: use the existing `MemoryCreator(extractor=...)` parameter (creator.py:706).
- Do NOT add `LLMFactExtractor` (T-003, next wave) — only wire the config to the `extractor=` parameter.
- Commit only `src/paa/runtime.py` with trailers `Task: T-005`, `Agent: W2-IMPL-02`, `Wave: 2`, `Report: <path>`.
- Scoped checks: `ruff check src/paa/runtime` → 0; `mypy src/paa/runtime` → 0.

## 2. Verification that the problem exists

[VERIFIED src/paa/config.py:259-264] `MemorySettings.extraction_model: str = ""` and `extraction_model_provider: str = ""` exist (T-004, cb79438).
[VERIFIED src/paa/memory/creator.py:697-717] `MemoryCreator.__init__` accepts `extractor: FactExtractor | None = None` and defaults to `RuleBasedExtractor()` when `None`.
[VERIFIED src/paa/runtime.py] `Runtime.build()` constructed the agent heap but never built a `MemoryCreator`, so the extraction-model config flag had no consumer in the runtime composition root.
[RAN rg `LLMFactExtractor` src/paa → no matches] `LLMFactExtractor` does not exist (T-003, next wave); it cannot be imported or instantiated in this write-set.

## 3. Root cause

`T-004` added the config knobs but nothing reads them. `MemoryCreator` has an injectable `extractor` seam (the `FactExtractor` protocol at creator.py:239 exists precisely for this) but the runtime never constructs a `MemoryCreator` with that seam wired to `settings.memory.extraction_model`, so the opt-in LLM extraction path was unreachable from the runtime.

## 4. Approach chosen and why

- In `Runtime.build()`, after `_build_agents(...)`, build a `MemoryCreator` via a new `_build_memory_creator(settings, db, model_adapter)` helper and store it on `Runtime.memory_creator` (new dataclass field, typed `Any` to follow the existing `vector_store`/`graph_store`/`queue` convention).
- `_build_memory_creator` reads `settings.memory.extraction_model`:
  - empty → `extractor=None` → `MemoryCreator` falls back to `RuleBasedExtractor()` (unchanged behavior, zero cost/latency).
  - non-empty → resolves `LLMFactExtractor` lazily from `paa.memory.creator` via `getattr(..., "LLMFactExtractor", None)` and instantiates it with `model=model_adapter` (matching the `StrategicPlanner`/`CriticReviewer`/`TaskRouter` `model=` convention). If the class is absent (T-003 not yet landed) or construction fails, it degrades to `RuleBasedExtractor` with a logged warning — the runtime's degrade-don't-crash rule, so a configured-but-unimplemented flag never crashes boot.
- `LLMFactExtractor` is resolved lazily (function-scope import of `paa.memory.creator`) so there is no module-scope import graph change, no cycle, and no hard dependency on a class that does not yet exist. This keeps `ruff`/`mypy` green today and makes T-003 a drop-in: once `LLMFactExtractor` exists, the non-empty path uses it with no further runtime.py changes.
- `settings.memory.extraction_model_provider` is part of the same config knob and will be consumed by `LLMFactExtractor` (T-003); reading `extraction_model` as the opt-in flag is the operative wiring.

## 5. What changed (per file)

| File | Change | Why |
|------|--------|-----|
| src/paa/runtime.py | Added `Runtime.memory_creator: Any = None` field; build it in `build()` via `_build_memory_creator(settings, db, model_adapter)`; helper resolves `LLMFactExtractor` lazily when `settings.memory.extraction_model` is non-empty, else `None` (`RuleBasedExtractor` default) | Wires the extraction-model config to `MemoryCreator`'s `extractor=` parameter; only write-set file |

## 6. Blast radius and contracts

- `MemoryCreator` constructor unchanged; `FactExtractor` protocol unchanged; no new public APIs.
- `daemon.py`/`api/app.py`/`cli/main.py`/`coder/runtime_tools.py` still construct their own `MemoryCreator(db)` (default rule-based) — out of this write-set; they can migrate to `rt.memory_creator` when T-003 lands.
- Backward compatible: default `""` preserves rule-based extraction exactly. No schema/persistence change.
- Import-conventions lock (`tests/conventions/test_import_conventions.py`) still green — `paa.memory.creator` is imported lazily inside a function, matching the `_try_vector`/`_try_graph`/`_try_queue` pattern, no module-scope import added to `paa.runtime`.

## 7. Tests

No test files added or changed (write-set is `src/paa/runtime.py` only). Existing suites green:
- `pytest tests/daemon/test_drain_e2e.py tests/memory/test_creator.py tests/memory/test_llm_extractor.py -q` → 20 passed.
- `pytest tests/conventions/test_import_conventions.py -q` → 19 passed.
- Scoped gate: `ruff check src/paa/runtime.py` → 0 findings; `mypy src/paa/runtime.py` → 0 issues.

The `LLMFactExtractor` path is not directly exercised yet because the class does not exist (T-003). It will be exercised once T-003 adds the class; the lazy `getattr` resolution means no runtime.py change is needed then.

## 8. Edge cases and abuse scenarios

- `extraction_model` set but `LLMFactExtractor` absent → degrades to `RuleBasedExtractor`, logs `runtime.llm_extractor_unavailable`, boot continues.
- `extraction_model` empty → `RuleBasedExtractor`, unchanged.
- Constructor failure of `LLMFactExtractor` → caught, logged, degrades to rule-based (never crashes boot).

## 9. Long-run test

- Source: `get_settings().memory.extraction_model` readable; default `""` → rule-based (verified).
- Concurrency: single `MemoryCreator` built per runtime; per-signal isolation is creator's existing `_commit` transaction behavior.
- Rollback: revert the `memory_creator` field + build call + helper; returns to no runtime MemoryCreator (pre-T-005 state).

## 10. Deviations from the plan/brief; ownership issues; deferred dependencies

- Deviation (documented): Plan1 C05 suggests passing `extract_model`/`extract_provider` to `MemoryCreator`; the constructor actually takes `extractor=`, so the config flag is converted into an extractor instance rather than passed raw. `LLMFactExtractor` constructor signature is assumed (`model=`) per codebase convention and is not verified until T-003.
- Deferred dependency: `LLMFactExtractor` class (T-003, next wave) — until it lands, non-empty `extraction_model` degrades to rule-based rather than crashing.
- No ownership violations: write-set limited to `src/paa/runtime.py`; `config.py`/`creator.py` untouched.

## 11. Out-of-scope findings

None.

## 12. Known limitations, risks, rollback notes

- `LLMFactExtractor` constructor signature not verified (class absent); lazy resolution defers the failure to T-003's integration.
- `extraction_model_provider` read as part of the same knob but not passed anywhere yet (consumed by `LLMFactExtractor` at T-003).
- No secrets/PII in code or report.

## 13. Where I am least confident / what the reviewer should scrutinize

- The assumed `LLMFactExtractor(model=model_adapter)` constructor — T-003 should confirm or adjust; the lazy `getattr` means a mismatch only surfaces when the flag is non-empty and the class exists.
- Whether `paa.memory.creator` should be a module-scope import rather than function-scope (kept lazy to avoid import-graph churn and match the optional-backend pattern).

## 14. Self-review checklist

- [x] Root cause, not symptom
- [x] No band-aid patterns
- [x] Only write-set files changed (`src/paa/runtime.py`)
- [x] No secrets/PII
- [x] Scoped tests green
- [x] ruff/mypy green on changed files
- [x] Commit contains only `src/paa/runtime.py`; trailers present

## 15. Commits

code: a8aad7a | report: a8aad7a
