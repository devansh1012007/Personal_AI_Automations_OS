# Brief — W2-IMPL-03 (IMPL) · wave 2 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-009 tier DDL + v5 migration (T-009b) — columns in BOTH SQL files (parity lock) + deterministic T2 backfill in the migration step itself. Why: existing DBs must gain the tier columns without losing the runner contract; fresh DBs must get them from the CREATE TABLE files.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (storage parity lock — table/column-identical, same commit; guard-on-effect; S608 per-line rule — never widen a per-file ignore; LIKE irrelevant here)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (hotspot: both SQL files + `migrations.py` single-writer this wave)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P09 row (columns on `hot_serving_active_facts` ONLY, both SQL files, runner step bumps SCHEMA_VERSION, deterministic backfill in-step to T2, no Settings flag) + §H T10 (DDL half only — sweeper is W3)
5. ADR `docs/adr/0037-tier-migration-and-replay-rule.md` (table list, column names/types, backfill semantics — follow it exactly)
6. Investigation (authoritative): `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-02.md` §P-009 — dual-file DDL evidence (sqlite :259-286, postgres :217-242, identical 15-col set), migration tail (SCHEMA_VERSION=4, next is v5, `MigrationStep` shape, guard-on-effect rule, `_arrival_seq_step` template SQLite pragma guard :657-668 + separate backfill guard :669-673, PG information_schema guard precedent `:930-950`), Fix A (dual-file + v5 step), hostile CHECK constraint, long-run test (separate guards or backfill never runs; re-runnable; fresh DBs stamped at current version), test plan (parity one-file→fail proof; v4→v5 unit; idempotent re-run; crash-mid-step), single-v5 warning (P-003 normalisation is v6 — do NOT combine here).
7. Template: implementation report ORCH-DIGEST first
8. Code (verify at HEAD): `src/paa/storage/relational/schema_sqlite.sql` + `schema_postgres.sql` (`hot_serving_active_facts` CREATE TABLEs), `src/paa/storage/relational/migrations.py` (tail + `_arrival_seq_step` + `_span_parent_id_step` + guard-on-effect doc :323-345), `tests/storage/test_schema_parity.py` (what it parses), `tests/storage/test_postgres_migrations.py` shape (for the gated PG half — do NOT run it without DSN).

## 3. Task and acceptance criteria
Task T-009b-P009b. Specifically:
- Both `schema_sqlite.sql` and `schema_postgres.sql` `hot_serving_active_facts` CREATE TABLEs: add `tier TEXT DEFAULT 'T2' CHECK (tier IN ('T0','T1','T2','T3','T4'))` (+ PG types) and `tier_updated_at` (type per ADR-0037; nullable — NULL means "never moved", default-T2 path). Same column NAMES in both files, same commit (parity lock — `test_schema_parity.py` must pass). Column order: append after `superseded_by` (or wherever ADR-0037 says; do not reorder existing columns). No other table changes (ADR-0037 lists exactly one table).
- `migrations.py`: append `Migration(version=5, ...)` to `MIGRATIONS` (SCHEMA_VERSION auto-derives — do not edit the derivation line). Steps with SEPARATE guards (guard-on-effect — never one guard covering both effects or the backfill never runs): (1) add `tier` column (SQLite pragma guard, PG information_schema guard), (2) add `tier_updated_at` column, (3) deterministic backfill UPDATE existing rows → `tier='T2'` (guarded separately; `tier_updated_at` stays NULL for backfilled rows unless ADR-0037 says otherwise — state your reading). CHECK constraint on ALTER (SQLite allows `CHECK(...)` in ADD COLUMN; PG same). Follow the `_arrival_seq_step`/`_span_parent_id_step` idioms; no per-file noqa widening; any interpolated SQL takes per-line `# noqa: S608` per house convention (memory/facts.py precedent) — prefer parameterised/guarded forms over interpolation where the precedent does.
- Tests (must FAIL before, pass after): (a) parity: `test_schema_parity.py` green with tier in both; PROVE the lock by showing one-file-only fails (document the one-file run in your report — run it as a scratch check, not a committed test); (b) migration unit: build a v4 DB (Schema at v4 — follow existing migration-test fixtures), run the runner (tmp DB only, NEVER a shared DB), assert `SCHEMA_VERSION==5`, all pre-existing rows `tier='T2'` + `tier_updated_at IS NULL`, version row 5; (c) idempotency: re-run runner → no double-backfill, no error, rows still T2; (d) crash-mid-step: column added but backfill not run (simulate by applying step 1 only or truncating mid-run with a tmp DB) → re-run completes backfill. New test file `tests/storage/test_tier_migration_p009.py` (SQLite path). The PG migration test is gated (`requires_postgres`, needs `PAA_TEST_POSTGRES_DSN`) — write it mirroring `test_postgres_migrations.py` shape and mark it correctly, but it will SKIP here (record NOT-RUN honestly — never claim PG green without a DSN).
- Trace tag `PLAN:integration-fix:P009` comments in the DDL + migration step.
- Acceptance: parity test green; `SCHEMA_VERSION==5`; v4→v5 + idempotent + crash-mid-step green; `pytest tests/storage/test_schema_parity.py tests/storage/test_like.py -q` green; ruff 0, mypy total stays 3; `git diff --stat` shows the 3 files (+test) only.

## 4. Ownership
WRITE-SET (only these): `src/paa/storage/relational/schema_sqlite.sql`, `src/paa/storage/relational/schema_postgres.sql`, `src/paa/storage/relational/migrations.py`, `tests/storage/test_tier_migration_p009.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/core/types.py`/`ledger/*` (W2-IMPL-02 owns vocab — your columns' names must match ADR-0037, not invented here), `src/paa/config.py`, `docs/adr/*`, P-003 normalised columns (v6 is W4 — do NOT add them; single-v5 warning), `src/paa/memory/decay.py` (sweeper W3).
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: post COMMS CLAIM for the 3 hotspot files before editing (sole owner this wave).

## 5. Parallel siblings this wave
W2-IMPL-01 (touch — disjoint), W2-IMPL-02 (tier vocab — shared DEPENDS-ON only at W3 sweeper; your DDL does not wait on their vocab), W2-IMPL-04 (Dockerfile.embed), W2-GEN-01 (P-002 minor tests). You are the ONLY writer of both SQL files + `migrations.py` this wave (parity + version discipline).

## 6. Skip list and known traps
- Fresh DBs get columns from the edited .sql files and are stamped at current version (runner `:1214`) — do NOT add a migration path for fresh DBs; the .sql files ARE the fresh path.
- Guard-on-effect: one guard per effect. A single guard covering ADD COLUMN + backfill means the backfill silently never runs (migrations.py:323-345) — separate guards are the root-cause requirement, not style.
- SQLite has no `ADD COLUMN IF NOT EXISTS` — pragma guard precedent is mandatory. PG: information_schema guard with `schema_scoped=True` where the precedent uses it.
- `tier_updated_at` type: follow ADR-0037 (TIMESTAMPTZ/TEXT per engine types). Do not invent NULL-vs-default — state the ADR reading.
- `hot_serving_active_facts` readers: do NOT widen `_FACT_COLUMNS` (INV-01: `Fact` contract stable; sweeper SELECTs tier in W3). Your DDL must not change any existing read.
- Never run migrations against a shared/home DB or the repo's own state — tmp_path fixtures only. Baseline category-A sandbox/docker failures are not yours.

## 7. Environment and tests
Scoped only: your new test file + parity/like scoped suites + ruff/mypy scoped. No full suite, no containers, no shared-DB migration, PG suite NOT-RUN (no DSN — mark honestly).
Commit protocol: code+tests commit first (BOTH SQL files in the SAME commit — parity lock), then report in a separate docs-only commit. Trailers `Task: T-009b-P009b`, `Agent: W2-IMPL-03`, `Wave: W2`, `Report: docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-03.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w2-implementation/W2-IMPL-03.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile (tier CHECK)/operability (flag-off, column ignored)/reversal (drop step → runner refuses)). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
