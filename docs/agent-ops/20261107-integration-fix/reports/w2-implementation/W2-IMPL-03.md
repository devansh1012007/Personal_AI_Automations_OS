<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-03 | domain: P-009 storage half (tier DDL + v5 migration) | status: DONE
items: 4/4 acceptance items done (DDL both files, migration v5, 8 SQLite tests + 1 gated PG test skipped, parity one-file proof)
proposed_tasks: 0 (this is the implementation)
top_risks: PG migration half NOT-RUN locally (no PAA_TEST_POSTGRES_DSN — gated test skips honestly; CI must run it); en-dash/noqa lint friction fixed and re-verified ruff clean
cross_domain_conflicts: none (sole writer of both SQL files + migrations.py this wave; P-003 normalised columns are v6 W4 — not here)
out_of_plan_discoveries: 1 (ruff RUF001/RUF002 en-dash landmines in new code — fixed by using ASCII hyphens; pattern noted for other writers)
confidence: high (SQLite scoped suite 8 passed; parity/like 31 passed; existing migration suite 55 passed; fresh-vs-migrated _shape equal; fail-before proven by monkeypatched runner)
last_comms_line_read: 2026-11-07T15:00:01Z W2-IMPL-01 -> ALL | CLAIM | T-004a-P004a
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-03 — P-009 tier DDL + v5 migration (T-009b-P009b)

## Status: DONE

## 0. Scope and method
Task: implement plan P-009 tier DDL + v5 migration exactly per brief `docs/agent-ops/20261107-integration-fix/briefs/W2-IMPL-03.md`.
WRITE-SET only: `src/paa/storage/relational/schema_sqlite.sql`, `src/paa/storage/relational/schema_postgres.sql`, `src/paa/storage/relational/migrations.py`, `tests/storage/test_tier_migration_p009.py` (new). Nothing else touched.
Required reading done in brief order: STANDING-ORDERS.md, AGENTS.md (injected), 00-mission.md, ADR-0037 (`docs/adr/0037-tier-layer-and-replay.md` — brief's filename said `-tier-migration-and-replay-rule.md`; actual file at HEAD is `0037-tier-layer-and-replay.md`, read in full), W0-INV-02 §P-009 (full), plan rev5 P09/T10 rows (grep-verified), then code (both SQL files in full, migrations.py in full, test_schema_parity.py, test_postgres_migrations.py, test_migrations.py, tests/storage/conftest.py).

Branch verified: `ops/20261107-integration-fix` [RAN git branch --show-current -> ops/20261107-integration-fix].
Working tree at start: orchestrator/other-agent files only (`01-work-ledger.md`, `COMMS.md`, `reports/w2-implementation/`) — never touched [RAN git status --porcelain=v1].

COMMS read at start; last line: 2026-11-07T15:00:01Z W2-IMPL-01 -> ALL | CLAIM | T-004a-P004a. START + CLAIM lines posted before first edit.

## Context Report (pre-implementation, per mandate section 1)
Root cause: feature gap. `hot_serving_active_facts` had no tier columns on either dialect [VERIFIED schema_sqlite.sql:259-286, schema_postgres.sql:217-242 — no `tier`/`tier_updated_at`; grep in INV-02 confirms]. ADR-0037 settles: columns `tier` + `tier_updated_at` on this table ONLY, both schemas one commit; one `MigrationRunner` step (v5) whose step itself backfills every existing row to T2 deterministically, no Settings flag; `Fact` gains no field (sweeper SELECTs tier itself — `_FACT_COLUMNS` never widened).
Affected files: both SQL files (facts CREATE TABLE each), migrations.py (v5: `_tier_step` + `_tier_updated_at_step`; SCHEMA_VERSION auto-derives from `MIGRATIONS[-1].version` [VERIFIED migrations.py:984] — the derivation line was NOT edited), new test file. Downstream: W3 sweeper (SELECTs tier), fresh-DB path (.sql files), existing-DB path (migration runner).
Invariants preserved: parity lock (column-name sets per table identical across both files [VERIFIED test_schema_parity.py:98-111]); fresh-vs-migrated shape equality incl. column order/type/default/CHECKs [VERIFIED test_migrations.py:683-730 `_shape` comparison]; guard-on-effect [VERIFIED migrations.py:323-345 — separate guards per effect or backfill silently never runs]; every statement guarded; guard placeholder count == params; schema-scoped guards name their schema; step SQL names its table+column (tests in test_migrations.py:447-533 — all green after this change).
Risks/edge cases: (a) SQLite ALTER ADD COLUMN with `NOT NULL DEFAULT 'T2'` fills existing rows with T2 already, so backfill looks redundant — but brief mandates the separately-guarded backfill (it repairs "column exists, values never assigned" — e.g. a column added nullable by an aborted build). (b) PG backfill guard must be a DATA guard (unqualified): `_guard_for` renders schema_scoped guards via `_render_comparison` (quoted literal [VERIFIED migrations.py:1632-1640]), so `$schema$.table` in a schema_scoped guard would render `'schema'.table` — invalid. The unqualified data guard resolves through the same search_path the runner resolved the schema through; catalog-span trap applies to information_schema, not to data guards. (c) tier NOT NULL vs brief shorthand `tier TEXT DEFAULT 'T2'` — see Decision D1. (d) tier_updated_at nullable (NULL = never moved); backfill leaves it NULL [ADR-0037 reading: backfill assigns tier only]. (e) No P-003 normalised columns (v6 is W4) — single-v5 warning honored.
Expected behavior: `SCHEMA_VERSION == 5`; fresh DB has tier (T2 default, T0-T4 CHECK) + tier_updated_at; v4 DB migrated: all rows tier='T2', tier_updated_at IS NULL; idempotent re-run; crash-mid-step (column added, backfill not run) repaired on re-run.
Required tests: parity green + one-file-only scratch proof; v4→v5; idempotent re-run; crash-mid-step; PG gated test mirroring test_postgres_migrations.py (skipped without DSN — NOT-RUN honestly).

Phases (atomic):
- Phase A: report stub + COMMS CLAIM [DONE].
- Phase B: DDL both SQL files + migrations.py v5.
- Phase C: tests/storage/test_tier_migration_p009.py (SQLite + gated PG).
- Phase D: scoped verification.
- Phase E: self-review + final review, commit code, commit report separately.

## Phase log
### Phase A — report stub + COMMS CLAIM (DONE, self-reviewed)
- Created this report file with ORCH-DIGEST first; posted COMMS START + CLAIM before any edit. No code touched.

### Phase B — DDL + migrations.py (DONE, self-reviewed)
- `schema_sqlite.sql`: `hot_serving_active_facts` + `tier TEXT NOT NULL DEFAULT 'T2' CONSTRAINT ck_fact_tier CHECK (tier IN ('T0', 'T1', 'T2', 'T3', 'T4'))` + `tier_updated_at TEXT` after `superseded_by`; `PLAN:integration-fix:P009` comment.
- `schema_postgres.sql`: same names; PG types (`TEXT`, `TIMESTAMPTZ`); same comment. Both in the same commit (parity lock).
- `migrations.py`: `_tier_step` (two guarded blocks per engine: add column (pragma guard / information_schema guard + schema_scoped) + backfill `UPDATE ... SET tier = 'T2' WHERE tier IS NULL` (data guard, unqualified)); `_tier_updated_at_step` (one guarded block per engine); `Migration(version=5, ...)` appended to `MIGRATIONS` (derivation line untouched). S608: unused-looking noqa lines flagged by ruff — only lines ruff actually reports carry a per-line `# noqa: S608` (verified: ruff 0 findings at the end; one unused-noqa removed, one added where ruff reports).
- Scratch verification (uncommitted, in session temp): scratch ALTER matrix confirmed ADD COLUMN works with NOT NULL DEFAULT + named inline CHECK on SQLite; DROP COLUMN tier works (column-level CHECK drops with the column) — enabling the downgrade helper without a table rebuild. Scratch e2e: v4→v5 migration T2s rows, hostile insert rejected by `ck_fact_tier`, idempotent re-run, crash-mid-step (nullable add + NULL rows) repaired to T2 with version 5.
- Key engineering decisions (for the reviewer):
  - **D1 (NOT NULL on tier):** brief shorthand says `tier TEXT DEFAULT 'T2'`; INV-02's Fix A example says `ADD COLUMN tier TEXT NOT NULL DEFAULT 'T2'`. A NULL tier passes CHECK (SQL NULL semantics) and silently evades the sweeper's ladder logic. NOT NULL DEFAULT 'T2' is adopted, honoring the authoritative investigation and making the hostile-CHECK acceptance real. Revertible in one token if the wave prefers brief-literal; flagged here deliberately.
  - **D2 (CHECK placement):** column-level named constraint in BOTH CREATE files + identical inline form in ALTER. A trailing table-level constraint would be read last in fresh CREATE text but SQLite's stored migrated statement puts appended column defs before table constraints — verified by scratch — breaking fresh-vs-migrated CHECK order equality. Column-level keeps `ck_fact_tier` first in both, matching the repo's own invariant. Scratch shape check with `_shape` (columns, checks, triggers, indexes, FKs, strict): **equal**.
  - **D3 (backfill predicate `WHERE tier IS NULL`):** ADD COLUMN's DEFAULT already makes every pre-existing row read 'T2' the moment the add commits, on both engines. The UPDATE's real work is repairing the one reachable "never assigned" state. Rows already assigned a tier (sweeper moves) must never be reset to T2 by a repaired hole in the migration. `tier_updated_at` left NULL on backfilled rows = "never moved".
  - **D4 (separate guards, `_arrival_seq_step` template):** 3 guarded blocks with 3 independent guards (add / backfill / tier_updated_at) — never one guard covering two effects, per migrations.py:323-345.

### Phase C — tests (DONE, self-reviewed)
New file `tests/storage/test_tier_migration_p009.py` — 9 tests:
- SQLite (8): SCHEMA_VERSION==5; fresh DB has columns + version row; v4→v5 backfill (rows written at v4 → 'T2', tier_updated_at NULL); hostile tier refused (CheckViolation, names `ck_fact_tier`); idempotent re-run; crash-mid-step (nullable add, NULL rows → repaired); assigned tier not reset (T0 preserved, NULL→T2); migrated-vs-fresh shape (columns + CHECK expression order equal — written with a local `_check_ranges` helper mirroring test_migrations' extractor).
- Plus a PG gated test (1) mirroring test_postgres_migrations.py shape (`postgres_dsn` fixture, `requires_postgres` + skipif, `ALTER ... DROP CONSTRAINT IF EXISTS ck_fact_tier` + DROP COLUMN downgrade, row assertions).
- Downgrade helper mirrors the house pattern (reverse the current shape), with checked facts: it proceeds from a fresh DB, drops the two columns, rewinds the log to 4, and the tests look at the DB before AND after connecting (same discipline as `test_the_reversal_really_produced_a_version_1_database`).
- Reported explicitly in the file docstring: a migration runs in one transaction, so the crash-mid-step partial state is constructed by hand (nullable add, no default), and that is the exact state the guard-on-effect rule exists for. NOT hidden.
- The PG test SKIPS here (no DSN) — honest.

### Phase D — verification (see Evidence section)
All scoped. Full-suite and containers NOT run per brief/environment rules.

### Phase E — self-review + final review (see Review Audit)
Done below. Then two commits: code (both SQL files, migrations.py, test file), then this report in a separate docs-only commit listing code SHA.

## Long-run test answers (per standing orders)
- **Source:** Guard-on-effect honored structurally — 3 guarded blocks, 3 guards, each asking about its own finished effect. The backfill's guard asks "does any row lack a tier?" — a state only the backfill produces — so the crash-mid-step test (nullable-add + NULL rows) fails without the backfill statement (evidenced by scratch monkeypatched runner: versions stuck at [1,2,3,4], tier absent — the same assertions the committed tests make).
- **Load (100x, concurrency, partial failure):** Backfill is a full-table UPDATE — O(rows), no per-row batching needed at memory scale; both engines handle it metadata-fast when the column is added first. Concurrency: runner holds the advisory lock / exclusive SQLite writes for the whole migration. Partial failure: steps + version row are one transaction on both engines; a crash rolls back to v4, and the next start re-runs to the same end state. Idempotency: once at v5, `pending()` is empty — nothing re-runs.
- **Hostile input:** tier values outside T0–T4 rejected at the schema by named CHECK `ck_fact_tier` in BOTH shapes (fresh + migrated). NULL-tier rejected by NOT NULL. No user text interpolated into SQL — every statement is built from the step's own table/column names and bound guard params (S608-clean).
- **Operability (observe, roll back, migrate):** observe via `SELECT version FROM schema_migrations` + tier column presence. Roll back: `TierSettings.dry_run=True` or sweeper off — columns ignored one-way by design (ADR-0037). Migrate: v4→v5 deterministic, no Settings flag.
- **Reversal (which test fails if the fix is reverted?):** Remove the v5 Migration → `test_a_version_4_database_is_migrated_and_backfilled`, `test_the_backfill_guard_repairs_a_crash_mid_step`, `_test_schema_version_is_five`, and the PG gated test fail (monkeypatched-runner scratch showed versions [1,2,3,4] and no tier column). Remove the backfill UPDATE → crash-mid-step test fails (NULL rows stay NULL). Remove one SQL file's columns → `test_schema_parity.py::test_columns_match_per_table` fails (proven in scratch). Remove the CHECK → `test_a_tier_outside_the_ladder_is_refused` fails.

## Review Audit
- **Phase B self-review (fresh read of the diff):**
  - [MINOR, fixed] An initial draft put `ck_fact_tier` as a trailing table-level constraint in CREATE tables. Scratch measurements proved SQLite stores ALTER-added column defs BEFORE table constraints, so migrated CHECK order would read [tier, conf, imp, embed, prov] vs fresh [conf, imp, embed, prov, tier] — breaking fresh-vs-migrated shape equality (the repo's own invariant test would catch this for any future v4→v5 comparison). Fixed by making the CREATE constraint column-level (D2); shape equality then verified equal by scratch using the real `_shape` helper.
  - [MINOR, fixed] PG tier_updated_at add initially carried `ADD COLUMN IF NOT EXISTS` (copied from arrival_seq) while the tier add used plain ADD COLUMN — inconsistent, and the IF NOT EXISTS variant could silently accept a wrongly-shaped pre-existing column. Brought to plain ADD COLUMN under the info_schema guard (mirrors `_span_parent_id_step`).
  - [MINOR, fixed] ruff flagged en-dashes (`T0–T4`) as RUF001/RUF002 ambiguous unicode; replaced with ASCII `T0-T4` in code strings and docstrings. Ruff also reported per-line `# noqa: S608` needs: two unused noqa removed (ruff said unused), one added where ruff actually reports (the schema-qualified UPDATE statement). Final `ruff check` on both files: 0 findings (only the pre-existing module-comment warning at migrations.py:542, present at HEAD — verified via git show HEAD line; no files added to the per-file allow-list, no rule relaxed).
  - [MAJOR, kept & documented] PG fresh-vs-migrated constraint name parity rests on the same name `ck_fact_tier` in fresh CREATE (column-level) and in ALTER ADD COLUMN — valid PG behavior, NOT verified locally (no DSN, gated suite skips). Flagged as the top residual risk for CI; the PG `_columns`/`_constraints` comparison in test_postgres_migrations.py's shape test will catch it there if wrong.
  - [MAJOR, verified] PG backfill guard is intentionally a data guard WITHOUT the schema placeholder (see risk (b)) — a schema_scoped data guard would be rendered as a quoted literal in name position and produce invalid SQL. The comment in the code spells this out; the SQLite half needs no placeholder at all and the guard wording is identical on both engines.
  - No independent reviewer subagent was available to this session (no separate reviewer invocation in this environment) — the final-review requirement is self-review plus the stated residual risk. This limitation is recorded rather than waived.
- **Phase C self-review:**
  - [MINOR, fixed] An early draft put `pytestmark` at module level, which would have wrongly gated the SQLite tests behind the PG env. Fixed: gate applied only to the PG test via decorators (matching test_postgres_migrations.py's pattern per-test there).
  - [MINOR, fixed] `pytest.raises(Exception)` in the hostile test tightened to the real `CheckViolation`, and the assertion checks the constraint name appears (`ck_fact_tier`) — not just that "something" failed.
  - Reviewed each test for "fails without the fix": all exercised against the scratch monkeypatched runner (fail-before), and the crash test fails if the backfill UPDATE is removed (NULL rows stay NULL by construction).
- **Final review:** `git status`/`git diff --stat` show exactly the 4 write-set files. No stray edits, no other write-set file touched, no per-file noqa/ignore widened, no `# type: ignore` added, no other table DDL changes. The migrations.py diff is viewed fresh: only the two step builders + one `Migration(version=5, ...)` entry — the `SCHEMA_VERSION = MIGRATIONS[-1].version` derivation line is untouched, as required.

## Verification Evidence ([RAN ...] claims)
- [RAN `.venv/Scripts/python.exe -m pytest tests/storage/test_tier_migration_p009.py -q` -> 8 passed, 1 skipped (PG gated, no DSN)]
- [RAN `.venv/Scripts/python.exe -m pytest tests/storage/test_schema_parity.py tests/storage/test_migrations.py -q` -> 55 passed]
- [RAN `.venv/Scripts/python.exe -m pytest tests/storage/test_schema_parity.py tests/storage/test_like.py -q` -> 31 passed]
- [RAN `.venv/Scripts/python.exe -m ruff check src/paa/storage/relational/migrations.py tests/storage/test_tier_migration_p009.py` -> 0 findings (plus a pre-existing warning about the HEAD module-comment line 542, not a finding and untouched by this change)]
- [RAN `.venv/Scripts/python.exe -m mypy src` -> 3 errors — the exact baseline (sandbox subprocess_backend.py POSIX symbols), unchanged]
- [RAN scratch scripts (uncommitted, session temp): ALTER matrix (ADD COLUMN+CHECK acceptance, hostile rejection, DROP COLUMN works on column-level CHECK, fresh-vs-migrated CHECK order equality); e2e v4→v5 + hostile + idempotent; fresh-vs-migrated `_shape` equal; crash-mid-step repair; parity one-file scratch (mismatch exactly {tier, tier_updated_at}); monkeypatched-runner fail-before ([1,2,3,4], no tier)]
- NOT RUN: full test suite (scoped only, per brief); PG migration suite against a live server (no `PAA_TEST_POSTGRES_DSN` — the gated test skipped honestly); containers.

## Unverified Assumptions & Remaining Risks
- The PostgreSQL ALTER path (named CHECK in ADD COLUMN, `DEFAULT 'T2'` catalog rendering `'T2'::text` vs fresh, same constraint name) is written from PG syntax knowledge and NOT executed here. The gated PG test mirrors test_postgres_migrations.py and will exercise it in CI; if CI's `test_a_migrated_database_has_the_same_shape_as_a_fresh_one` fails on the new constraint/name, the fix is a one-line DDL-text change (e.g., explicit `DEFAULT 'T2'::text` or separate `ADD CONSTRAINT`). The PG schema file is applied by the same file the fresh-vs-migrated comparison reads, so PG's own `_columns`/`_constraints` comparisons are self-consistent by construction modulo engine normalization.
- `tier NOT NULL` (Decision D1) is stricter than the brief's literal shorthand; if Wave-2 reviewers rule brief-literal wins, the DDL becomes `tier TEXT DEFAULT 'T2'` (one token in each file + one in the migration statements) and the crash-test's raw-nullable-add construction matches the real statement even more directly. No other code depends on tier nullability (`_FACT_COLUMNS` untouched; sweeper W3 only SELECTs tier).
- No independent reviewer subagent could be spawned from this environment (the tool-call session had no reviewer agent reachable). Self-review completed per-phase + final; flagged so the orchestrator can dispatch a fresh-context reviewer if it wants one.

## Git State
- Branch: `ops/20261107-integration-fix` (verified before each commit).
- Code commit: `228ea3a` — `storage: tier columns + v5 migration backfill (P-009)` (4 files: both SQL files, migrations.py, test_tier_migration_p009.py; nothing else).
- Report commit: separate docs-only commit listing the code SHA.
- Pre-existing uncommitted changes from other agents/orchestrator in `01-work-ledger.md` and `COMMS.md` are left alone (never staged).
