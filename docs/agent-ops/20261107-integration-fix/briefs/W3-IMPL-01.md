# Brief — W3-IMPL-01 (IMPL) · wave 3 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-009 tier sweeper (T-009c) — dry-run-default sweeper that moves facts to archive per ADR-0037 precedence, emitting `TIER_DEMOTED`/`TIER_ARCHIVED` events BEFORE hot-row moves, idempotent by the full ADR-0008 key. Why: vocabulary (W2 T-009a) + columns/backfill (W2 T-009b) exist; without the sweeper the tier layer never moves a fact and T10/T10b's sweeper half is unproven.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (ledger-before-side-effect — event BEFORE row move, always; snapshot+recovery rule for new state; never append outside `ledger/store.py` transaction; S608 per-line rule)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (hotspots; baseline)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P09 row (sweeper, dry_run, precedence, idempotency, ordering after curator DECAY, rejection of replacing matrix) + §H T10/T10b + §J/§K (dry_run=True for 1 week, point of no return = first non-dry-run tick, `tier.moves` observability) + §I (tier-vs-floor union case)
5. ADR `docs/adr/0037-tier-migration-and-replay-rule.md` (FULLY — precedence union, ttl map, discriminator, dry_run, event-before-move, recovery-already-done semantics) + ADR-0008 idempotency key (find enforcement in `ledger/store.py`/events)
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` §P-009 (sweeper mechanics: DecaySweeper keyset scan 500 batch, event-before-row-move, double-emit dedupe, crash between event and row-move → already-done; `recovery.py` vacuous — verify, don't invent) + `W0-INV-02.md` §P-009 (DDL contract you consume: tier/tier_updated_at, T2 default)
7. W2 evidence: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-02.md` (vocab API: `TierPolicy`, `apply_tier_event`, `TierPolicy.resolve` for validation) + `W2-IMPL-03.md` (v5 columns/backfill/CHECK). `from paa.memory import TierPolicy` now works (W2-FIX-01 export).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/memory/decay.py` (`DecaySweeper.sweep` keyset scan, immutable/unknown handling, `touch_many`), `src/paa/memory/curator.py` (6 phases; DECAY ordering), `src/paa/config.py` `TierSettings` (declared W1 — CONSUME only, do not edit), `src/paa/ledger/*` (how to append events — the store's transaction boundary; `create`/`append` shape + ADR-0008 discriminator construction), existing curator/decay tests for the injected-clock pattern.

## 3. Task and acceptance criteria
Task T-009c-P009c. Specifically:
- New tier sweeper: new `src/paa/memory/tier.py` (preferred over growing decay.py — one module per concern; decay.py owns eviction, tier.py owns moves) or a function in `decay.py` if the constructor/injection surface demands it — implementer's proven choice, recorded. It scans hot facts (bounded keyset batch, reuse DecaySweeper's 500-batch scan idiom), computes due moves from `TierPolicy.ttl_hours` (past `tier_updated_at` + ttl → demote/archive per ladder T0→T1→T2→T3→T4), respects ADR-0037 UNION precedence (retention decides survival, `prune_floor` decides hot-set membership — a tier move never deletes a fact the decay guarantee keeps), skips unknown domains (skip-and-count, mirror DecaySweeper) and immutable facts, and honours `TierSettings.dry_run` (default True: log moves, write zero events, mutate zero rows; assert in tests).
- Per move: append the ledger event FIRST (`TIER_DEMOTED`/`TIER_ARCHIVED`, payload `{fact_id, from_tier, to_tier, reason}` — read ADR-0037 for the exact `reason` values), THEN update the hot row (`tier`/`tier_updated_at`). Idempotency: double-emit with the same ADR-0008 key → single event (test); crash between event and row-move → recovery sees event-without-move as already-done, no double-archive, no re-emit (test via simulated crash/sweep-again). Use the full key `(correlation_id,event_type,attempt,discriminator)` — find the store's dedupe path, do not invent one. Ordering: run after curator DECAY phase in the same tick (if a hook exists — new loop/function called from curator's DECAY tail or daemon maintenance loop is the W3 call; inspect how curator phases are invoked and attach at that seam with a one-line callsite; do NOT restructure the loop).
- `TierSettings.dry_run` wiring: the sweeper takes its settings via constructor injection (follow the file's existing injection pattern — if DecaySweeper takes no settings, take a `dry_run: bool` param with the TierSettings value passed by the caller; do not read global settings inside the sweeper). `config.py` NOT edited (declared W1).
- Observability: log `tier.moves` structured events (plan §K metric name) on each move + dry-run moves.
- Tests (must FAIL before, pass after): T10 — injected clock `now=T0+4h1s, tier=T0, dry_run=False` → fact moved to archive tier per ADR-0037, `TIER_ARCHIVED` precedes row move (assert event-ledger order + final row state); boundary T0 at exactly 4h (no move) vs 4h+1s (move); empty hot table; unknown-domain skip; immutable skip. T10b-sweeper-half: replay from scratch (delete projection, replay incl. TIER_DEMOTED/ARCHIVED) → tier dict equals live hot column (use W2 `apply_tier_event` fold). Dry-run: `dry_run=True` → zero events + zero row mutations (assert both). Idempotency/crash: double-emit + crash-between tests above. Hypothesis property (plan §A): tier TTL never shortens decay guarantee — extend the `tests/memory/test_decay.py` pattern ONLY if the ADR names a property test; otherwise assert the union rule with a concrete case where tier would archive but decay floor keeps (no Hypothesis invention beyond ADR-0037). New test file `tests/memory/test_tier_sweeper_p009.py`.
- Trace tag `PLAN:integration-fix:P009` in the sweeper module.
- Acceptance: T10 + tier-rebuild + dry-run + idempotency/crash tests green with fail-before shown; `tests/memory/test_decay.py` unchanged/green; ruff 0, mypy total stays 3; no schema/migration/config edits (`git diff --stat` proves it).

## 4. Ownership
WRITE-SET (only these): `src/paa/memory/tier.py` (new), `src/paa/memory/curator.py` (DECAY-tail callsite only if the hook demands it — minimal), `tests/memory/test_tier_sweeper_p009.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `src/paa/memory/decay.py` (unless the hook genuinely needs it — then post NEED and stop; decay.py eviction stays untouched), `src/paa/ledger/*`, `src/paa/storage/relational/*`, `docs/adr/*`, `src/paa/memory/domains.py` (W2-owned, consume TierPolicy), `src/paa/memory/world_model.py`.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing the curator callsite (shared file).

## 5. Parallel siblings this wave
W3-IMPL-02 (vault — `world_model.py`/`reflection.py`), W3-IMPL-03 (panels — `tui/*`/`api/*`/`facts.py`), W3-IMPL-04 (coder fallback — `coder/runtime_tools.py`/`runtime.py`). Disjoint; you own the tier sweeper exclusively. `tests/memory/test_decay.py` is a shared regression guard — nobody edits it this wave.

## 6. Skip list and known traps
- Dry-run default lives in Settings (W1) — your sweeper must default to the Settings value the caller passes; never hardcode `dry_run=True/False` inside the sweeper (plan: no hardcoded defaults). The first DELETING tick is the point of no return (plan §K) — your tests must prove dry-run writes nothing, because production still starts dry.
- Event-before-move ordering is the invariant — a test that only checks final row state without event order does not satisfy T10.
- Do not add tier filtering to retrieval (INV-01: retrieval unaffected unless ADR says so) and do not widen `_FACT_COLUMNS` (select tier only in the sweeper).
- Baseline failures A–D are not yours. `tests/memory/test_decay.py` must pass UNCHANGED (derivation math untouched).

## 7. Environment and tests
Scoped only: your new test file + `tests/memory/test_decay.py` + `tests/ledger/test_replay.py` + ruff/mypy scoped. Tmp DB fixtures only (never a shared DB). No full suite, no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-009c-P009c`, `Agent: W3-IMPL-01`, `Wave: W3`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-01.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-01.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile/operability/reversal incl. rollback=flag-off). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
