# W5-IMPL-03 — T-007b closer (P007b): schedule allocator loop, name producer, fill attention_top, emit DRAIN_THROTTLED

## ORCH-DIGEST

- **Status:** COMPLETE (code+tests committed; this report docs-committed after) — **PARTIAL on production activation:** the `DRAIN_THROTTLED` emit is implemented and tested creator-side, but the live wire needs one line in `runtime.py::_build_memory_creator` (READ-ONLY for this task; NEED posted in COMMS — see §6). Until that line lands, production creators stay log-only by design (safe fallback, no fake liveness).
- **Task:** T-007b-P007b (`PLAN:integration-fix:P007`). (a) daemon allocation tick mirroring the reflection loop (supervised, `allocator.enabled`-gated, 60s cadence, curation-lock-ordered after the maintenance prune); (b) producer decision **OPERATOR-FED** (no production enqueue to `workers:alloc` exists — verified by grep [VERIFIED src/paa: `WORKERS_ALLOC` enqueue only in `storage/queue/__init__.py` docstring example]); (c) `attention_top` filled from `ATTENTION_ALLOCATED` ledger events (score-desc, `max_results` bound, empty → honest null); (d) `DRAIN_THROTTLED` emitted on breach only, full ADR-0008 key, log-only when no ledger handle.
- **Disabled default = inert:** `AllocatorSettings.enabled=False` → tick returns `None` before touching the orchestrator/queue (test-pinned).
- **Commits:** code+tests commit SHA recorded below (docs commit follows listing it).
- **Baseline failures (not mine):** `tests/tui/test_dashboard.py::test_ledger_with_events` — pre-existing HEAD typo `"%H:%:%S"` in `src/paa/tui/dashboard.py:347` [VERIFIED via `git show HEAD:...`], file outside write-set → COMMS FINDING.

## 1. Root cause & context

R-W4-002: `ChiefOrchestrator.run_allocation` callable-only, no scheduler — the P-013 disease. R-W4-006: `DRAIN_THROTTLED` vocab+replay shipped, emitter unowned; creator holds no ledger handle. REV-W4b §8: `attention_top` honestly null until this task. Affected: `daemon.py` (5th loop), `tui/runner.py` (`build_panel_fields`), `memory/creator.py` (throttle sites + optional handle), `context/allocator.py` (docstring only). Invariants preserved: ledger-before-side-effect (emit before notify unchanged), claim-failure → `underfed` (R-W4-007, untouched), at-most-once notify, empty → honest null, `EventType` set unchanged.

## 2. Phased changes

### Phase 1 — Daemon allocation tick (`src/paa/daemon.py`)
- New `_allocation_loop` + `_run_allocation_tick`, mirroring `_reflection_loop`: supervised via `_supervise("allocation", ...)`, 5th task in `start()`; tick gated by `settings.allocator.enabled` (early `None` return, queue untouched); `async with self._curation_lock` around `orchestrator.run_allocation()` so the pass is serialized after the maintenance prune exactly like reflection (never parallel); result logged as `daemon.allocation_completed` (outcome + counts), tick errors propagate to the loop which logs `daemon.allocation_failed` and keeps the daemon alive.
- `allocation_interval=60.0` constructor kwarg (DECISION: between the 1s ingestion cadence and the 1h reflection poll; advisory-only loop, a hot claim cadence would be waste).
- Module docstring loop list updated (item 6). No other daemon code touched.

### Phase 2 — `attention_top` fill (`src/paa/tui/runner.py`)
- New `_attention_top(settings, rt)`: scans open lineages (bounded `_PANEL_SCAN_LIMIT`, same shape as `_decision_tracker`), reads raw events via `read_correlation` (projection folds to nothing — the decision IS the record), filters `EventType.ATTENTION_ALLOCATED`, validates payload (`item_id`/`score` numeric), ranks score-desc/`item_id`-asc (the allocator's total order), bounds by `allocator.max_results` (getattr chain, default 5). Empty → `None` (honest-null preserved; W3 pins at `test_runner_panels.py:94` and `test_dashboard.py:339` still pass).
- Wired into `build_panel_fields` under its own try/except per-panel isolation (`tui.panel_failed`, `attention_top`). Rendered by the existing `_panel_attention` list-of-dicts branch (list order = rank) — no dashboard change.

### Phase 3 — `DRAIN_THROTTLED` emitter (`src/paa/memory/creator.py`)
- `MemoryCreator.__init__` gains optional keyword-only `ledger: _LedgerAppender | None = None` (structural Protocol mirroring the allocator seam; every existing construction — runtime, api, coder, daemon fallback — keeps working unchanged). No handle → log-only, exactly the pre-emit behaviour.
- `_emit_drain_throttled_event` (end-of-file free function, respecting the placement convention): emits only at the two throttle log sites (full defer and partial cap), payload `{fact, threshold, drain_max_per_second, indexed_this_window, window_seconds, limit, effective_limit, reason}` — mirrors the tier-throttle `{fact/threshold}` + `reason` shape since ADR-0036 is silent; deterministic lineage `uuid5(NAMESPACE_URL, "paa:memory:drain-throttled")`; ADR-0008 key `(correlation, DRAIN_THROTTLED, attempt=0, discriminator=drain_throttled:<window-ms>:<cap>)` so the second breach in one window dedupes in the store. Emit failure is caught + logged (`memory.drain_throttled_emit_failed`) — telemetry never breaks the drain.

### Phase 4 — Producer decision (`src/paa/context/allocator.py`, docstring only)
- Module docstring now states the **operator-fed** decision explicitly: no code enqueues to `workers:alloc` (only the queue docstring example); a synthetic poll-loop producer is forbidden; until external enqueue exists the tick is an `underfed` no-op. Test pins the phrase so W6/final review cannot mistake the loop for live.

### Phase 5 — Tests
- `tests/context/test_allocator_scheduling_p007b.py` (new, 8 tests): enabled tick allocates+notifies through the real allocator; disabled tick returns `None` with zero queue/store contact; empty queue `underfed`; docstring producer pin; breach emits one event with threshold payload; second breach same window dedupes (1 stored, 2 appended); no-breach → 0 events; no-handle breach → no raise.
- `tests/tui/test_dashboard.py` (append only): ranked-from-real-ledger test (3 events → top 2 by score, `max_results=2`, correlation_id carried) and empty → `None`.

## 3. Review audit (self-review, fresh-eyes pass)

- Correctness: daemon tick returns the real `AllocationResult`; disabled gate is *before* the curation lock (no spurious serialization delay); the `int(window_started*1000)` discriminator is stable within a process window; runner `entry["score"]` is float before sort (no mixed-type sort crash).
- MAJOR (fixed pre-commit): `Final` was used in creator.py without import — would have been a `NameError` at import time; caught by reading the typing import line; fixed.
- MINOR: creator `__init__` comment references "this wave" — acceptable, consistent with file's `PLAN:` tag style.
- Line-shift risk: creator.py edits shift later `path:line` doc citations below the edit sites (constructor/throttle regions) — the refs lock only checks line-exists/non-blank; semantic reconciliation of citations below shifted lines is flagged for W5-DOCS/final review (out of my write-set to fix docs).
- No independent reviewer was available in this subagent turn; per brief §Independent review, if the orchestrator wants a fresh reviewer, the diff is: daemon.py +58, runner.py +71, creator.py +114, allocator.py +10, new test file, dashboard append. Recorded as REVIEW_REQUIRED only if orchestrator requires it — self-checks all green.

## 4. Verification evidence (scoped only)

- [RAN `ruff check` on 6 changed files -> All checks passed]
- [RAN `mypy` on 4 changed src files -> Success: no issues]
- [RAN `pytest tests/context/test_allocator_scheduling_p007b.py tests/context/test_allocator.py -q` -> 32 passed]
- [RAN `pytest tests/context/ -q` -> all passed after retry (2 transient `test_metrics` failures mid-run traced to sibling W5-IMPL-01's in-flight edit; pass on retry, not my files)]
- [RAN `pytest tests/tui/ -q` -> 48 passed, 1 failed: `test_ledger_with_events` pre-existing HEAD `dashboard.py:347` typo (see digest; FINDING posted, dashboard.py outside write-set)]

## 5. Long-run answers

- **Source/Load (100x):** tick claims ≤25 items/pass at 60s cadence; panel scan bounded at 50 lineages; no unbounded loops introduced.
- **Hostile input:** runner panel skips non-dict payloads and non-numeric scores; allocator payload poisoning was already handled upstream (nack path untouched).
- **Operability:** `allocator.enabled=false` = inert (one early return per tick); rollback = flag off + drain cap high (ADR-0036); emit needs the NEED line to activate in production.
- **Reversal:** deleting `_run_allocation_tick` fails the scheduling tests; deleting the two `_emit_drain_throttled_event` calls fails the breach tests; deleting `_attention_top` fails the ranked test.

## 6. Unverified / deferred

- **NEED (outside write-set):** one line in `runtime.py::_build_memory_creator` — `MemoryCreator(db, ..., ledger=ledger)` — to activate production `DRAIN_THROTTLED`. Posted in COMMS; the creator-side emit is done, guarded, and tested. Until then, production is intentionally log-only (honest, no fake liveness).
- Pre-existing `dashboard.py:347` typo not fixed (outside write-set) — FINDING posted.
- Full suite not run per brief (scoped only); full-suite/doc-citation reconciliation deferred to final pass.

## 7. Git state

Branch `ops/20261107-integration-fix` [VERIFIED]. Code+tests commit: `f06b5bca5b03bd910651085a727a3b61d59f5153` (this report docs-committed separately, listing that SHA). Only write-set files committed with explicit paths; sibling changes in the shared tree left untouched.
