<!-- ORCH-DIGEST:START -->
agent: W2-FIX-01 | scope: wave 2 fix R-W2-002/003/004/005/008 + TREV R-W2-101/102/103/104/105/106/107 + T06 test half (R-W2-007) | status: IN-PROGRESS
items: Dockerfile.embed USER root/paa; apply_tier_event tier validation + falsy fact_id no-op; TierPolicy export; touch call-site + redaction locks; discriminator W3-placeholder comment; guard-level idempotency test; static Dockerfile lock test; T06 requires_embeddings test (skips here); ci.yml reword; migrations docstring correction
top_risks: docker build NOT-RUN (docker absent, CI only); PG migration NOT-RUN (no DSN); T06 test SKIPs on this host (no extra)
confidence: high (baseline verified: scoped 47 passed+1 skipped, ruff 0, mypy 3 baseline)
<!-- ORCH-DIGEST:END -->

# Fix report — W2-FIX-01

## 0. Scope, branch, method
- Branch verified `ops/20261107-integration-fix` before starting.
- Required reading done in brief order: brief, STANDING-ORDERS, AGENTS.md, REV-W2 §§4-5+§9, TREV-W2 §§5-6, W2-IMPL-04 §2 + COMMS NEED, then code (Dockerfile.embed, Dockerfile:246,249, replay fold, domains TierPolicy.resolve, memory __init__, touch/tier/migration/vector tests, ci.yml embed header).
- COMMS last line read at start: 2026-11-07T09:30:42Z ORCH -> ALL | CORRECTION | W2 (line 77).
- Baseline [RAN]: scoped touch+tier_vocab+tier_replay+tier_migration = 47 passed, 1 skipped (PG, no DSN); `ruff check src tests` = 0; `mypy src` = 3 errors all `sandbox/subprocess_backend.py:548` baseline; docker absent (DOCKER-ABSENT).
- Working tree at start: `M docs/agent-ops/20261107-integration-fix/01-work-ledger.md` (orchestrator-owned, never touched).

## 1. Context report (root causes)
- R-W2-002: Dockerfile.embed has no USER; inherits `USER paa` (Dockerfile:249) over root-owned /opt/venv (Dockerfile:246, builder root) -> pip install + `rm -rf /build` fail EACCES. Fix at source: USER root before install, USER paa after.
- R-W2-003: `apply_tier_event` trusts any non-empty string `to_tier` (T9/garbage pass) and str()-coerces junk fact_ids ("", True, ['x']) into dict keys. DB CHECK forbids T9, so rebuilt != live. Root cause is split validation: TierPolicy.resolve validates, fold does not share it. Fix: share TierPolicy.resolve; falsy + non-scalar fact_id -> no-op (totality preserved, never raise).
- R-W2-005/104: TierPolicy not exported from paa.memory (DomainPolicy is); defensive properties true but unpinned.
- R-W2-101/102: touch AST lock pins definition count only; failure-branch redaction unasserted (removing redact() survives).
- R-W2-105: discriminator tests pin compute_idempotency_key, not a W3 sweeper construction site (sweeper does not exist yet) — placeholder, not a real contract test.
- R-W2-106: `test_running_the_migration_again_changes_nothing` proves runner-level only (no pending steps at v5); guards themselves never exercised directly.
- R-W2-107: Dockerfile.embed static shape grep-verified only, no pytest lock.
- R-W2-007 test half: exact T06 test in W2-IMPL-04 §2; A5b weights fixture deferred to W3 (not invented here).
- R-W2-008: ci.yml comment claims torch lands "once into the base image's wheelhouse" — Dockerfile:65 wheelhouse groups exclude embeddings.
- R-W2-004: migrations `_tier_step` docstring claims backfill guard is "exactly like _arrival_seq_step's" — _arrival_seq PG has one schema_scoped catalog guard and no separate data guard; tier PG data guard is unqualified while its statement is schema-qualified (single-schema correct, wording wrong). Wording-only fix.

## 2. Phased changes executed

### Phase 1 — Source fixes (Dockerfile.embed, replay.py, memory/__init__.py, migrations.py docstring, ci.yml)
- **R-W2-002**: `Dockerfile.embed` — `USER root` inserted before the install `RUN` (after the `COPY`s), `USER paa` appended after the `RUN` block, with a comment citing the inherited-user trap (base `USER paa` Dockerfile:249 over root-owned `/opt/venv` Dockerfile:246). No other Dockerfile changes. `// INVARIANT:` runtime stage never ships as root.
- **R-W2-003/103**: `src/paa/ledger/replay.py` — `apply_tier_event` now validates `to_tier` via `TierPolicy().resolve()` (shared canonical validator, `paa.memory.domains` has no paa imports so no cycle; fresh-instance import at top level, verified by the import-conventions sweep). Invalid tiers (`"T9"`, `"not-a-tier"`, `""`, non-string) fall back to T2. Falsy `fact_id` (`None`/`""`/`0`/`False`) and non-scalar/boolean `fact_id` are a no-op copy — never a junk dict key, never a raise. `// DECISION:` fresh `TierPolicy()` per tier event rather than a shared module instance, because R-W2-006's mutable table (do-later) must not become shared state. Integer ids still coerce (`12345` -> `"12345"`, existing test preserved). Docstring updated to state both rules; this is the single-vocabulary fix, not a second VALID_TIERS copy.
- **R-W2-005**: `src/paa/memory/__init__.py` — `TierPolicy` added to the `paa.memory.domains` import and to `__all__`, mirroring `DomainPolicy`. (`DEFAULT_TIER` is a `TierPolicy` ClassVar, not module-namespaced in domains, so there is nothing else to export.) Verified: `from paa.memory import TierPolicy` works.
- **R-W2-004**: `src/paa/storage/relational/migrations.py` — wording-only correction to `_tier_step`'s docstring: states the backfill data guard is *not* the `_arrival_seq_step` PG shape (single schema_scoped catalog guard, no separate data guard there), and that the unqualified PG data guard vs placeholder-qualified statement agree only for single-schema deployments. No behaviour change.
- **R-W2-008**: `.github/workflows/ci.yml` — embed-image header comment reworded: torch is downloaded once for the base build (gate's `all` shape; base wheelhouse excludes embeddings per Dockerfile:65) and once into the derived image.

### Phase 2 — Tests (5 files touched + 1 new)
- **R-W2-101**: `tests/memory/test_touch_unification.py` — new `test_every_recall_surface_imports_and_calls_the_one_helper`: AST walk asserts each of `api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `agents/context_agents.py` both imports `touch_recalled` from `paa.memory.touch` and calls it. (Implementer's choice recorded: call-site assertion, not the `touch_many`-caller-count variant.)
- **R-W2-102**: same file — new `test_touch_failure_log_is_redacted`: `RuntimeError("sk-ABCDEF1234567890")` from `touch_many`, `structlog.testing.capture_logs` (W2-GEN-01 pattern), asserts a `memory.recall_touch_failed` entry carries `***REDACTED***` and not the token.
- **R-W2-003/103**: `tests/memory/test_tier_vocab_p009.py::TestTierHostileInput` — parametrized `test_unknown_or_empty_to_tier_defaults_to_t2` (`T9`/`not-a-tier`/`""` -> T2), `test_falsy_fact_id_is_a_noop` (`""`/0/False -> unchanged state), `test_non_scalar_fact_id_is_a_noop` (`["x"]`/`{"bad":1}`/True -> `{}`).
- **R-W2-104**: same file `TestTierPolicy` — 3 tests: ctor rejects `DEFAULT_TIER=` (`TypeError`), per-instance `ttl_hours` isolation, frozen-instance assignment raises `FrozenInstanceError`.
- **R-W2-105**: `tests/ledger/test_tier_replay_p009.py` — `TestDiscriminatorContract` docstring now leads with `W3 PLACEHOLDER (R-W2-105): pins compute_idempotency_key's shape, not a sweeper construction site`. No test renamed or deleted (comment chosen over rename, recorded).
- **R-W2-106**: `tests/storage/test_tier_migration_p009.py` — new `test_v5_step_guards_are_satisfied_on_a_v5_database`: evaluates every v5 step's SQLite guards (from `MIGRATIONS`) against a v5 DB via raw sqlite3 and asserts no guard returns a row — exactly the runner's `_still_needed == False` condition, so a step re-run executes nothing. Implementer's choice recorded: guard-level test implemented (not the docstring-honesty fallback), because the guards are plain SQL evaluable without the runner's transport-bound `_run_step`. Test docstring states the runner-level test's limit it closes.
- **R-W2-007 test half**: `tests/storage/test_vector.py` — appended ONLY the T06 function from W2-IMPL-04 §2, with `@pytest.mark.requires_embeddings` + `skipif(not HAS_SENTENCE_TRANSFORMERS)` and a function-local `SentenceTransformerEmbedder` import (no module-scope heavy import, no import-block edit). A5b weights fixture NOT attempted (deferred, §6).
- **R-W2-107**: new `tests/deployment/test_dockerfile_embed.py` (5 tests, repo-root pattern from `test_compose_config.py`): exact `FROM paa/runtime:4.1` pin, retry loop + `PIP_RETRIES`/`PIP_TIMEOUT`, `pip check` is the last pip command, no `--no-index` in a RUN line (comments only), `USER root` < install < `USER paa` ordering. No docker invoked.
- Trace: existing `PLAN:integration-fix:P00x` tags kept; no new tags.

## 3. Review audit (self-review + final review)
- Self-review of the full diff (re-read from scratch): no band-aids (no try/catch added, no type suppression — an initial `setattr` formulation tripped ruff B010 and was replaced with direct assignment; no `type: ignore`/`noqa` added anywhere), no scope creep (write-set only; `01-work-ledger.md`, `COMMS.md`, sibling scratch files untouched by me), no behaviour change in migrations.py (docstring only).
- CRITICAL/MAJOR check: fold totality holds on all new hostile inputs (probes: `T9` -> `{'f1':'T2'}`, `""` fact_id -> `{}`); import of `paa.ledger.replay` in a fresh interpreter does not cycle (import-conventions sweep 21 passed); Dockerfile comment + lock agree on `<install>` = the `retry ... pip install` line (header comment's prose mention of pip install initially false-triggered the lock — fixed by anchoring to the retry line).
- Docs-lock note (MINOR, for W2-FIX-DOCS residual list): the lock goes 44 -> 48 blank-line citations. The +4 come from my `replay.py` line shifts (top-level import + docstring/body). Per brief §5 I ran the lock only as a check and edited no docs/**. W2-FIX-DOCS's final re-run covers the re-point. [RAN docs lock -> 48 blank]
- No independent reviewer was spawned (this is a one-shot subagent; the brief's review gates are the orchestrator's REV/TREV + this self-review). Recorded honestly, not skipped silently.

## 4. Verification evidence (verbatim)
- [RAN `pytest touch+tier_vocab+tier_replay+tier_migration+deployment+vector::T06 -q` -> 67 passed, 2 skipped] (skips: PG gated qdrant/vector T06 `sentence-transformers is not installed`; PG `requires_postgres` no DSN — both honest skips, not passes).
- [RAN `ruff check src tests` -> All checks passed! (0)].
- [RAN `mypy src` -> 3 errors, all `sandbox/subprocess_backend.py:548` baseline (killpg/getpgid/SIGKILL) — unchanged].
- [RAN `pytest tests/storage/test_schema_parity.py -q` -> 6 passed].
- [RAN `pytest tests/conventions/test_import_conventions.py -q` -> 21 passed] (fresh-interpreter sweep holds with replay -> memory.domains import).
- [RAN yaml parse of ci.yml -> YAML-PARSE-OK, jobs ['gate','embed-image']].
- [RAN `Get-Command docker` -> DOCKER-ABSENT; docker build NOT-RUN (CI only) — R-W2-002 fix is by Dockerfile-text proof + CI proof pending].
- [RAN `from paa.memory import TierPolicy` -> OK, resolve('T9') == 'T2'].
- [RAN Select-String Dockerfile.embed '^USER ' -> 86:USER root, 120:USER paa].
- [RAN docs lock (check only) -> FAILED, 48 blank-line citations (44 baseline + 4 from my replay line shifts); not fixed here — W2-FIX-DOCS owns docs].
- NOT RUN: full suite, docker build, shared-DB migration, PG migration path (no `PAA_TEST_POSTGRES_DSN`).

## 5. Long-run test answers
- **Source**: fold and DB now share one tier vocabulary (TierPolicy.resolve), so rebuilt tier == live column by construction; Dockerfile USER chain restored to the base's contract.
- **Load (100x/concurrency/partial)**: fold is O(1) per event + one TierPolicy() construction per tier event (5-entry dict); no new state, no concurrency surface. Migration guard test proves re-execution is a no-op at the guard level, not just the runner level.
- **Hostile input**: `T9`/garbage/empty `to_tier`, falsy/non-scalar/boolean `fact_id`, secret-shaped touch exceptions, `--no-index` in a RUN line, unpinned FROM — each has a failing-without-the-fix test.
- **Operability (observe/roll back/migrate)**: v5 migration unchanged in behaviour; rollback unchanged (dry_run off per ADR-0037). Embed image rollback is "do not build/use the derived image". ci.yml change is comment-only (yaml parses).
- **Reversal (which test fails if the fix is reverted?)**: remove USER root -> `test_user_chain_root_for_install_paa_for_runtime` fails; remove tier validation -> `test_unknown_or_empty_to_tier_defaults_to_t2` fails (`T9` recorded verbatim, REV-proven); remove falsy check -> `test_falsy_fact_id_is_a_noop` fails (junk `""` key); remove `redact()` in touch -> `test_touch_failure_log_is_redacted` fails; inline `touch_many` at one surface -> call-site lock fails; break a step guard to always-true -> guard test fails (guard returns a row).

## 6. Deferred / out-of-scope
- R-W2-007 remainder (A5b parity weights fixture + CI in-container pytest): DEFERRED to W3 per brief §6. Exact open question: who vendors `all-MiniLM-L6-v2` weights, and whether the CI embed-image job mounts sources+pytest into the container to run `pytest -m requires_embeddings` there. Not invented here.
- R-W2-006 (TierPolicy mutable/unhashable): DO LATER per brief; the fold's fresh-instance choice avoids depending on shared mutable state meanwhile.
- R-W2-009/010/011/012 NITs: accepted + noted, per brief.
- No NEED posted: everything briefed fit inside the write-set. No FINDING outside scope worth a COMMS line.

## 7. Git state
- Code+tests commit: `f009484` (11 files, +331/-23).
- Report commit: separate docs-only commit listing code SHA `f009484`.
- Branch: `ops/20261107-integration-fix` (verified before committing).
- Working tree after my commits: only sibling/orchestrator files remain uncommitted (`01-work-ledger.md`, `COMMS.md`, `W2-FIX-DOCS.md`, `w2-*.txt/py` scratch) — none touched by me.

