<!-- ORCH-DIGEST:START -->
agent: TREV-W2 | scope: wave 2 / W2 test quality (touch, tier vocab+replay, tier migration, semantic +2, Dockerfile.embed static shape) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 64 (touch 7, tier_vocab 26, tier_replay 6, tier_migration 9, semantic 16)
fail-before/pass-after confirmed: 32 / reasoned: 32 / not-possible: 0
findings: BLOCKER 0 | MAJOR 3 | MINOR 5 | NIT 3
flaky_suspects: none (3 repeat runs of the 39 fast pure tests, all green)
missing_edge_cases: P1 3 | P2 7 | P3 4
mutation_survivors: 4 of 14 plausible bugs not caught (see §3)
changed_behavior_without_tests: Dockerfile.embed + ci.yml embed-image job (no pytest; CI-only by design, T06 NEED still open); TierPolicy not re-exported from paa.memory despite COMMS NEED; touch failure log redaction unasserted
confidence: high (<all 64 tests run by me + 3x repeats + ruff 0; PG half skipped without DSN, docker build NOT-RUN>)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W2

## 0. Method

- Blind pass first: read the full diff `8faa64e..HEAD` (24 files, +2391/-111) and all 5 test files
  before opening any implementer report. Claims audit afterwards against W2-IMPL-01..04 + W2-GEN-01.
- Ran it myself [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_touch_unification.py
  tests/memory/test_tier_vocab_p009.py tests/ledger/test_tier_replay_p009.py
  tests/storage/test_tier_migration_p009.py tests/agents/test_semantic_derivation_p002.py -q` ->
  63 passed, 1 skipped (PG gated, no `PAA_TEST_POSTGRES_DSN`)].
- Flakiness: [RAN the 3 fast pure files (touch + tier_vocab + tier_replay, 39 tests) 3x -> 39 passed
  each run, no variance]. Migration + semantic files run once (real tmp DBs; deterministic inputs).
- [RAN `ruff check` on all 5 new/changed test files + touch.py/domains.py/replay.py/types.py/migrations.py
  -> All checks passed].
- Read-only mutation probes via `python -c` (no file edits): TierPolicy ctor/shared-dict behavior,
  `apply_tier_event` empty/zero fact_id, call-site import/call counts in the 4 touch consumers.
- No temporary worktree was needed: fail-before is reasoned from the pre-wave tree (4 `_touch_recalled`
  defs at `api:346, coder:886, context_agents:403, cli:699` per 00-mission.md:42, `SCHEMA_VERSION=4`,
  no `apply_tier_event`/`TierPolicy` symbols) plus implementer-reported import-error / monkeypatched-runner
  fail-before runs, which I treat as confirmed evidence, not as my own runs.

## 1. Per test/suite assessment

### tests/memory/test_touch_unification.py (7 tests, W2-IMPL-01)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_touch_recalled_exists_with_contract_signature` | Yes — coroutine, identity of the two import paths, exact param names + annotations | Yes — pre-wave the symbol does not exist (ImportError) | Yes | No | Annotation assertion is brittle-by-design (contract lock); fine. |
| `test_exactly_one_touch_helper_definition_in_src` | Structural — walks `src/paa`, AST-parses every `.py`, collects defs named `_touch_recalled`/`touch_recalled` | Yes — pre-wave finds exactly the 4 old defs [VERIFIED 00-mission.md:42] | Yes (sorted walk, no shell-out) | No | See R-W2-101: it locks *definition count*, not *which files call it*. A renamed second copy (`touch_all`) or an inline `DecaySweeper.touch_many` at one call site survives. |
| `test_duplicate_ids_deduped_order_preserved` (2 params) | Yes — `touch_many` receives exactly `["a","b","c"]` / `["x"]` | Yes — removing `dict.fromkeys` in touch.py fails the first param | Yes | Mild — patches `DecaySweeper.touch_many` (the callee, not the unit); real SQL is covered by the 4 per-surface suites | `cast(RelationalBackend, object())` is honest: db is never dereferenced because the class method is patched. Same idiom as the pre-existing per-surface suites. |
| `test_db_none_is_noop` / `test_empty_list_is_noop` | Yes — raises if `touch_many` runs | Yes — removing either early return fails | Yes | Same as above | Good pair: both branches of the `if db is None or not fact_ids` guard pinned separately. |
| `test_touch_failure_is_tolerated` | Half — asserts no raise only | Yes for the swallow, **no** for the redaction inside it | Yes | Same as above | See R-W2-102: the `log.warning(..., error=redact(str(exc)))` line is unasserted. Removing `redact()` survives. |

### tests/memory/test_tier_vocab_p009.py (26 tests, W2-IMPL-02)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `TestTierVocabulary` (3) | Yes — members exist, non-terminal, replay totality (`unknown_events == []`) | Yes — implementer ran fail-before: `ImportError: cannot import name 'apply_tier_event'` [W2-IMPL-02 §2] | Yes, pure | No | Totality + non-terminal is exactly the `_NO_PHASE_CHANGE` contract. |
| `TestTierDictFold` (7) | Yes — sequence fold, input not mutated (identity check), old-events→T2, no-fact_id noop, non-tier never resurrects, non-tier cannot set tier, double-emit idempotent | Yes — the resurrect/containment pair is documented as failing on the implementer's first draft [W2-IMPL-02 §7]; those two are the strongest tests in the file | Yes, pure | No | `test_task_events_without_fact_id_are_noop_copies` asserts value equality but not copy identity — a mutating implementation that returns the same dict would pass here and fail only in the purity test. Acceptable, since the purity test does check `is not`. |
| `TestTierHostileInput` (3) | Yes — missing fact_id noop, non-string fact_id coerced to `"12345"`, non-string `to_tier` → T2 | Yes — a raising fold fails | Yes, pure | No | Totality is the contract (replay must never raise). Empty-string fact_id / `0` / `False` are *not* pinned — see R-W2-103; I probed them: `""` → `{"": "T1"}`, `0` → `{"0": "T1"}`. Total but arguably junk keys. |
| `TestTierPolicy` (13 incl. params) | Yes — exact TTL table incl. `math.isinf(T4)`, resolve known/unknown incl. case mismatch `"t2"`→T2 | Yes — ImportError fail-before | Yes, pure | No | Missing: frozen-ness, `DEFAULT_TIER` not settable via ctor, per-instance dict isolation — see R-W2-104. I probed: ctor raises `TypeError` for `DEFAULT_TIER=` [RAN], and mutating one instance's `ttl_hours` does **not** leak into a new instance [RAN]. Both are true but unpinned. |

### tests/ledger/test_tier_replay_p009.py (6 tests, W2-IMPL-02, T10b-core)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `TestTaskReplayUnchanged` (2) | Yes — interleaved TIER_* leave phase/completed_steps/tokens identical; TIER_* alone keeps `CREATED` | Yes — removing the `_NO_PHASE_CHANGE` additions puts TIER_* in `unknown_events` (implementer proved in-process [W2-IMPL-02 §2]) | Yes | No | Strong. Uses a real 5-event task stream, not a stub. |
| `TestTierRebuild` (2) | Yes — rebuild equals fold of full stream; prefix/suffix fold equals full fold | Yes | Yes | No | `test_prefix_suffix_fold_is_associative` is weaker than its name: single fact, 3 events, suffix is a single event. Because `apply_tier_event` never mutates, prefix cannot be corrupted — good, but multi-fact interleaving at the split point is untested (P3). |
| `TestDiscriminatorContract` (2) | **No** — pins `compute_idempotency_key` arithmetic, not any production construction site | **No** — passes with or without any sweeper; the sweeper does not exist yet (W3) | Yes | No | See R-W2-105: these are library-characterization tests wearing a contract's clothes. They will catch a change to `events.py` key derivation, but cannot catch a W3 sweeper that forgets `discriminator=fact_id+to_tier`. A `grep`-style structural assertion over `src/` for the discriminator construction site would. |

### tests/storage/test_tier_migration_p009.py (9 tests, W2-IMPL-03; 8 run + 1 PG skip locally)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_schema_version_is_five` | Trivially — version constant | Yes — pre-wave it is 4 | Yes | No | Pin, fine as a tripwire. |
| `test_a_fresh_database_has_the_tier_columns` | Yes — columns present + full version row 1..5 | Yes — pre-wave neither column exists | Yes, tmp_path DB | No (raw `Database`, no mocks) | |
| `test_a_version_4_database_is_migrated_and_backfilled` | Yes — exact rows `[(_FACT,"T2",None), ...]` | Yes — implementer monkeypatched-runner scratch showed versions stuck at `[1,2,3,4]`, no tier column [W2-IMPL-03 §Phase C] | Yes | No | Rows written at v4 via raw SQL, so the migration — not the test setup — assigns the tier. |
| `test_a_tier_outside_the_ladder_is_refused` | Yes — `CheckViolation` **and** `"ck_fact_tier" in str(exc)` | Yes — removing the CHECK lets the insert through | Yes | No | Tight: names the constraint, not just "an error". |
| `test_running_the_migration_again_changes_nothing` | Yes, but weakly — see below | **Only at runner level** | Yes | No | See R-W2-106: once at v5 the runner has no pending steps, so this test would pass even if every step *guard* were broken (e.g. always-true). The per-effect guards are only exercised via the crash tests. |
| `test_the_backfill_guard_repairs_a_crash_mid_step` | Yes — nullable tier column, NULL rows → repaired to T2, version 5 | Yes — merging the backfill into the add-column's guard leaves NULL rows forever; deleting the backfill UPDATE likewise | Yes | No | The centrepiece test; it is the only one that kills the single-guard mutation. The file docstring honestly states the real runner is single-transaction so this state is hand-constructed [test file:25-30]. |
| `test_an_assigned_tier_is_not_reset_by_a_repaired_hole` | Yes — T0 survives, NULL→T2 | Yes — a backfill without `WHERE tier IS NULL` resets T0→T2 and fails | Yes | No | Kills the most dangerous backfill mutation. |
| `test_a_migrated_database_shape_matches_a_fresh_one` | Yes — column tuples (name/type/default/notnull/pk) equal + CHECK expression text equal, in order | Yes — a table-level CHECK placement or wrong column order fails the CHECK-order leg | Yes | No | Local `_check_ranges` mirrors `test_migrations.py`'s extractor. Does not compare FKs/triggers/indexes (test_migrations' `_shape` does); acceptable scope for the tier columns specifically, but see P2. |
| `test_postgres_version_4_is_migrated_and_backfilled` | Yes | Yes (would fail pre-v5 on a live server) | **NOT-RUN here** — skips without `PAA_TEST_POSTGRES_DSN` [RAN: 1 skipped] | No | The entire PG ALTER path (named CHECK in `ADD COLUMN`, `'T2'::text` default rendering, constraint-name parity fresh-vs-migrated) is unverified on this host. Honestly gated + honestly reported by the implementer; CI must run it. Residual risk carried, not a test defect. |

### tests/agents/test_semantic_derivation_p002.py — the +2 (W2-GEN-01, now 16)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_corrupt_row_hydration_failed_log_is_redacted` | Yes — payload fallback 0.42/0.33, `fact_id` key present, `***REDACTED***` present, raw `sk-…` token absent | Yes — deleting `redact()` at the per-hit line flips both legs (implementer proved `from_iso` embeds the input and `redact()` maps the token to the marker [W2-GEN-01 §4]) | Yes — real DB fixture, injected `now`, `structlog.testing.capture_logs` | No — wraps nothing; corrupt value written by raw SQL (the honest route) | Kills the exact survivor TREV-W1b flagged. |
| `test_duplicate_hit_ids_deduped_before_in_clause` | Yes — 2 elements, 1 `fetch_all`, `sql.count("?")==1`, `list(params)==[fact_id]` | Yes — removing `dict.fromkeys` doubles placeholders and params | Yes | No — counting wrapper delegates to the real `fetch_all` | Correctly pins placeholder count, not call count. |

### Dockerfile.embed + `.github/workflows/ci.yml` embed-image job (W2-IMPL-04)

| Check | Asserts behavior? | Fails without fix? | Notes |
|---|---|---|---|
| CI grep `^FROM paa/runtime:4.1$` | Yes, at CI time | Yes, at CI time | There is **no pytest test anywhere in the repo** that asserts on `Dockerfile.embed` content [VERIFIED grep `Dockerfile.embed` in `tests/` -> 0 hits]. The implementer states this themselves [W2-IMPL-04 §4]. Static shape (pin, retry loop, `PIP_RETRIES`/`PIP_TIMEOUT`, `pip check` last, no `--no-index` in a RUN) is grep-verified by the implementer but not locked by any test that runs in the normal suite. See R-W2-107. |
| CI torch-free / hash-fallback / import / `pip check` assertions | Yes, at CI time | Yes, at CI time | Docker absent on this host [W2-IMPL-04 §4]; I did not re-run. The T06 pytest (`requires_embeddings`) is still a NEED with exact content in the implementer's report §2 — **not yet written** [COMMS 2026-11-07T08:55:38Z]. |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **touch.py**: all branches covered (None / empty / dedupe / success / failure). The failure branch's
  *side effect* (warning event name + redaction) is uncovered — R-W2-102.
- **apply_tier_event**: covered branches — TIER_* happy, TIER_* malformed `to_tier` (non-string),
  TIER_* legacy (missing `to_tier`), non-tier unseen fact, non-tier seen fact, no-fact_id. Uncovered:
  TIER_* with `to_tier == ""` (empty string → falls to T2 by the `and to_tier` check — I verified the
  branch reads correctly), `fact_id` of `""`/`0`/`False` (junk keys, see R-W2-103), `to_tier` being a
  *valid-format but unknown* tier like `"T9"` (fold trusts `to_tier` verbatim — no validation against
  TierPolicy; the DB CHECK refuses it at write time, but the fold itself will happily record `"T9"`).
  That last one is by design per the code comment ("the fold trusts `to_tier`"), but no test says so.
- **TierPolicy**: TTL table + resolve/ttl_for defaults covered. Frozen-ness, ctor-rejection of
  `DEFAULT_TIER`, and per-instance dict isolation uncovered (probed true by me, unpinned).
- **migrations v5**: add / backfill / assigned-tier-preserved / CHECK refusal / shape equality / version /
  fresh / idempotent-runner covered on SQLite. PG: written but NOT-RUN. The step *guards* themselves
  (re-executing a `GuardedStatement` twice directly) are never exercised — only the runner path.
- **Semantic +2**: both new branches (per-hit redact, dedupe) covered. The skipped R-W1b-03 TypeError
  path remains uncovered — explicitly out of scope per brief, recorded by W2-GEN-01.
- **Dockerfile.embed / ci.yml**: no pytest coverage. CI-time only.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Remove backfill guard (merge into add-column guard) | **YES** | `test_the_backfill_guard_repairs_a_crash_mid_step` — NULL rows stay NULL |
| Backfill UPDATE without `WHERE tier IS NULL` (resets moved tiers) | **YES** | `test_an_assigned_tier_is_not_reset_by_a_repaired_hole` |
| Remove `ck_fact_tier` CHECK from migration | **YES** | `test_a_tier_outside_the_ladder_is_refused` |
| Remove TIER_* from `_NO_PHASE_CHANGE` | **YES** | `test_replay_totality_for_tier_events` + `TestTaskReplayUnchanged` (implementer proved in-process) |
| Remove `TIER_DEMOTED`/`TIER_ARCHIVED` EventType members | **YES** | ImportError / `EventType("TIER_DEMOTED")` lookup fails |
| Remove tier vocab (`TierPolicy` / `apply_tier_event`) | **YES** | ImportError at collection (implementer's fail-before) |
| Add a second copy of the touch helper (either spelling) | **YES** | `test_exactly_one_touch_helper_definition_in_src` (AST walk) |
| **Add a second copy under a different name, or inline `DecaySweeper.touch_many` at one call site** | **NO — SURVIVOR** | Nothing: the AST lock only matches two spellings, and no test asserts the 4 call sites import/call `touch_recalled`. The per-surface suites would still pass (same observable behavior). R-W2-101 |
| **Remove `redact()` in `touch.py`'s except branch** | **NO — SURVIVOR** | `test_touch_failure_is_tolerated` asserts only no-raise. R-W2-102 |
| **Break a step guard (e.g. backfill guard always-true) and re-run at v5** | **NO — SURVIVOR** | `test_running_the_migration_again_changes_nothing` only proves the *runner* has no pending steps. R-W2-106 |
| **W3 sweeper forgets `discriminator=fact_id+to_tier`** | **NO — SURVIVOR (by construction, sweeper is W3)** | `TestDiscriminatorContract` pins `compute_idempotency_key`, not any construction site. R-W2-105 |
| Remove `dict.fromkeys` dedupe in `touch.py` | **YES** | `test_duplicate_ids_deduped_order_preserved` (param 1) |
| Remove `dict.fromkeys` dedupe in `_semantic` | **YES** | `test_duplicate_hit_ids_deduped_before_in_clause` |
| Remove per-hit `redact()` in `_semantic` | **YES** | `test_corrupt_row_hydration_failed_log_is_redacted` |
| Tier fold raises on a hostile payload (e.g. `to_tier` is a dict) | **YES** | `test_non_string_to_tier_defaults_to_t2` |
| **Non-tier event resurrects an archived fact to T2** (the implementer's first draft) | **YES** | `test_non_tier_event_does_not_resurrect_an_archived_fact` |

4 survivors of 14 considered plausible bugs — all 4 are test holes, not implementation bugs. Each is
cheap to close (see §6).

## 4. Flakiness evidence and test-suite health

- [RAN 3x the 39 fast pure tests -> 39 passed each time, identical]. No order dependence visible:
  pure functions, no shared state, no clock, no RNG.
- Migration tests: tmp_path DBs with fixed UUIDs (`_FACT`, `_ENTITY`), fixed stamp. No shared state.
  The raw-`sqlite3` setup helpers bypass the runtime deliberately and state why. One caveat: the
  downgrade helpers (`_downgrade_to_v4`, `_v4_database_with_rows`) build a fresh DB then *rewind* it —
  if a future v6 lands and `SCHEMA_VERSION` moves, `_rewind_to_v4` hard-codes version 4, which is
  correct for this file's scope but will need care. Not a flake.
- Semantic +2: real `Database` fixture (tmp), injected `now`, monkeypatch-scoped wrappers. Deterministic.
- Touch tests: no DB at all (`object()` cast, class-level patch auto-undone by monkeypatch). Deterministic.
- PG test: would use the shared `postgres_dsn` fixture — the only test in the set that touches any
  shared resource, and it is gated + skipped here. Its isolation depends on the fixture making its own
  database (per AGENTS.md convention); I did not verify the fixture itself.
- Slow: migration file is the slowest of the set (8 real DB builds + migrations) — still seconds. No
  timeouts, no sleeps anywhere in the new tests [VERIFIED by reading all 5 files].

## 5. Findings

- **R-W2-101 [MAJOR] Touch AST lock does not pin the call sites.** `tests/memory/test_touch_unification.py:50-52`
  asserts exactly one *definition* exists, but nothing asserts that `api/app.py`, `cli/main.py`,
  `coder/runtime_tools.py`, `context_agents.py` actually import and call `touch_recalled`. A future
  regression that inlines `DecaySweeper(db).touch_many(ids)` at one site (behavior-identical today,
  drift tomorrow — the exact failure mode this task exists to prevent) passes every test in the set.
  Evidence: I verified all 4 files currently import + call it (`from paa.memory.touch import
  touch_recalled` present; call counts 2/2/1/2 [RAN]) — but no test does. Recommended fix: extend the
  AST walk to assert each of the 4 modules imports `touch_recalled` from `paa.memory.touch` and contains
  a call to it, or assert `DecaySweeper.touch_many` has exactly one caller in `src/paa` outside
  `decay.py` itself.
- **R-W2-102 [MAJOR] Touch failure log redaction unasserted.** `src/paa/memory/touch.py:44-45`
  logs `error=redact(str(exc))`; `test_touch_failure_is_tolerated` (touch test:92-98) asserts only that
  nothing raises. Deleting `redact()` — a secret-leak into logs via a crafted touch failure — survives.
  This is the same class of bug P-001 exists to kill; the unified helper is now the single place a
  redaction regression would land, so it should be pinned. Recommended fix: capture logs with
  `structlog.testing.capture_logs`, raise a `RuntimeError("sk-ABCDEF1234567890")`, assert the
  `memory.recall_touch_failed` entry carries `***REDACTED***` and not the token (mirror the W2-GEN-01
  pattern verbatim).
- **R-W2-103 [MINOR] `apply_tier_event` accepts junk fact_ids silently.** Probed [RAN]: `fact_id=""` ->
  `{"": "T1"}`; `fact_id=0` -> `{"0": "T1"}`. The `if fact_id is None` check (replay.py) lets falsy-but-
  not-None values through and `str()`-coerces them into permanent dict keys. Totality (never raise) is
  preserved, so this is not a crash bug — but a rebuilt tier dict containing a `""` key is a silent
  data-quality issue the W3 sweeper will inherit. Either pin the current behavior with a test that says
  so, or treat falsy fact_ids as absent. Do not special-case silently.
- **R-W2-104 [MINOR] TierPolicy's defensive properties are unpinned.** The ClassVar decision
  (`TierPolicy(DEFAULT_TIER="T0")` must raise), frozen-ness, and per-instance `ttl_hours` isolation are
  all load-bearing for the "policy cannot be silently moved" claim in domains.py, and I verified all
  three hold [RAN]. No test asserts any of them. A refactor that reintroduces `DEFAULT_TIER` as a field
  (the implementer's first draft!) would pass the entire suite. Recommended fix: 3 small tests.
- **R-W2-105 [MAJOR] `TestDiscriminatorContract` cannot fail for the reason it exists.** Both tests in
  `tests/ledger/test_tier_replay_p009.py:94-119` construct the discriminator *in the test* and check
  `compute_idempotency_key` against itself. They pin `ledger/events.py`, which is not wave-2 code. The
  actual contract — "the W3 sweeper constructs `LedgerEvent(discriminator=fact_id + to_tier)`" — has no
  production site to test against yet, so the honest verdict is: untestable until W3, and the test names
  over-claim. Recommended fix: rename/comment as a placeholder for W3, and add the real assertion when
  the sweeper lands (structural: the sweeper module contains `discriminator=...fact_id...to_tier`, or a
  behavioral test that a double emit dedupes at the store). Not a blocker for wave 2.
- **R-W2-106 [MINOR] Idempotency test proves runner-level, not guard-level, idempotency.**
  `test_running_the_migration_again_changes_nothing` (migration test:237-252) reconnects a v5 DB; the
  runner has no pending steps, so it passes even if every `GuardedStatement` guard were broken. The
  guard-level property ("re-running the *statements* changes nothing") is only covered indirectly by
  the crash tests from the other direction. Recommended fix (P2): one test that re-executes the v5
  step statements against a v5 DB via the runner's step API, or at minimum documents in the docstring
  that guard-level idempotency is unasserted.
- **R-W2-107 [MINOR] Dockerfile.embed static shape has no pytest lock.** Pin, retry loop, pip budget,
  `pip check`-last, no `--no-index` in a RUN line — all grep-verified by the implementer, none asserted
  by a test that runs in the normal suite. A well-meaning "simplification" that drops the retry loop
  or unpins FROM passes the entire local suite and is only caught when the CI embed-image job runs
  (which it will — the grep step is first). The repo already has precedent for static-shape locks
  (the touch AST lock in this very diff; `test_compose_config.py`). Recommended fix (P1, needs a writer
  outside W2's write-sets): a small `tests/deployment/test_embed_image.py` asserting the FROM pin,
  presence of the `retry()` loop + `PIP_RETRIES`/`PIP_TIMEOUT`, `pip check` as the last RUN command,
  and that `docker-compose.yml` does not reference the embed image. The T06 `requires_embeddings`
  pytest (exact content already in W2-IMPL-04 §2) should land in the same task.
- **R-W2-108 [NIT] `TierPolicy` is not re-exported from `paa.memory`.** `DomainPolicy` is (via
  `memory/__init__.py`), `TierPolicy` is not [VERIFIED diff + RAN `'TierPolicy' in paa.memory.__all__`
  -> False]. The tier tests import from `paa.memory.domains` directly so nothing fails, but the
  asymmetry the implementer flagged in their COMMS NEED is real and still open. One-line fix for
  whoever next owns `memory/__init__.py`.
- **R-W2-109 [NIT] `test_replay_totality_for_tier_events` asserts `phase is not None` rather than the
  exact phase.** `CREATED` is the expected value (the replay test file asserts it exactly in
  `test_tier_events_alone_keep_initial_phase`). The weaker form in the vocab file would pass if replay
  wrongly advanced the phase to anything non-None. Tighten to `TaskPhase.CREATED` for consistency.
- **R-W2-110 [NIT] `test_events_lacking_tier_default_to_t2` conflates two branches.** It feeds a
  legacy TIER_* (missing `to_tier`) and a non-tier event with `fact_id` — two distinct fallback paths in
  one test. Splitting them would localize a future failure to one branch.

## 6. Missing edge cases and tests (prioritized)

**P1 (must add)**
1. **Touch call-site lock** (closes R-W2-101). Scenario: one of the 4 consumers inlines the touch logic
   again. Input: the current tree. Expected: test fails. Where: `tests/memory/test_touch_unification.py`.
   Sketch: in the existing `_touch_defs` walk, also collect `ImportFrom` nodes with module
   `paa.memory.touch` / `paa.memory` importing `touch_recalled`, and `Call` nodes to it; assert the set
   of importing modules is exactly `{api.app, cli.main, coder.runtime_tools, agents.context_agents}`.
   Alternative sketch: assert `DecaySweeper.touch_many` appears in exactly 1 call site under `src/paa`
   outside `memory/decay.py`.
2. **Touch failure redaction** (closes R-W2-102). Scenario: `touch_many` raises with a secret-shaped
   message. Input: `RuntimeError("sk-ABCDEF1234567890")`. Expected: no raise, one
   `memory.recall_touch_failed` entry, `***REDACTED***` present, token absent. Where: same file.
   Sketch: copy the W2-GEN-01 capture-logs pattern.
3. **Dockerfile.embed static lock + T06 pytest** (closes R-W2-107). Scenario: pin drift / retry loop
   deleted / `latest` FROM. Expected: local pytest fails before CI. Where: new
   `tests/deployment/test_embed_image.py` + the exact T06 test already drafted in W2-IMPL-04 §2 into
   `tests/storage/test_vector.py`. Both need a writer outside the W2 write-sets (the T06 NEED is
   already posted).

**P2 (should add)**
4. **TierPolicy defensive properties** (closes R-W2-104). Cases: `TierPolicy(DEFAULT_TIER="T0")` raises
   `TypeError`; `TierPolicy().ttl_hours["T9"] = 1` does not affect a new instance; attribute assignment
   on the frozen instance raises `FrozenInstanceError`. Where: `test_tier_vocab_p009.py::TestTierPolicy`.
5. **Guard-level migration idempotency** (closes R-W2-106). Case: re-execute the v5 `GuardedStatement`s
   against a v5 DB; assert no error and no row change. Where: `test_tier_migration_p009.py`, using the
   runner's step API rather than `Database.connect()`.
6. **Fold with a valid-format but unknown `to_tier`** (`"T9"`). Case: `TIER_DEMOTED` with `to_tier="T9"`
   on an unseen fact. Expected (current design): recorded verbatim as `"T9"` — pin it, so a future
   "validate in the fold" change is a deliberate decision with a failing test to update, not a silent
   behavior change. Where: `TestTierHostileInput`.
7. **Fold with empty-string `to_tier`** (`""`). Case: TIER_* with `to_tier=""`. Expected: T2 (the
   `and to_tier` branch). Currently covered only implicitly (the dict case exercises the `isinstance`
   leg, not the truthiness leg). Where: same class.
8. **Empty-table v4→v5.** Case: downgrade with `with_rows=False`, migrate, assert version 5 + zero
   rows + no error. The shape test already builds this shape; asserting the migration over it is one
   small test. Where: `test_tier_migration_p009.py`.
9. **Fresh-vs-migrated: compare `tier_updated_at` type + tier default explicitly.** The column-tuple
   comparison covers type/default/notnull, but a named assertion (e.g. fresh `tier` default is `'T2'`)
   localizes failure better than a tuple diff. Optional; the tuple diff already fails correctly.
10. **Multi-fact prefix/suffix split.** Case: interleave two facts' moves and split mid-stream.
    Expected: prefix+suffix fold == full fold. Where: `test_tier_replay_p009.py::TestTierRebuild`.

**P3 (nice to have)**
11. **Empty-string / zero / boolean `fact_id`** — pin or reject (closes R-W2-103). Cases: `""`, `0`,
    `False`, `[]`. Decide the semantics first; the test documents the decision.
12. **Discriminator contract rename** (closes R-W2-105's over-claim). Rename to
    `test_idempotency_key_shape_for_tier_discriminator` with a comment that the sweeper-side assertion
    lands in W3.
13. **Touch with a large duplicate-heavy id list** (100x). Case: 100 ids with 50 duplicates. Expected:
    `touch_many` called once with 50 ids. Bounds the batch; mirrors the semantic load test's spirit.
14. **`apply_tier_event` with a huge payload.** Case: payload with a 1 MB string value. Expected: no
    raise, correct tier. The implementer's report claims "huge payload tolerated" but no test feeds one.

## 7. What I could not verify

- **PostgreSQL migration half**: skipped without `PAA_TEST_POSTGRES_DSN` [RAN: 1 skipped]. The PG
  `ADD COLUMN ... CONSTRAINT ck_fact_tier CHECK ...` syntax, the `'T2'::text` default rendering, and
  fresh-vs-migrated constraint-name parity on a live server are unwritten-on-paper only. CI must run
  `test_postgres_version_4_is_migrated_and_backfilled`; if it fails, W2-IMPL-03 §"Unverified
  Assumptions" already names the one-line DDL fixes to try first.
- **Docker build / embed-image CI job**: docker absent on this host (implementer's evidence, not
  re-run by me). The entire P-005 deliverable is CI-verified only.
- **Fail-before by my own execution**: I confirmed fail-before by reasoning against the pre-wave tree
  (missing symbols, 4 defs, version 4) and by the implementers' reported import-error / monkeypatched-
  runner runs. I did not check out `8faa64e` into a worktree and run the new tests against it.
- **The 4 per-surface touch suites and the schema-parity suite**: not re-run by me (out of my scoped
  set); the implementers report them green. I verified the call-site rewiring statically (imports +
  call counts in all 4 files [RAN]).
- **R-W1b-03 (TypeError corrupt path)**: still uncovered, explicitly skipped by W2-GEN-01 per brief.
  Carried forward, not a wave-2 regression.
