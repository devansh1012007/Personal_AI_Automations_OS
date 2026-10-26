# ORCH-DIGEST — FINAL operability review

Branch: `memory-polish` (HEAD `7568bcb`), base `4ac96db`.
Scope reviewed: full diff `git diff 4ac96db..HEAD -- src/ tests/` (2215 lines, 13 files, ~+1970/-5), plus all agent reports under `docs/agent-ops/20261024-memory-polish/reports/{w0,w1,w2,w3}` and `00-mission.md`, `COMMS.md`.
Waves covered: W1 (CLI group + 4 API routes), W2 (config fields, `LLMFactExtractor`, runtime wiring, e2e drain test), W3 (optional LLM summarizer in compaction, optional LLM ranker in gatherer).

Baseline gate at branch point was green (ruff 0, mypy 0, 2907 passed). The branch is additive, well-tested, and locally green on the scoped runs the agents performed. It is **not** safe to merge as-is: there is a BLOCKER-level secret-handling defect in the LLM extraction error path, plus operability gaps (the configured LLM extractor is wired but never exposed; the W3 LLM seams are unreachable).

Verdict: **APPROVE-WITH-CHANGES** — conditional on fixing the BLOCKER and the MAJOR exposure issues before merge.

---

# Findings

## BLOCKER — Unredacted model exceptions flow into logs, signal `error_detail`, and potentially the ledger, leaking the OpenRouter key

**Files:** `src/paa/memory/creator.py:661-666`; `src/paa/memory/creator.py:863`; `src/paa/memory/creator.py:1257-1269` (`_quarantine_malformed`); `src/paa/runtime.py:777-778`

**Evidence:**

`LLMFactExtractor.extract` catches every model failure broadly and interpolates the raw exception into a `PaaError`:

```python
try:
    response = await self._model.complete_structured(prompt, _FACT_TRIPLE_SCHEMA)
except Exception as exc:  # model failure quarantines the signal
    raise MalformedSignalError(
        f"LLM fact extraction failed: {exc}", signal_id=envelope.id
    ) from exc
```

`MalformedSignalError` extends `PaaError`, whose `to_payload()` writes `message` into a ledger event payload verbatim (`src/paa/core/errors.py:53-55`). `process_signal` then calls `_quarantine_malformed(envelope, str(exc))` (creator.py:863), which:

- writes `error_detail = reason[:2000]` to `cold_lake_signals` (creator.py:1264),
- logs `log.warning("memory.signal_malformed", signal_id=..., reason=reason)` (creator.py:1268),
- archives the full raw payload to `cold_lake_artifacts_archive.payload_content`.

Separately, `runtime.py:_build_memory_creator` logs `log.warning("runtime.llm_extractor_unavailable", error=str(exc))` (runtime.py:778).

**Why it matters:** `src/paa/models/base.py` states the governing rule explicitly: *"An API key must never reach a log line, a ledger payload or an exception message,"* and `StructuredOutputError` exists precisely to scrub provider output for this reason. `LLMFactExtractor` violates it by interpolating unredacted `str(exc)`. The extractor is configured with an OpenRouter model via `PAA_MEMORY__EXTRACTION_MODEL`; if the provider/router raises a 401/429/transport error whose message echoes the key, the key is permanently recorded in a log line, a DB column, and possibly the ledger. This is a direct violation of a stated security invariant. The agent report W2-IMPL-03 §10 ("No secrets in prompts/logs — OpenRouter key never enters this class") is **incorrect as stated**: the key never enters the class as input, but `str(exc)` can carry it out as output.

**Recommended fix:** Scrub the exception before interpolating it, e.g. `from paa.models.base import redact` and `raise MalformedSignalError(f"LLM fact extraction failed: {redact(str(exc))}", ...)`. Apply the same redaction to `runtime.py:_build_memory_creator`'s `error=str(exc)` log. Do not log `prompt` or `envelope.raw` (currently not logged — keep it that way).

---

## MAJOR — LLM extraction is wired into `Runtime.memory_creator` but exposed nowhere an operator can use

**Files:** `src/paa/runtime.py:739-779` (wiring); `src/paa/cli/main.py:318-357` (drain command); `src/paa/api/app.py:86-93` (`/memory/drain`); `src/paa/daemon.py:170-179` (`_memory_creator`)

**Evidence:** `Runtime.build()` builds `memory_creator = _build_memory_creator(settings, db, model_adapter)` and stores it on the runtime (runtime.py:739, 747). `_build_memory_creator` returns a `MemoryCreator` whose extractor is `LLMFactExtractor` when `settings.memory.extraction_model` is non-empty (runtime.py:772-779). However:

- CLI `paa memory drain` builds `MemoryCreator(rt.db)` — rule-based, no extractor (cli/main.py:329-332).
- API `POST /memory/drain` builds `MemoryCreator(runtime.db)` — rule-based (app.py:91-92).
- `daemon.py:_memory_creator()` returns `MemoryCreator(self._rt.db)` with no extractor (daemon.py:179).

**Why it matters:** Setting `PAA_MEMORY__EXTRACTION_MODEL` has **zero effect** on every drain path an operator actually invokes (CLI, API, live daemon). The feature flag exists, the runtime wires it, but nothing consumes `rt.memory_creator`. Operators cannot opt in to LLM extraction through any surface, cannot observe whether it is active, and cannot roll it back via a visible toggle because the toggle appears to do nothing. W2-IMPL-02 §6 acknowledges this ("they can migrate to `rt.memory_creator` when T-003 lands") but the migration never happened.

**Recommended fix:** Make the CLI drain command, the `/memory/drain` route, and `daemon.py:_memory_creator` use `rt.memory_creator` when present (falling back to `MemoryCreator(db)` for safety), or document that the flag only affects programmatic `MemoryCreator` construction. Add a `paa memory doctor`-style visibility (or log at startup) showing whether the LLM extractor is active, so the flag is observable and rollback-able (set `extraction_model=""`).

---

## MAJOR — W3 LLM summarizer/ranker seams are unreachable from the operator (dead opt-in paths)

**Files:** `src/paa/context/compaction.py:98-106` (`ContextCompactor.summarizer`); `src/paa/context/gatherer.py:542-574` (`BoundedContextGatherer.ranker`)

**Evidence:** Both accept an optional callable (`summarizer: Callable[[str], str] | None`, `ranker: Ranker | None`) defaulting to `None`, and both preserve the mechanical path byte-for-byte when absent. Tests confirm exactly-once invocation and fail-loud propagation. There is **no config field, no CLI flag, no API route, and no runtime construction** that passes a non-None summarizer or ranker. `ContextCompactor` and `BoundedContextGatherer` are instantiated elsewhere with defaults only.

**Why it matters:** The W3 feature ("optional LLM summarizer/ranker") cannot be activated by an operator at all. It is opt-in in code but there is no opt-in mechanism in the product. This is an operability gap: the surface exists, is tested, but is unreachable, so it neither delivers value nor can be rolled back (there is nothing to turn off). Cross-wave with W2: the same "flagged but unexposed" problem as the extraction model.

**Recommended fix:** Either (a) add config/CLI wiring so an operator can inject a summarizer/ranker (e.g. via `MemorySettings` or context settings, constructed through `Runtime`), or (b) document that these are programmatic-only seams for embedders and not operator features. If (a), add startup observability (log when a summarizer/ranker is active) and a rollback path (clear the config).

---

## MAJOR — Scale: LLM extractor invoked per signal, sequentially, with no batching, no timeout, no retry

**Files:** `src/paa/memory/creator.py:650-710` (`LLMFactExtractor.extract`); `src/paa/memory/creator.py:910-951` (`run_batch`)

**Evidence:** `run_batch` is a sequential `for envelope in await self._claim(limit): ... await self.process_signal(envelope)` loop (creator.py:931-933). Each signal triggers exactly one `complete_structured` call. At 100x signals/traffic that is 100x sequential model calls, each blocking its signal. There is no per-call timeout on `complete_structured`, so a slow/hung provider stalls the whole batch; there is no extractor-level retry or backoff for transient 429s (reliance on the router's internal retries is implicit and unverified at this seam); there is no batching of multiple signals into a single model call.

**Why it matters:** Cost and latency scale linearly with signal volume; a single slow model call stalls the ingestion batch (head-of-line blocking); transient provider failures have no backoff at this layer, so `PAA_MODELS__EXTRACTION_MODEL` unreachable → every signal quarantined (`MalformedSignalError`) rather than retried.

**Recommended fix (DO NOW):** Add a timeout to `complete_structured` and surface a configurable retry/backoff budget (honor `ModelSettings.max_transport_retries` per AGENTS.md model-layer convention) so transient 429s are absorbed instead of quarantining every signal. (DO LATER): evaluate batching multiple signal raws into one structured call only if the schema/latency trade-off is acceptable; do not batch without a deterministic merge strategy.

---

## MAJOR — Scale: LLM ranker invoked per `compile()` with no caching

**Files:** `src/paa/context/gatherer.py:573-574`

**Evidence:** `_compile()` calls `self.ranker((pool, required, invariant_floor))` exactly once per `compile()`/`compile_worker_packet()` (gatherer.py:573-574). There is no caching of ranker outputs, no memoization by input hash, and no TTL. At 100x compiles that is 100x ranker calls even for identical pools.

**Why it matters:** The ranker is the LLM-backed path; calling it on every compile with no cache makes context building linearly more expensive in model traffic as compile frequency grows, with no deduplication for repeated identical candidate pools.

**Recommended fix:** Add optional input-hash memoization (bounded TTL cache) keyed on `(pool, required_slots, invariant_importance)` so repeated identical compiles reuse the ranker output. Keep it opt-in and off by default to preserve the deterministic mechanical default.

---

## MINOR — Config env-var prefix mismatch with the mission brief

**Files:** `src/paa/config.py:258-263`

**Evidence:** The brief specifies `PAA_MODELS__EXTRACTION_MODEL` / `PAA_MODELS__EXTRACTION_PROVIDER`. pydantic-settings (`env_prefix="PAA_"`, `env_nested_delimiter="__"`, `MemorySettings` nested under `Settings`) maps these to `PAA_MEMORY__EXTRACTION_MODEL` / `PAA_MEMORY__EXTRACTION_PROVIDER`. W2-IMPL-01 §12 documents this deviation; W2-IMPL-02's NEED assumed `extraction_model`/`extraction_model_provider` names, which match the actual fields.

**Why it matters:** Operators following the brief's `PAA_MODELS__*` spelling will find the flag has no effect (wrong namespace), causing silent fallback to rule-based extraction and confusion about whether LLM extraction is on.

**Recommended fix:** Document the effective env names (`PAA_MEMORY__EXTRACTION_MODEL/_PROVIDER`) in operator-facing docs, or add a custom `settings_customise_sources` to honor `PAA_MODELS__*` if the brief's spelling is required.

---

## MINOR — No observability of LLM extraction volume/latency/cost

**Files:** `src/paa/memory/creator.py:625-710`

**Evidence:** `LLMFactExtractor` emits no log lines or metrics for successful calls, only the error path (which logs unredacted `str(exc)` — see BLOCKER). Operators cannot see how many extractions ran, how long they took, how many were quarantined vs. ignored, or token/cost estimates.

**Why it matters:** Without logs/metrics, an operator running LLM extraction at scale cannot detect cost spikes, latency regressions, or a provider silently returning empty results (which yields `IGNORED`, not an error).

**Recommended fix:** Add structured log lines (`memory.llm_extraction_called`, `memory.llm_extraction_completed/failed`) keyed by signal_id with outcome and latency; do not log the prompt or raw signal text.

---

## NIT — `extraction_model_provider` is read but never passed to the extractor

**Files:** `src/paa/runtime.py:770-779`; `src/paa/memory/creator.py:637-639`

**Evidence:** `_build_memory_creator` reads `settings.memory.extraction_model` as the opt-in flag but never passes `extraction_model_provider` anywhere (runtime.py:776 instantiates `LLMFactExtractor(model=model_adapter)` only). `LLMFactExtractor.__init__` accepts `model:` only. W2-IMPL-02 §98 acknowledges: "`extraction_model_provider` read as part of the same knob but not passed anywhere yet."

**Why it matters:** The provider field is configured but inert; changing it has no effect, which is misleading.

**Recommended fix:** Either thread the provider into `LLMFactExtractor` (so it can resolve the right model/transport) or document that the provider field is reserved for future use and currently ignored.

---

# Audit of agent-report claims

| Claim | Report | Verdict |
|---|---|---|
| "No secrets in prompts/logs — OpenRouter key never enters this class" | W2-IMPL-03 §10 | **Overstated/incorrect** — key doesn't enter as input, but `str(exc)` can carry it out via `MalformedSignalError` → log + `error_detail` + possible ledger (BLOCKER above). |
| "degrades to rule-based, logs `runtime.llm_extractor_unavailable`, boot continues" | W2-IMPL-02 §8 | **True** for availability, but the log line interpolates unredacted `str(exc)` (BLOCKER-adjacent). |
| "`paa memory drain` … `MemoryCreator(rt.db)`" | W1-IMPL-01 §5 | **True** — and this is exactly why the wired `rt.memory_creator` is unused (MAJOR exposure gap). |
| W3 summarizer/ranker "opt-in … default-off … observable via constructor param" | W3-IMPL-01 §8 / W3-IMPL-02 §8 | **True for programmatic use, false for operator use** — there is no constructor call site passing a non-None value (MAJOR unreachable-seam finding). |
| Backpressure: all 4 routes apply `_admit` → 429 | W1-IMPL-02 §4 | **True** (diff: app.py:17,37,58,89). |
| Signals SQL uses bound params + LIMIT 50 + ORDER BY | W1-IMPL-02 §10 | **True** (app.py:60-72). |
| CLI signals `IN (...)` bound params | W1-IMPL-01 §9 | **True** (cli/main.py:280-281). |
| world-model uses `BeliefDocument` enum (no path traversal) | W1-IMPL-01 §10 | **True** (cli/main.py:372-375). |
| `LLMFactExtractor` containment before model call; NUL/ANSI/marker refused | W2-IMPL-03 §9 | **True** (creator.py:650 `_reject_suspicious` before `complete_structured`). |

---

# Data migration / backward compatibility

- No schema changes; `tests/storage/test_schema_parity.py` unaffected (no DDL diff). ✓
- Default `extraction_model=""` preserves rule-based extraction exactly; backward compatible. ✓
- W3 defaults (`summarizer=None`, `ranker=None`) preserve mechanical behavior byte-for-byte; existing `test_compaction.py`/`test_gatherer.py` unchanged and green. ✓
- New facts carry `provenance.extractor="llm"` and confidence 0.70 (< rule-based `_CONFIDENCE_TRIPLE` 0.95), so LLM facts lose contradiction scoring to attested facts — intended, documented in creator.py:618-622. ✓

---

# Optimization opportunities

| Opportunity | Category | Recommendation |
|---|---|---|
| Redact `str(exc)` in `LLMFactExtractor` + `runtime.py` log | DO NOW | Security BLOCKER — fix before merge. |
| Expose `rt.memory_creator` to CLI drain / `/memory/drain` / daemon | DO NOW | Without this the LLM flag is a no-op for operators (MAJOR). |
| Timeout + retry/backoff on `complete_structured` | DO NOW | Prevents hung batches and unnecessary quarantine on transient 429 (MAJOR). |
| Operator-visible flag state (startup log / doctor) | DO NOW | Makes the opt-in observable and rollback-able. |
| Document effective env names (`PAA_MEMORY__*`) | DO LATER | Resolves the prefix mismatch (MINOR). |
| Memoize ranker by input hash (bounded TTL) | DO LATER | Cuts repeat ranker calls at scale (MAJOR). |
| Add LLM extraction logs/metrics (no prompt/raw) | DO LATER | Observability for cost/latency (MINOR). |
| Batch multiple signals into one model call | NOT WORTH IT | Requires a deterministic merge strategy; schema is per-signal triples. Not worth the complexity now. |
| Thread `extraction_model_provider` into `LLMFactExtractor` | NOT WORTH IT | Currently inert; only wire if a provider-resolution need emerges (NIT). |

---

# Verdict

**APPROVE-WITH-CHANGES**

The branch is well-engineered, additive, locally green on the agents' scoped gates, and the security/containment design (C0/NUL/ANSI/marker refusal before inference, one-transaction-per-signal, defensive triple parsing, fail-loud injected callables) is sound. However, the unredacted exception interpolation in the LLM extraction error path directly violates the project's stated no-secrets-in-logs/ledger rule and can leak the OpenRouter key — that must be fixed before merge. The exposure gaps (wired-but-unused LLM extractor; unreachable W3 seams) mean the flagship opt-in features cannot actually be activated or observed by an operator, which is an operability defect rather than a code defect. Fix the BLOCKER and the MAJOR findings, re-run `ruff check src tests`, `mypy src`, and the full pytest gate, then the branch is mergeable.

No source files were modified by this review; this report is read-only output.
