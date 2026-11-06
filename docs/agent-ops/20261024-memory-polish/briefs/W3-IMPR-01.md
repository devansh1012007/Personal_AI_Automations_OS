# Brief — W3-IMPR-01 (improver) · wave-impr · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Fix the BLOCKER security defect and a MAJOR scale defect in `LLMFactExtractor` (creator.py) and `runtime.py`. The BLOCKER: unredacted `str(exc)` from model failures flows into `MalformedSignalError` → ledger/logs, potentially leaking the OpenRouter API key. The MAJOR: `complete_structured` has no timeout/retry, so transient 429s quarantine every signal and a hung provider stalls the whole batch. Your role: improver.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions)
4. docs/agent-ops/20261024-memory-polish/reports/final/FINAL-operability.md (BLOCKER + MAJOR scale findings, full detail)
5. docs/agent-ops/20261024-memory-polish/reports/final/FINAL-correctness.md (cross-check)
6. docs/agent-ops/20261024-memory-polish/templates/report-improvement.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from last WAVE-IMPR-START)
8. Source entry points:
   - src/paa/memory/creator.py:650-710 (LLMFactExtractor class, extract method ~661-666, quarantine path ~863, _quarantine_malformed ~1257-1269)
   - src/paa/memory/creator.py:1167-1181 (_reject_suspicious), :1235-1254 (_scalar_to_text)
   - src/paa/runtime.py:427-454 (_build_memory_creator, error=str(exc) log at ~778)
   - src/paa/models/base.py (redact function — check existence/signature), :53-55 (PaaError.to_payload)
   - src/paa/config.py:520-528 (ModelSettings.max_transport_retries — AGENTS.md model-layer convention)

## 3. Task and acceptance criteria
Fix these findings:

**BLOCKER (must fix before merge):** Unredacted model exceptions leak to logs/ledger
- In `LLMFactExtractor.extract`, replace `f"LLM fact extraction failed: {exc}"` with a redacted version using `paa.models.base.redact(str(exc))` (or equivalent redaction)
- In `runtime.py:_build_memory_creator`, replace `error=str(exc)` with `error=redact(str(exc))` in the `log.warning("runtime.llm_extractor_unavailable", ...)` call
- Verify: no `str(exc)` interpolation of model exceptions reaches `MalformedSignalError`, logs, or `error_detail`

**MAJOR (scale):** Timeout + retry/backoff on `complete_structured`
- Add a per-call timeout to `self._model.complete_structured(...)` (use `asyncio.wait_for` or equivalent)
- Add retry/backoff honoring `ModelSettings.max_transport_retries` for transient failures (429, 5xx, timeouts)
- On exhausted retries, raise `MalformedSignalError` (signal quarantined, not crash)
- Verify: tests still pass; redact path unchanged

Acceptance criteria (measurable):
- `LLMFactExtractor.extract` never interpolates unredacted exception text into `MalformedSignalError`
- `runtime.py:_build_memory_creator` log never contains unredacted exception text
- `complete_structured` call has a bounded timeout
- Transient failures are retried with backoff up to `max_transport_retries`
- After retry exhaustion, `MalformedSignalError` is raised (signal quarantined)
- Scoped: pytest tests/memory/test_llm_extractor.py -q green; ruff check src/paa/memory src/paa/runtime → 0; mypy src/paa/memory src/paa/runtime → 0

## 4. Ownership
WRITE-SET (may edit): src/paa/memory/creator.py, src/paa/runtime.py
READ-ONLY: src/paa/models/base.py (read redact function), src/paa/config.py (read max_transport_retries)
FORBIDDEN: src/paa/cli/main.py, src/paa/api/app.py, src/paa/daemon.py (IMPR-02's files)
Need something outside? Post NEED in COMMS.md.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W3-IMPR-02 | IMPR-02 (expose rt.memory_creator) | src/paa/cli/main.py, src/paa/api/app.py, src/paa/daemon.py | Drain paths use rt.memory_creator |

Known interactions: None — disjoint files. You cannot see IMPR-02's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — these are new findings from final review.
False leads: Do NOT redact the prompt or envelope.raw (they are not logged — keep it that way). Do NOT add batching (reviewer marked NOT WORTH IT). Do NOT change the rule-based default path.
Flaky/pre-existing failures: None in tests/memory.
Gotchas: Check that `paa.models.base.redact` exists and has the right signature before using it. If it doesn't exist, implement a simple redaction inline (e.g., strip API key patterns) and document why.

## 7. Environment and tests
Scoped commands: pytest tests/memory/test_llm_extractor.py -q; ruff check src/paa/memory src/paa/runtime; mypy src/paa/memory src/paa/runtime
Do NOT: run full suite, restart containers, shared-DB migrations, fixed ports.
Unique test resources: tmp_path fixtures; no DB needed for redaction/timeout unit logic.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w3-improve/W3-IMPR-01-BLOCKER-MAJOR.md (ORCH-DIGEST first)
Commits: code commit first (trailers: Task: IMPR-01, Agent: W3-IMPR-01, Wave: impr, Report: <path>), then report commit
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (improver)
- Address every BLOCKER/MAJOR finding: FIXED or REJECTED-with-evidence or DEFERRED-with-reason.
- Improve code AND tests (add redaction test, timeout test).
- No band-aids; fix at source.
- Start report file early and append as you work.
