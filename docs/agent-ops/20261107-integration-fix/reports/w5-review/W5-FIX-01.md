<!-- ORCH-DIGEST:START -->
agent: W5-FIX-01 | domain: wave-5 fix (REV+TREV W5) | status: DONE-WITH-DEFERRED
items: 10 done (R-W5-001/002/003/004/005/007/008-cur/partial, TREV R-W5-001/002/003/004/005/006) / 1 DEFERRED (R-W5-006 orchestrator threading) / 1 NEED (daemon cost_gate line)
proposed_tasks: W5-FIX-DOCS2 (docs lock residual below), W5-FIX-02 (R-W5-006 + daemon cost_gate threading, needs orchestrator.py + daemon.py ownership)
top_risks: PG substrate for cursor fix proven by query-shape pin only (PG suite NOT-RUN, no DSN); producer lock is line-shaped (multi-line enqueue evasions possible, disclosed)
cross_domain_conflicts: none (sole editor this wave; COMMS left uncommitted for ORCH)
out_of_plan_discoveries: none new (1 lock-visible docs residual + lock-blind shifts recorded for DOCS2 per brief)
confidence: high (all acceptance counts executed; 5 mutation classes asserted caught by construction)
last_comms_line_read: 2026-11-07T14:57:39Z ORCH -> ALL | CORRECTION (own NEED appended at 19:45Z)
<!-- ORCH-DIGEST:END -->

# Report — W5-FIX-01 (wave 5 fix)

## 1. Status: DONE-WITH-DEFERRED

All §3 items implemented except REV R-W5-006 (allocator-settings threading),
which is unreachable inside the write-set and is reported as
DEFERRED-DEPENDENCY with a NEED posted and a executed reproduction.
Everything else: code + tests committed separately, report committed after
(listing code SHA below).

## 2. Root cause & context (verified at HEAD before editing)

Investigation was self-performed (subagent nesting unavailable in this
session, same as W5 implementers); every claim below is [VERIFIED
file:line]. Summary of the named findings:

- **R-W5-001**: `_build_memory_creator` built `MemoryCreator(...)` with no
  `ledger=` while `ledger` was in scope in `Runtime.build` — production
  DRAIN_THROTTLED stayed log-only.
- **R-W5-002**: cost-gate pagination seeded `cursor = ""` for
  `id > ?`; SQLite TEXT ids accept it, Postgres UUID ids reject `''` —
  first page raises, gate never runs on PG.
- **R-W5-003**: `_attention_top` scanned the oldest-50 open lineages,
  appended every allocation event (duplicates, stale by construction),
  and could not see terminal lineages (head `is_terminal=1` filter).
- **REV R-W5-004**: discriminator `window_started = loop.time()` — two
  processes same-ledger / restart collision.
- **REV R-W5-005 / TREV-001**: policy-coverage test tautological;
  read-only half unpinned.
- **TREV-002..006**: 50-task "routing precision" test hardcodes the
  target (measures delegation success); daemon `start()`/loop untested;
  partial-cap second edit site untested; package-exports test cannot see
  a stub file; producer pin is docstring substrings only.
- **REV R-W5-006**: daemon gate (`rt.settings`) vs allocator gate
  (`get_settings().allocator` in `run_allocation`) — orchestrator.py is
  FORBIDDEN in write-set → DEFERRED (see §7).
- **REV R-W5-007**: cost gate O(N) suggestions + O(N) log lines per pass.
- **REV R-W5-008**: cost gate reads global singleton; curator already
  accepts injected `MemorySettings` — same pattern applied.

## 3. Phased changes executed

**Phase 1 — R-W5-001 (runtime.py, ledger= line only + unavoidable call-site
lines).** `Runtime.build` passes `ledger=ledger` into `_build_memory_creator`,
which gained a keyword-only `ledger: Any = None` and forwards it to
`MemoryCreator(..., ledger=ledger)`. No other runtime.py change.
DECISION: kwarg named `ledger` to match `MemoryCreator.__init__` exactly.

**Phase 2 — REV R-W5-004 (creator.py, discriminator only).** Module-level
`_DRAIN_PROCESS_ID = uuid4().hex`; discriminator now
`drain_throttled:<process-id>:<window-ms>:<cap>`. INVARIANT (module
docstring + constant comment): idempotency is per-process — same process
+ window + cap still dedupes; fresh processes record their own event.
Bounded cost: one telemetry event per process per window, never a lost
breach.

**Phase 3 — R-W5-002 / R-W5-007 / R-W5-008 (curator.py).**
(a) Cursor: `cursor: str | None = None`; first page issues
`WHERE superseded_by IS NULL ORDER BY id LIMIT ?` with params `(batch,)`
— no id predicate, no `''` param. Later pages keep the total-order keyset
`id > cursor`. Cursor stored as `str(row["id"])` so PG UUID values are
passed as strings (the same shape store.py uses).
(b) Cap: `_COST_GATE_SUGGESTION_LIMIT = 100`; all crossings collected,
sorted by (-total, fact_id), top-K extended into the report; per-fact
`cost_gate.suggestions` lines fire only for kept rows; one
`cost_gate.suggestion_summary` line carries {total, kept, threshold}.
Advisory semantics unchanged (never deletes; list body still excluded
from `summary()`).
(c) Injection: `MemoryCurator(..., cost_gate: CostGateSettings | None =
None)`; `self._cost_gate = cost_gate or get_settings().cost_gate`
(MemorySettings pattern). `_phase_analyze` reads `self._cost_gate`.
Production threading from daemon (`daemon.py:418`) is outside the
write-set → NEED (see §7). The curator-side acceptance + both override
directions are tested.

**Phase 4 — R-W5-003 (runner.py, attention_top only).** Scan window is
`heads[-_PANEL_SCAN_LIMIT:]` (freshest 50); dedupe by `item_id` in a dict
with later events overwriting (read order is `state_version` ASC, so
later = latest event, not best score); final sort unchanged
(-score, item_id). Docstring documents: recency window, latest-wins
dedupe, and the terminal-lineage drop (panel shows in-flight attention
only; accepted by design, now explicit contract). Empty → `None`
preserved.

**Phase 5 — Tests (specialist file).** Policy test rewritten to exact
pins (`mutating == {"submit_task","run_task","remember_fact"}` +
`read_only == set(READ_ONLY_TOOLS)` + union equality) — a new tool in
either class fails loudly. Package-exports test is now the file-set glob
`["__init__.py","coding.py"]`. 50-task test renamed to
`test_fifty_scripted_tasks_delegate_successfully_over_50_lineages`, with
a docstring stating delegation-success (not precision), named-target
router bypass per ADR-0041, and that no classifier exists in the path.

**Phase 6 — Tests (allocator scheduling file).** Daemon home chosen:
existing `tests/context/test_allocator_scheduling_p007b.py` (the daemon +
drain home; no new file created).
- `TestProductionWiring`: `_build_memory_creator(..., ledger=ledger)._ledger
  is ledger` + full `Runtime.build` breach → one `DRAIN_THROTTLED` in the
  real ledger.
- `TestDrainThrottledEmit::test_partial_cap_breach_emits_with_effective_limit`
  (cap 2, indexed 1, limit 100 → `effective_limit == 1`, `threshold == 2`,
  returns 0) and `test_discriminator_carries_process_id`.
- `TestProducerLock`: line-shaped grep over `src/` (`.enqueue(` +
  WORKERS_ALLOC off the queue-docstring file → offender) + operator-fed
  statement present.
- `TestDaemonScheduling`: `start()` creates 5 supervised tasks (loops
  stubbed idle; `daemon.started loops=5` logged); tick-raise →
  `daemon.allocation_failed` logged AND second tick runs
  (`allocation_completed` logged; orchestrator called twice — first raise,
  second success; fake sleep stops the loop after 2 passes, mirroring the
  reflection tick test shape).

**Phase 7 — Tests (cost gate file).** Multi-page scan (12 facts, batch 5,
all ids suggested, no duplicates); PG-shape spy (first page SQL has no
`id >`, params exactly `(batch,)`; page 2 has the cursor); cap test
(limit 5, 10 facts → 5 kept, summary total=10/kept=5, exactly 5 per-fact
lines); injection tests both directions (injected enabled beats global
disabled and vice versa).

**Phase 8 — Tests (dashboard file, append only).** Same item twice →
one row, latest score (0.2 after 0.8 — latest-wins, not max); cross-lineage
later-wins (0.3 supersedes 0.9, correlation of newer lineage carried);
recency window (limit stubbed 2, oldest highest-score lineage excluded);
terminal lineage → `None` (documented behavior).

No `src/paa/daemon.py`, `src/paa/context/allocator.py`, `core/types.py`,
orchestrator, EventType edits. Allocator docstring needed no change:
operator-fed statement already present [VERIFIED allocator.py:42-50];
the producer *lock* landed test-side.

## 4. Review audit (self-review as stranger's PR)

- **Correctness/root cause**: each fix changes the named mechanism, not
  the symptom. Cursor: existence of an id param on page 1 is what PG
  rejects; removing it (rather than a NULL-sentinel branch) is the
  minimal correct change (a NULL guard would still compare against a
  value). Cap: sorting happens over the collected set, not capping
  collection — count stays exact. Attention: dedupe key + overwrite
  order derive from `read_correlation` replay order [VERIFIED
  store.py:324-327]; window flip derives from `updated_at` ASC
  [VERIFIED store.py:355-358].
- **Edge cases**: inf totals sort first under `-total` (verified);
  NaN never occurs (function guarantees finite-or-inf); empty collected
  → no summary line; `heads[-N:]` with <N heads = all heads (unchanged);
  `_PANEL_SCAN_LIMIT` monkeypatched to 0 would return all (never set in
  prod; constant 50).
- **Security**: cursor param is a real DB id re-passed as a parameter —
  no injection surface. Cap bounds per-pass report/log growth. No new
  input surface.
- **Tests (mutation mental check)**: removing `ledger=` fails both
  wiring tests; removing the first-page branch fails the spy test
  (SQLite alone would still pass — the spy is what pins the PG shape);
  uncapping fails the cap test (10 ≠ 5); removing ctor kwarg TypeErrors;
  auto-delete flip still fails the advisory tests (unchanged); dedupe
  removal fails both dup tests; window flip removal fails the recency
  test; deleting `start()`'s allocation task fails the 5-task assert;
  deleting the second emit fails the partial-cap test; adding a stub
  `research.py` fails the file-set test; adding a same-line
  `.enqueue(WORKERS_ALLOC)` anywhere fails the producer lock.
- **Findings (all fixed in-phase)**: [MINOR] producer-lock read text
  without UTF-8 (Windows `UnicodeDecodeError`) → `encoding="utf-8"`;
  [MINOR] first lock flagged the allocator *docstring* prose ("enqueue"
  + "workers:alloc" on one line) → narrowed to call shape `.enqueue(` +
  stream, disclosed multi-line-call limitation in the test docstring;
  [MINOR] allowed-set path missed the `paa/` prefix → fixed;
  [MINOR] ruff I001 on the two-import block in the wiring test class →
  `--fix` applied, all checks re-run green; [MINOR] wiring identity test
  used global `Settings()` → switched to `tmp_path` home (isolation).
- **Independent review**: nesting unavailable; mitigated by the
  self-review above + full scoped execution. Recommend re-review
  (REV-W5b/TREV-W5b) per plan.

## 5. Verification evidence [RAN]

- `.venv\Scripts\python.exe -m pytest tests/context/test_allocator_scheduling_p007b.py tests/memory/test_cost_gate_p008.py tests/agents/test_coding_specialist_p011.py tests/context/test_metrics.py -q` → **221 passed, 0 failed** (allocator 16 = 8+8 new; cost gate 14 = 9+5 new; coding 21; metrics 170).
- `.venv\Scripts\python.exe -m pytest tests/agents/ tests/memory/ -q` → **557 passed, 1 x-skip** (pre-existing).
- `.venv\Scripts\python.exe -m pytest tests/tui/ -q` → **64 passed, 1 FAILED**: `test_ledger_with_events` — pre-existing `dashboard.py:347 "%H:%:%S"` ValueError (identical at HEAD, brief baseline). All 4 new attention tests + both pre-existing attention tests pass (6/6).
- `.venv\Scripts\python.exe -m ruff check src tests` → **All checks passed!** (0).
- `.venv\Scripts\python.exe -m mypy src` → **3 errors, all pre-existing `sandbox/subprocess_backend.py:548`** (baseline A-001, unchanged; total = 3 as required).
- `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q` → 1 lock-visible failure (residual, §6); `test_import_conventions.py` → **21 passed**.
- R-W5-006 scratch repro (created, run, deleted — not committed):
  `test_gate_split` passed, proving the split: daemon gate
  `rt.settings.allocator.enabled=True` passes, allocator falls back to
  global (disabled) and returns `outcome="disabled"`, which the daemon
  logs as `daemon.allocation_completed`. Evidence in §7.
- Full suite: NOT-RUN (standing orders §16, brief §7). Containers: none.

## 6. Docs-lock residual (for W5-FIX-DOCS2 — docs edits forbidden here)

My shifts break the lock on exactly **one** cite:
- `docs/modules/sandbox.md:193` cites `runtime.py:441` (blank now).
  Holder `return get_queue(settings, db)` moved HEAD:441 → **:446**
  (+5 from the `Runtime.build` call-site expansion). Semantic
  re-point needed (read, don't blind-shift).

Lock-blind semantic shifts (cited lines still non-blank; the lock cannot
see them — DOCS2 must sentence-read every cite into the four edited files
at/after the edit regions): net shifts are **runtime.py +5** (after the
`Runtime.build` call site), **creator.py +11** (module docstring) with
citied 1515-region +11, **curator.py +15-ish** (ctor + constant before
`_phase_analyze`; `_PHASE_ORDER:64` unchanged — added after it),
**runner.py +26** inside/after `_attention_top`. Docs files citing these
files (25): `adr/0036-allocator-and-attention-loop.md`, `architecture/
component-interactions.md`, `architecture/overview.md`,
`architecture/repository-map.md`, `backend/modules-and-services.md`,
`backend/overview.md`, `data/data-lifecycle.md`,
`data/persistence-and-storage.md`, `development/developer-workflow.md`,
`flows/workflow-01-submit-and-run.md`, `flows/workflow-03-crash-recovery.md`,
`interfaces/api-reference.md`, `modules/agents.md`, `modules/daemon.md`,
`modules/ledger.md`, `modules/memory.md`, `modules/sandbox.md`
(+ 8 further files in the grep — full list from
`Select-String "(runtime|curator|runner|creator)\.py:\d+"` over
`docs/**`; the sandbox.md:193 cite is the only blank one).
Concrete semantic holders: `runtime.py:446` (above), `creator.py:1533`
(`drain_pending_embeddings`, was :1522) — DOCS2 verifies by reading.

## 7. DEFERRED-DEPENDENCY — REV R-W5-006 (allocator-settings threading)

Not implementable in the write-set: the fix needs `orchestrator.py`
(FORBIDDEN) and a `daemon.py` edit beyond the one-log-line allowance.
Reproduction (scratch, executed then deleted): daemon gate passes with
`rt.settings.allocator.enabled=True`, allocator built without settings
falls back to `get_settings().allocator` (global disabled) → outcome
`"disabled"` logged as a completed pass — the exact split REV names.
Required change (for a task owning both files):
1. `orchestrator.py::run_allocation(self, *, limit=25,
   settings: AllocatorSettings | None = None)` → pass
   `settings=settings or get_settings().allocator` to `allocate_attention`.
2. `daemon.py::_run_allocation_tick` → `run_allocation(settings=
   self._rt.settings.allocator)`.
3. Test: Runtime with non-global `Settings` → tick gate and allocator
   gate agree (completed pass vs `None`, not `disabled`).
Same chain also carries REV R-W5-008's production half:
`daemon.py:418` `MemoryCurator(...)` gains `cost_gate=self._rt.settings.
cost_gate` (curator already accepts it; tests prove both directions).
NEED posted in COMMS 19:45Z. No failing test committed for this (a red
pin would break the suite; the scratch repro is the evidence).

## 8. Long-run test

- **Source**: all premises verified at file:line in §2; cursor fix
  derives from the DDL type difference (TEXT vs UUID), not from tests.
- **Load (100x)**: cost-gate pass output now bounded (100 entries + 1
  summary line + ≤100 per-fact lines) regardless of fact count; scan
  stays O(N) keyset batches (necessary for correctness). Attention
  panel unchanged complexity (full-lineage reads ≤50 lineages — flagged
  earlier as NOT WORTH IT; untouched).
- **Hostile input**: hostile stored rows still error-skip per row
  (unchanged path, in the rewritten loop); id cursor is DB-sourced and
  parameterized; producer lock enumerates the tree deterministically.
- **Operability**: rollback = flag off (cost gate), `ledger=` unwrap,
  reverting any single hunk fails its named tests. Observability:
  `cost_gate.suggestion_summary`; production DRAIN_THROTTLED now live
  via R-W5-001 (previously log-only).
- **Reversal**: see §4 mutation list — every fix has a test that fails
  when the fix is reverted, except the documented-behavior pins
  (terminal drop, default-inert) by design.

## 9. Skipped per brief (not in §3 scope)

R-W5-009/010 NITs (seen/recorded, REV §4); TREV minors outside §3
(exact-threshold R-W5-007, attention malformed/tie/bound R-W5-008,
hostile-row/summary R-W5-009 except the pagination half which §3
required, block-emit asymmetries R-W5-010, isolation nit R-W5-011);
PG actual run (NOT-RUN, no DSN — cursor PG-shape pinned by spy test,
stated honestly, never claimed green).

## 10. Git state

- Branch [RAN `git branch --show-current` → `ops/20261107-integration-fix`].
- Code+tests commit: `c2e746c09fb82bea3dfff88abfbbea8c75019813` (4 src + 4 test files, explicit paths).
- Report commit: separate docs-only commit (this file) listing `c2e746c09fb82bea3dfff88abfbbea8c75019813`.
- COMMS.md: 3 appends (START, CLAIM, NEED), left uncommitted for ORCH.
- `docs/agent-ops/**` itself is excluded from the docs lock; no other
  docs touched.
- Pre-existing untracked junk from siblings (`briefs/W4-*.md`,
  `reports/w4-review/*`) untouched.
