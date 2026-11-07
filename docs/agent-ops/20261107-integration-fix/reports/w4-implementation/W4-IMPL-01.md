<!-- ORCH-DIGEST:START -->
agent: W4-IMPL-01 | domain: P-007 allocator + drain throttle (T-007-P007) | status: DONE
items: 1 impl (allocator + 2 EventTypes + replay + drain throttle + orchestrator callsite) / 0 NOT-REPRODUCIBLE
proposed_tasks: n/a (implementer)
top_risks: types.py+replay.py hotspot (sole writer this wave); windowed throttle breaks test_creator test_drain_105 expectation (stale, not mine to edit); notify at-most-once vs decision exactly-once gap documented
cross_domain_conflicts: test_drain_105 stale-expectation (needs owner update — CONFLICT); drain EventType emitted no-where this wave (creator has no ledger)
out_of_plan_discoveries: 0 so far
confidence: high (ADR-0036 read fully; all contracts read at HEAD; store dedupe verified)
last_comms_line_read: 2026-11-07T12:26:13Z ORCH -> ALL | DECISION | W4 | wave 4 start
<!-- ORCH-DIGEST:END -->

# Implementation report — W4-IMPL-01 — P-007 allocator + drain throttle

## 0. Scope and method

- Brief: `docs/agent-ops/20261107-integration-fix/briefs/W4-IMPL-01.md`. Task `T-007-P007`.
- Required reading in order: STANDING-ORDERS.md; AGENTS.md (ledger-is-truth; EventType checklist; `class X(str,enum.Enum)`; ledger-before-side-effect; full ADR-0008 key); 00-mission.md (hotspots); plan rev5 §E P07 + §H T08/T15 + §I + §K; ADR-0036 (full, wins over plan); W0-INV-03 §P-007 + W0-INV-02 §P-007 (queue half) + W0-INV-05 §P-014 (skim — config verified directly instead); W2-IMPL-02 (tier `_NO_PHASE_CHANGE` precedent); template (ORCH-DIGEST above).
- Code verified at HEAD before changing (line numbers re-verified by me, not trusted from plan):
  - `attention_allocation_score` (K·I)/(F·(1+P)·(1+R)) at `context/metrics.py:315`, guards `_unit`/`_finite` (`metrics.py:80-93`) reject NaN/inf/outside [0,1] — no ZeroDivision path exists (divisor >= 1 by construction).
  - `StreamName.WORKERS_ALLOC="workers:alloc"` at `storage/queue/base.py:60` (StrEnum, waiver — no fix), `CONTROL_PLANE_STREAMS` excludes it (shed-first; starvation fallback per ADR-0036, not improvised).
  - `MessageQueue` claim/ack/nack/depth at `base.py:199-240`; `QueueMessage` (id/correlation_id/session_id/payload/attempts/max_attempts/enqueued_at/visible_after) at `base.py:99-162`; at-least-once + ledger dedupe is the house semantics (`base.py:12-16`).
  - No allocator EventType members at HEAD: `core/types.py:122-192` (tier members at :191-192 are the nearest precedent).
  - `_PHASE_MAP` (17) at `ledger/replay.py:150-168`; `_NO_PHASE_CHANGE` (7) at :379-389; `apply_event` at :171-288 (match arm at :200-286; unknown -> `unknown_events` + warning, never raises).
  - `LedgerStore.append` dedupes by full key, returns the EXISTING event on duplicate (no raise) at `ledger/store.py:116-144`; fresh append preserves `event_id` — hence duplicate detection by `persisted.event_id != fresh.event_id`.
  - `Orchestrator._emit` (LedgerEvent + store.append, attempt/discriminator threaded) at `agents/orchestrator.py:263-288`; orchestrator keeps no queue import at module scope (see `BackpressureLike` docstring at :142-154) — therefore lazy in-method import.
  - `MemoryCreator.drain_pending_embeddings(limit=100)` at `memory/creator.py:1497`; ctor at :1115-1136 is fully injected (no settings today); construction sites `runtime.py:506`, `daemon.py:195`, `api/app.py:292` all pass no cap (backward-compat default required); call sites `daemon.py:165`, `cli/main.py:663`, `api/app.py:297` call with no args.
  - `AllocatorSettings(enabled=False, max_results=5)` at `config.py:680-697`; `MemorySettings.drain_max_per_second=100` at `config.py:300` (CONSUME only — config is not mine).
  - `BoundedContextGatherer.compile` is sync/pure at `context/gatherer.py:335-366`; `ContextElement` (id/content/relevance/confidence/importance/slot/memory_domain/token_cost) at :103-158; `_rank_key` deterministic `id` tie-break at :242-278.
  - `compute_idempotency_key(correlation_id, event_type, attempt, discriminator)` at `ledger/events.py:68-102`.
  - pydantic-settings + python-dotenv are CORE deps at `pyproject.toml:20-32` — allocator importing `paa.config` at module scope is minimal-install safe (gatherer already imports `config` at `gatherer.py:30`).
  - Log capture in tests: `structlog.testing.capture_logs()` (house pattern, e.g. `test_e2e_remember_recall.py:277`); `db`/`settings` fixtures in `tests/conftest.py:33-53`.
  - Existing drain tests pin unbounded back-to-back draining (`tests/memory/test_creator.py:502-509` drains 100 then 5 on ONE instance; `:481-492` drains 2 then 1). Any honest per-second window breaks the 105-case second call — outside my write-set, will be reported as CONFLICT, not fixed.
- Branch verified: [RAN git branch --show-current -> `ops/20261107-integration-fix`]. Working tree clean at start [RAN git status --short -> empty].
- Scoped checks only; no full suite, no containers, no migrations.

## 1. Context report (root cause, entry points, invariants, risks)

- Root cause: ADR-0012 formula landed as pure code with zero producers/consumers; `workers:alloc` stream registered with zero traffic; drain loop unthrottled. This is a net-new loop, not a repair.
- Architectural entry points: (a) new `src/paa/context/allocator.py` consuming `workers:alloc` claim-ack; (b) 2 new `EventType` members + replay wiring (hotspots, sole writer this wave); (c) windowed throttle in `drain_pending_embeddings`; (d) one orchestrator wiring callsite.
- Invariants that must hold:
  - Ledger-before-side-effect: emit (store.append) BEFORE notify enqueue; ack only after event persists.
  - Full ADR-0008 key: (correlation_id, event_type, attempt=0, discriminator=item_id).
  - Gatherer never blocks on queue: gatherer is not called, imported, or gated by allocator; empty -> underfed no-op; disabled -> queue untouched.
  - Poison payloads terminate via nack (max_attempts dead-letter in backend) — never loop.
  - Drain never drops silently: deferred facts stay `pending`; every throttle action logs.
  - K/F/R never evaluated raw P*R (ADR-0012 guards only).
  - `import paa.context.allocator` succeeds in minimal install; allocator sits ABOVE metrics (metrics never imports allocator).
- Decisions (recorded, ADR-0036 wins over plan):
  1. EventType names (ADR-0036 leaves them to the implementer): `ATTENTION_ALLOCATED` (allocator decision, emitted this wave) and `DRAIN_THROTTLED` (drain telemetry vocabulary, replay-total this wave; NOT emitted this wave — creator has no ledger handle, and daemon/orchestrator drain emission is outside the write-set).
  2. Both map to `_NO_PHASE_CHANGE`, never `_PHASE_MAP`; no `match` arm; no TERMINAL_EVENTS addition (review: neither closes a lineage).
  3. Per-item decision events on the item's lineage (msg.correlation_id, else payload correlation, else uuid5(ns, item_id)) — a run-level synthetic correlation would write the answer into nobody's projection.
  4. Dedupe: store.append dedupe + `event_id` comparison -> duplicate skips notify but acks. Crash between emit and notify loses the notification, never the decision (at-most-once notify, exactly-once decision; gatherer remains the fallback). Documented in module docstring.
  5. Throttle is a per-second window counting INDEXED facts (config wording "may embed per second"), budget computed before the fetch, effective limit = min(limit, remaining). Default cap (Settings) = 100 keeps daemon 1 Hz cadence unaffected.
  6. Drain cap consumed via ctor: `drain_max_per_second: int | None = None` -> Settings value when None; explicit invalid (<1) raises ValueError. All 5 MemoryCreator construction sites therefore light the knob without edits.
  7. Notify = enqueue to `orchestrator:core` (the only controller channel the brief allows) carrying the decision back-reference. No second producer stream invented.
- Risks / edge cases: 100x queue ping-pong (claim limit bounds each run; per-item dedupe makes re-runs no-ops); concurrency overshoot of drain window (no lock — documented); huge payloads (no size cap invented; structural validation only); concurrent allocator runs racing store.append (store serializes per correlation; worst case two identical events -> dedupe); visibility-timeout reclaim re-delivers -> dedupe path acks.
- Expected behaviour + required tests: T08 (6 items -> top 5 sorted, empty -> underfed no-op, disabled -> untouched, gatherer pure, drain T15 cap+log+no-drop, replay totality, idempotent re-claim, poison dead-letter path, P/R=0 via guards, deterministic tie-break).

## 2. What will be changed (phased)

### Phase 1 — tests first (fail-before)
New `tests/context/test_allocator.py` (PLAN:integration-fix:P007): fakes for queue/store/sink; real LedgerEvents; T08/T15/replay/idempotency/poison/P-R=0/tie-break/disabled/gatherer-purity cases. New `tests/context/test_allocator.py` must fail before src changes (ModuleNotFoundError / AttributeError / TypeError).

[Ran .venv\Scripts\python.exe -m pytest tests/context/test_allocator.py -q -> collection ERROR: ModuleNotFoundError: No module named 'paa.context.allocator' (fail-before confirmed).]

### Phase 2 — vocab (core/types.py)
PLAN:integration-fix:P007 — add `ATTENTION_ALLOCATED` and `DRAIN_THROTTLED` after the tier group; payload/idempotency contract in comments; TERMINAL_EVENTS review recorded (no addition).

### Phase 3 — replay (ledger/replay.py)
PLAN:integration-fix:P007 — both members into `_NO_PHASE_CHANGE` only (never `_PHASE_MAP`); no match arm; totality holds (never raises; unknown-append never used for these).

### Phase 4 — allocator (context/allocator.py, new)
PLAN:integration-fix:P007 — `AttentionAllocator` + `allocate_attention()` (class method + module-level wrapper). K/F/R derivations per ADR-0036 (K: explicit criticality/priority else 0.5; I: density else relevance*confidence projection else 0.0; F: planning_cost else 1.0; P: pollution_ratio else 0.0; R: explicit risk else 0.0). Ledger event first, ack after persist, nack on poison, `allocator.enabled=False` gate, empty -> `allocator.underfed` no-op, deterministic total order (score desc, item_id asc). Trace tag in module docstring.

### Phase 5 — drain throttle (memory/creator.py, throttle only)
PLAN:integration-fix:P007 — ctor `drain_max_per_second` (None -> Settings); windowed rate check before pending fetch; breach -> `memory.embedding_drain_throttled` backpressure log + defer (facts stay pending); backward-compatible (no caller changes).

### Phase 6 — orchestrator callsite (agents/orchestrator.py)
PLAN:integration-fix:P007 — `run_allocation` wiring method (lazy import; `_emit`-pattern via the store inside allocator). No other orchestrator changes.

## 3. Verification log

### Fail-before (new tests vs unmodified src)
[RAN `.venv\Scripts\python.exe -m pytest tests/context/test_allocator.py -q` at Phase 1 -> collection ERROR: `ModuleNotFoundError: No module named 'paa.context.allocator'`. Confirms the new tests exercise new code.]

### Pass-after (post-implementation, post-self-review-fix)
- [RAN `.venv\Scripts\python.exe -m pytest tests/context/test_allocator.py --collect-only -q` -> `24` tests]
- [RAN `.venv\Scripts\python.exe -m pytest tests/context/test_allocator.py tests/context/test_metrics.py tests/ledger/test_replay.py --tb=no -q` -> 197 passed (24 new + 152 metrics unchanged + 21 replay), 0 failed.
- [RAN `.venv\Scripts\python.exe -m ruff check src/paa/context/allocator.py src/paa/core/types.py src/paa/ledger/replay.py src/paa/memory/creator.py src/paa/agents/orchestrator.py tests/context/test_allocator.py` -> `All checks passed!`. Full-tree `[RAN .venv\Scripts\python.exe -m ruff check src tests` -> `All checks passed!` (sibling fixes have since landed; earlier sibling-local findings were live state, never mine).]
- [RAN `.venv\Scripts\python.exe -m mypy src` -> `Found 3 errors in 1 file (checked 141 source files)` — exactly the pre-existing baseline at `sandbox/subprocess_backend.py:548` (killpg/getpgid/SIGKILL), unchanged; gate holds (3 stays 3).]
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py -q --tb=no` -> 52 passed, 1 FAILED: `TestDrainPendingEmbeddings::test_drain_105_backlog_drained_not_paginated` — the only casualty, predicted before writing code (see §5 CONFLICT). Captured log shows the intended throttle line: `memory.embedding_drain_throttled deferred=True drain_max_per_second=100 indexed_this_window=100 limit=100 window_seconds=1.0`.]
- [RAN `.venv\Scripts\python.exe -m pytest tests/daemon/test_drain_e2e.py tests/ledger/test_tier_replay_p009.py -q --tb=line` -> 16 passed (drain e2e 10 + tier replay 6; daemon relies on ONE drain call per 1 Hz tick, never throttled at default cap).]
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py -q --tb=line` -> 19 passed (limit=2 drains unaffected: 2+1 within a 100-budget window).]
- [RAN `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q --tb=line` -> 2 FAILED but every cited failing line is in `facts.py`/`daemon.py` (W4 siblings in flight); zero blank citations in my four files. Separately, a differential script (HEAD vs worktree per file, in-doc-scope cites only) measured semantic staleness: 22 lock-blind stale cites (types 3 / replay 7 / creator 12), 0 lock-visible blanks introduced by me, 130 cites preserved. Orchestrator damage: 0 (see §4 F6).]
- Acceptance greps: [RAN `grep allocate_attention src/` -> 6 matches: defs `allocator.py:243` (method), `:384` (module fn), `:392` (delegation), caller `orchestrator.py:1139,1141`] and [RAN `grep WORKERS_ALLOC|workers:alloc src/` -> consumer `allocator.py:254` (`queue.claim(... StreamName.WORKERS_ALLOC, "allocator", ...)`) plus the controller-notify producer `queue.enqueue(StreamName.ORCHESTRATOR_CORE, ...)` (`allocator.py:288-292` region, claim-before-notify ordering by construction).]
- No `config.py`/schema edits: [RAN git status --short verified during work -> none of my 6 paths; `config.py`, `schema_*.sql`, `migrations.py` untouched by me (siblings' working-tree changes are theirs, never committed by me).]

### Reversal/mutation checks (reasoned + one executed)
- Remove `ATTENTION_ALLOCATED`/`DRAIN_THROTTLED` from `_NO_PHASE_CHANGE`: replay folds them to `unknown_events: [...]` + `replay.unknown_event` warning -> `TestAllocatorReplay::test_replay_phase_unchanged_and_total` fails (unknown asserted == []). Fails for the right reason.
- Swap emit/notify order: `test_ledger_event_before_notify_before_ack` (`["emit","notify","ack:m1"]`) fails.
- Remove the throttle window (budget = limit): T15 cap test returns 100 not 5, and no throttle log -> fails. Revert to raw P*R: P/R=0 test would ZeroDivisionError — the guards in metrics are the only divisor used.
- Executed reversal analog: throttle-window saturation already demonstrated by `test_drain_105` failure above (throttled second drain returns 0 — the test's expected 5 is precisely the pre-fix behaviour).

### Manual trace (long-run, standing-order 8)
- Source: decision record = `ATTENTION_ALLOCATED` event payload `{item_id, score, inputs, message_id, top_n}` keyed by `(correlation, type, 0, item_id)`; replay never folds allocator state into projections.
- Load (100x): each run claims one bounded batch (`limit=25` default); per-item dedupe makes an accidental double-run a no-op (verified by reclaim test). Drain window counting indexed (not attempted) facts caps sustained ingest without stalling retries after a transient sink failure.
- Hostile input: non-dict/bool/wrong-type/non-finite/out-of-range payloads all nack (dead-letter after max_attempts — no loop); negative F rejected by the score's own guard, never evaluated around.
- Operability: `allocator.enabled=False` rollback stops consuming (queue untouched); throttle respects `PAA_MEMORY__...` via Settings default 100 parity; logs carry `allocator.*` / `memory.embedding_drain_throttled` / `drain.throttle`-equivalent names.
- Reversal: flag off + drain cap high restores pre-P07 behaviour; the two new EventTypes remain readable in old ledgers (`_NO_PHASE_CHANGE`, never unknown); nothing migrated.

## 4. Review audit

Self-review of the complete diff as a stranger's PR (each finding with severity):

- **F1 [MINOR -> FIXED] `AllocationResult.selected` included duplicate-redelivered items** (`allocator.py` pre-fix loop appended every acked top item, so a reclaim run would report items "selected" that it did not notify). Fixed to fresh-notified-only; new test `test_derived_correlation_dedupes` also pins `second.selected == ()`. [RAN -> mechanism covered by the 24-test green run.]
- **F2 [MAJOR-gap -> FIXED] Partial-failure + derived-correlation paths untested** (standing-order 10 mandates partial-failure coverage). Added `test_store_failure_preserves_message`, `test_notify_failure_preserves_message`, `test_derived_correlation_dedupes` (correlation-less messages get a deterministic uuid5 lineage and still dedupe). [RAN -> 24 passed.]
- **F3 [MAJOR -> REPORTED, not fixed] `test_drain_105_backlog_drained_not_paginated` stale expectation** (`tests/memory/test_creator.py:502-509`): drains 100 then 5 on ONE instance back-to-back; the second drain is intentionally throttled to 0 under the ADR-0036 per-second cap. The test pins the pre-throttle unbounded contract; the new contract is what ADR-0036 orders. Outside my write-set (only `tests/context/test_allocator.py`), so NOT edited — CONFLICT posted, owner update needed (see §5). Mitigation analysis (REJECTED): per-call batch cap preserved this test but violates the brief's explicit "windowed rate check" and the Setting's per-second semantics; a batches-per-second reading breaks T15's observable cap. Windowed-per-second + stale-test escalation is the honest shape. Scope: [RAN `tests/daemon/test_drain_e2e.py` + `tests/memory/test_e2e_remember_recall.py` -> all green] so this is the single casualty.
- **F4 [MINOR -> ACCEPTED/documented] Notify at-most-once gap.** A crash between store.append and enqueue loses the notification while the decision persists; the gatherer fallback covers it. The brief's "single notify" is the at-most-once side; exactly-once notify across a crash is unimplementable without more state. Documented in the allocator module docstring.
- **F5 [MINOR -> ACCEPTED] `below_cut` bookkeeping loop** — harmless O(1) counter; left as-is for readability.
- **F6 [MINOR -> MITIGATED by placement, residual reported] Documentation line-shift damage.** Mid-file insertions shift `path:line` cites ("wrong without being broken", ADR-0036/R-W1-003 pattern). mitigations applied: `run_allocation` placed at END of `ChiefOrchestrator` (after `boot`) with lazy `get_settings`/`allocate_attention` imports (orchestrator.py cited lines: 0 changed); creator's `_DRAIN_WINDOW_SECONDS` appended at EOF, clock via `asyncio.get_running_loop().time()` (creator.py imports unchanged — no module-scope config/time import); types additions kept at enum end. Measured result: 22 lock-blind stale cites / 0 lock-visible blanks (differential script, in-scope docs only). Residual (types 3 / replay 7 / creator 12) needs a docs re-point task — outside write-set, incl. two ADR-0036 cites I am forbidden to edit. CONFLICT posted.
- **F7 Final pass:** cross-phase interactions re-checked — allocator/gatherer independent (no import either way; gatherer compiles without allocator, tested); `_NO_PHASE_CHANGE` additions cannot affect `TERMINAL_EVENTS`/recovery (asserted non-membership in tests); creator throttle cannot affect `process_signal` path (only drain branch instrumented); orchestrator diff is +1 private wiring method (lazy imports), `submit`/`run`/gates untouched. No `any`/`type: ignore`/`noqa` added; mypy 3 = baseline; ruff 0 (final full-tree run).
- Independent reviewer: not dispatched (brief does not require one). Status flag: `REVIEW_REQUIRED` recommended by me — hotspot + ledger-adjacent + nonzero doc-damage + one known-red pre-existing-style test = worth a fresh-context pass before merge.

## 5. Assumptions / risks / DEFERRED-DEPENDENCY

- **NEED (CONFLICT, owner: `tests/memory/test_creator.py`):** update `test_drain_105_backlog_drained_not_paginated`'s second assertion (`== 5`) deliberately: under the ADR-0036 cap it must now expect `== 0` + a `memory.embedding_drain_throttled` line, or construct the creator with a window in the past. Suggested minimal update lives in the T15 tests (`tests/context/test_allocator.py::TestDrainThrottleT15`) as the canonical contract. Exact reproduction: [RAN -> `assert 0 == 5` at `test_creator.py:508` with throttle log in captured output].
- **NEED (docs re-point task):** 22 lock-blind stale cites (differential enumeration; files: `docs/glossary.md` (types.x2), `docs/adr/0036` (creator x2, replay x1 — read-only for me), `docs/adr/0037` (replay x1), `docs/adr/0041` (types x1), `docs/data/persistence-and-storage.md`, `docs/flows/workflow-01/03`, `docs/modules/ledger.md` (x2), `docs/operations/deployment-and-runtime.md` (x3), `docs/reviews/documentation-audit.md`, `docs/memory-polish/plans/Plan1.md` (x6)). Re-point by READING each (per AGENTS), never blind +N. My files introduce 0 blank-line lock failures (verified by the docs-lock run: blanks land only in sibling files `facts.py`/`daemon.py`).
- **CONTRACT (for W5+ daemon wiring, no action this wave):** `AttentionAllocator(queue, store, settings)` takes a structural `_LedgerAppender` (store.append); the orchestrator caller is `ChiefOrchestrator.run_allocation()`. Daemon-side claiming of `workers:alloc` is deliberately NOT wired (outside write-set); the queue will stay empty until an operator wires the loop — gatherer unaffected either way (enabled defaults False).
- **CONTRACT:** `DRAIN_THROTTLED` EventType is vocabulary + replay totality only this wave; no emitter exists (creator has no ledger handle; logging is the channel — by ADR-0036, the implementer splits the two members; drain telemetry rides the `memory.embedding_drain_throttled` log). A future emitter needs a creator-side ledger path.
- [ASSUMED] Notify payload schema for `orchestrator:core` (`{kind, correlation_id, item_id, score, inputs, event_id, idempotency_key}`) is this task's contract — the daemon's `run(cid)` branch will re-drive the task with this correlation; no daemon-side decode exists (outside write-set) and none is needed for the loop's correctness.
- Out-of-scope discoveries (none new beyond the pre-existing ones; not re-litigated): StreamName StrEnum waiver (ADR-0036 § segment — left alone per brief), test_105 staleness (above), sibling in-flight breakage (never committed by me; CORRECTION posted when siblings fixed).

## 6. Git state

- Branch: `ops/20261107-integration-fix` (verified [RAN `git branch --show-current` -> `ops/20261107-integration-fix`]; the pre-existing untracked file `docs/agent-ops/20261106-memory-remembering-agent/briefs/DOCS-README-01.md` was never touched; other agents' uncommitted working-tree changes — `daemon.py`, `cli/main.py`, `reflection.py`, `facts.py`, `dialect.py`, `migrations.py`, `schema_*.sql`, `tui/dashboard.py`, `tests/improvement/test_reflection_wiring_p013.py`, `COMMS.md` — were never reverted, reformatted, or committed by me).
- Code commit (code+tests, explicit paths only — no `add -A`/`-a`): SHAs recorded here after committing (see §6 addendum).
- Report commit (docs-only, separate, lists code SHA): recorded after committing.

### 6-addendum (commit hashes)
- Code+tests commit: `0136693` (6 files: `src/paa/context/allocator.py` new, `src/paa/core/types.py`, `src/paa/ledger/replay.py`, `src/paa/memory/creator.py`, `src/paa/agents/orchestrator.py`, `tests/context/test_allocator.py` new)
- Report commit: next docs-only commit (lists `0136693` per protocol).
