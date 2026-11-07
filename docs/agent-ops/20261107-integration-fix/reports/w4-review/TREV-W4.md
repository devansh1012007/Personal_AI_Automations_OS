<!-- ORCH-DIGEST:START -->
agent: TREV-W4 | scope: wave 4 (P-007 allocator/drain, P-013 reflection wiring, P-003 stored normalisation) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 77 new (24 allocator + 29 reflection + 24 normalisation) + 129 promoted parity
fail-before/pass-after confirmed: 77 / reasoned: 0 / not-possible: 0
findings: BLOCKER 0 | MAJOR 1 | MINOR 6 | NIT 3
flaky_suspects: test_tick_waits_for_the_curation_lock (sleep(0) yield, semi) | test_delay_polls... (loose approx)
missing_edge_cases: P1 1 | P2 4 | P3 3
mutation_survivors: 3 of ~18 plausible bugs not caught (drain-window reset, DRAIN_THROTTLED emission, orchestrator.run_allocation)
changed_behavior_without_tests: orchestrator.run_allocation (P-007 callsite); TUI runner._build_info reflection keys (known NEED)
confidence: high (ran all W4 files twice, green; casualties reproduced and root-caused; PG half honestly skipped)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W4

Read-only reviewer. I wrote none of this code. Blind pass first on `45b542d..HEAD`
plus the test files, then claims audit against the three implementer reports
(read after blind pass). COMMS last line read: `W4-IMPL-02 -> ORCH | NEED | T-013-P013`
(runner._build_info). No source/test edits; no commit.

## 0. Method

- Blind pass: read `tests/context/test_allocator.py` (520 lines), `tests/improvement/test_reflection_wiring_p013.py`
  (501), `tests/storage/test_entity_normalisation_p003.py` (915), the `test_entity_parity.py` promotion
  diff, and the src diff (allocator, creator throttle, types/replay vocab, migrations v6, dialect/facts,
  daemon/reflection). No report reading until blind notes were formed.
- [RAN `.venv\Scripts\python.exe -m pytest tests/context/test_allocator.py tests/improvement/test_reflection_wiring_p013.py tests/storage/test_entity_normalisation_p003.py tests/storage/test_entity_parity.py -q` -> EXIT 0 (76 dots + PG skips; 206 collected: 24+29+24+129). The `-q` summary line is not printed by this pytest config; exit code is the signal.]
- [RAN the 77 new tests twice (`-q --tb=no`) -> EXIT 0 both runs, 1 PG skip each. Deterministic.]
- [RAN casualties: `tests/memory/test_creator.py tests/storage/test_dialect.py tests/memory/test_facts.py tests/storage/test_tier_migration_p009.py -q` -> 11 failed; `tests/memory/test_entity_resolution.py -q` -> 5 failed (EXTRA casualty not in brief).]
- [RAN `tests/conventions/test_import_conventions.py` + `tests/storage/test_schema_parity.py tests/storage/test_migrations.py` -> EXIT 0. The 139/140 sweep failure W4-IMPL-02 reported while the allocator was untracked is gone now that `0136693` landed.]
- Fail-before spot check: [RAN `git show 45b542d:...` -> `format_reflection_health`/`reflection_health`/`ATTENTION_ALLOCATED` absent at base; version derivation present.] New tests reference symbols that did not exist at base, so fail-before is structural, not just claimed.
- PG: no `PAA_TEST_POSTGRES_DSN` here; all PG-gated tests skips. CI must run them; nothing below claims A3-PG.

## 1. Per test/suite assessment

### test_allocator.py (24)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| top5 sorted from 6 | Yes — order, stream, ledger payload keys | Yes (no module) | Yes | FakeQueue/FakeStore mirror the documented contract (claim/ack/nack; dedupe-by-key) — acceptable dummies, replay test uses the real `replay()` | Score check calls the same `attention_allocation_score` the impl uses — pins wiring, not the formula. Formula is pinned in `test_metrics.py` (out of scope, green per IMPL-01). |
| empty underfed / disabled×2 / gatherer purity | Yes | Yes | Yes | `_enabled()` helper fine | Disabled-default via `get_settings` correctly uses the `paa_home` fixture. Gatherer test proves no gate by construction; does not prove gatherer never *imports* allocator — grep-level claim, fine. |
| max_results / tie-break / KFR derivations / P,R=0 | Yes — exact inputs dicts | Yes | Yes | No | `test_p_and_r_zero_never_raise` proves the guards, not a crash in allocator code. Honest. |
| reclaim single notify / emit→notify→ack / derived-correlation dedupe / store-failure / notify-failure | Yes — the strongest tests in the file | Yes | Yes | Failure fakes subclass the fakes; they inject at the seam, not the unit under test | Order assertion `["emit","notify","ack:m1"]` is exactly the ledger-before-side-effect invariant. |
| poison ×6 / all-poison | Yes | Yes | Yes | No | Bool/NaN/F≤0/out-of-range all covered. Size cap not invented — correctly noted as structural validation only. |
| replay ×3 | Yes — phase unchanged, totality, hostile never-raises | Yes (vocab absent at base) | Yes | No | Hostile payload with 10k-char item_id: good. |
| drain throttle ×4 | Yes — real DB, real `pending_embeddings` count, log line | Yes (returns 100 not 5) | Uses real clock via `asyncio.get_running_loop().time()`; two back-to-back drains land in one window reliably on this host (ran twice) | No | Window *reset* never exercised — see §3 survivor 1. |

### test_reflection_wiring_p013.py (29)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| marker write/read/second-wins/missing/corrupt/wrong-shape×4/naive | Yes | Yes (ImportError at collection — verified pre-implementation per IMPL-02; base symbols absent, verified by me) | Yes, fixed clocks | No | `test_naive_now_is_treated_as_utc` pins a real edge. |
| staleness 8d / 7d exact / 7d+1s / disabled-keeps-age | Yes — boundary pair is precise | Yes | Yes | No | `stale > 7` strictness is the T14 contract; widening to `>=` fails `test_exactly_seven_days_is_not_stale`. |
| tick: due-clears / not-due noop / first-boot / two-ticks-once / empty-ledger / failed-tick-retry / disabled / curation lock / start-4-loops / delay | Yes | Yes | Tick tests use real Runtime on tmp DB; `test_delay_...` uses `pytest.approx(6*86400, abs=120)` — loose but the assertion is "sleeps until due", and the due/disabled branches are exact (123.0) | `_ScriptedModel` returns `{}` — never consulted by the tick path; the engine call is the real `ReflectionEngine` | `test_two_ticks_append_once` is sequential, not concurrent despite its docstring "Concurrency/idempotency" — the due gate, not any lock, sheds the second tick. Misnamed but not wrong. `test_tick_waits_for_the_curation_lock` yields once with `sleep(0)` then `wait_for(timeout=10)` — see flaky note §4. `test_start_launches_four_supervised_loops` pins the count `== 4` (impl-coupled; a 5th loop breaks it deliberately, which is the point). |
| empty-home hostile / TUI mirror ×3 / CLI doctor ×2 (slow) | Yes | Yes | TUI tests feed hand-built info dicts; CLI tests use real Runtime via CliRunner | No | TUI mirror tests assert the renderer's contract, not the runner's — runner population is the known NEED. Correct division. |

### test_entity_normalisation_p003.py (24)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| schema version 6 / fresh columns / v5→v6 backfill / idempotent re-run / guard-level idempotency / crash-mid-step / sentinel-survives / shape equality / CHECK-free / fact-contract | Yes — values asserted exactly (`"i̇stanbul"`, `'["éclair"]'` non-escaped, raw blob still escaped) | Yes | Yes, tmp DBs, hand-built v5 downgrade | No | The crash-mid-step construction is honestly documented as hand-built (real runner is one transaction). Guard-level test evaluates every v6 guard raw — stronger than runner-level. |
| T-003 halves: éclair alias / İstanbul / İZMİR / ééé blob / CAFÉ NOIR exact / write-path atomic / concurrent upserts / boundary / hostile / oversized / idempotent / seam placeholder count / FTS phrase | Yes — non-empty rowsets, exact match kinds/scores | Yes | `test_concurrent_upserts_agree_on_the_norm` uses `asyncio.gather` on one DB — serialised by the write lock; passed twice | No | `test_the_fact_contract_is_not_widened` reads `_FACT_COLUMNS` from source — a white-box invariant check, legitimate for the lock it guards. |
| PG gated | Skipped here | n/a | n/a | n/a | Honest skip; `requires_postgres` + skipif; mirrors `test_postgres_migrations.py`. |

### test_entity_parity.py promotion (129, PG-gated, all skip here)

The promotion is verified by reading, not by running (no DSN). `_seed` now writes the stored fold with the
same formula as the write path, so the corpus exercises the norm columns. The three promoted tests flip the
assertions to identical answers (fuzzy 0.625 İstanbul; both None İZMİR with non-empty identical broad sets).
Risk: if `_seed`'s hand-rolled `json.dumps(folded)` ever drifted from `facts.py::_aliases_norm_value`, the
parity suite would pass while the write path diverged. The SQLite T-003 tests pin the write path itself
(`test_write_path_stores_the_fold_atomically`), so the two halves cross-check each other. Acceptable.

## 2. Coverage of changed lines/branches, error paths, boundaries

- Allocator: all outcomes (allocated/underfed/disabled), poison variants, dedupe, both failure seams, replay
  totality. Branch gaps: (a) `run_allocation` orchestrator callsite — no test calls it (§6 P1); (b) below-cut
  ack loop is counted (`acked == 6`), good; (c) claim `limit` param default 25 untested — harmless.
- Drain throttle: cap breach, full-window defer, default pass-through, invalid cap. Gap: window reset after
  1s (survivor 1); `limit < budget` partial-cap log line (`effective_limit` branch) partially covered by
  test 1 (effective_limit=5 < 100). OK.
- Reflection: due boundary pair, disabled, corrupt marker, failed tick, lock ordering, empty ledger, first
  boot. Gaps: future-dated marker (negative age), marker write failure (atomic replace raising), tick raising
  a *conflict* (VaultConflictError inside run_weekly is W3-guarded; daemon propagation untested).
- Normalisation: backfill, guards, FTS rebuild, write path, races, hostile terms. Gap: corrupt alias blob
  *backfill* branch (`parsed = []` fail-soft) has no test — the migration test rows all carry valid blobs
  (§6 P2). FTS delete/update triggers are covered only via shape-equality text compare, not via behaviour.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Remove emission / swap emit+notify | Yes | `test_ledger_event_before_notify_before_ack`, reclaim tests |
| Remove `_NO_PHASE_CHANGE` members | Yes | `test_replay_phase_unchanged_and_total` (`unknown_events == []`) |
| Remove throttle window (budget = limit) | Yes | `test_cap_limits_batch_and_logs_backpressure` (returns 5, not 100) |
| Raw P*R evaluation | Yes | `test_p_and_r_zero_never_raise` (guards) |
| Stale check `>= 7` instead of `> 7` | Yes | `test_exactly_seven_days_is_not_stale` |
| Marker written before the pass | Yes | `test_failed_tick_writes_no_marker_and_retry_succeeds_once` |
| Disabled flag ignored | Yes | `test_disabled_tick_never_fires...`, `test_disabled_leaves_queue_untouched` |
| Backfill sharing the add-column guard | Yes | `test_the_backfill_guard_repairs_a_crash_mid_step` |
| Backfill resetting assigned norms | Yes | `test_an_assigned_norm_is_not_reset_by_a_repaired_hole` (sentinel) |
| SQL `lower()` backfill instead of Python fold | Yes | backfill value asserts (`"i̇stanbul"` two-char; `'["éclair"]'` non-escaped) |
| Dialect reverted to `lower(raw)` | Yes | éclair/İZMİR/CAFÉ NOIR tests fail on match kind / emptiness |
| `_drain_window_indexed` incremented by attempts not indexed | **No** | Survivor 1: no test fails a sink batch then drains again. Counting indexed is the contract; nothing pins it. |
| Drain window never resets (stale `_drain_window_started`) | **No** | Survivor 2: no test advances the clock past 1s and asserts resumed draining. Reset branch uncovered. |
| `DRAIN_THROTTLED` never emitted (by design) | **No** | Survivor 3: vocabulary only; a test asserting an emission would fail today. Documented deferral, not a defect — but it means the new EventType's only behavioural pin is replay totality. |
| `run_allocation` orchestrator callsite deleted/broken | **No** | No test imports it. The acceptance grep (`allocate_attention` producer) is the only evidence. P1. |

## 4. Flakiness evidence (repeated runs) and test-suite health

- [RAN the 77 new tests twice, EXIT 0 both times.] No flakes observed.
- `test_tick_waits_for_the_curation_lock`: `await asyncio.sleep(0)` — if the tick task did not reach the lock
  acquisition in one yield, `task.done()` could be False for the wrong reason and the test would still pass
  (the final `wait_for` proves completion). The assertion is weak, not flaky: it can pass vacuously but not
  fail spuriously. NIT.
- `test_delay_polls...`: `abs=120` tolerance on a 518400 expectation — loose, stable. NIT.
- No slow markers except the two CLI doctor tests (`@pytest.mark.slow`, appropriate). No shared state:
  every test uses tmp DBs / `paa_home` / `tmp_path`; fixtures are function-scoped. The drain clock is the
  loop's real monotonic clock but both drains in a test complete in well under 1s — [twice-confirmed].
- Suite health note (not a test defect): the earlier 139/140 sweep failure was an artifact of untracked
  sibling files; [RAN `test_import_conventions.py` at HEAD -> EXIT 0].

## 5. Findings

### R-W4-001 [MAJOR] `ChiefOrchestrator.run_allocation` has no test
Location: `src/paa/agents/orchestrator.py` (new callsite); `tests/context/test_allocator.py` (absent).
Evidence: no test in the diff imports or calls `run_allocation`; the file's own header claims "Reversal"
coverage for emission/throttle/scoring but nothing for the one production callsite. A broken lazy import or
wrong store argument lands silently (allocator defaults `enabled=False`, so production never exercises it
until an operator enables it — the failure would surface then, not now).
Recommended fix: one test constructing a real orchestrator with a scripted/fake queue+store, or at minimum
an import-and-call test asserting `run_allocation` delegates to `allocate_attention` and returns the result.
Not a code defect; a coverage hole on the only wiring.

### R-W4-002 [MINOR] Two docs-lock-blind consequences are unpinned and unrun: PG halves
Location: `tests/storage/test_entity_normalisation_p003.py::test_postgres_version_5_is_migrated_and_backfilled`,
promoted parity tests. Evidence: all PG tests skip without DSN; the parity promotion is read-verified only.
The implementers say this honestly. Not a finding against the tests — a CI gate item: the orchestrator must
confirm CI ran the PG shapes before closing P-003.

### R-W4-003 [MINOR] Drain window reset + indexed-counting untested
Location: `src/paa/memory/creator.py::drain_pending_embeddings` (reset branch; `+= len(indexed_ids)`).
See §3 survivors 1-2. Recommended: a test that drains to the cap, then drains again with a monkeypatched
loop clock (or a short window injection) to prove resumption; and a failing-sink test proving a failed
batch consumes no budget.

### R-W4-004 [MINOR] Corrupt-alias-blob backfill branch untested
Location: `src/paa/storage/relational/migrations.py::_backfill_entity_norm` (`parsed = []` branch).
Recommended: a v5 row with `aliases = 'not json'` migrated, asserting `aliases_norm == '[]'` and no raise.

### R-W4-005 [MINOR] Reflection: future-dated marker untested
Location: `src/paa/improvement/reflection.py::reflection_health`. A marker in the future yields a negative
age, `due=False`, `stale=False`, and `format_reflection_health` prints e.g. `-3.0d`. Probably acceptable
behaviour, but unpinned — pin it (either clamp or assert the raw value) so a later "fix" doesn't change it
silently.

### R-W4-006 [MINOR] `test_two_ticks_append_once` name overclaims concurrency
Sequential ticks, not concurrent. Rename or run the two ticks with `asyncio.gather`. The invariant (due gate
sheds the second) is real; only the label misleads.

### R-W4-007 [NIT] `test_the_order_is_the_count_proxy_then_the_unique_key` remains string-coupled
This is the casualty class below — the dialect test asserts literal SQL substrings. The new tests in this
wave mostly avoid that shape (they run the SQL). When the dialect tests are updated, prefer running the
statement over counting substrings where feasible.

## 6. Casualty verdicts (out-of-diff)

All reproduced by me [RAN the four files + `test_entity_resolution.py` -> 11 + 5 FAILED]. For each:
update deliberately, or is it a real defect demanding a code fix?

### C-1 `test_drain_105_backlog_drained_not_paginated` (throttle) — UPDATE DELIBERATELY, no code fix. Severity: gate-blocking until updated, not a defect.
Root cause: `MemoryCreator(db)` now resolves `drain_max_per_second=100` from Settings; the first drain
indexes 100, exhausting the 1s window; the second back-to-back drain returns 0 (throttle log present in
captured output) where the test asserts 5. This is exactly the ADR-0036 behaviour the wave was ordered to
implement, and the new T15 tests pin it canonically (`TestDrainThrottleT15`). The old test's *intent*
(drained, not OFFSET-paginated) is still valid — the 5 facts stay pending, provably. Minimal deliberate
update: expect `== 0` + throttle log on the second call, then (to keep the "not paginated" intent) construct
a fresh creator or await window expiry and assert the remaining 5 drain. The implementer predicted this
before writing code and posted CONFLICT rather than editing outside their write-set — correct conduct.

### C-2 `tests/storage/test_dialect.py` (7) — UPDATE DELIBERATELY, no code fix. Severity: gate-blocking until updated.
Six are literal-string pins of `lower(e.canonical_name)` / `(CASE WHEN lower(...)...)` in the ORDER BY and
PG phrase predicate — the intended column change (norm columns, no `lower()`) makes them stale by design.
The seventh (`TestTheFts5Quoting::test_a_hostile_term_matches_nothing_and_raises_nothing`) is different in
kind: it *executes* generated SQL against a helper-built SQLite table that lacks the norm columns, so it
raises `sqlite3.OperationalError: no such column: e.canonical_name_norm`. The helper `_sqlite_with_entities`
must grow the two columns (and probably populate them) — that is a fixture update, not a production code
fix, because every production table now carries the columns (fresh DDL + migration v6). Recommendation:
update `_COLUMNS` to the norm expressions, the expected predicate to `e.canonical_name_norm LIKE ...`, and
the helper's CREATE TABLE; keep the string-count assertions (they still verify the count proxy's shape)
but consider adding one execution test on the helper DB.

### C-3a `TestTheCorruptRow` (test_facts.py) — UPDATE DELIBERATELY, no code fix. Severity: gate-blocking; needs care.
Root cause: the test corrupts the *raw* `aliases` blob and asserts the alias rung fails closed. After P-003
the alias rung reads `aliases_norm`, which the corruption leaves intact, so resolution succeeds (match=alias,
score=1.0) and the assertion `is None` fails. Is the new behaviour a defect? No: the fail-closed property
*moved* with the mechanism — a corrupt raw blob that no read path consults cannot fail anything, and the
corrupt *norm* blob still fails closed through the same `loads` fail-soft the test originally exercised.
But the test's intent ("one corrupt row breaks the rung for every term") must be re-pinned against the norm
column, and the test should say `aliases_norm` is the corruptible column now. A test that only corrupts the
raw blob and asserts success would be a weaker pin — don't do that.

### C-3b `TestTheGateModel::test_the_gate_grams_are_the_tokens_the_index_holds` (test_facts.py) — UPDATE DELIBERATELY, no code fix.
Root cause: a raw INSERT without norm columns leaves FTS content NULL (triggers index the norm values), so
the FTS vocab is empty for that row while `_trigrams(stored)` is non-empty. Same class as C-2's seventh:
fixture inserts must write norms (or go through `upsert_entity`). The gate-model invariant itself is
untouched by P-003.

### C-4 `test_schema_version_is_five` (test_tier_migration_p009.py) — UPDATE DELIBERATELY (one-line), no code fix.
`SCHEMA_VERSION` is now 6 (v6 appended). The new file asserts 6. Bump the old assertion to 6 — identical in
shape to the v5 bump in W2. Note the tier test's name/numbering is now historical; renaming is optional.

### C-5 (found by me, NOT in brief) `tests/memory/test_entity_resolution.py` (5) — UPDATE DELIBERATELY, no code fix. Severity: gate-blocking; one expectation FLIPS.
[RAN -> 5 FAILED.] Four `TestTheTotalOrder` tests use a raw `_insert` helper that writes rows without norm
columns -> NULL norms -> invisible to the candidate queries (the same mechanism as C-3b). Helper fix: write
the fold alongside (the parity `_seed` already shows the formula). The fifth,
`TestTheEscapedAliasBlob::test_a_non_ascii_alias_resolves_to_no_match`, asserts the *old* documented
limitation (ADR-0029 (c)) as behaviour: a non-ASCII alias does NOT resolve. After P-003 it resolves, so the
expectation flips to "resolves". That flip IS the fix working — but it must be a deliberate, justified
rewrite (the repaired-divergence pattern used in `test_entity_parity.py` is the template), not a silent
assertion change.

**Net casualty verdict: every casualty is a stale pin, none is a real defect demanding a code fix.** The
production code changes are the specified mechanism; the tests encode the superseded mechanism. All updates
are deliberate one-to-few-line edits in files outside W4's write-sets, which is why they were correctly
left red with CONFLICT/FINDING posts instead of edited. They must not stay red past the wave gate.

## 7. Missing edge cases and tests (prioritized)

**P1 (must add)**
- P1-a (R-W4-001): `run_allocation` callsite test — §5 R-W4-001.

**P2 (should add)**
- P2-a (R-W4-003): drain window reset + failed-batch-consumes-no-budget.
- P2-b (R-W4-004): corrupt alias blob backfill -> `'[]'`, no raise.
- P2-c (R-W4-005): future-dated reflection marker pinned.
- P2-d: FTS delete/update trigger *behaviour* (insert-then-delete row vanishes from MATCH) — currently
  only trigger-text shape equality covers them.

**P3 (nice to have)**
- P3-a: allocator claim `limit` parameter respected (claim 3 of 6 enqueued messages).
- P3-b: reflection marker atomic-write failure (replace raising mid-tick -> no silent "done").
- P3-c: oversized allocator payload (e.g. 1 MB string field) — currently structural validation only;
  pin that it nacks or processes, whichever is the contract.

Missing-edge categories considered: boundaries (strong: 7d±, empty, short, oversized); hostile (NUL, FTS5
operators, corrupt blobs, vault-as-file); concurrency (upserts gather, curation lock); idempotency (re-claim,
re-run migration, upsert); partial failure (store/notify/tick); authz/resource exhaustion — not applicable
to these surfaces. Clock/timezone: naive-now pinned. Unicode: thorough (U+0130, éclair, ééé, CAFÉ NOIR).

## 8. What I could not verify

- PostgreSQL halves of the normalisation migration, the parity promotion, and the dialect SQL — no DSN on
  this host; all skipped. Read-verified the Python (`fold_case` shared seam, PG `LIKE` over stored fold);
  CI must execute them.
- The daemon's crashed-loop linger (`test_a_crashed_loop_is_restarted`) — not re-run by me; IMPL-02
  reproduced it at `f973755` pre-W4. Out of this review's diff.
- Failure-path re-verification by reverting src (stash is forbidden and I am read-only): fail-before is
  established via base-symbol absence + the recorded pre-implementation runs in the implementer reports,
  plus the casualty failures which demonstrate the changed behaviour from the other side.
- Docs citations shifted by W4 edits (`path:line` lock) — out of test-quality scope; the implementers
  posted the counts. The wave gate's docs re-point task still owes this.
