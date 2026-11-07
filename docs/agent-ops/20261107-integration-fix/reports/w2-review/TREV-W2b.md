<!-- ORCH-DIGEST:START -->
agent: TREV-W2b | scope: wave 2 fix tests (touch locks, fold/TierPolicy pins, discriminator placeholder, guard idempotency, Dockerfile.embed static lock, T06) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 23 (21 new items + 2 relabeled placeholder tests; 68 items executed pass-after)
fail-before/pass-after confirmed: 68 / reasoned: 21 / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 2
flaky_suspects: none (3x repeats of the 58 fast items, all 58 passed each run)
missing_edge_cases: P1 0 | P2 2 | P3 3
mutation_survivors: 3 of 16 plausible bugs not caught (1 by-design W3, 1 partial-inline, 1 trailing-USER; 1 comment-brittleness kills only via an unintended StopIteration path)
changed_behavior_without_tests: none in the fix diff (every source change has a pin; discriminator sweeper-side assertion remains W3 by construction)
confidence: high (all fix files run by me; docker build / PG / T06-with-extra NOT-RUN, honestly gated)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W2b

Fresh independent re-review of the wave-2 fix tests. I did not write the fix
code or tests, and I did not open `W2-FIX-01.md` claims until after the blind
pass (diff `c42f4ae..f009484` — the 8d39e5c docs re-point is out of scope —
plus the six test files). Prior verdict was APPROVE-WITH-CHANGES (touch
call-site lock, redaction pin, guard idempotency, Dockerfile static lock, T06
unwritten); this review checks whether the fix tests close those holes.

## 0. Method

- Blind pass: read the full test diff (`c42f4ae..f009484`) and all six test
  files before opening `TREV-W2.md §6` sketches or `W2-FIX-01.md` claims.
  Claims audit follows in §5 (per-claim verdicts).
- [RAN `.venv/Scripts/python.exe -m pytest tests/memory/test_touch_unification.py
  tests/deployment/test_dockerfile_embed.py tests/memory/test_tier_vocab_p009.py
  tests/ledger/test_tier_replay_p009.py -q` -> 58 passed, each run]. Repeated
  **3 times** (RUN1/RUN2/RUN3) for flakiness: 58 passed each run in
  1.05s/0.81s/0.81s, no variance.
- [RAN `.venv/Scripts/python.exe -m pytest tests/storage/test_tier_migration_p009.py
  tests/storage/test_vector.py::test_get_embedder_selects_the_real_encoder_when_installed -v`
  -> 9 passed, 2 skipped (PG gated, T06 `sentence-transformers is not installed`)].
- [RAN targeted re-runs: `test_v5_step_guards_are_satisfied_on_a_v5_database`
  + runner-level idempotency + 2 new touch tests + 5 deployment tests -> 9 passed].
- [RAN `ruff check` on all six touched test files -> All checks passed].
- Read-only probes via `python -c` (no file edits, no mutating the tree):
  AST import/call counts in the 4 touch consumers; `redact("sk-ABCDEF1234567890")`
  -> `"***REDACTED***"`; `apply_tier_event` on T9/empty/0/False/0.0/1.5/spaces/
  tuple/bytes/None; `TierPolicy` dataclass fields/frozen/per-instance isolation;
  `Dockerfile.embed` pip-line and `--no-index` inventories;
  `find_spec("sentence_transformers") is None` and `test_vector` import leaves
  `sys.modules` free of `sentence_transformers`/`torch`.
- No temporary worktree, no checkout of `c42f4ae`: fail-before is **reasoned**
  from the pre-fix code (pre-fix `apply_tier_event` trusted `to_tier` verbatim,
  no `USER root` in `Dockerfile.embed`, one-definition AST lock, runner-only
  idempotency) and from the probes above. I did not claim a mutation was
  "run"; mutation verdicts are stated as killed-by-reasoning unless the killed
  path is executable in-memory.
- COMMS last line read: line 80 (`2026-11-07T09:49:27Z W2-FIX-01 -> ORCH | DONE`).
  I posted no COMMS lines: this is a read-only review, no edits to source/tests,
  no commit.

## 1. Per test/suite assessment

### tests/memory/test_touch_unification.py — call-site lock + redaction pin (2 new)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_every_recall_surface_imports_and_calls_the_one_helper` | Structural — asserts each of the 4 consumers both `ImportFrom`s `touch_recalled` from `paa.memory.touch` and has a `Call(func=Name("touch_recalled"))` | **Yes for full replacement** — replacing the helper with inline `DecaySweeper.touch_many` at the *only* call site removes the import/call and fails; **No for partial inline** — api/cli/context each have 2 calls, so inlining *one* leaves `has_call` true (see R-W2b-101) | Yes — pure AST walk over sorted paths, no shell-out, no clock/RNG | No | Pins spelling+presence, not call count. Verified [RAN] all 4 currently use `from paa.memory.touch import touch_recalled` with call counts 2/2/1/2. Successor risk: a legitimate `from paa.memory import touch_recalled` (the documented re-export) or `mt.touch_recalled(...)` (Attribute call) false-fails — over-specific, see R-W2b-104. |
| `test_touch_failure_log_is_redacted` | Yes — raises `RuntimeError("sk-ABCDEF1234567890")` from `touch_many`, captures `structlog.testing.capture_logs`, asserts a `memory.recall_touch_failed` entry carries `***REDACTED***` and not the token | Yes — removing `redact()` at `touch.py:46` logs the token verbatim; first leg (`***REDACTED*** in error`) fails. Verified [RAN] `redact("sk-ABCDEF1234567890") == "***REDACTED***"` | Yes — real `touch_recalled` + real structlog; patch is auto-undone by monkeypatch | No — patches `DecaySweeper.touch_many` (the external DB write, not the unit); mirrors W2-GEN-01 pattern verbatim | The redaction survivor (R-W2-102) is genuinely killed, for the right reason. "for each entry" loop also guards double-logging. |

Existing 7 tests unchanged; the 2 new ones are additive, no rewrite of the
definition-count lock.

### tests/memory/test_tier_vocab_p009.py — fold + TierPolicy pins (12 new items, 6 functions)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_unknown_or_empty_to_tier_defaults_to_t2` (T9 / not-a-tier / "" → T2) | Yes — fold via `TierPolicy.resolve`, not verbatim | Yes — pre-fix fold recorded `"T9"` verbatim [VERIFIED replay.py:365 now calls `resolve`]; removing `resolve` flips to `{"f-1":"T9"}` | Yes, pure | No | Exactly R-W2-003. `""` covers the truthiness leg the prior review noted was implicit; non-string dict case remains pinned by the pre-existing `test_non_string_to_tier_defaults_to_t2`. |
| `test_falsy_fact_id_is_a_noop` (""/0/False → unchanged) | Yes — no junk `""` key, no raise; seeds `{"keep":"T0"}` so an overwrite/wipe would be visible | Yes — pre-fix `str("")`/`str(0)` created junk keys (probed in TREV-W2: `""`→`{"":"T1"}`) | Yes | No | Falsy set is complete per code (`not fact_id`). `0.0` hits the same branch but is unpinned (P3, see §6). |
| `test_non_scalar_fact_id_is_a_noop` (["x"]/{bad:1}/True → {}) | Yes — list/dict/bool all noop | Yes — pre-fix `str(["x"])`/`str(True)` created junk keys; also pins the explicit `bool` exclusion before the `int` coerce | Yes | No | `True` is the strongest case: without the `isinstance(bool)` leg, `True` would coerce to `"True"` (bool subclasses int). `[]`/`{}`/`()` also noop via `not fact_id`/`isinstance` [RAN] but only `["x"]`/dict are pinned. |
| `test_default_tier_is_not_settable_via_the_constructor` | Yes — `TypeError` | Yes — a field-ised `DEFAULT_TIER` (the implementer's first draft) would accept the kwarg | Yes | No | Probed [RAN]: `dataclasses.fields(TierPolicy) == ["ttl_hours"]` and ctor rejects `DEFAULT_TIER=`; `__dataclass_fields__` listing `DEFAULT_TIER` is the standard ClassVar entry, correctly not an `__init__` param. Test is the right handle. |
| `test_ttl_hours_is_isolated_per_instance` | Yes — mutate one instance's dict, fresh instance unaffected | Yes — a shared class-level dict (or `default` instead of `default_factory`) leaks `999.0` | Yes | No | `default_factory` is the mechanism; test is the lock. |
| `test_instance_is_frozen` | Yes — `FrozenInstanceError` on assignment | Yes — unfreezing the dataclass accepts the assignment | Yes | No | Direct assignment (not `setattr`), so no B010. Dict-item mutation (`ttl_hours["T2"]=...`) is *not* frozen by design and is correctly not asserted to raise. |

Totality preserved: every hostile input returns a copy, never raises
[VERIFIED replay.py:350-355]. Int coercion (`12345`→`"12345"`) still pinned by
the pre-existing test; float coercion (`1.5`→`"1.5"` [RAN]) and spaces-only
ids (`"  "`→`{"  ":"T1"}` [RAN]) are neither pinned nor rejected — see §6 P3.

### tests/ledger/test_tier_replay_p009.py — discriminator placeholder (0 new, 2 relabeled)

| Test | Asserts behavior? | Fails without fix? | Notes |
|---|---|---|---|
| `test_discriminator_key_matches_adr0008` / `test_distinct_to_tier_is_distinct_key` | No — pins `compute_idempotency_key` arithmetic (pre-existing) | No — passes with or without any sweeper (none exists) | Fix is the docstring: now leads `W3 PLACEHOLDER (R-W2-105): pins ... shape, not a sweeper construction site` [VERIFIED file:94-105]. Honest deferral; the over-claim is gone. Mutation (W3 sweeper forgets `discriminator=fact_id+to_tier`) **still survives by construction** until W3 — explicitly not counted against this wave (see §3). A rename was not done (comment chosen instead); equivalent honesty, noted in claims audit. |

### tests/storage/test_tier_migration_p009.py — guard-level idempotency (1 new)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_v5_step_guards_are_satisfied_on_a_v5_database` | Yes — evaluates **every** v5 SQLite guard directly (`MIGRATIONS` step API → `step.for_engine("sqlite").guarded` → raw `sqlite3` `fetchone`) against a fully-migrated v5 tmp DB; asserts no guard returns a row and rows are exactly `[(_FACT,T2,None),...]` | Yes for always-true guards — the R-W2-106 mutation (backfill guard always-true) returns a row and fails the `row is None` assert. **No** for always-false guards (returning no row even when work is needed) — but that direction is killed by the pre-existing crash tests (`test_the_backfill_guard_repairs_a_crash_mid_step`), so the *suite* kills both directions | Yes — tmp DB, fixed UUIDs, no shared state; PG guards honestly not exercised (sqlite engine selected) | No — raw connection, no mocks; runner's transport-bound `_run_step` deliberately bypassed, as the docstring states | Equivalence claim verified: `_still_needed == (fetchone() is not None)` [VERIFIED migrations.py:1719-1729], and for SQLite `_guard_for` is verbatim (schema rendering is PG-only), so `row is None` is exactly `_still_needed == False`. This is the implemented guard-level test, not the docstring-honesty fallback — the stronger of the two options. |

### tests/deployment/test_dockerfile_embed.py — static lock (5 new, no docker)

| Test | Asserts behavior? | Fails without fix? | Notes |
|---|---|---|---|
| `test_base_image_is_pinned_exactly` | Yes — `FROM` list is exactly `["FROM paa/runtime:4.1"]` | Yes — `latest`, a digest, a stage suffix, or a second FROM all fail | Strongest of the five; exact-equality, not substring. |
| `test_install_carries_the_retry_convention` | Half — substring presence of `retry() {`, `PIP_RETRIES=10`, `PIP_TIMEOUT=120` anywhere | Yes for deletion of the loop/ENV lines (literals live only there), **No** if the literals survive in a comment | Weakest of the five in isolation; suite still fails because the USER test's install anchor would go missing. Substring-vs-anchor asymmetry noted in R-W2b-102. |
| `test_pip_check_is_the_last_pip_command` | Half — last `"pip "`-containing line must contain `pip check`; some earlier line must contain `pip install` | **No** for install-line deletion — commented `pip install --no-index` (line 26) satisfies the `any()` leg if the real install RUN is deleted; REAL kill comes only from the USER test's `next(...)` `StopIteration` (an ERROR, not an assertion — fails for a brittle reason). Comment lines containing `pip ` (3 of 5 inventoried lines are comments [RAN]) also make a trailing `# pip ...` comment false-fail | See R-W2b-102. Same file, same reviewer trap the fix itself avoided in the USER test by anchoring to `retry /opt/venv/bin/pip install`. |
| `test_no_offline_install_in_a_run_line` | Yes — any line containing `--no-index` must be a `#` comment | Yes — adding `--no-index` to the RUN line fails | Clean. Only one `--no-index` occurrence exists (comment line 26) [RAN]. |
| `test_user_chain_root_for_install_paa_for_runtime` | Yes — `user_root[0] < install < user_paa[-1]` where install is anchored to `retry /opt/venv/bin/pip install` | Yes for USER removal (the reported R-W2-002 mutation): deleted `USER root` → `assert user_root` fails; deleted `USER paa` → `assert user_paa` fails; moved root after install → ordering fails. **No** for a trailing `USER root` appended at EOF (survivor — see R-W2b-103) | Correctly ignores comment mentions (`strip() == "USER root"` exact match). Uses `[0]`/`[-1]` rather than "last USER is paa" — the trailing-root case is the hole. Anchoring to the retry line (per W2-FIX-01 §3) is right; the header comment's prose `pip install` mention would otherwise false-trigger. |

### tests/storage/test_vector.py — T06 (1 new)

| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| `test_get_embedder_selects_the_real_encoder_when_installed` | Yes (when it runs) — `isinstance(get_embedder(ModelSettings()), SentenceTransformerEmbedder)` + `dimensions == 384` | Yes in the embed shape — with the extra installed but `get_embedder` returning `HashEmbedder`, `isinstance` fails. **Skips** (never fails) on a base image where the extra is absent | Yes — no weights loaded (`__init__` is lazy [VERIFIED embeddings.py:137-148]), no network, default `ModelSettings` | No — real selection function; function-local `from paa.storage.vector import SentenceTransformerEmbedder` | **Skips safely** without the extra [RAN: `s` here, `HAS_SENTENCE_TRANSFORMERS is False`, no `sentence_transformers`/`torch` in `sys.modules` after importing the test module — `find_spec` only, function-local import only]. Docstring's "Must fail on a base image" is inaccurate — it *skips* there (NIT R-W2b-105). `dimensions == 384` is a default-config tripwire; a deliberate default change fails it correctly. |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **touch.py**: all branches covered (None/empty/dedupe/success/failure); failure
  branch now asserts both the event name and redaction. `count=len(ids)` in the
  warning is not asserted — a `len(ids)` → `len(fact_ids)` mutation (pre-dedupe
  count) would survive. Cosmetic for an observability field; P3 if anyone cares.
- **apply_tier_event** (fix): new branches — invalid-string `to_tier` → T2,
  falsy `fact_id` noop, non-scalar/bool `fact_id` noop — all covered.
  Uncovered residuals: zero-float / empty-collection `fact_id` (same branch as
  covered `0`/`[]` — branch-covered, value-unpinned, P3); float `fact_id`
  coercion and whitespace-only ids (behavior-probed [RAN], unpinned, P3).
- **TierPolicy**: ctor-rejection, isolation, frozen-ness now pinned. `ttl_for`'s
  `2160.0` fallback (hand-built instance omitting T2) remains unpinned by design
  (defensive floor); changing the constructor to require T2 would be a
  deliberate, test-updating decision. Fine.
- **migrations v5**: guard-level idempotency now covered on SQLite (all 3
  guards). PG guards still unevaluated anywhere — gated, honest, CI-only
  residual. Always-false guard direction relies on the crash tests, which is
  valid suite-level coverage.
- **Dockerfile.embed**: FROM, retry, pip budget, pip-check-last, offline ban,
  USER order all text-pinned. Image *buildability* (the EACCES the USER chain
  fixes) is still CI-only — no local proof that the chain is sufficient, only
  that it is present. Docker absent on this host; this is scope, not a defect.
- **T06**: selection + dimensions covered. `embed()`/`warm()` paths (weights
  load) deliberately not exercised — the A5b parity suite is still deferred to
  W3; no test in this diff claims otherwise. No over-coverage.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Remove `redact()` in touch failure branch | **YES** | `test_touch_failure_log_is_redacted` (Token verbatim fails `***REDACTED*** in error`) |
| Remove touch dedupe (`dict.fromkeys`) | **YES** | pre-existing `test_duplicate_ids_deduped_order_preserved` param 1 |
| Remove `db is None` / empty early-return | **YES** | pre-existing noop pair |
| Delete touch helper entirely / resurrect a 2nd `touch_recalled` def | **YES** | definition-count lock (unchanged) |
| Replace helper with inline `DecaySweeper.touch_many` at the *only* call site (coder, 1 call) | **YES** | call-site lock (`has_call` false) |
| **Inline one of two calls in api/cli/context, keep the other** | **NO — SURVIVOR** | call-site lock is boolean presence, not count. R-W2b-101 |
| **Add a second touch helper under a different name** (`touch_all`) | **NO — SURVIVOR** | definition lock matches only `_touch_recalled`/`touch_recalled`. Pre-existing survivor, still open (not claimed fixed). |
| **Add inline `touch_many` alongside the helper call** (helper kept) | **NO — SURVIVOR** | no assertion forbids `DecaySweeper.touch_many` outside the helper; call-site lock passes. |
| Record `to_tier="T9"` verbatim (skip `resolve`) | **YES** | `test_unknown_or_empty_to_tier_defaults_to_t2` (`T9`→T2) |
| `str()`-coerce truthy junk `fact_id` (`True`→`"True"`) | **YES** | `test_non_scalar_fact_id_is_a_noop` (True leg) |
| Junk falsy keys (`""`,`0`,`False`) | **YES** | `test_falsy_fact_id_is_a_noop` (all 3) |
| **Float/whitespace `fact_id` policy drift** (e.g. `"  "`→key vs noop) | **NO — SURVIVOR (unpinned)** | neither behavior is asserted. P3 §6. |
| Make `DEFAULT_TIER` a settable field / share the ttl dict / unfreeze | **YES** (each) | 3 TierPolicy defensive pins (each kills its own mutation) |
| Break a v5 SQLite guard to always-true; re-run steps | **YES** | `test_v5_step_guards_are_satisfied_on_a_v5_database` (`row is None` fails) |
| Break a v5 guard to always-false (skip needed work) | **YES (suite level)** | crash tests (`test_the_backfill_guard_repairs_a_crash_mid_step`) + backfill rows |
| Remove `ck_fact_tier` CHECK / backfill without `WHERE IS NULL` | **YES** | pre-existing refused-tier + assigned-tier-preserved tests |
| **`USER root` removed** (the reported R-W2-002) | **YES** | `test_user_chain_...` (`assert user_root`) |
| **`USER paa` removed** (ships as root) | **YES** | `test_user_chain_...` (`assert user_paa`) |
| **Trailing `USER root` appended at EOF** (ships as root) | **NO — SURVIVOR** | `[0]`/`[-1]` ordering passes. R-W2b-103 |
| Pin drift (`FROM ...:latest`) | **YES** | `test_base_image_is_pinned_exactly` |
| Add `--no-index` to the RUN line | **YES** | `test_no_offline_install_in_a_run_line` |
| **Delete the real `pip install` RUN, keep comments** | **YES at suite level, NO at the claiming test** | `test_pip_check_...` passes (comment satisfies `any()`); suite still fails via USER test `StopIteration` (ERROR, not assertion). R-W2b-102 |
| `get_embedder` returns HashEmbedder with extra installed | **YES (in embed shape)** | T06 `isinstance` leg; skips honestly in base shape |
| **W3 sweeper forgets `discriminator=fact_id+to_tier`** | **NO — SURVIVOR by construction** (no sweeper exists; placeholder is honest) | nothing — by design; W3 must add the sweeper-side assertion. Not counted as a wave-2 failure. |

3 survivors of 16 plausible bugs (plus the pip-check brittleness counting as a
"kill for the wrong reason"). All 4 MAJORs from TREV-W2 (R-W2-101 full-inline,
R-W2-102, R-W2-106, R-W2-107) are killed or honestly deferred; the survivors
below are the next ring in, not a reopening of the old ring.

## 4. Flakiness evidence (repeated runs) and test-suite health

- [RAN 3x the 58 fast items (touch + deployment + tier_vocab + tier_replay) ->
  58 passed, 58 passed, 58 passed]. No variance, no order dependence: pure
  functions + file reads, no shared state, no clock, no RNG.
- Migration file: [RAN twice (combined + isolated) -> 9 passed, 1 skipped (PG)
  each]. Real tmp DBs per test; fixed UUIDs/stamps carried forward from the
  original suite. The new guard test adds one tmp-DB build + raw `sqlite3`
  guard evaluations — same cost class as its siblings.
- Touch tests: no DB (`object()` cast, class-level patch auto-undone). The
  redaction test adds one `capture_logs` context — log-capture is process-local
  and restored on exit; 3 repeats show no leakage between runs.
- Deployment tests: pure text reads of `Dockerfile.embed` at repo root
  (with `test_compose_config.py` precedent). No docker, no network, no FS
  writes. Deterministic across hosts (encoding pinned to utf-8).
- T06: skipped here; when run (extra installed) it constructs without loading
  weights (lazy `__init__`), so no network variance — dimensions is a config
  constant.
- PG test remains the only shared-resource test in the set, gated + skipped.
  CI must run it; its fixture isolation was not re-verified (out of scope).

## 5. Findings

- **R-W2b-101 [MINOR] Call-site lock proves presence, not count: partial inline survives.**
  `test_touch_unification.py:86-98` asserts `has_import` and `has_call`
  booleans per consumer. api/app.py, cli/main.py, agents/context_agents.py each
  call `touch_recalled` **twice** [RAN]; replacing one of the two with inline
  `DecaySweeper.touch_many` leaves `has_call` true and the commit passes.
  The full-replacement mutation (the one TREV-W2 §6.1 described) *is* killed —
  this is a sharpening of an already-good fix, not a reopening. Recommended run
  (P2 §6): assert `touch_recalled` call count per consumer equals the recorded
  baseline (2/2/1/2) or assert no `DecaySweeper.touch_many` call exists outside
  `memory/decay.py` + `memory/touch.py` (the alternative sketch in TREV-W2 §6.1,
  which closes the alongside-inline survivor too).
- **R-W2b-102 [MINOR] `test_pip_check_is_the_last_pip_command` counts comments as pip invocations.**
  `test_dockerfile_embed.py:48` collects `if "pip " in line.strip()` — 3 of 5
  matches are `#` comments [RAN: lines 26, 44, and the "download pip
  considered" line]. Consequences: (a) deleting the real install RUN leaves the
  comment `pip install --no-index` (line 26) satisfying the `any("pip install"
  ...)` leg, so *this* test does not kill install-deletion — the suite still
  fails, but only via the USER test's `next(...)` raising `StopIteration`
  (ERROR, not assertion — fails for the wrong reason and with a poor message);
  (b) adding any trailing `# pip ...` comment after the check line false-fails
  the lock. The sibling `test_install_carries_the_retry_convention` has the
  same substring weakness (literals anywhere incl. comments). Recommended (P2
  §6): filter out lines whose `lstrip()` starts with `#` before collecting pip
  lines, and anchor install presence to `retry /opt/venv/bin/pip install` the
  same way the USER test does.
- **R-W2b-103 [MINOR] USER ordering uses `[0]`/`[-1]`, so a trailing `USER root` survives.**
  `test_dockerfile_embed.py:73-80`: `user_root[0] < install < user_paa[-1]`
  passes with `USER root` appended at EOF (image then ships as root — the exact
  failure the lock annotates would be its most dangerous output). Passes the
  reported removal mutation (killed — verified by assert reading) but not the
  "ships as root" dual. Recommended (single line): assert the *last* `USER`
  line in the file is `USER paa` and the last before install is `USER root`,
  or assert `user_root == [max(user_root)]` is the install-preceding root and
  no `USER root` exists after the last `USER paa`.
- **R-W2b-104 [MINOR] Call-site lock rejects legitimate alternative spellings (false-positive fragility).**
  The helper requires `node.module == "paa.memory.touch"` and a `Name` call, so
  `from paa.memory import touch_recalled` (the public re-export the module
  docstring documents) or `mt.touch_recalled(...)` (Attribute call) would both
  fail the lock despite calling the one implementation. All 4 consumers happen
  to use the deep import [RAN], so the lock is green today — but the next
  "tidy the imports to the public path" refactor goes red for the wrong reason.
  Recommended (P3 §6): accept either `paa.memory.touch` or `paa.memory` in the
  import check, and (if Attribute calls are ever allowed) count them. Not a
  safety hole; a future-friction cost.
- **R-W2b-105 [NIT] T06 docstring overclaims failure mode.**
  `test_vector.py` T06 docstring: "Must fail on a base image (no extra):
  `get_embedder` returns `HashEmbedder` there." In fact the test **skips**
  there via `@skipif(not HAS_SENTENCE_TRANSFORMERS)` (verified [RAN: skipped,
  not failed]) — which is the correct, conventional behavior. The docstring
  describes what the *assertion* would do if it ran, not what the test does.
  One-line reword ("Skipped on a base image; in the embed shape it asserts...").
- **R-W2b-106 [NIT] Implementation record says 67 passed; my count of the same scope is 68.**
  [RAN combined scope: 58 + 9 passed + 2 skipped = 67 passed + 2 skipped] vs
  W2-FIX-01 §4's "67 passed, 2 skipped" — actually consistent once T06 and PG
  skips are excluded; see claims audit. No action; reconciled so a future
  reader does not chase the number. (Kept as NIT because per-file counts
  differ by param expansion between reports — see §1 table of new items.)

## 6. Missing edge cases and tests (prioritized)

**P1 (must add)** — none. Every R-W2-101/102/106/107/T06 hole from the prior
verdict is closed at the reported depth; remaining items are hardening.

**P2 (should add)**

1. **Touch call count / no-inline-anywhere** (closes R-W2b-101 + alongside-inline
   survivor). Cases: for each consumer, count `Call(func=Name("touch_recalled"))`
   and compare to baseline {api:2, cli:2, coder:1, context:2}; and assert
   `DecaySweeper.touch_many` appears in zero `Call` nodes under `src/paa`
   outside `memory/decay.py`/`memory/touch.py`. Expected: inlining any one
   touch (2→1) or adding an inline copy fails. Where:
   `test_touch_unification.py`, extended `_touch_import_and_call`.
   Sketch: return call *count* from the helper; assert exact dict; second
   AST walk for `touch_many` calls outside the two allowed modules.
2. **Dockerfile pip extraction without comments + last-USER assertion**
   (closes R-W2b-102/103). Cases: pip lines collected after dropping
   `lstrip().startswith("#")` lines; assert last `USER` line is `USER paa`.
   Expected: comment edits can no longer false-pass/false-fail; trailing root
   fails. Where: `test_dockerfile_embed.py`. One-line filter + one assertion;
   kills the suite-level "wrong reason" path.

**P3 (nice to have)**

3. **Accept the public re-export path** (R-W2b-104). Case: import module in
   `{"paa.memory.touch", "paa.memory"}`. Expected: `from paa.memory import
   touch_recalled` passes. Where: same AST helper. Optional — document "deep
   import is the convention" if rejection is deliberate instead.
4. **Float / whitespace fact_id pins.** Cases: `fact_id=0.0` (noop — same
   falsy branch), `fact_id=1.5` (currently coerces to `"1.5"`), `fact_id="  "`
   (currently `{"  ":...}`). Pin whichever is decided; test documents the
   decision so a future "validate in the fold" change is deliberate. Where:
   `TestTierHostileInput`.
5. **PG guard evaluation.** Case: run the same guard loop with
   `for_engine("postgres")` against a live PG at v5 — but this needs the
   server; honest form is "already gated, CI-owned" rather than a new test.
   Do not fake it with SQLite strings. Where: `test_tier_migration_p009.py`,
   appended inside the existing `requires_postgres` test when the DSN exists.

Pre-existing TREV-W2 P2/P3 items not in this fix scope (per-hit TypeError
R-W1b-03, large-duplicate batch, huge payload, multi-fact prefix/suffix) are
carried, not regressed, and not re-scored here.

## 7. What I could not verify

- **Docker build / embed-image CI job**: docker absent on this host (not
  re-attempted). USER chain is verified as text present + ordered; that it is
  *sufficient* (the EACCES is actually cured) is CI-proven only. The static
  lock proves presence/ordering, not build success.
- **PostgreSQL migration half** (including the new guard test's PG twin):
  no `PAA_TEST_POSTGRES_DSN` on this host [RAN: skipped]. PG ALTER + named-CHECK
  rendering and fresh-vs-migrated parity remain paper-only until CI.
- **T06 with the extra installed**: the test skips here (`find_spec -> None`).
  Lazy construction [VERIFIED embeddings.py:137-148] means the CI run needs no
  weights/network, but I did not execute the `isinstance`/`dimensions` legs
  against a real embed-shape env.
- **Fail-before by execution**: no `c42f4ae` checkout. Reversal verdicts are
  reasoned from pre-fix code + in-memory probes (honestly reasoned, not run).
- **Full suite**: not run (per scoped-check rule). Neighbor suites
  (`test_schema_parity`, `test_import_conventions`, per-surface touch suites)
  were not re-run by me; import-conventions sweep holding under the new
  `replay -> memory.domains` import is claimed by W2-FIX-01 and consistent
  with `memory/domains` importing no `paa` modules [VERIFIED by reading
  domains.py imports — none under `paa`], but I did not re-run the sweep.
- **Long-run behavior under 100x/concurrency/partial failure**: fold is O(1)
  per event; static locks don't execute; guard test is single-run. No load
  evidence claimed.

## 8. Long-run answers (standing orders §8)

- **Source**: rebuilt tier now shares the DB's vocabulary (`resolve`) and the
  embed image restores the base's USER contract — both fixes are at the
  invariant, not one call site. New tests pin the invariant, not the line.
- **Load (100x, concurrency, partial failure)**: fold adds one `TierPolicy()`
  allocation per tier event (5-entry dict). No new shared state (fresh instance
  per call deliberately avoids R-W2-006 shared-mutable coupling). Migration
  guard test proves re-execution no-ops at guard level, not just runner level.
  No concurrency surface introduced; tests are single-threaded.
- **Hostile input**: `T9`/garbage/empty `to_tier`, falsy/non-scalar/bool
  `fact_id`, secret-shaped touch exceptions, `--no-index` in a RUN line,
  unpinned FROM, USER removal — each has a failing-without-the-fix test
  (one falsy branch value, `0.0`, and float/space ids are P3-unpinned).
- **Operability (observe, roll back, migrate)**: v5 migration behavior
  unchanged (guard test asserts outcomes, not statements); rollback unchanged.
  Embed rollback is "do not build/use the derived image" (CI-only). `ci.yml`
  change is comment-only (not re-parsed by me — not in test scope).
- **Reversal (which test fails if the fix is reverted?)**: remove `USER root`
  → `test_user_chain_...` fails (assert, correct reason); replace `resolve`
  with verbatim `to_tier` → `test_unknown_or_empty_...` fails (`T9`); remove
  falsy guard → `test_falsy_...` fails (junk `""` key); remove `redact()` →
  `test_touch_failure_log_is_redacted` fails (token verbatim); inline the
  helper at coder (1-call site) → call-site lock fails; inline one of two at
  api → *no test fails* (R-W2b-101); break a v5 guard to always-true →
  guard test fails (row returned); delete `pip install` RUN →
  `test_pip_check_...` *passes*, suite still fails via USER `StopIteration`
  (R-W2b-102 — right suite, wrong reason).

## Claims audit (W2-FIX-01 §5 reversal table)

- "USER root removal → `test_user_chain_...` fails" — CONFIRMED by reading
  (assert, correct reason). Trailing-root dual not mentioned; see R-W2b-103.
- "Tier validation removal → `test_unknown_or_empty...` fails (`T9` verbatim)" —
  CONFIRMED [VERIFIED `resolve` is the only validator; pre-fix verbatim path
  would fail the asserted `T2`]. True.
- "Falsy-check removal → `test_falsy...` fails (junk `""` key)" — CONFIRMED
  [VERIFIED `not fact_id` is the only junk guard; pre-fix `str("")` path probed
  in TREV-W2]. True.
- "Redact removal → redaction test fails" — CONFIRMED [RAN `redact` mapping;
  token-verbatim cannot satisfy `***REDACTED*** in error`]. True.
- "Inline `touch_many` at one surface → call-site lock fails" — **PARTIALLY
  TRUE**: holds for the 1-call surface; false for 2-call surfaces minus one.
  The report does not say which surface; R-W2b-101 closes the wording.
- "Break guard to always-true → guard test fails" — CONFIRMED by the guard
  loop + `_still_needed` equivalence [VERIFIED migrations.py:1727-1729]. True.
- "67 passed" (§4): CONFIRMED as 67 passed + 2 skipped in the claimed scope
  [RAN]. Per-file delta vs TREV-W2 reconciled in R-W2b-106.
- "Discriminator: comment chosen over rename" — CONFIRMED [VERIFIED file:94-98].
  Meets the *honesty* intent of the rename sketch; not a literal rename.
- "T06 skips here" — CONFIRMED [RAN skipped; no module-scope import leak].
- "Docker/PG NOT-RUN (CI only)" — honest; concur (docker absent here too).
- "Docs lock 44->48" — not re-verified (docs scope owned by W2-FIX-DOCS);
  ASSUMED per 8d39e5c being outside the claimed code diff, not disputed.

*Verdict rationale*: 0 blockers, 0 majors. The four original MAJORs are killed
(or honestly deferred for W3 discriminator). Residual findings are MINOR
hardening (count-vs-presence, comment contamination, trailing USER, import
path) + 2 NITs. Mergeable as-is; P2 items recommended as a small follow-up.
