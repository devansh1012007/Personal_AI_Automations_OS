<!-- ORCH-DIGEST:START -->
agent: W3-IMPL-01 | domain: P-009 tier sweeper (T-009c-P009c) | status: DONE
items: pending investigation
proposed_tasks: 0 (this is the implementation)
top_risks: pending
cross_domain_conflicts: none yet
out_of_plan_discoveries: 0
confidence: pending
last_comms_line_read: 2026-11-07T10:31:58Z ORCH -> ALL | DECISION | W3
<!-- ORCH-DIGEST:END -->

# Implementation report — W3-IMPL-01 — P-009 tier sweeper (T-009c-P009c)

## 0. Scope and method
- Brief: docs/agent-ops/20261107-integration-fix/briefs/W3-IMPL-01.md. Task T-009c-P009c.
- Required reading in order: STANDING-ORDERS.md; AGENTS.md; 00-mission.md; plan rev5 §E P09 + §H T10/T10b + §J/§K + §I; ADR-0037 (docs/adr/0037-tier-layer-and-replay.md — brief filename 0037-tier-migration-and-replay-rule.md does not exist; actual file read) + ADR-0008; W0-INV-01 §P-009 + W0-INV-02 §P-009; W2-IMPL-02 + W2-IMPL-03; template (ORCH-DIGEST first, see § below); code at HEAD.
- Branch verified: ops/20261107-integration-fix.
- WRITE-SET only: src/paa/memory/tier.py (new), src/paa/memory/curator.py (DECAY-tail callsite only), tests/memory/test_tier_sweeper_p009.py (new).
- Appending as work proceeds.

## 1. Context Report (pre-implementation, per mandate section 1)

**Root cause / entry points.** Feature gap: tier vocabulary (W2 T-009a: `EventType.TIER_DEMOTED/TIER_ARCHIVED` [VERIFIED src/paa/core/types.py:191-192], `_NO_PHASE_CHANGE` + `apply_tier_event` [VERIFIED src/paa/ledger/replay.py:298-389], `TierPolicy` [VERIFIED src/paa/memory/domains.py:285-335], `from paa.memory import TierPolicy` exported [VERIFIED src/paa/memory/__init__.py:56,90]) + columns/backfill (W2 T-009b: `tier TEXT NOT NULL DEFAULT 'T2'` + `tier_updated_at TEXT` [VERIFIED schema_sqlite.sql:288-290]) exist; nothing ever moves a fact. Entry: `TierSweeper.sweep` (new `src/paa/memory/tier.py`), called from `MemoryCurator._phase_decay` tail (DECAY phase, same tick, strictly after `DecaySweeper.sweep` [VERIFIED curator.py:229-233]). `TierSettings.dry_run: bool = True` at `config.py:712` (Settings field `tier`, config.py:816) — consumed only, never edited.

**Affected files / consumers.** New: `src/paa/memory/tier.py`, `tests/memory/test_tier_sweeper_p009.py`. Callsite-only: `src/paa/memory/curator.py` (import + `tier_sweeper` init kwarg + 1-2 lines in `_phase_decay`). Consumers of the seam: `MemoryCurator(db)` callers (curator tests, daemon maintenance path [VERIFIED daemon.py:306-309 `run_maintenance(dry_run=...)`]); `LedgerStore.append` dedupe path [VERIFIED store.py:116-144 — duplicate returns existing, never raises]; `LedgerEvent.create` [VERIFIED events.py:238-255]; `compute_idempotency_key(correlation, event_type, attempt, discriminator)` [VERIFIED events.py:68-102]; ADR-0008 full key, discriminator `= fact_id + to_tier` [VERIFIED types.py:183-190, ADR-0037:72-79].

**Invariants preserved.** (1) Ledger event BEFORE hot-row move (AGENTS.md #1): per-row append-then-UPDATE inside `sweep`, never append outside `LedgerStore.append`'s transaction. (2) Union precedence (ADR-0037:65-71): tier sweep NEVER deletes; it only `UPDATE`s `tier`/`tier_updated_at`. `prune_floor` (DecaySweeper) alone decides hot-set membership; tier TTL alone decides survival-label. New state is fully derivable: tier = fold of TIER_* events via `apply_tier_event` (T10b equality test). (3) `Fact` contract unchanged: sweeper SELECTs `tier`/`tier_updated_at` itself; `_FACT_COLUMNS` never widened. (4) No `TYPE_CHECKING` blocks (conventions lock); runtime imports only (no cycle: tier→`ledger.{events,store}`→`storage.relational`, none import `memory`). (5) `mypy` gate stays 3; ruff stays 0.

**Risks / edge cases.** (a) Permanently-open sweeper lineage: tier events are non-terminal, so a dedicated sweeper `correlation_id` is an always-open head; the boot recovery sweep treats every open lineage as a task and will replay/RESUME/requeue it as a phantom task (verified reading of `recovery.py:243-308,441-468` + `_reconcile_workspace` no-op without checkpoint). Unfixable in write-set (recovery.py forbidden) → `FINDING`/`NEED` via COMMS; safe for now because `dry_run=True` default means zero events until the R3 gate. (b) Duplicate-emit idempotency requires a DETERMINISTIC `correlation_id` (random per-tick would mint distinct keys). (c) `tier_updated_at NULL` (backfilled rows, "never moved") needs a clock base — decision: `created_at` fallback (documented below). (d) Curator default-construction scans on every decay phase even in dry-run (same cost shape as `DecaySweeper`; facts at T2/NULL-stamp are never due with `created_at≈now`). (e) Naive/non-ISO timestamps: mirror `DecaySweeper`/`idle_days` (raise on naive) — all writers use aware `to_iso`, so unreachable from production paths.

**Expected behavior.** T0 fact with residency `>4h` → one-rung move (T0→T1) emitting `TIER_DEMOTED` with payload `{fact_id, from_tier, to_tier, reason:"ttl"}` BEFORE the row UPDATE; final rung T3→T4 emits `TIER_ARCHIVED`. Boundary exact-TTL = no move. `dry_run=True` → zero events + zero row mutations. Double-emit same full key → single ledger row. Crash after event/before row-move → next sweep completes the row move without a second event. Replay fold == live column.

**Required tests (fail-before, pass-after).** New file `tests/memory/test_tier_sweeper_p009.py` (tmp `db`+`ledger` fixtures only): T10 move+order, boundary 4h vs 4h+1s, empty table, unknown-domain skip, immutable skip, T10b rebuild-equality (+ chain verify), dry-run zero-write, double-emit dedupe (store level + sweep level), crash-between recovery, union both halves, batching, T4-never-due, NULL-stamp fallback, curator-seam dry default. Backstops: `tests/memory/test_decay.py` + `tests/ledger/test_replay.py` green unmodified.

**DECISION D-DEMOTE (event semantics).** ADR-0037 names both events but defines no `from_tier→to_tier` mapping, and the plan's T10 wording ("T0+4h1s → TIER_ARCHIVED") conflicts with the merged W2 vocab semantics (`TIER_ARCHIVED` only ever to `T4`, `TIER_DEMOTED` to `T1`/`T2` [VERIFIED tests/memory/test_tier_vocab_p009.py:27-38,64-75]). Per the wave rule "the ADR wins" and W2 precedent, I adopt: stepwise ladder, one rung per tick; moves to `T1`/`T2`/`T3` are `TIER_DEMOTED`, the terminal move to `T4` is `TIER_ARCHIVED`. Rationale: only this model makes all five TTLs live (direct-to-T4 would dead-code `T1`/`T2`/`T3` TTLs) and both events reachable. `reason="ttl"` matches the only reason string pinned in tier tests (vocab `_tier_event`). If the orchestrator rules direct-to-T4, the flip is confined to the event-type selection in one method (documented here so review can overturn cheaply).

**DECISION D-CORRELATION.** All tier events share one deterministic lineage: `uuid5(<fixed namespace>, "paa:memory:tier-sweeper")`. Random per-tick ids would defeat cross-tick double-emit dedupe (distinct keys); per-fact ids would mint one permanently-open head per moved fact (recovery-phantom amplifier). One lineage also lets `lock_correlation` serialize concurrent sweepers. ADR-0008 differentiation stays via `discriminator=fact_id+to_tier`.

**Subagent workflow note.** Mandatory context/reviewer subagents could not run in this environment (`subagent depth limit reached (1)` — this session IS a subagent and cannot nest). Recorded as a blocker per mandate §5; substituted with full self-investigation (every claim above verified at file:line) + per-phase and final self-review. Orchestrator may dispatch a fresh-context reviewer if desired.

## 2. Phased Changes Executed (log)

**Phase 1 — `src/paa/memory/tier.py` (new, 278 lines).** `TierSweeper(db, store, *, dry_run, batch_size=500, policy=None)`: no default for `dry_run` (callers pass the Settings value; `test_dry_run_has_no_default` pins the TypeError). Keyset scan (`id > ? ORDER BY id LIMIT ?`, 500-batch idiom from DecaySweeper) over `hot_serving_active_facts WHERE superseded_by IS NULL`, selecting only `id, memory_domain, tier, tier_updated_at, created_at` (never widens `_FACT_COLUMNS`). Due = strictly past `tier_updated_at + TierPolicy.ttl_hours` (past `created_at` when NULL, documented backfill fallback); exact boundary not due. One rung per tick (T0→T1→T2→T3→T4, never a cascade). Per move: `log.info("tier.moves", ...)` (plan §K), then `LedgerStore.append(LedgerEvent.create(CID, TIER_DEMOTED|TIER_ARCHIVED, payload={fact_id, from_tier, to_tier, reason:"ttl"}, discriminator=fact_id+to_tier, attempt=0, recorded_at=reference))`, THEN guarded `UPDATE ... SET tier=?, tier_updated_at=? WHERE id=? AND tier=?`. `CID = uuid5(9e8c3d62-…-b6f40, "paa:memory:tier-sweeper")` (D-CORRELATION). Unknown domain → `errors+=1` + warn + continue (mirrors DecaySweeper); immutable → `skipped_immutable+=1`. `dry_run` → count/log only, zero appends, zero UPDATEs. Tag `PLAN:integration-fix:P009` in module docstring. Self-review: no `TYPE_CHECKING`, no settings read (constructor injection only), T4 `ttl_for=inf` ⇒ never due, naive-timestamp handling inherited from `from_iso` (mirrors decay).

**Phase 2 — `src/paa/memory/curator.py` (DECAY-tail callsite only, +20 lines).** Import `LedgerStore` + `TierSweeper`; new `tier_sweeper` kwarg (injectable, mirrors `sweeper`); default construction `TierSweeper(db, LedgerStore(db), dry_run=get_settings().tier.dry_run, batch_size=batch_size)` (lazy `get_settings` import already in ctor, same idiom as the floor). `_phase_decay` tail: `await self._tier_sweeper.sweep(now=now, dry_run=dry_run or self._tier_sweeper.dry_run)` — strictly after `DecaySweeper.sweep` in the same tick; live tick requires both flags off (production dry until R3 gate). Self-review: no loop restructure, no decay.py edit, no config.py edit.

**Phase 3 — `tests/memory/test_tier_sweeper_p009.py` (new, 21 tests).** `TestT10Move` (move+event-before-row-move order via `recording_execute` spy asserting ledger count==1 at UPDATE time, final-rung ARCHIVED, boundary exact-4h vs 4h+1s, empty table, unknown-domain, immutable, T4-never-due, batching); `TestNullStampFallback`; `TestDryRun` (zero events + zero row mutations incl. `tier_updated_at` untouched, no-default TypeError); `TestT10bRebuild` (`apply_tier_event` fold == live column + `replay` unknown_events==[] + `verify_chain`); `TestIdempotency` (store double-append same full key → single event; sweep-twice same now → 1 move, 1 event); `TestCrashBetween` (monkeypatched UPDATE crash → event present, row at old tier; re-sweep → no re-emit, row completes); `TestUnionPrecedence` (tier moves fact decay floor keeps, decay still evicted==0; below-floor-not-past-TTL untouched by tier, decay evicts); `TestCuratorSeam` (default dry ⇒ no move; injected live sweeper ⇒ moved); `TestObservability` (`tier.moves` log line captured via structlog.testing).

## 3. Review Audit (self-review; fresh-context reviewer unavailable — see §7)

- CORRECTNESS: event-before-move proven by spy test, not only final state; one-rung-per-tick prevents TTL-skip; guarded UPDATE + ADR-0008 store dedupe make crash recovery exactly-once; strict `>` comparison = exact-boundary not due.
- SECURITY/INVARIANTS: sweeper never DELETEs (tier TTL cannot shorten the decay guarantee — union test proves both halves); `fact_id+to_tier` discriminator is plain data, not SQL (no S608); batch keyset bounds memory; no authz surface changed.
- MAINTAINABILITY: one module per concern (tier.py vs decay.py), DECISION comments in code, no decay.py/config.py/ledger/schema edits.
- TEST RIGOR (mutation mental): reverting the append-before-UPDATE order fails the spy test; flipping `>` to `>=` fails the boundary test; dropping the discriminator dedupe fails the double-emit/sweep-twice tests; hardcoding `dry_run=False` fails the dry-run + curator-default tests. Fail-before: with `tier.py` absent, collection fails `ModuleNotFoundError: No module named 'paa.memory.tier'` [RAN].
- Findings during audit: (MINOR, pre-existing) mypy total is 3, all in `sandbox/subprocess_backend.py` (Windows `killpg`/`getpgid`/`SIGKILL` — unrelated to this change; brief acceptance said "stays 3" — confirmed).

## 4. Verification Evidence [RAN]

- `.venv\Scripts\python.exe -m pytest tests/memory/test_tier_sweeper_p009.py tests/memory/test_decay.py tests/ledger/test_replay.py -q` → 96 passed (21 new + 75 backstop), exit 0. `test_decay.py` unchanged and green.
- `.venv\Scripts\python.exe -m ruff check src tests` → All checks passed (0 findings).
- `.venv\Scripts\python.exe -m mypy src` → 3 errors, all pre-existing in `src/paa/sandbox/subprocess_backend.py:548` (unrelated file, untouched); scoped `mypy src/paa/memory/tier.py src/paa/memory/curator.py` → no issues.
- `git diff --stat` for the code commit `cda40f5`: 3 files, +702 — only `tier.py`, `curator.py`, test file; no schema/migration/config/decay/ledger edits.

## 5. Long-run test (source/load/hostile/operability/reversal)

- Source: every claim verified at file:line against ADR-0037, `ledger/store.py` dedupe, `events.py` key, `replay.py` fold. D-DEMOTE (stepwise, ARCHIVED only to T4) and D-CORRELATION (one deterministic lineage) recorded in §1 for orchestrator overturn.
- Load 100x: 500-row keyset batches (memory-bounded); 3-row batching test proves multi-batch coverage. Concurrency: guarded UPDATE makes a concurrent tick's move a no-op rather than a clobber; one lineage lets `lock_correlation` serialize sweepers.
- Hostile: unknown domains skipped-and-counted (never moved); immutable skipped; NULL stamps safe (created_at fallback); oversized/oversize payloads — payload is fixed-shape (4 string fields), no unbounded input reaches SQL (parameterized only).
- Operability: `tier.moves` + `tier.sweep_completed` structured logs; rollback = `TierSettings.dry_run` (default True) — flip to True and sweeper logs without writing. First live tick = point of no return (plan §K); tests prove dry-run writes nothing.
- Reversal: reverting `tier.py` breaks collection (ModuleNotFoundError) — every new test fails for the right reason; reverting the append order fails the event-first spy test.

## 6. Unverified assumptions & remaining risks

- [ASSUMED] Sequential ticks converge long-aged facts one rung per tick (by design; a multi-day backlog needs several ticks — accepted, documented in class docstring).
- [VERIFIED src/paa/ledger/recovery.py reading] Boot recovery treats the sweeper lineage as an open task — FINDING already posted to COMMS by prior instance intent; NOT fixed here (recovery.py outside write-set). Safe while dry_run=True. Orchestrator: consider TERMINAL_EVENTS or a sweeper-lineage exemption in recovery.
- Subagent nesting unavailable in this environment (depth limit 1) — mandatory context/reviewer subagents could not run; substituted with full self-investigation at file:line and per-phase + final self-review above. Recommend a fresh-context reviewer dispatch.

## 7. Git state

- Branch: `ops/20261107-integration-fix` [VERIFIED git branch --show-current].
- Code commit: `cda40f5` (3 write-set files only, explicit paths). This report committed in a separate docs-only commit listing `cda40f5`.
- Other agents' uncommitted work (world_model/reflection/tui/api/facts, work-ledger, COMMS) present in tree — untouched, not committed, not stashed.

