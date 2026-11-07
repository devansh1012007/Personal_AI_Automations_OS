<!-- ORCH-DIGEST:START -->
agent: TREV-W4b | scope: wave-4 fix tests (8971e83 + 48b9b5a + 9e8e688) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 3 run_allocation + 3 graph cross-writer + 5 TUI mirror + 1 backfill-corrupt + C1-C5 updates (7 dialect/12 facts/21 entity-resolution re-runs)
fail-before/pass-after confirmed: 12 new+updated pins by inspection+revert-reasoning / reasoned: 5 (all new tests) / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 2 | NIT 2
flaky_suspects: none (no clock/sleep/thread dependence in any fix test; structlog capture is isolated)
missing_edge_cases: P1 0 | P2 2 | P3 2
mutation_survivors: 2 of ~14 plausible bugs not caught (dashboard-to-shared-formatter dedup unpinned; drain window reset + indexed-counting still unpinned — carried from TREV-W4)
changed_behavior_without_tests: none new (DRAIN_THROTTLED emission still vocabulary-only, declared deferred)
confidence: high (ran the fix files myself, green incl. 3 repeats of the fast set; casualty flips each re-verified stale by mechanism; PG halves skipped, no DSN)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W4b (fresh independent re-review of the wave-4 fix)

Read-only reviewer. I wrote none of this code or these tests. Blind pass first
on the `8971e83` diff (src + tests), then claims audit against TREV-W4 §6 and
W4-FIX-01 (which I read only after forming blind notes). COMMS last line read:
2026-11-07T13:52:40Z W4-FIX-DOCS2 DONE. No source/test edits; no commit.

## 0. Method

- Blind pass: read the full `8971e83` diff for all six src files and all new/
  changed test files; read `reflection.py:210-253` (shared formatter),
  `creator.py:1550-1620` (drain window), `orchestrator.py:1118-1147`
  (run_allocation body).
- [RAN `.venv/Scripts/python.exe -m pytest tests/agents/test_allocation_callsite_p007.py
  tests/storage/test_graph.py tests/storage/test_tier_migration_p009.py
  tests/storage/test_dialect.py -q` -> EXIT 0 (68% line: all pass, PG/kuzu skips).]
- [RAN `... tests/tui/test_dashboard.py tests/memory/test_creator.py
  tests/memory/test_facts.py tests/memory/test_entity_resolution.py
  tests/context/test_allocator.py -q` -> exactly 1 FAILED:
  `test_ledger_with_events` (ValueError: Invalid format string at
  `dashboard.py:347`, `strftime("%H:%:%S")`). Verified pre-existing: the same
  typo exists at `dc06ed5` (`git show dc06ed5:...dashboard.py`). Matches the
  FIX-01 disclosure; not a regression.]
- [RAN the fast new/updated set (3 run_allocation + 2 graph + 1 corrupt-backfill
  + 2 TUI + C1) twice, then a final 9-test repeat -> EXIT 0 every time.
  3 clean repeats, deterministic.]
- [RAN `tests/conventions/test_import_conventions.py -q` -> EXIT 0, 21 passed.
  The new `AllocationResult` import sits in the existing classified
  `TYPE_CHECKING` block (orchestrator.py:21 `from __future__ import
  annotations` makes the annotation stringified, so no runtime cycle).]
- Mutation survivorship argued per-mutant in §3 against the actual
  assertions; I did not edit src to run mutants (read-only role).

## 1. Per test/suite assessment

### test_allocation_callsite_p007.py (3 new) — closes TREV-W4 R-W4-001

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| allocates_through_the_orchestrator | Yes — outcome, selected, exact claim args `(WORKERS_ALLOC,"allocator",25)`, ack, notify stream, ledger row `ATTENTION_ALLOCATED` on the orchestrator's ledger | Yes (no module at base; any wiring break fails it) | Yes | `_Queue` records and delivers; the unit under test (`run_allocation`->`allocate_attention`) is real, ledger is the real fixture | Pins consumer name + default limit 25, store identity (reads back through the same ledger). Strongest pin in the file. |
| degrades_to_underfed_on_claim_failure | Yes — outcome underfed, claimed 0, `allocator.claim_failed` logged, ledger empty, no raise | Yes (claim guard removed -> RuntimeError propagates out of the test) | Yes | `_RaisingQueue` injects at the documented seam | The R-W4-007 tripwire. |
| disabled_leaves_the_queue_untouched | Yes — outcome disabled, zero claim/ack calls | Yes | Yes | No | `monkeypatch.setattr("paa.config.get_settings", ...)` targets exactly the lazy import at orchestrator.py:1139 — verified. Simpler than a full Settings; honest. |

### test_graph.py cross-writer (3 new) — closes R-W4-004

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Notes |
|---|---|---|---|---|
| graph_upserted_resolves_through_facts | Yes — exact-rung resolution of `Éclair Project`, stored `canonical_name_norm == "éclair project"`, `aliases_norm == "[]"` | Yes: NULL norm -> invisible to exact rung; `lower()` -> Éclair fold mismatch | Yes | Non-ASCII name pins the real `fold_case`, not an engine lower(). |
| facts_upserted_visible_to_graph_reads | Yes — facts-written entity is a graph neighbour; its alias still resolves (alias rung) | Partially (guards the other direction) | Yes | Does not assert norm values; covers the reads-join direction, which is the part that already worked. Acceptable as a cross-writer pin. |
| graph_rename_refreshes_the_fold | Yes — old name `is None`, new name exact; both directions of staleness | Yes: no DO UPDATE fold refresh -> old name still resolves AND new name doesn't | Yes | Pins REV effect (b) both ways. |

sqlite-only by construction with the reason stated (kuzu never touches this
table) — [VERIFIED] the claim is about the backend's own table.

### test_dashboard.py mirror (5 new) — closes R-W4-003 (population half)

Render tests assert `"2.5d"`, `"9.2d (stale)"`, `"disabled"` absence/presence —
behavior, not string-matching the implementation. Runner tests write a real
marker under `tmp_path` and prove `_build_info` populates all three keys;
the no-marker case pins unknown (None age). Runner mutant (keys removed) ->
KeyError / assertion failure. Caught. The dedup half is NOT pinned — see §3.

### test_tier_migration_p009.py corrupt-backfill (1 new) — closes R-W4-008 (partial)

Pins completion to v6, corrupt row -> (`corrupt service`,`[]`), healthy row ->
real norms, exactly one `migration.backfill_alias_corrupt` line with
`corrupt_rows == 1`. Removing the log -> `len(backfills) == 1` fails. Caught.
The docstring honestly discloses the v5-shape STRICT wedge and why the row
arrives after the column adds (NEED posted by FIX-01, COMMS 18:52). The
construction (hand-built crash-mid-step state) is disclosed, not hidden.

### C1-C5 updates — verdict: every flip is genuinely stale, none weakens

- C1 (`test_creator.py`): 100 then 0 + throttle log, 5 still pending, then a
  FRESH creator drains the 5. Preserves the drain-not-OFFSET intent (nothing
  dropped, full backlog drains). Remove-throttle mutant -> second call 5 != 0.
  Caught. The fresh-creator trick does NOT exercise window reset (see §3).
- C2 (`test_dialect.py`): literals + fixture table moved to norm columns;
  CASE/ESCAPE shape still asserted. Revert-to-`lower()` mutant -> literal
  assertions fail. Caught.
- C3a (`test_facts.py`): corruption moved to `aliases_norm` — the column the
  rung actually reads now. If the rung read the raw blob, both `is None`
  assertions fail (resolution would succeed). Caught, and it's the RIGHT
  column. The dropped `PRAGMA ignore_check_constraints` is correct: the norm
  column has no CHECK.
- C3b: raw INSERT now carries the fold; gate assertion compares against
  `_trigrams(fold_case(stored))` — the text the index actually holds (norm
  columns are the FTS token source). Drop-norms mutant -> tokens empty vs
  gate grams -> fail. Caught. Not weakened: it now measures the real source.
- C4: `5 -> 6`, name kept historical with the reason in the docstring. Honest.
- C5: `_insert` writes `fold_case` norms (fixture fix); the escaped-blob test
  FLIPS from "no match" to "resolves" AND additionally asserts the raw blob is
  still escaped and `aliases_norm == '["ééé"]'`. Asserts strictly more than
  before. Drop-helper-norms mutant -> 4 order tests + flip fail. Caught.

No casualty edit weakens an assertion. TREV-W4's "all stale, no defect"
ratings are confirmed.

## 2. Coverage of changed lines/branches

- Allocator claim guard: raise->underfed, and the empty batch still logs
  `allocator.underfed` (covered by pre-existing tests). Ack/nack failure still
  propagates — deliberately out of FIX-01 scope, disclosed in FIX-01 §6.3. Not
  a new gap.
- Graph upsert: INSERT + DO UPDATE non-alias columns + rename. `upsert_entity`
  unique-violation path unchanged (pre-existing tests). `aliases_norm` on
  INSERT is the constant `"[]"` — covered by the `== "[]"` assert.
- Runner keys: enabled+fres marker, enabled+no marker; disabled via
  `getattr` default is covered implicitly by pre-existing runner tests
  (fakes without a reflection block still construct info). The
  `settings.home` for `reflection_health` — a fake settings lacking `home`
  would raise; production Settings always has it. Minor, disclosed in code.
- Backfill: corrupt count fires only when non-zero (guard `if
  corrupt_alias_rows:`). The zero-corrupt case is covered by every other
  migration test completing without the line; no test asserts the line's
  ABSENCE on a clean run. NIT (see §5).
- Context exception swallowed in allocator claim guard is `except Exception`
  — matches the house posture; `KeyboardInterrupt`/`SystemExit` still
  propagate. Correct.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| run_allocation deleted / lazy import broken / wrong store | Yes | allocates test (claim args, ledger read-back, notify stream) |
| Claim guard removed (raise propagates) | Yes | claim-failure test (RuntimeError uncaught) |
| Claim guard returns wrong outcome (`disabled`/`allocated`) | Yes | `outcome == "underfed"` + `claimed == 0` |
| Graph INSERT drops norm columns | Yes | graph_upserted test (exact rung + stored-value asserts) |
| DO UPDATE doesn't refresh `canonical_name_norm` | Yes | rename test, both directions |
| DO UPDATE writes `aliases_norm = '[]'` (brief-literal mutant) | Yes | facts_upserted test's alias-resolution assert fails (erased alias) |
| `aliases_norm` INSERT wrong (e.g. NULL) | Yes | `row["aliases_norm"] == "[]"` |
| Runner stops populating 3 keys | Yes | both runner tests (KeyError / None assertions) |
| Dashboard render breaks (e.g. always "disabled") | Yes | enabled-and-running test (`"disabled" not in out`) |
| Dashboard reverts to pre-fix duplicated strings | **NO** | SURVIVOR 1 — see §5 R-W4b-001 |
| Backfill count-log removed | Yes | corrupt test `len(backfills) == 1` + `corrupt_rows == 1` |
| Backfill count wrong (counts healthy rows) | Yes | `corrupt_rows == 1` exact |
| Throttle window removed (budget = limit) | Yes | C1 second-call `== 0` |
| Drain window never resets | **NO** | SURVIVOR 2 — carried from TREV-W4 §3; §5 R-W4b-002 |
| Failed sink batch consumes budget (`_drain_window_indexed += attempts`) | **NO** | SURVIVOR 2 — partial-sink tests assert marking, not budget |
| C-class reverts (each casualty) | Yes | per §1 C1-C5 reasoning |

## 4. Flakiness evidence

- [RAN the fix set 3 times -> EXIT 0 all 3.] No sleeps, no clock dependence,
  no threads, no shared state: real tmp DBs/files, function-scoped fixtures.
  `structlog.testing.capture_logs` is process-global but asserted within the
  same test — the pattern used across the suite.
- Suite health: the only failure anywhere in the files I ran is the
  pre-existing `test_ledger_with_events` strftime typo (verified at base).
  No order dependence observed (ran subsets and whole files).

## 5. Findings

### R-W4b-001 [MINOR] The "shared formatter" dedup is not pinned by any test
Location: `src/paa/tui/dashboard.py::_reflection_doctor_value`.
[VERIFIED reflection.py:246-253] `format_reflection_health` produces
byte-identical strings to the pre-fix duplicate for every state
(`disabled` / `unknown (due)` / `9.2d (stale)` / `2.5d`). Therefore a
mutation restoring the duplicated implementation passes all 5 new TUI tests
and the 3 pre-existing P013 mirror tests. The rendered BEHAVIOUR is well
pinned (population half is closed); the single-source-of-truth claim
(FIX-01 Phase 2) rests on code inspection + the module-scope import only.
Consequence is maintainability, not behaviour: a future divergence of the two
surfaces would be invisible to the suite. Recommended: a test asserting
`_reflection_doctor_value` agrees with `format_reflection_health` over the
four states (pins the contract, not the import), or accept the gap and label
the dedup "verified by reading". FIX-01 §8's reversal claim ("revert the
rune -> the TUI mirror tests fail by disabled") is true only for the runner
half; the formatter half has no reversal tripwire. Not a gate blocker.

### R-W4b-002 [MINOR] TREV-W4 drain-window survivors remain unpinned (carried, not in FIX-01 scope)
Location: `src/paa/memory/creator.py:1556-1558,1615`.
No test advances the clock past `_DRAIN_WINDOW_SECONDS` and asserts draining
resumes, and no test proves a failed/partial sink batch consumes no budget
(`test_drain_partial_sink_return_*` assert marking only). The "window never
resets" mutant survives the entire C1+T15 suite because every test's second
drain happens in-window on a fresh instance. TREV-W4 rated this P2-a; W4-FIX-01
did not list it in its scope, so this is a carried residual, not a fix
regression. Recommended: monkeypatch the loop clock (or a short injected
window) — drain to cap, advance >1s, assert drain resumes; failing-sink test
asserting a full budget remains. Still owed.

### R-W4b-003 [NIT] No test asserts the corrupt-count line is ABSENT on a clean backfill
`if corrupt_alias_rows:` — a mutant that logged unconditionally with
`corrupt_rows=0` would pass. One-line addition to an existing clean-run
migration test (`capture_logs` + no `migration.backfill_alias_corrupt` entry).

### R-W4b-004 [NIT] `test_facts_upserted_entity_is_visible_to_graph_reads` doesn't assert norm values
Cross-writer in both directions is claimed in the header; the facts->graph
direction asserts neighbour visibility + alias resolution but not the stored
fold. Adequate for what it proves; noting for completeness.

## 6. Missing edge cases and tests (prioritized)

**P1 (must add):** none for the fix diff. All four fix items have at least one
failing-without-it test.

**P2 (should add):**
- P2-a (R-W4b-002): drain window reset + failed-batch budget — sketched in §5.
- P2-b (R-W4b-001): formatter-agreement test across the four states.

**P3 (nice to have):**
- P3-a (R-W4b-003): clean-run absence of the corrupt-count line.
- P3-b: runner `_build_info` with a settings fake lacking `home` — pins the
  documented degrade-vs-raise boundary (currently `settings.home` raises
  AttributeError on a SimpleNamespace without it; production always has it).
  Pinning whichever behaviour is intended prevents a silent 2s-tick crash if
  a fake slips in.

Categories considered: boundaries (7d, empty, disabled), hostile (corrupt
blobs, NUL via `_candidates`), concurrency (upsert race unchanged),
idempotency (backfill re-run), partial failure (claim/store/notify/sink),
unicode (Éclair, ééé), clock (naive-now, fixed clocks). Authz/resource
exhaustion: not applicable to these surfaces. PG halves of graph/backfill:
skipped here (no DSN), honestly gated.

## 7. What I could not verify

- PostgreSQL shapes of the backfill/graph/dialect paths — no
  `PAA_TEST_POSTGRES_DSN`; CI must run them. The corrupt-count line in the PG
  path is read-verified only.
- Live mutant execution (read-only role): survivorship is argued from the
  assertions, not by editing src. The two survivors were verified by
  reading the relevant code paths, not by running a mutation.
- The v5-shape STRICT wedge (FIX-01 NEED, COMMS 18:52) — accepted as
  disclosed DEFERRED-DEPENDENCY; the corrupt test's crash-mid-step
  construction is the honest reachable shape. Re-verifying the wedge
  empirically is out of this read-only scope.
- Docs citations (48b9b5a + 9e8e688) are numbers-only diffs; the lock green
  run is DOCS2's verification, not re-run by me. Out of test-quality scope.

## 8. Claims audit (W4-FIX-01 §5/§8 vs my runs)

- "9 test files -> all pass (PG/kuzu skips only)" + the disclosed pre-existing
  `test_ledger_with_events` failure: CONFIRMED by my runs (same single
  failure, verified at base).
- "revert `_UPSERT_ENTITY_SQL` -> cross-writer tests fail": CONFIRMED by
  assertion inspection (exact rung + stored values).
- "revert the rune -> TUI mirror tests fail by disabled": PARTIALLY — true
  for the runner half (keys removed -> KeyError/assert), FALSE for the
  formatter half (see R-W4b-001).
- "revert the claim wrap -> raising-queue test fails with RuntimeError":
  CONFIRMED.
- "revert the count-log -> corrupt test finds no line": CONFIRMED.
- "mypy 3 = baseline, ruff 0": not re-run by me (out of scoped need);
  import-conventions lock re-run green. No contrary evidence.
- C1-C5 casualty ratings (STALE, in TREV-W4 §6): CONFIRMED in full — each
  flip follows from the mechanism change, and several assert more than before.
