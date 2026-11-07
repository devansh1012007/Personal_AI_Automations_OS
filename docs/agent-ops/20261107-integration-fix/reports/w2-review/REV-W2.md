<!-- ORCH-DIGEST:START -->
agent: REV-W2 | scope: wave 2 (T-004a, T-009a, T-009b, T-005, W2-GEN-01) | verdict: REJECT
findings: BLOCKER 2 | MAJOR 1 | MINOR 4 | NIT 5
claims_audit: 21 checked / 3 unsupported-or-false
band_aids_found: none (no swallowing/mocking/suppression introduced; touch except-clause is pre-existing verbatim behaviour)
top_3_risks: Dockerfile.embed cannot build as committed (inherited USER paa cannot write root-owned /opt/venv or rm /build) | docs lock 0->44 broken citations gates the suite | apply_tier_event accepts invalid tier strings, rebuilt tier can diverge from DB CHECK
optimizations: DO-NOW 0 | DO-LATER 3 | NOT-WORTH-IT 1
reversal_check: done (executed probes + reruns; fail-before ImportError claims reasoned only, not re-executed)
confidence: high (USER + docs lock + tier probes executed; Docker build itself NOT-RUN - docker absent - so USER finding is by Dockerfile-text proof, not a build log)
<!-- ORCH-DIGEST:END -->

# Code review — REV-W2

## 0. Method
Diff reviewed: `8faa64e..HEAD` (commits 3a966d8, 21030ce, 228ea3a, 064e83c, 5b7bfd6 + 5 report commits). Blind pass first on diff + surrounding code (types, replay, domains, migrations runner, Dockerfile, ci.yml, embeddings, decay, config, pyproject, ADRs 0037/0042, all 5 new/changed test files); implementer reports W2-IMPL-01..04 + W2-GEN-01 read only after blind drafting. COMMS last line read at start: line 76 (W2-IMPL-03 DONE). Read-only review: no source edited, no commit.

Commands run by me [RAN]:
- `ruff check src tests` -> All checks passed (0)
- `mypy src` -> 3 errors, all `sandbox/subprocess_backend.py:548` baseline (killpg/getpgid/SIGKILL) - unchanged
- W2 scoped (touch + tier vocab + tier replay + tier migration + semantic P002 + schema parity) -> 69 passed, 1 skipped (requires_postgres, no DSN)
- `test_migrations` + `test_schema_parity` + `test_replay` -> all passed (76 dots, no failures)
- IMP01 surface set (runtime_tools + api/memory + cli/memory + context_agents + touch) -> 133 passed, 1 failed = pre-existing category-D `test_remember_then_recall_round_trips` (note-text, baseline #2, fails on `"semantic recall unavailable" in note` - unrelated to touch)
- `test_import_conventions` -> 21 passed (runtime-import contract + fresh-interpreter sweep hold for touch.py)
- `test_docs_references::test_every_referenced_line_is_not_blank` -> FAILED, 44 cited lines blank (counted from output), all citing W2-touched files (api/app, cli/main, coder/runtime_tools, context_agents, replay, domains)
- Probes (python -c): invalid-tier fold, junk fact_id coercion, TierPolicy mutation/hash, memory import, discriminator collision, get_embedder fallback, from_iso/redact, yaml parse, docker absence (DOCKER-ABSENT)

## 1. Blind-pass findings (before reading implementer reports)
1. **Dockerfile.embed USER**: no `USER` instruction; base runtime sets `USER paa` (Dockerfile:249) over root-owned `/opt/venv` (Dockerfile:246, builder root). Inherited paa cannot pip-install or `rm -rf /build` -> build fails deterministically. Not caught by any local check; CI would go red at the derived-build step.
2. **Docs lock 0->44**: every W2 mid-file insertion landed in heavily-cited files; suite gate now red. Mechanical, no code wrong, but gate-blocking by repo rules and W1 precedent.
3. **apply_tier_event tier validation**: accepts any non-empty string (`T9`, `not-a-tier`) despite docstring promising contract violations fall back to T2; rebuilt tier can contain values the DB CHECK forbids, so rebuilt != live column for malformed streams.
4. **TierPolicy not exported** from `paa.memory` (DomainPolicy is); DEFAULT_TIER duplicated in replay/TierPolicy/SQL literals; TierPolicy frozen-but-mutable dict, unhashable.
5. **PG backfill guard** unqualified while statement is schema-qualified (works single-schema, can diverge multi-schema); PG path NOT-RUN locally (CI only) - residual risk, plus docstring claim of exact _arrival_seq pattern is inaccurate.
6. **T06/A5b**: CI asserts import+pip-check only; no pytest selection test, no parity suite - deferred by write-set, needs orchestrator task.
7. Touch unification, DDL parity, guard-on-effect, S608-per-line, runtime-import convention, scope discipline (no P-003 cols, no sweeper, no fallback, no config change) all clean on blind pass.

## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| IMP01: 4 defs deleted, 1 remains; 8 sites rewired, behaviour identical | YES | grep `_touch_recalled` in src = 0 [VERIFIED]; diff args/ordering unchanged; surface set rerun 133+1 pre-existing [RAN] |
| IMP01: 7 touch tests; ruff scoped clean; mypy 3 baseline; import-conventions 21 | YES | all rerun by me, same outcomes [RAN] |
| IMP01: category-D failure untouched/not ours | YES | failure is note-text at runtime_tools:437, baseline #2 [RAN] |
| IMP02: 32 new tests green; ruff 0; mypy 3 | YES | in my 69-pass scoped run; ruff/mypy rerun [RAN] |
| IMP02: fail-before ImportError on new tests | REASONED | plausible (symbols net-new) but I did not revert src to re-execute |
| IMP02 §6: hostile "missing/non-string fact_id -> no-op copy" | FALSE | code coerces via `str()`; test asserts `{"12345":"T3"}`. Report contradicts its own Phase 4/code. See R-W2-003. |
| IMP02: recovery.py vacuous for P09 | YES | recovery.py dispatches only RECOVERY_REPLAY_COMPLETED/STATE_ROLLBACK/TASK_ABANDONED [VERIFIED grep] |
| IMP02: discriminator=fact_id+to_tier contract pinned | YES | test_tier_replay passes; concatenation collision dismissed: valid tiers fixed-width (2 chars) so fact_id+to_tier is injective over valid inputs |
| IMP03: 8 SQLite pass + 1 PG gated skip; parity/migrations 55 pass | YES | rerun by me [RAN] |
| IMP03: PG NOT-RUN honestly stated | YES | skip observed; test docstring states transaction limit plainly |
| IMP03: fresh-vs-migrated shape equal | YES | committed shape test passes [RAN] |
| IMP03 docstring: PG data guard "exactly like _arrival_seq_step's" | FALSE (detail) | _arrival_seq PG uses one schema_scoped catalog guard, no separate data guard; tier PG uses unqualified data guard. Functionally OK single-schema; wording wrong. See R-W2-004. |
| IMP04: pin `FROM paa/runtime:4.1` exact; retry/PIP conventions; ADR-0042 online install | YES | Dockerfile.embed:58,67-68,91-105 read [VERIFIED]; yaml parse rerun [RAN] |
| IMP04: docker build NOT-RUN, CI only | HONEST | docker absent confirmed [RAN Get-Command docker -> absent]; never claimed otherwise |
| IMP04: "torch downloaded once into base image's wheelhouse" (CI comment) | FALSE (detail) | Docker wheelhouse groups = vector,api,redis,postgres,s3,otel, embeddings deliberately absent (Dockerfile:65). True only of gate's `all` pip leg. Timeout rationale misstated. See R-W2-007. |
| IMP04 self-review: "No findings" on correctness | FALSE | missed R-W2-002 (USER) - static review should have caught inherited-user vs root-owned venv; CI will catch it later at cost of a red build |
| IMP04: T06 pytest deferred, NEED posted, exact test delivered | YES | COMMS NEED line + report §2 test content read; write-set genuinely excluded tests/ |
| GEN-01: 16 tests pass x3; ruff 0 scoped | YES | collect = 16 [RAN]; file in 69-pass run; full ruff 0 [RAN] |
| GEN-01: non-vacuity (token in raw error, redact maps it) | YES | rerun scratch: from_iso error carries token verbatim; redact -> ***REDACTED*** [RAN] |
| GEN-01: R-W1b-03 skipped per brief | YES | recorded in report §2/§6, no src edit in diff |
| IMP03: Decision D1 `tier NOT NULL` stricter than brief shorthand | YES, justified | NULL bypasses CHECK; INV-02 Fix A example agrees. Correct call, flagged deliberately. |
| IMP02: no DDL/config touched; IMP03: only 4 write-set files; IMP04: ci.yml +121/-0 | YES | `git diff --stat 8faa64e..HEAD` confirms file lists exactly [RAN] |

21 claims checked, 3 false details (1 report self-contradiction + 2 comment/wording inaccuracies), 1 self-review miss that hides a BLOCKER.

## 3. Long-run test (independent answers)
- **Source**: touch single-home is correct root-cause fix (4 drifted copies -> 1). Tier: fold + TierPolicy + DDL CHECK are three sources for the tier vocabulary (fold validates none, TierPolicy validates, SQL CHECK validates) - they can disagree (R-W2-003). Version 5 derived, no flag, deterministic T2 backfill - right.
- **Load (100x/concurrency/partial)**: touch = one execute_many batch, unchanged. Tier fold O(events) copies - trivial. Migration: single transaction + advisory lock/exclusive writes; crash rolls back to v4, re-run converges (SQLite proven; PG reasoned). Dockerfile retry x5 + pip budget absorbs transient; USER bug makes load irrelevant (never builds).
- **Hostile**: schema CHECK + NOT NULL reject hostile tiers at DB. Fold does not (R-W2-003); junk fact_ids create junk dict keys ("", "True", "['x']") - memory/dict pollution only, no crash, no SQL. Touch dedup bounds batch; ids DB-sourced. Embed install online without hashes - supply-chain surface accepted by ADR-0042, noted.
- **Operability**: log event names preserved (logger identity changes paa.* module - NIT). Tier rollback = dry_run/sweeper off, columns ignored (ADR-0037) - sound. Docs lock red means operators reading docs get blank-line citations - mechanical fix only.
- **Reversal**: revert _NO_PHASE_CHANGE -> totality tests fail (IMP02 in-process proof, reasoned). Remove v5 Migration -> tier migration tests fail (IMP03 scratch, reasoned). Restore a 2nd touch def -> AST lock fails. Remove USER fix -> embed build fails (this review's prediction; CI will prove). Remove dedup/redact -> GEN-01 tests fail (scratch-proven).

## 4. Findings
### R-W2-001 [BLOCKER] Docs lock broken 0 -> 44 by W2 line shifts
- Location: 44 citations into `src/paa/api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `agents/context_agents.py`, `ledger/replay.py`, `memory/domains.py` · Evidence: [RAN docs lock test -> FAILED, 44 blank cited lines] · Why: suite gate is red; AGENTS baseline is zero and W1 treated identical breakage (63) as BLOCKER requiring re-point before DONE. Every wave inserting mid-file into these files will re-break it - that is a process fact, not a waiver. · Recommended fix: W2-FIX-DOCS task exactly as W1 (read + re-point each citation, not blind renumber); do not revert code.

### R-W2-002 [BLOCKER] Dockerfile.embed cannot build: inherited USER paa vs root-owned /opt/venv
- Location: `Dockerfile.embed:58-105` (no USER) vs `Dockerfile:246,249` · Evidence: [VERIFIED] builder creates /opt/venv as root (no USER in builder, Dockerfile:109-144), runtime `COPY --from=builder /opt/venv` preserves root ownership, final `USER paa`; derived image inherits paa; `pip install` must write root:root 755 site-packages and `rm -rf /build` must write root-owned /build -> EACCES under `set -eux`, build fails deterministically on any Docker host. CI embed-image job will go red at "build the derived embeddings image". · Recommended fix (root cause): `USER root` before the install RUN, `USER paa` after it (preserve runtime user; never ship runtime stage as root). Add a CI-visible comment citing the inherited-user trap so it is not "tidied" away.

### R-W2-003 [MAJOR] apply_tier_event accepts invalid tier strings, contradicting its own contract
- Location: `src/paa/ledger/replay.py:340-345` · Evidence: [RAN probe -> `apply_tier_event({}, TIER_DEMOTED to_tier='T9') == {'f1':'T9'}`, `'not-a-tier'` likewise] while docstring (replay.py:308-311) promises contract violations fall back to DEFAULT_TIER, and ADR-0037's T10b goal is rebuilt tier == live hot column (DB CHECK forbids T9). `TierPolicy.resolve` validates; the fold does not share that validation - three vocab sources disagree. Also junk keys: fact_id `''`/`True`/`['x']` -> keys `''`/`'True'`/`"['x']"` (totality achieved by pollution). · Recommended fix: validate `to_tier` against the canonical set (share TierPolicy.resolve or a single VALID_TIERS constant - domains has no paa imports, cycle-safe) falling back to T2; treat empty/non-scalar fact_id as no-op. Add tests for `T9`/garbage/empty.

### R-W2-004 [MINOR] PG backfill guard unqualified vs schema-qualified statement; PG path unverified
- Location: `migrations.py` `_tier_step` postgres second block · Evidence: guard `FROM hot_serving_active_facts` (search_path resolution) vs statement `UPDATE $schema$.hot_serving_active_facts`; `_guard_for` only renders schema_scoped guards, so a qualified data guard is not constructible today - single-schema deployments (incl. CI fixture DB) are correct, multi-schema search_path with same-named table elsewhere could guard one schema and write another. PG test skipped locally (no DSN), CI-only. Docstring "exactly like _arrival_seq_step" is inaccurate (see claims audit). · Recommended fix: keep behaviour; correct docstring; add PG shape/constraint comparison coverage in CI run (IMP03 already flags this as top residual - concur).

### R-W2-005 [MINOR] TierPolicy not exported from paa.memory; DEFAULT_TIER in 3+ places
- Location: `src/paa/memory/__init__.py` (absent) vs `memory/domains.py:315`, `ledger/replay.py:294`, SQL `'T2'` literals · Evidence: [RAN probe -> `from paa.memory import TierPolicy` raises ImportError]; NEED already posted by IMP02 (COMMS) but __init__ was IMP01-owned and TierPolicy asymmetry with exported DomainPolicy remains. W3 sweeper's natural import path fails. · Recommended fix: one-line export in owning task; later unify default-tier source.

### R-W2-006 [MINOR] TierPolicy frozen-but-mutable + unhashable
- Location: `memory/domains.py:286-336` · Evidence: [RAN probe -> mutating `ttl_hours['T2']=999` changes `ttl_for`; `hash(TierPolicy())` raises TypeError: unhashable dict] deviates from DomainPolicy scalar value-object pattern; a sweeper caching policies in a set/dict or relying on canonical TTLs could be silently surprised. · Recommended fix: MappingProxy/frozenset-of-pairs or document mutability; not blocking pre-W3.

### R-W2-007 [MINOR] T06 pytest + A5b parity not delivered (deferred dependency, not a defect in write-set)
- Location: `tests/storage/test_vector.py` (absent), CI embed job · Evidence: IMP04 NEED + report §2 exact test content; ADR-0042 says parity suite "runs only inside the embed image" - CI runs import + pip check, no pytest-in-container, no weights fixture. · Recommended fix: orchestrator follow-up task (write the delivered test; decide weights fixture + in-container pytest) or formal deferral recorded like RSK items.

### R-W2-008 [NIT] CI comment misstates torch download path
- Location: `.github/workflows/ci.yml` embed-image header · Evidence: "once into the base image's wheelhouse" - Docker wheelhouse excludes embeddings (Dockerfile:65). · Fix: reword to base build + derived torch install.

### R-W2-009 [NIT] Touch logger identity changes
- Location: `memory/touch.py:17` · Evidence: old per-module `get_logger(__name__)` (api/cli/coder/context_agents) now `paa.memory.touch`; event names identical, logger name differs - log filters by logger break silently. Tests pass. · Fix: note in ops docs or accept.

### R-W2-010 [NIT] Crash-mid-step repair restores data, not schema
- Location: `_tier_step` backfill · Evidence: hand-built nullable tier column (test construction) gets NULLs -> T2 but stays nullable/CHECK-less, divergent from fresh shape. Unreachable via real transactional runner (steps + version row one tx) - test-only state. · Fix: accept + comment, or add schema-repair guard; not blocking.

### R-W2-011 [NIT] GEN-01 dup test len(calls)==1 is brittle to future _semantic queries
- Location: `test_semantic_derivation_p002.py` dup test · Evidence: placeholder/params legs are the real pin (report says so); call-count leg breaks if _semantic legitimately issues another fetch_all. · Fix: accept, or drop count leg in later touch.

### R-W2-012 [NIT] Embed install unpinned (sentence-transformers>=3.3 floats)
- Location: `pyproject.toml:42` via Dockerfile.embed · Evidence: two builds a week apart resolve different torch/ST; matches base-image float pattern + ADR-0042 silence. Reproducibility/supply-chain note only.

## 5. Attempts to break it
- Invalid/empty/junk tier fold inputs [RAN]: T9/garbage pass through (R-W2-003 confirmed); empty/bool/list fact_id create junk keys; missing fact_id -> no-op copy (correct); non-tier event after archive does not resurrect (correct, test-pinned).
- Discriminator collision `f1T+1` vs `f1+T1` [RAN]: keys equal in isolation, but dismissed - with valid fixed-width tiers the mapping is injective, and ADR-0037 literally specifies this discriminator. Not a finding.
- TierPolicy mutation/hash [RAN]: both confirmed (R-W2-006).
- Migration: SQLite idempotency, crash-mid-step, assigned-tier preservation, shape equality all pass via committed tests [RAN]; hostile T9 insert refused by CHECK [RAN via test]; PG equivalent NOT-RUN (no DSN) - stated.
- Touch: None/empty/dup/failure paths test-pinned and passing [RAN]; no second def in src [RAN grep].
- Dockerfile: build NOT-RUN (docker absent [RAN]); USER break proven by text chain (R-W2-002), not a build log - I do not claim a build was run.
- Docs lock: counted 44 failures [RAN], all into W2-touched files; at W2 base it was 0 (W1 gate), so regression is W2-attributable, mechanical in nature.

## 6. Dimension checklist
- Correctness: FAIL (R-W2-002, R-W2-003) · Root cause vs symptom: touch/DDL root-cause correct; tier fold symptom-level validation split (R-W2-003) · Regression/compat: no prod regressions found; category-D pre-existing; docs gate regressed (R-W2-001) · Concurrency/idempotency: migration guards sound (SQLite proven, PG CI-only); fold pure/idempotent; touch batch unchanged · Error handling: touch degrade preserved verbatim; fold total (over-total: junk keys) · Security: CHECK/NOT NULL at schema; redaction pins pass; embed online-install accepted per ADR; runtime USER must stay paa after R-W2-002 fix · Performance: no issues (O(rows) backfill, O(events) fold, batch touch) · Migration/rollback: v5 one-way + dry_run-off rollback per ADR-0037, sound · Observability: event names preserved, logger identity NIT · Maintainability/conventions: runtime-import, enum style, S608 per-line, guard-on-effect all hold; TierPolicy export/mutability MINOR · Docs impact: R-W2-001 BLOCKER, R-W2-008 NIT.

## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | replay.py fold vs TierPolicy.resolve | two validators, one lax | divergent rebuilt tier | small (share one) | low | DO NOW (as R-W2-003 fix, correctness not opt) |
| 2 | DEFAULT_TIER x3+ | replay/TierPolicy/SQL | drift on ADR change | small | low | DO LATER |
| 3 | .dockerignore lacks Dockerfile.embed | harmless context entry | context hygiene | trivial | none | DO LATER |
| 4 | batch backfill / fold micro-opt | O(n) already metadata-fast | negligible | - | - | NOT WORTH IT |

## 8. Cross-agent / cross-task interactions observed
- IMP01 owned `memory/__init__.py`, IMP02 needed TierPolicy export there - NEED posted, not actioned (R-W2-005). Orchestrator must assign.
- IMP01's mid-flight ruff FINDING in migrations.py was IMP03's in-flight en-dash/noqa work, later fixed (ruff now 0) - resolved, no action.
- IMP04's NEED (T06 test writer) + FINDING (A5b fixture) both correctly escalated, not silently dropped (R-W2-007).
- No scope creep: no P-003 columns, no sweeper, no vector fallback, no config/schema changes beyond tier pair. Shared hotspots (types/replay, both SQL files, migrations) single-writer per wave - held.

## 9. What I could not verify
- Docker build of either image (docker absent on host; CI only) - R-W2-002 is a Dockerfile-text proof, prediction of CI red, not an observed build failure. Fix must be proven by CI build.
- PostgreSQL v5 migration end-to-end (no DSN; gated test skipped) - syntax/shape reasoned from PG knowledge + IMP03 scratch, CI must execute.
- Fail-before ImportError claims (IMP02/IMP03 scratch) - reasoned, not re-executed (would require reverting src, out of read-only mandate).
- Full suite not run (scoped only per standing orders); baseline 14 failures not re-baselined - only W2-scoped + surface sets rerun.
