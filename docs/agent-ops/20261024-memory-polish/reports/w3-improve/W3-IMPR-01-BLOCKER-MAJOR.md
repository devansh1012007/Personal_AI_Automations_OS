# W3-IMPR-01 — BLOCKER (redaction) + MAJOR (timeout/retry) · improvement mini-wave · run 20261024-memory-polish

## ORCH-DIGEST

Agent: W3-IMPR-01 (improver) | Task: IMPR-01 | Branch: `memory-polish`
Write-set: `src/paa/memory/creator.py`, `src/paa/runtime.py`, `tests/memory/test_llm_extractor.py`
Status: FIXED (BLOCKER) + FIXED (MAJOR)
Code commit: baf5834 | Report commit: <PENDING — this commit>
Scoped gate: `pytest tests/memory/test_llm_extractor.py -q` → 29 passed; `ruff check src/paa/memory/creator.py src/paa/runtime.py tests/memory/test_llm_extractor.py` → 0 findings; `mypy src/paa/memory/creator.py src/paa/runtime.py` → 0 errors; `pytest tests/conventions/test_import_conventions.py -q` → 19 passed.
Blockers: none. No out-of-write-set files touched. COMMS START line appended; no NEED posted (all required symbols exist in write-set or read-only deps).

---

## Summary

Fixed two findings from the final operability review (FINAL-operability.md §BLOCKER and §MAJOR-scale) on branch `memory-polish`:

1. **BLOCKER — unredacted model exceptions leak to logs/ledger.** `LLMFactExtractor.extract` interpolated raw `str(exc)` into `MalformedSignalError`, which flows through `PaaError.to_payload()` into the ledger payload verbatim, into `cold_lake_signals.error_detail` via `_quarantine_malformed`, and into the `memory.signal_malformed` log line. A provider error echoing the OpenRouter key therefore persists the key in a log line, a DB column, and possibly the ledger — violating the model-layer rule ("an API key must never reach a log line, a ledger payload or an exception message", `paa/models/base.py:25`). Fixed by redacting via `paa.models.base.redact()` at both raise sites.
2. **MAJOR (scale) — `complete_structured` had no timeout or retry.** A slow/hung provider stalled the whole batch (head-of-line blocking) and transient 429s quarantined every signal instead of being absorbed. Added a per-call `asyncio.wait_for` timeout plus bounded exponential backoff honoring `ModelSettings.max_transport_retries`; on exhaustion the signal is quarantined via `MalformedSignalError` (not a crash).

## Changes

### `src/paa/memory/creator.py`

- Imported `ModelUnavailableError, redact` from `paa.models.base` (sorted per isort/I001).
- `LLMFactExtractor.__init__` gained keyword-only knobs `max_retries=0`, `timeout=None`, `retry_backoff_base=0.5`, `retry_backoff_cap=5.0` — defaults preserve the previous fail-fast behaviour for every existing caller; `max_retries=0` means no retry, `timeout=None` means no `wait_for`.
- `LLMFactExtractor.extract` now awaits `self._complete_structured_with_retry(prompt)` and redacts the exception: `f"LLM fact extraction failed: {redact(str(exc))}"`. No unredacted `str(exc)` reaches `MalformedSignalError`, `error_detail`, or a log line.
- Added `_is_transient(exc)` — retryable when it is a `TimeoutError` or a `ModelUnavailableError` whose `status_code` detail is `408`, `429`, or `>= 500` (the same transient set as `paa/models/_http.py`).
- Added `_complete_structured_with_retry(prompt)` — per-call `asyncio.wait_for` timeout when `timeout` is set; transient failures retried with capped exponential backoff up to `max_retries` additional attempts; non-transient failures raised immediately; exhaustion re-raises the last exception so `extract` quarantines the signal redacted instead of crashing the batch.

### `src/paa/runtime.py`

- Imported `redact` from `paa.models.base`.
- `_build_memory_creator`: `log.warning("runtime.llm_extractor_unavailable", error=redact(str(exc)))` — the degradation log no longer carries unredacted exception text.
- `_build_memory_creator` now threads `max_retries=settings.models.max_transport_retries` and `timeout=settings.models.request_timeout_seconds` into `LLMFactExtractor(...)`, so the configured transport-retry budget and per-call timeout govern extraction.

### `tests/memory/test_llm_extractor.py`

Added 5 tests to `TestLLMFactExtractor`:
- `test_model_exception_is_redacted_before_it_leaks` — a provider-echoed OpenRouter key (`sk-or-v1-...`) in a `ModelUnavailableError` message never appears in `result.error` (and hence not in `error_detail` or the malformed log line).
- `test_transient_timeout_is_retried_then_succeeds` — a `TimeoutError` on the first attempt is retried once and the second call answers → processed, 2 model calls.
- `test_transient_429_exhaustion_is_quarantined_redacted` — a 429 is retried up to `max_retries` (3 calls total) then quarantined with a redacted message (no key, no crash).
- `test_per_call_timeout_is_bounded` — a hung model (30s sleep) is bounded by a 0.1s per-call timeout → quarantined quickly; the whole test is wrapped in `asyncio.timeout(2.0)` so it cannot hang.
- `test_non_transient_error_is_not_retried` — a `ValueError` is quarantined on the first attempt (1 call, no retry).

## Verification (scoped only — full suite not run per standing orders)

- `pytest tests/memory/test_llm_extractor.py -q` → 29 passed [RAN .venv/bin/python -m pytest tests/memory/test_llm_extractor.py -q -> exit 0]
- `ruff check src/paa/memory/creator.py src/paa/runtime.py tests/memory/test_llm_extractor.py` → 0 findings [RAN -> exit 0]
- `mypy src/paa/memory/creator.py src/paa/runtime.py` → 0 errors [RAN -> exit 0]
- `pytest tests/conventions/test_import_conventions.py -q` → 19 passed [RAN -> exit 0] (validates the new module-scope import does not break the import sweep)

## Root cause vs band-aid

The redaction is at the raise site (`extract`) and at the runtime degradation log — the two places model-exception text enters `MalformedSignalError`/logs/`error_detail` — not a downstream scrub. The timeout/retry is at the `complete_structured` seam itself (the actual blocking call), honoring the existing `max_transport_retries` budget, so transient failures are absorbed at the source rather than quarantining every signal. The rule-based default path is untouched (`max_retries=0`, `timeout=None` when the extractor is built without the runtime wiring), so default behaviour is byte-for-byte unchanged.

## Reversal test

- Reverting the redaction (`redact(str(exc))` → `str(exc)` in both places) lets a provider-echoed key reach `MalformedSignalError.reason`, `error_detail`, and the `memory.signal_malformed` / `runtime.llm_extractor_unavailable` log lines — `test_model_exception_is_redacted_before_it_leaks` fails.
- Removing `wait_for`/retry lets a hung provider stall the batch indefinitely and a transient 429 quarantine on the first attempt — `test_per_call_timeout_is_bounded` / `test_transient_429_exhaustion_is_quarantined_redacted` / `test_transient_timeout_is_retried_then_succeeds` fail.

## Out-of-scope findings (reported, not fixed)

None raised a NEED; all required symbols (`redact`, `ModelSettings.max_transport_retries`, `ModelUnavailableError`) exist in read-only deps and were used as-is. No batching added (reviewer-marked NOT WORTH IT); no changes to CLI/API/daemon drain paths (IMPR-02's write-set); `extraction_model_provider` left inert (NIT, NOT WORTH IT).

## COMMS record

Last line read at start: line 60 (`ORCH -> ALL | DECISION | T-000 | WAVE-IMPR-START ...`). Appended START line at `2026-10-25T11:12:12Z`.
