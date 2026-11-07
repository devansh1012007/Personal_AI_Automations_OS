<!-- ORCH-DIGEST:START -->
agent: REV-W4 | scope: wave 4 (T-007,T-013,T-003) diff 45b542d..HEAD | verdict: REJECT
findings: BLOCKER 1 | MAJOR 3 | MINOR 4 | NIT 1 (+5 casualty-group ratings, 1 undisclosed second-writer defect)
claims_audit: 21 claims checked / 2 unsupported-or-fragile
band_aids_found: none in src; test-side: _DRAIN_WINDOW_SECONDS-at-EOF docs-shift avoidance (accepted, documented by implementer)
top_3_risks: (1) docs lock red blocks landing; (2) allocator loop unreachable in production (no scheduler, no producer) — P-007 "wired" claim is callable-only; (3) graph SQLite backend writes un-normalised entity rows that entity resolution can never see (missed second writer)
optimizations: DO-NOW 2 | DO-LATER 3 | NOT-WORTH-IT 2
reversal_check: reasoned-only (per-claim reversal lists cross-checked; no code reverted by me — read-only reviewer)
confidence: high (diff + surrounding code read; all gate facts re-run independently; PG paths NOT-RUN — no DSN)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W4 (independent, read-only; did not write this code)

## 0. Method
- Diff reviewed: `45b542d..HEAD` (0136693 allocator+drain, 620ab30 reflection wiring, b14228f stored-normalisation, +3 report commits). 15 src files + 3 new test files read in full context.
- Read first, in order: STANDING-ORDERS.md, templates/review-code.md, 01-work-ledger.md (wave 4), 00-mission.md (baseline), COMMS.md (through line 119). Then requirements: briefs W4-IMPL-01/02/03, ADR-0036 (full), ADR-0029 (via implementer excerpts + code), W0 INV excerpts as cited in briefs.
- Order followed: blind pass on diff + surrounding code (allocator, replay, creator drain, daemon, reflection, migrations v6, facts/dialect, graph backend, store dedupe, metrics guards, daemon stop, TUI runner) → drafted findings → THEN read implementer reports W4-IMPL-01/02/03 → claims audit. Blind findings that the implementers had already disclosed are marked as such; the one major finding they missed (R-W4-004) was found blind.
- Commands run by me (all scoped; no full suite, no containers, no shared DB, no commits, source untouched):
  - [RAN `.venv\Scripts\python.exe -m ruff check src tests` -> All checks passed! (0 findings)]
  - [RAN `.venv\Scripts\python.exe -m mypy src` -> exactly 3 errors, all at `src/paa/sandbox/subprocess_backend.py:548` (baseline; gate holds)]
  - [RAN `pytest tests/context/test_allocator.py tests/improvement/test_reflection_wiring_p013.py tests/storage/test_entity_normalisation_p003.py tests/storage/test_entity_parity.py` -> 76 passed, 130 skipped (PG-gated skips, honest)]
  - [RAN `pytest tests/conventions/test_import_conventions.py` -> 21 passed (allocator imports clean in fresh sweep; the 139/140 transient failure reported mid-wave is resolved)]
  - [RAN `pytest tests/storage/test_schema_parity.py tests/storage/test_like.py tests/ledger/test_replay.py tests/context/test_metrics.py` -> 204 passed]
  - [RAN casualty set (`test_drain_105`, `tests/storage/test_dialect.py` (7), `tests/memory/test_facts.py` (2), `test_schema_version_is_five`, `tests/memory/test_entity_resolution.py` (5)) -> all failures reproduced; each root-caused below]
  - [RAN `pytest tests/conventions/test_docs_references.py` -> 2 FAILED (`test_every_referenced_line_is_not_blank`, `test_every_referenced_line_is_within_the_file`); `dialect.py:588` cited but file has 583 lines]
  - [RAN `git branch --show-current` -> `ops/20261107-integration-fix`]
- COMMS last line read: `2026-11-07T12:55:59Z W4-IMPL-02 -> ORCH | NEED | T-013-P013` (line 119).

## 1. Blind-pass findings (before reading implementer reports)
Drafted blind, then cross-checked against implementer disclosures:

1. **Allocator is callable, not a loop.** `grep run_allocation src/` → exactly one hit (the definition at `orchestrator.py:1117`); nothing in daemon/runtime/orchestrator calls it. `grep WORKERS_ALLOC src/` → consumer only (allocator.py:254) + a docstring example. No producer, no scheduler. The ADR-0036 "loop" exists as a function. Flag default `enabled=False` means production behaviour is unchanged — but "ledger-wired allocator" is not live by any path executable in the shipped tree.
2. **TUI doctor mirror is dead code.** `dashboard.py:416-423` `_reflection_doctor_value` reads `reflection_enabled` / `reflection_last_run_age_days` / `reflection_stale` keys; `grep` across `src/paa` shows `runner.py` never sets any of them → TUI always renders `disabled` even when reflection is enabled and running. Additionally the helper re-implements `format_reflection_health` (reflection.py) instead of calling it — same strings today, two places to drift tomorrow.
3. **A live second writer bypasses normalisation (UNDISCLOSED — the one that matters).** `src/paa/storage/graph/sqlite_backend.py:79-86` `_UPSERT_ENTITY_SQL` inserts `(id, class, canonical_name, created_at, updated_at)` with NO `canonical_name_norm`/`aliases_norm`, and its `ON CONFLICT DO UPDATE` does not refresh them. Consequences: (a) graph-created entities are invisible to the exact rung (`canonical_name_norm = ?` never matches NULL), the candidate query (`LIKE` on NULL), and FTS (trigger indexes NULL); (b) facts→graph on the same id leaves a stale fold after a rename; (c) graph→facts on the same name: facts' SELECT by norm misses the graph row and attempts a duplicate INSERT → `UNIQUE(canonical_name)` violation. This breaks the plan's "one normalisation point" claim. No in-tree prod caller of `GraphStore.upsert_entity` was found in `src/paa` (latent, substrate-contract break; kuzu backend unaffected — it does not touch this table).
4. **Notify-failure comment is false.** `allocator.py:332` "decision stands; notify waits for reclaim" — but on reclaim, `store.append` returns the existing event whose `event_id` differs from the freshly constructed event's uuid4 (`ledger/events.py:154`), so the duplicate branch (allocator.py:340-341) skips notify and acks. Notify after a transient enqueue failure is permanently lost — exactly what the module docstring (allocator.py:33-34) admits is accepted, but the inline comment claims the opposite. Also the failed message is left unacked, causing one pointless reclaim cycle.
5. **Claim failure propagates.** `queue.claim` raising (backend down) escapes `allocate_attention`; the sole caller `orchestrator.run_allocation` has no guard. Against the house "factories degrade, never raise" posture; docstring only promises no-raise for poison/empty.
6. **Backfill is destructive-fail-soft + unbounded.** `_backfill_entity_norm` (migrations.py): a corrupt/unparseable `aliases` blob backfills to `'[]'` and is then marked done by the guard — the alias data is silently replaced with empty and never revisited. `fetchall()` loads the whole NULL-row set; per-row UPDATEs in one migration transaction (largest lock/memory hold in this wave). Canonical `None` → `str(None)` = `"none"` (unreachable only if `canonical_name NOT NULL` holds — it does, but the code does not rely on it explicitly).
7. **`DRAIN_THROTTLED` has zero emitters** (grep: types.py + replay.py only). Verdict in §4/R-W4-006: sound vocabulary-only step, not a defect — but no ledger task owns the follow-up emitter.
8. **Docs lock red.** Citations past EOF (`dialect.py:588`, file 583 lines) + blank-line citations in facts/daemon. Standard wave process item; gate-blocking.
9. Nits: `orchestrator.run_allocation -> Any`; `_correlation_for` silently falls back to uuid5 on a malformed payload `correlation_id` instead of poisoning; `inputs` dict shared mutable across event/notify/decision; reflection marker stamped with tick *start* time; future marker → sleep > 7d (delay formula); below-cut items acked with no ledger record anywhere (by design — top-N only — but then "decisions are ledger events" strictly covers only top-N).

Break attempts that FAILED (code held): naive-datetime marker crash (from_iso attaches UTC — `database.py:71-73`); shadowed `run_weekly` crash (W3 guard intact, loop logs and continues); replay totality for new events (`replay.py:189-197` → `_NO_PHASE_CHANGE`, no match arm, falls through); store dedupe (`store.py:116-144` returns existing under per-lineage lock); score NaN/inf/out-of-range (metrics `_unit`/`_finite` raise ValueError → nack, `metrics.py:88-107`); FTS trigger crash-mid-migration (single transaction, `migrations.py:2000-2003`, re-runnable guards); shutdown while reflection waits on `_curation_lock` (cancellable, `daemon.py:91-100`).

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| ruff 0 full tree | Yes | [RAN ruff check src tests -> 0] |
| mypy total 3 = baseline | Yes | [RAN mypy src -> 3 at sandbox/subprocess_backend.py:548] |
| W4 scoped green (1 PG skip) | Yes | [RAN scoped set -> 76 passed, 130 skipped] |
| test_105 is stale (throttle caps 100/window) | Yes | [RAN -> assert 0 == 5 at test_creator.py:508; throttle log present]. Rated STALE in §4. |
| test_dialect 7 failures from new columns | Yes | [RAN -> 7 FAILED; 6 are stale `lower(e.canonical_name)` literals/fixtures, 1 hostile fixture lacks norm columns]. Rated STALE in §4. |
| test_facts 2 failures from column change | Yes | [RAN -> 2 FAILED]. Corrupt-row: STALE (corrupts raw blob; rung now reads norm). Gate-model: REAL behaviour change for foreign rows — consequence of the same class as R-W4-004. |
| test_tier `test_schema_version_is_five` fails (now 6) | Yes | [RAN -> assert 6 == 5]. STALE expectation; SCHEMA_VERSION=6 is the intended change. |
| test_entity_resolution 5 failures (IMPL-03 only; orchestrator gate omitted) | Yes | [RAN -> 5 FAILED; raw `_insert` helper at test_entity_resolution.py:358 writes no norm cols]. STALE fixtures. Implementer was right; gate list undercounted. |
| Docs lock FAILS 2 tests (not_blank + within_file) | Yes | [RAN -> 2 FAILED; dialect.py:588 past EOF (583 lines)] |
| DRAIN_THROTTLED deferred as vocab+totality (no creator ledger handle) | Yes, accurately disclosed | [VERIFIED grep DRAIN_THROTTLED src/ -> types.py:228 + replay.py:397 only]. Sound per ADR-0036 (see R-W4-006). |
| Store dedupes by full key, returns existing event | Yes | [VERIFIED src/paa/ledger/store.py:116-144] |
| Ledger-before-notify-before-ack; poison nack; idempotent reclaim | Yes | [VERIFIED allocator.py:308-342] + [RAN test_allocator.py green] |
| Reflection: exactly one production caller of run_weekly | Yes | [VERIFIED grep run_weekly src/ -> daemon only] |
| TUI mirror renders disabled default until runner wires keys (NEED posted) | Yes, accurate disclosure | [VERIFIED dashboard.py:416; grep runner.py -> no reflection keys]. Gap real (R-W4-003). |
| "Daemon claiming not wired; queue stays empty until operator wires loop" (IMPL-01 §5) | Yes, accurate disclosure | [VERIFIED grep run_allocation src/ -> definition only]. Gap real (R-W4-002). |
| Migration runner holds advisory lock (PG) / serialises (SQLite) | Yes | [VERIFIED migrations.py:150-156, 2000-2003] |
| All 5 MemoryCreator sites light the knob without edits | Yes | [VERIFIED grep MemoryCreator( src/ -> 5 sites incl. coder/runtime_tools.py:620] |
| Import sweep 139/140 failure caused by sibling allocator.py | Superseded (transient) | [RAN test_import_conventions.py -> 21 passed]. Resolved at HEAD. |
| "Value strings byte-identical between CLI and TUI surfaces" (IMPL-02) | Partly (true today, fragile) | TUI re-implements the formatter (dashboard.py:414-423) instead of calling `format_reflection_health` — strings match now, drift risk unpinned by construction. |
| "22 lock-blind stale cites; 0 lock-visible blanks from my 4 files" (IMPL-01 F6) | Count UNKNOWN | Lock failures at HEAD are in sibling files (facts/daemon/dialect) [RAN], consistent with the claim; exact count not re-measured by me. |

Claims checked: 21 · unsupported-or-fragile: 2 (string-identity-by-duplication; citation count).

## 3. Long-run test (independent answers)
- **Source:** Allocator decisions are ordinary ledger events → rebuildable by replay; replay totality verified (`_NO_PHASE_CHANGE`, [RAN replay suite green]). Reflection marker is explicitly bookkeeping-not-truth (losing it costs one gated re-run) — acceptable. Backfill is a deterministic Python fold, idempotent and re-runnable. The one exception: below-cut allocator items leave NO record; after ack there is no way to know they were ever scored. Accepted by top-N design; noted, not a defect.
- **Load (100x / concurrency / partial failure):** Allocator claim bounded (`limit=25`); double-run dedupes via store key (verified path). Drain window caps sustained ingest without stalling retries (counts indexed, not attempted — correct choice). Reflection cost is one `events_since` read/week. Weak point: v6 backfill is full-table `fetchall` + per-row UPDATE in ONE migration transaction — at 100x entity scale this is the wave's largest memory/lock hold. DO-LATER chunking.
- **Hostile input:** All allocator poison classes nack (non-dict, bool, wrong-type, NaN/inf, out-of-range, F≤0 — score guards are the range check, [VERIFIED metrics.py:88-107]). Malformed marker degrades to unknown/due, never crash [VERIFIED reflection.py:139-165 + from_iso UTC-attach]. `LIKE` still comes from `like.py` (no new f-string patterns; [VERIFIED ruff 0 incl. S608]). Residual hostile surface: a raw-SQL writer (or the graph backend) creates norm-NULL rows that are silently invisible — no probe/count anywhere. DO-LATER: startup `COUNT(*) WHERE canonical_name_norm IS NULL` gauge.
- **Operability:** Rollback is clean: `allocator.enabled=False` (loop stops, gatherer unaffected), raise `drain_max_per_second`, `PAA_REFLECTION__ENABLED=false`, delete the marker file. v6 one-way: old code ignores the new nullable columns; downgrade requires dropping FTS triggers first (implementer's own finding — recorded, not in any runbook). Observability: `allocator.*`, `memory.embedding_drain_throttled`, `daemon.reflection_*`, doctor row — adequate; norm-NULL probe missing (above).
- **Reversal (which test fails if the fix is reverted?):** Removing `_NO_PHASE_CHANGE` entries → allocator replay test fails (unknown_events non-empty). Swapping emit/notify → call-order test fails. Removing throttle window → T15 cap test fails (and, notably, casualty C1 test_105 would flip back to PASSING — confirming it pins pre-throttle behaviour). Reverting dialect columns to `lower(raw)` → éclair/İ/CAFÉ NOIR tests in the new P003 file fail. Removing v6 → version/backfill/guard/shape tests fail. These agree with implementers' lists [checked against their reports].

## 4. Findings
### R-W4-001 [BLOCKER] Docs-reference lock red: citations past EOF + on blank lines
- Location: `tests/conventions/test_docs_references.py` (2 failing tests); citations in `docs/adr/0029-entity-resolution-parity.md:301` (`dialect.py:588`, file now 583 lines), `docs/adr/0029…:160` (`facts.py:203` blank), `docs/data/data-models-and-relationships.md:138` (`daemon.py:130` blank), et al. · Evidence: [RAN pytest tests/conventions/test_docs_references.py -> 2 FAILED] · Why: the wave's insertions/deletions shifted lines in types/replay/creator/daemon/facts/dialect; the gate cannot land red, and per AGENTS a corrected citation must be read, not blindly renumbered. W4-IMPL-01 measured 22 lock-blind companions in addition — the lock-visible failures are the smaller half. · Fix: W4-FIX-DOCS re-point task (W1/W2/W3 precedent); the `_DRAIN_WINDOW_SECONDS`-at-EOF dodge worked for creator.py but not the other five files.

### R-W4-002 [MAJOR] Allocator loop has no scheduler and no producer — "wired" is callable-only (DISCLOSED DEFERRAL)
- Location: `src/paa/agents/orchestrator.py:1117-1145` (`run_allocation`, zero callers), `src/paa/context/allocator.py:254` (sole consumer), nothing enqueues `workers:alloc` in `src/`. · Evidence: [VERIFIED grep run_allocation src/ -> 1 hit = definition; grep WORKERS_ALLOC -> consumer + docstring example]. W4-IMPL-01 §5 disclosed this ("daemon-side claiming deliberately NOT wired; the queue will stay empty"). · Why: ADR-0036 specifies a loop; the brief's acceptance letter was met (def + orchestrator caller), but the spirit — the same "engine tested, never scheduled" disease P-013 just cured for reflection — repeats. Side effect: W3's `attention_top` panel (`runner.py:256`, hardcoded `None` until P-007) stays null forever until this follow-up lands. Default-off flag keeps production safe, so MAJOR not BLOCKER. · Fix: follow-up task — daemon tick (mirror the reflection loop's shape) or orchestrator scheduling of `run_allocation`, plus a named producer decision (or an ADR amendment stating the stream is operator-fed). Track in ledger; do not silently treat P-007 as "live".

### R-W4-003 [MAJOR] TUI doctor mirror never shows reflection state (DISCLOSED NEED)
- Location: `src/paa/tui/dashboard.py:362-423`; missing population in `src/paa/tui/runner.py` (`_build_info`). · Evidence: [VERIFIED grep reflection_enabled src/ -> dashboard.py only]. CLI doctor works [VERIFIED cli/main.py:275-288]; TUI renders `disabled` in all states. The helper also duplicates `format_reflection_health` logic instead of sharing it. · Fix (small, cross-ownership): populate the three keys in `runner._build_info` from `reflection_health(settings.home, enabled=…)` and have `_reflection_doctor_value` call the shared formatter (or accept the health dict). NEED already posted by W4-IMPL-02; convert to a fix task.

### R-W4-004 [MAJOR] Graph SQLite backend is a second, un-normalised writer of the entity table (UNDISCLOSED — missed)
- Location: `src/paa/storage/graph/sqlite_backend.py:79-86` (`_UPSERT_ENTITY_SQL`), `:144-149` (`upsert_entity`). · Evidence: [VERIFIED by read + grep: the only other `INSERT INTO hot_serving_entity_index` in `src/` besides `facts.py:448`; kuzu backend does not touch this table]. Blind-found; not mentioned in W4-IMPL-03 report (INV-02 scoped the write path to `upsert_entity` in facts and missed this). Effects: graph-written entities are invisible to exact/alias rungs, candidate query, and FTS; rename-via-graph leaves a stale `canonical_name_norm`; facts-after-graph on the same name → `UniqueViolation` on `canonical_name` (reproduced the failure class in tests/memory/test_entity_resolution.py output [RAN]). Latent today (no in-tree prod caller of `GraphStore.upsert_entity`) but it breaks the substrate contract and the plan's "one normalisation point" claim exactly where parity is supposed to hold. · Fix: include `canonical_name_norm`/`aliases_norm` in `_UPSERT_ENTITY_SQL` (both INSERT and DO UPDATE), compute via `fold_case`, add a cross-writer round-trip test (graph-upsert → facts-resolve finds it).

### R-W4-005 [MINOR] Notify-failure path contradicts its own comment; notify is lost on reclaim
- Location: `src/paa/context/allocator.py:332-341`. The comment "notify waits for reclaim" is false: on redelivery the store returns the existing event (different `event_id` than the fresh uuid4), the duplicate branch skips notify and acks. Implementer's F4 honestly documents at-most-once notify in the module docstring; the inline comment says the opposite. The unacked failed message also costs a reclaim cycle. · Fix: correct the comment to state the accepted loss; optionally ack-on-notify-failure (decision already persisted — ack is safe) to avoid churn.

### R-W4-006 [MINOR] DRAIN_THROTTLED emitter deferred — verdict: SOUND, not a defect; track it
- Location: `src/paa/core/types.py:218-228`, `src/paa/ledger/replay.py:397`, creator throttle at `src/paa/memory/creator.py:1542-1580`. · Reviewer verdict requested by orchestrator: **a sound vocabulary-only step, not a defect.** Why: ADR-0036's Decision orders the drain to "defer and log backpressure — never drop silently" (implemented + T15-tested); the Consequences section anticipates "two new EventType members … as the implementer splits them" (both exist); the brief's acceptance requires log + defer only. The creator holds no ledger handle; inventing one would have exceeded the write-set. The empty member + replay entry costs nothing (replay stays total for any lineage carrying it) and the types.py comment discloses the deferral honestly. · Remaining gap (why MINOR, not NIT): no ledger task owns the future emitter — a deferral with no owner is how vocabulary rots. Recommend a named residual in the work ledger (creator-side ledger path) and NOT landing it as "done telemetry". No code change required this wave.

### R-W4-007 [MINOR] Allocator propagates queue-backend failures; also `run_allocation -> Any`
- Location: `allocator.py:254` (`queue.claim` unguarded), `orchestrator.py:1117` (returns `Any`, loads settings every call, no failure mapping). A queue outage makes `run_allocation` raise, where the house posture is degrade-and-log. Fix: catch claim failure → log + return an error/underfed result; type the return as the allocator's `AllocationResult` via `TYPE_CHECKING` import.

### R-W4-008 [MINOR] v6 backfill: unbounded read, destructive fail-soft for corrupt alias blobs
- Location: `migrations.py::_backfill_entity_norm` (fetchall + per-row UPDATE; `parsed=[]` → writes `'[]'` and the guard then treats the row as finished). A corrupt `aliases` blob is permanently replaced with empty aliasesnorm — data loss presented as repair. Mitigation (DO-NOW-lite): log/count rows backfilled from unparseable blobs and record the count in the migration log; better: leave `aliases_norm` NULL for unparseable rows (guard keeps asking) — but that wedges the migration on a permanently corrupt row, so count-and-log `'[]'` with an explicit `migration.backfill_alias_corrupt` line is the honest middle. Chunked backfill is DO-LATER.

### R-W4-009 [NIT] Small warts (batch)
- `orchestrator.run_allocation` lazily imports `get_settings` per call (fine) but returns `Any`; `_correlation_for` falls back to uuid5 on a malformed payload `correlation_id` rather than poisoning (allocator.py:166-172); `inputs` dict is shared mutable across event payload, notify payload, and `AllocationDecision` (allocator.py:221/301/325/352 — frozen dataclass, mutable dict inside); reflection marker stores tick-start `when`, not completion (daemon.py:245); future-dated marker → sleep > 7 days (daemon.py delay formula); `_DRAIN_WINDOW_SECONDS` at EOF to dodge the docs lock (documented by implementer; works, but a constant that important deserves better than EOF placement — a DOCS fix task would make mid-file placement free).

### Casualty ratings (orchestrator gate items 1–4 + 1 additional, each rated real-defect vs stale-expectation)
| # | Casualty | Rating | Root cause / note |
|---|---|---|---|
| C1 | `test_creator.py::test_drain_105_backlog_drained_not_paginated` | STALE EXPECTATION | Pins pre-throttle unbounded draining; ADR-0036 orders the per-second cap. Second in-window call → 0 + throttle log [RAN]. Production daemon (~1 Hz, cap 100) never trips it. Fix: owner update (expect 0 + log, or two creators/windows). Not a code defect. |
| C2 | `test_dialect.py` 7 failures | STALE (6 literals/fixtures + 1 fixture) | 4× `_COUNT_CASE`/`lower(e.canonical_name)` literals; predicate literal; hostile-term fixture table lacks norm columns (`no such column: e.canonical_name_norm`). First assert in the order test (sqlite==postgres order) actually PASSED [RAN] — structure intact. Fixture updates in dialect test file; hostile-term behaviour on norm columns should be re-pinned by the new P003 tests (green [RAN]). |
| C3a | `test_facts.py::TestTheCorruptRow` | STALE EXPECTATION | Test corrupts the raw `aliases` blob; the alias rung now reads `aliases_norm` (untouched), so resolution still succeeds. Fail-closed property must be re-pinned against `aliases_norm` corruption. Design consequence is defensible (norm is truth) but was not recorded. |
| C3b | `test_facts.py::TestTheGateModel` | STALE TEST over a REAL design gap | Direct raw INSERT (no norm cols) → FTS indexes NULL → tokens `[]`. Same class as R-W4-004: any non-upsert writer is invisible. Test update + R-W4-004 fix cover it; a norm-NULL observability probe is DO-LATER. |
| C4 | `test_tier_migration_p009.py::test_schema_version_is_five` | STALE EXPECTATION | Version is legitimately 6. One-line update (→ 6); the W2 test's purpose (derivation) is untouched. |
| C5 (additional, found by me) | `test_entity_resolution.py` 5 failures (4 total-order + 1 escaped-blob) | STALE FIXTURES | Raw `_insert` helper (test_entity_resolution.py:358) writes no norm columns → exact rung finds nothing; escaped-blob test now resolves via norm (the intended fix). Helper needs `fold_case` columns. Implementer reported these; gate list omitted them. |

None of C1–C5 is a production code defect; C3b/C5 are the observable symptoms of R-W4-004's gap. All five groups need owner test updates before the gate can pass — that is wave-finish work, not a reason to revert the feature.

## 5. Attempts to break it
Concrete breaks attempted (reasoned + executed where noted):
1. **Naive/corrupt/future reflection marker** → from_iso attaches UTC [VERIFIED database.py:71-73]; corrupt → None/due; future → treated fresh, sleep > 7d (nit R-W4-009). Held.
2. **Poison allocator payloads** (bool item_id, string priority, NaN/Inf, F≤0, non-dict) → `_number`/`_unit`/`_positive` raise ValueError → nack [VERIFIED allocator.py:262-272, metrics.py:88-107]. Held.
3. **Double allocator run / visibility-lapse reclaim** → store dedupe returns existing event; second run acks, notifies once [VERIFIED store.py:116-144 + test_derived_correlation_dedupes green]. Held (except notify-loss comment, R-W4-005).
4. **Concurrent allocator runs** → per-lineage store lock serialises appends [VERIFIED store.py:126-134]. Held.
5. **Crash mid-v6-backfill** → one migration transaction [VERIFIED migrations.py:2000-2003]; re-run via guards. Held. (Implementer's crash-mid test green [RAN].)
6. **Graph→facts cross-writer** → BREAKS (R-W4-004): norm NULL → invisible; facts-after-graph → UniqueViolation. Confirmed by code read + failure class observed in [RAN test_entity_resolution.py output].
7. **Daemon shutdown while reflection holds/waits `_curation_lock`** → cancel propagates [VERIFIED daemon.py:91-100]. Held.
8. **Two ticks same week / kill-mid-reflection** → marker-after-run + fence; duplicate-rule worst case disclosed by implementer (run_weekly append behaviour, outside write-set). Held with disclosed residual.
9. **Drain throttle at 100x backlog** → windowed cap holds; failed batches don't consume budget, so retries aren't starved [VERIFIED creator.py:1559-1615]. Held.
10. **Allocator with `max_results`/`limit` extremes** → `max_results ge=1` [VERIFIED config.py:697]; claim(limit=0/negative) → backend-dependent, unguarded (folded into R-W4-007).

## 6. Dimension checklist
| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | MAJOR gaps | R-W4-002 (unused loop), R-W4-003 (dead mirror), R-W4-004 (un-normalised writer). Core mechanisms (allocator ordering, throttle, backfill, reflection tick) correct where reachable. |
| Root cause vs symptom | Good, one miss | Throttle/backfill/reflection target root causes. TUI mirror is a symptom-only patch (renders without data). Graph writer missed entirely. |
| Regression / compat | Casualties rated | C1–C5 stale/fixture; C3b/C5 expose R-W4-004. No new regressions in scoped suites [RAN]. |
| Concurrency / idempotency | Good | Store lock + full-key dedupe; marker-after-run; watcher delays bounded; drain overshoot documented best-effort. |
| Error handling / degrade | Minor gaps | R-W4-007 (claim raises), R-W4-008 (destructive fail-soft). Marker/poison paths excellent. |
| Security / abuse | Good | Ruff 0 (S608 per-line only [RAN]); no new injection surface (bound params, LIKE via helper); error logs carry own-validation text only. |
| Performance / scalability | DO-LATER | Backfill unbounded (R-W4-008); everything else bounded. |
| Migration / rollback | Good | v6 guarded, idempotent, single-transaction; rollback one-way; downgrade trigger-order gotcha recorded by implementer. |
| Observability | Adequate, one hole | Throttle/allocator/reflection logs + CLI doctor; norm-NULL rows invisible (no probe/count) — DO-LATER gauge. TUI doctor wrong (R-W4-003). |
| Maintainability / conventions | Minor | EventType checklist followed (non-terminal, `_NO_PHASE_CHANGE` only, no `match` arm needed); parity lock green [RAN]; dead vocab documented (R-W4-006); duplicated formatter (R-W4-003); EOF constant (nit). |
| Docs impact | BLOCKER | Lock red (R-W4-001). ADR-0029 amendment is report-delivered only (NEED posted) — orchestrator must file 0044+ before P-003 can be claimed documented. |

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| O1 | graph `sqlite_backend.py:79` | norm cols missing from second writer | fixes silent invisible entities | small | low | DO NOW (R-W4-004) |
| O2 | `dashboard.py:414` + `runner._build_info` | duplicated formatter; keys never set | TUI shows real state | small | low | DO NOW (R-W4-003) |
| O3 | `_backfill_entity_norm` | fetchall + per-row UPDATE, one txn | huge-table migration memory/lock | medium | medium | DO LATER (chunk by id; keep guards) |
| O4 | allocator scheduling | no caller of `run_allocation` | makes P-007 live | small-medium | low | DO LATER (needs producer decision first) |
| O5 | norm-NULL probe | no count of un-normalised rows anywhere | catches future second writers | tiny | none | DO LATER (startup count + gauge) |
| O6 | drain lock | per-instance window, no lock | concurrent-drain overshoot | n/a | perf | NOT WORTH IT (documented best-effort; cap is a throttle, not a quota) |
| O7 | `run_allocation -> Any` typing | loose return | readability only | tiny | none | NOT WORTH IT alone (fix with R-W4-007) |

## 8. Cross-agent / cross-task interactions observed
- **P-006 ↔ P-007:** `runner.build_panel_fields` hardcodes `attention_top: None` "until W4" (runner.py:256) — but with no allocator scheduler, W4 does not change its value. The honest-null is still correct; the P-006 panel stays "No data yet" until R-W4-002's follow-up lands. Cross-task debt, not a W4 code error.
- **P-003 ↔ graph substrate:** the missed second writer (R-W4-004) is outside W4-IMPL-03's write-set and INV-02's scope — an investigation-scope miss, not an implementer dodge. The fix task needs the graph owner.
- **Docs lock ↔ all three W4 tasks:** every W4 task inserted mid-file lines in cited files; each implementer flagged the re-point as somebody else's task. The 22 lock-blind citations (IMPL-01) + visible failures (IMPL-02/03 flags) exceed any single task's write-set → needs the standard W4-FIX-DOCS task.
- **Gate undercount:** orchestrator's casualty list omitted `test_entity_resolution.py`'s 5 (IMPL-03 reported them). Verified real [RAN]. Ledger/blast-radius accounting should use the implementer's fuller list.

## 9. What I could not verify
- PostgreSQL paths (`requires_postgres` suites, PG v6 ALTER/backfill/parity) — no `PAA_TEST_POSTGRES_DSN` on this host; 130 skips [RAN]. CI must prove A3-PG; no parity claim beyond SQLite is made here.
- Full `pytest tests/` suite (scoped only, per standing orders + brief) — baseline comparison beyond the re-run casualty files is [UNKNOWN]; the 14 baseline failures from 00-mission.md were not re-run except where they intersect W4 files (strftime category-B failure observed unchanged in the implementer's evidence; not re-run by me).
- Docker/S3 paths, live daemon timing (the `test_a_crashed_loop_is_restarted` host linger — implementer reproduced at HEAD; I did not re-run it).
- The exact count of 22 lock-blind citations (IMPL-01's differential script); lock-visible failures verified, count not re-measured.
- Whether the new P003 test file pins hostile FTS5 terms against the *norm* columns specifically (file is green [RAN]; individual test bodies not audited line-by-line).
