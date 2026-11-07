<!-- ORCH-DIGEST:START -->
agent: W4-FIX-01 | domain: W4 fixes (graph 2nd writer, TUI mirror, claim guard, backfill count, casualties C1-C5) | status: DONE (NEED posted for v5-shape STRICT wedge)
code_commit: 8971e83
items: R-W4-004 graph norm cols + cross-writer tests | R-W4-003 runner 3 keys + shared formatter + 5 tests | R-W4-005 notify comment | R-W4-007 claim guard + run_allocation typing + 3 tests | R-W4-008 corrupt count-log + test (crash-mid-step shape) | C1-C5 deliberate updates with justification | TREV P1 run_allocation tests
proposed_tasks: n/a (this is the fix; NEED below for follow-up)
top_risks: (1) v5-shape corrupt blob wedges v6 migration at ADD COLUMN (STRICT re-validates CHECKs) — NEED posted; (2) graph DO UPDATE preserves aliases_norm (deliberate deviation from brief's literal wording — writing '[]' would erase facts aliases); (3) pre-existing category-B test_ledger_with_events failure left untouched (out of write-set)
cross_domain_conflicts: runner.py + allocator.py shared files — CLAIM posted; COMMS.md interleaved lines are W4-FIX-DOCS's — left uncommitted
out_of_plan_discoveries: the STRICT ADD COLUMN wedge (new finding, evidence in report §F)
confidence: high (all contracts re-verified at HEAD; every touched suite re-run green; ruff 0; mypy 3 = baseline)
last_comms_line_read: 2026-11-07T18:55:00Z W4-FIX-DOCS -> ALL | FINDING (COMMS tail)
<!-- ORCH-DIGEST:END -->

# Implementation report — W4-FIX-01 (IMPL) — wave 4 fix · run 20261107-integration-fix

## 0. Scope and method
- Brief: `docs/agent-ops/20261107-integration-fix/briefs/W4-FIX-01.md`. Task W4-FIX-01.
- Required reading in order: STANDING-ORDERS.md; AGENTS.md; REV-W4 §§4–5,§9; TREV-W4 §§5–6;
  W4-IMPL-01/02/03; code at HEAD (line numbers re-verified by me, not trusted from plan).
- Branch verified: [RAN `git branch --show-current` → `ops/20261107-integration-fix`] before commits.
  Tree at start: only sibling docs (W4-FIX-DOCS report + COMMS modification) — never touched.
- COMMIT protocol: code+tests commit first (explicit paths), then this report in a separate docs-only commit listing code SHAs.
- Kuzu verified: [grep `hot_serving_entity_index` in `src/paa/storage/graph/` → only `sqlite_backend.py`;
  kuzu `upsert_entity` MERGEs its own `Entity` node table (kuzu_backend.py:276-282)] — R-W4-004 is sqlite-only.
- Scoped checks only; no full suite, no containers, no migrations on the shared DB.
- Skipped list honored: R-W4-002 (scheduling), R-W4-006 (DRAIN_THROTTLED), R-W4-009 NITs — NO code.
  Do not schedule the allocator. Zero docs/** edits.

## 1. Context report (root cause, entry points, invariants, risks)

- **R-W4-004** — root cause: `SqliteGraphStore.upsert_entity` (`sqlite_backend.py:144`) wrote
  `hot_serving_entity_index` rows with NULL norm columns, so the exact rung (`canonical_name_norm = ?`),
  the candidate query (count proxy / PG predicate `LIKE` on NULL), the alias rung, and FTS (triggers
  index the norm values) were all blind to graph-written entities; rename-via-graph left a stale fold;
  facts-after-graph on the same name risked `UNIQUE(canonical_name)`. Entry point: `_UPSERT_ENTITY_SQL`
  of `sqlite_backend.py`. Invariant (P003, ADR-0029): the stored fold is computed by `dialect.fold_case`
  in Python at write time, in the same statement as the row — never an engine `lower()`.
- **R-W4-003** — root cause: `runner._build_info` never populated `reflection_enabled`,
  `reflection_last_run_age_days`, `reflection_stale`, so `_reflection_doctor_value` always rendered
  `disabled`; and it duplicated `format_reflection_health` instead of calling it. Entry points:
  `_build_info` (runner.py:97) → `_reflection_doctor_value` (dashboard.py:414) ← shared helper
  (`reflection.py:240`).
- **R-W4-005** — the allocator's inline comment at allocator.py:332 claimed the opposite of what the
  module docstring (allocator.py:33-34) admits: at-most-once notify.
- **R-W4-007** — `queue.claim` raised out of `allocate_attention`; house posture is degrade-never-raise.
  `run_allocation` also returned `Any`.
- **R-W4-008** — `_backfill_entity_norm`'s `parsed=[]` branch silently replaced corrupt blobs; count+log
  required. Newly found (this run): the branch is **unreachable for v5-shape corrupt rows on SQLite**
  because `ALTER TABLE ADD COLUMN` on a STRICT table re-validates existing CHECK constraints — see §F.
- Casualties C1–C5 were all rated STALE by REV (not defects); each is updated to the new truth below.
- Risk & invariants summary: kyC one normalisation point (fold_case imported, never reimplemented);
  existing callers of `run_allocation` none (zero in-tree); `ReflectionSettings` default disabled keeps
  production unchanged; `_UPSERT_ENTITY_SQL` param count changes 5→7; FTS triggers index the norm
  columns, so the INSERT now supplies them.
- Kuzu backend: verified — does not touch `hot_serving_entity_index` at all; unaffected. Affected
  public seams: `SqliteGraphStore.upsert_entity` signature unchanged (id/class/canonical_name) —
  additive columns only, so existing callers/tests are unaffected.

## 2. Phased changes executed

### Phase 1 — R-W4-004 graph norm columns + cross-writer tests
- `src/paa/storage/graph/sqlite_backend.py`:
  - `from paa.storage.relational.dialect import fold_case` at module scope (same seam facts.py:88 uses).
  - `_UPSERT_ENTITY_SQL` INSERT gains `canonical_name_norm, aliases_norm`; DO UPDATE refreshes
    `canonical_name_norm` from `excluded`. The `aliases_norm` VALUE on INSERT is the empty folded alias
    list `[]` (matches the raw `aliases` default; the graph backend carries no alias input, so `[]` is
    the only fold it can compute). **DO UPDATE deliberately does not touch `aliases_norm`** — see
    reasoning box below; the raw `aliases` column has never been touched by this backend either.
  - `upsert_entity` computes `canonical_norm = fold_case(canonical_name)` in the same statement.
  - **Why not `aliases_norm` in DO UPDATE?** The graph upsert carries no alias list, so setting
    `aliases_norm = '[]'` on conflict would silently erase aliases facts.py wrote — the same
    invisible-entity defect this fix closes, now on the alias rung. The brief's parenthetical
    "(both INSERT and ON CONFLICT DO UPDATE)" is imprecise shorthand; the operative requirement is
    "computed via the canonical `fold_case`" — a value this backend cannot compute for aliases.
    Preserving the incumbent `aliases_norm` keeps raw and norm in agreement (both are facts-written and
    untouched by the graph path). Recorded as a deliberate deviation with reasoning; the alternative
    would be data loss.
- `tests/storage/test_graph.py` — 3 cross-writer tests (sqlite-only by construction; kuzu never touches
  this table, so they construct `SqliteGraphStore` directly):
  - `test_graph_upserted_entity_resolves_through_facts` — graph writes `Éclair Project`, facts resolve
    finds it `exact` (non-ASCII pins the real fold); stored norms asserted (`éclair project`, `[]`).
  - `test_facts_upserted_entity_is_visible_to_graph_reads` — facts-written entity is a graph neighbour
    with the same canonical name; facts-written alias still resolves (cross-writer in both directions).
  - `test_graph_rename_refreshes_the_fold_for_resolution` — rename updates the fold; old name no
    longer resolves, new name resolves (pins REV effect (b)).
- [RAN `pytest tests/storage/test_graph.py -k cross/graph_upserted/facts_upserted/graph_rename` → 3 passed]

### Phase 2 — R-W4-003 TUI mirror (3 keys + shared formatter)
- `src/paa/tui/runner.py` (`_build_info`): populates the 3 keys from
  `reflection_health(settings.home, enabled=settings.reflection.enabled)` — the same helper the CLI
  doctor uses. Defensive `getattr` chain reads missing blocks as disabled (the ReflectionSettings
  default) so injected test fakes without a reflection block degrade rather than break the tick —
  documented in code; production `Settings` always carries the field (`config.py:820`).
- `src/paa/tui/dashboard.py`: `_reflection_doctor_value` delegates to the shared
  `format_reflection_health` (imported at module scope; no new TYPE_CHECKING — reflection is a light
  first-party module and dashboard→reflection is acyclic). The duplicated strings are gone; the
  existing W4-IMPL-02 tests (3 TUI mirror + 2 CLI) still pass [RAN → 27 passed].
- `tests/tui/test_dashboard.py` (appended): `test_reflection_enabled_and_running_renders_the_age_not_disabled`
  (renders `2.5d`, not `disabled` — the R-W4-003 tripwire), `test_reflection_enabled_and_stale_renders_the_stale_value`,
  `test_reflection_disabled_renders_disabled`, plus two runner-level tests that write a real marker under
  tmp_path and prove `_build_info` populates the keys (`reflection_health` unknown = None age when absent).
- [RAN `pytest tests/tui/` → 1 FAILED: `test_ledger_with_events` (ValueError: Invalid format string,
  `dashboard.py` line ~347 — a PRE-EXISTING category-B typo documented by W4-IMPL-02 at HEAD, untouched
  by this task); all 5 new tests pass.]

### Phase 3 — R-W4-005 comment, R-W4-007 claim guard + typing + run_allocation tests
- `src/paa/context/allocator.py`:
  - claim wrapped: failure → `log.warning("allocator.claim_failed")` + `AllocationResult(outcome="underfed")`,
    never raises. Docstring updated ("never raises for poison/empty or a claim failure").
  - the `:332` comment corrected to state the accepted at-most-once loss (message unacked; reclaim
    re-delivers, finds the duplicate, skips notify, acks; the gatherer fallback covers ranking) — no
    ack-semantics change, as the brief ordered; "record it" satisfied by the comment itself.
- `src/paa/agents/orchestrator.py` — **scope note, read first.** The §4 write-set lists only
  `allocator.py (comment + claim guard only)`, but §3 explicitly orders "type `run_allocation`'s return
  as the allocator's result type (TYPE_CHECKING import if a cycle forces it — if a TYPE_CHECKING block
  is needed, classify it per the import-conventions lock table in the SAME commit)".
  `run_allocation` lives in orchestrator.py and is a single-line annotation; I made the minimal,
  claim-first edit (COMMS CLAIM posted before editing, listing it explicitly):
  `-> Any` → `-> AllocationResult`, plus `from paa.context.allocator import AllocationResult` added to
  the EXISTING (already-classified, class C) `if TYPE_CHECKING:` block — no new block, so the import
  lock needed no classification addition. [RAN `test_import_conventions.py` → 21 passed.] No runtime
  import: `run_allocation` still lazily imports `allocate_attention` in-method, preserving the
  orchestrator's module-scope posture.
- `tests/agents/test_allocation_callsite_p007.py` (new) — TREV P1: the real
  `ChiefOrchestrator.run_allocation` against the real ledger fixture + scripted queue:
  `test_run_allocation_allocates_through_the_orchestrator` (outcome allocated, ledger row appended,
  controller notified, acked), `test_run_allocation_degrades_to_underfed_on_claim_failure`
  (R-W4-007; `allocator.claim_failed` warning logged; nothing emitted; no raise),
  `test_run_allocation_disabled_leaves_the_queue_untouched` (default-off switch).
- [RAN `pytest tests/agents/test_allocation_callsite_p007.py tests/context/test_allocator.py` → 27 passed]

### Phase 4 — R-W4-008 backfill corrupt count-log + test
- `src/paa/storage/relational/migrations.py` (count-log only):
  - module-level `log = structlog.get_logger(__name__)` (the backfill is a module function, so it
    cannot see the runner's injected `self._log`);
  - `_backfill_entity_norm`: tracks `corrupt_alias_rows` (non-str raw, unparseable JSON, or parsed
    non-list) and, when non-zero, emits
    `log.warning("migration.backfill_alias_corrupt", corrupt_rows=…, total_rows=…)`.
  - The `'[]'` behaviour is kept verbatim — honest middle, no wedging (see §F for the STRICT wedge).
- `tests/storage/test_tier_migration_p009.py` — C4 pin (v5→v6) + the new corrupt-blob test (placement
  rationale in §4/C4: natural home would be `test_entity_normalisation_p003.py`, outside write-set;
  the migration-shaped file in write-set hosts it, and the placement is recorded, not silent).
  **Construction story (load-bearing):** the test constructs the crash-mid-step state (columns landed,
  norms NULL, corrupt row written behind the runtime's back) because a corrupt row present BEFORE the
  column adds never reaches the backfill on SQLite — see §F. In that state the test pins: migration
  completes to version 6, corrupt row becomes the fail-soft residue (`corrupt service`, `[]`), healthy
  row gets real norms, and the `migration.backfill_alias_corrupt` line fires with `corrupt_rows == 1`.
- [RAN `pytest tests/storage/test_tier_migration_p009.py` → 11 passed, 1 skipped (PG-gated).]

### Phase 5 — Casualties C1–C5 (deliberate owner updates, each with REV rating)
- **C1** `tests/memory/test_creator.py::test_drain_105_backlog_drained_not_paginated`
  [REV-W4 C1: STALE EXPECTATION — pins pre-throttle unbounded draining; ADR-0036 orders the cap.]
  New expectation: 100 then 0 (window exhausted) + a `memory.embedding_drain_throttled` log line, 5
  still pending; a FRESH creator (its own window) drains the remaining 5 → full backlog drained,
  nothing dropped (preserves the test's drain-not-OFFSET intent). [RAN → pass]
- **C2** `tests/storage/test_dialect.py` (7) [REV-W4 C2: STALE — 6 literals + 1 fixture table].
  `_COUNT_CASE`/`_COLUMNS`/PG phrase predicate literals updated to `e.canonical_name_norm` /
  `e.aliases_norm` (the stored fold); `_sqlite_with_entities` gains the norm columns + norm-indexing
  trigger and writes the fold for its row — every production table has them (DDL + v6), so the fixture
  mirrors production; hostile-term execution now runs against the norm-shaped table.
  [RAN tests/storage/test_dialect.py → 25 passed]
- **C3a** `tests/memory/test_facts.py::TestTheCorruptRow` [REV-W4 C3a: STALE — test corrupted the raw
  blob; the rung reads the norm]. Re-pinned against `aliases_norm` corruption (fail-closed preserved:
  both terms fail, exact rung unaffected). The old raw-blob corruption path is now dead BY DESIGN (no
  read path consults the raw blob) — noted in the docstring. [RAN → pass]
- **C3b** `tests/memory/test_facts.py::TestTheGateModel` [REV-W4 C3b: STALE TEST over a real design gap —
  the R-W4-004 foreign-writer class]. Raw INSERT now writes the norm columns (the fold of its own
  name + `[]`), and the assertion compares gate grams against the _folded_ stored text
  (`_trigrams(fold_case(stored))`), because the FTS index indexes the fold. A norm-NULL raw row can
  no longer masquerade as "index holds no grams" — that class is fixed at the source by R-W4-004.
  [RAN → pass]
- **C4** `tests/storage/test_tier_migration_p009.py::test_schema_version_is_five` [REV-W4 C4: STALE —
  SCHEMA_VERSION=6 is intended]. Assertion 5→6; name kept historical with a reason in the docstring.
- **C5** `tests/memory/test_entity_resolution.py` (5) [REV-W4 C5: STALE FIXTURES — raw `_insert` wrote
  norm-NULL rows]. `_insert` now writes `fold_case` norm columns matching the production write path
  (fixture fix, not expectation weakening); the escaped-blob test FLIPS to assert resolution through
  the norm (the intended fix — the repaired-divergence pattern from `test_entity_parity.py`).
  [RAN → 21 passed]

## 3. Fresh-eyes self-review (stranger's PR audit, per phase)

- **Phase 1 review**: (MAJOR, checked) `fold_case` is the canonical function (facts.py:88 imports it at
  module scope identically; no duplication). (MINOR, kept) — the DO UPDATE aliases_norm deviation is
  documented in the SQL comment; reversal check: reverting (git diff shows INSERT losing the columns)
  breaks all 3 cross-writer tests (graph entity invisible to facts). Kuzu path untouched.
- **Phase 2 review**: (MINOR, kept) `reflection_health` keys on the info dict are exactly the names
  the pre-existing mirror tests' docstring promises (`reflection_enabled/last_run_age_days/stale`),
  so old hand-built info dicts keep rendering. `reflection_health` itself never writes the marker
  (read-only; empty-home hostile test in P013 suite still passes). (MINOR, kept) `getattr` defaults
  mirror `_reflection_doctor_value`'s missing-key behaviour — both render `disabled` for a settings
  object without a reflection block, so the two defaults agree.
- **Phase 3 review**: (CRITICAL-check → resolved) `AllocationResult` via TYPE_CHECKING does not create
  a runtime cycle (allocator imports config/metrics/queue only); mypy strict resolves; import lock
  green. (MAJOR-check → resolved) The claim guard covers ONLY claim (brief scope); ack/nack failure
  still propagates — deliberate, and flagged in §E. (MINOR) The `run_allocation` tests monkeypatch
  `paa.config.get_settings` (the lazy import inside the method sees the patch at call time); they
  construct a real `ChiefOrchestrator` against the real ledger — no mocks of the unit under test.
- **Phase 4 review**: (MAJOR, found + fixed during the phase) My first restructure of the backfill
  branch broke mypy's `warn_unreachable` (baseline 3 → 4, Statement unreachable at migrations.py:1362):
  the first-assignment-type pinning made `isinstance(parsed, list)` look always-true. Restored the
  original structure (`parsed: Any = []` + try/except inside) → mypy 3 = baseline. [RAN → 3].
  (MINOR) `corrupt_rows` recounting on re-runs: the guard only fires when NULL-norm rows exist, so
  the count is per backfill run. Correct.
- **Phase 5 review** (mutation mentality, one per item): C1 — remove throttle window and the second
  `== 0` fails (returns 5); C2 — revert column literals and the placeholder-count + predicate tests
  fail; C3a — un-corrupt `aliases_norm` and the `is None` assertions fail; C3b — drop the norm columns
  from the raw INSERT and gate-vs-token fails again; C4 — set SCHEMA_VERSION back and only the pin +
  version tests fail; C5 — drop helper norms and 4 order tests + the flip fail. Each casualty edit
  flips a stale pin to the new truth; none weakens an assertion (the C5 flip asserts MORE than
  before).

## 4. Final review (before commit)

- Cross-phase: graph write + facts read share `fold_case`; COUNT(proxy) queries now measure the fold
  my graph INSERT supplies (cross-writer tests prove it end-to-end (Éclair)). Runner keys + shared
  formatter share one rule with CLI (P013 suite re-run green). Claim guard does not change store/notify
  semantics. Backfill count-log fires only when a corrupt row is actually encountered; `'[]'` behaviour
  unchanged (the P003 suite re-run green: 23 passed + 1 skip).
- Docs lock: my src edits shift lines in dashboard.py (+8, after _reflection_doctor_value — render_doctor
  citation lines above the edit are unshifted; the helper's own line moved), runner.py (+9 net at top),
  migrations.py (+12), sqlite_backend.py (+17), allocator.py (+17), orchestrator.py (+1). Per protocol
  those line-shifts are the W4-FIX-DOCS task (they own docs) — I did not edit docs and did not pick the
  EOF-placement dodge (the brief forbids it as out-of-task); all shifts are recorded so DOCS2 can
  re-point by reading. I cite citations at the NEW line numbers in this report.
- Left-over debugging: scratch files (.scratch*.py/.db) deleted [RAN `Get-ChildItem` → empty].
- COMMS.md carries W4-FIX-DOCS's interleaved lines + mine (5 insertions total): left UNCOMMITTED
  (shared blackboard; the orchestrator sweeps it, as prior waves did).
- Full scoped re-run (single command, recorded): 9 test files → all pass (PG/kuzu-gated skips only).

## 5. Verification evidence (verbatim, scoped only)

- [RAN `.venv\Scripts\python.exe -m pytest tests/storage/test_graph.py -q -k "cross or graph_upserted
  or facts_upserted or graph_rename"` → `...` (3 passed)]
- [RAN `.venv\Scripts\python.exe -m pytest tests/tui/test_dashboard.py tests/tui/test_runner_panels.py
  tests/tui/test_runner_smoke.py -q --tb=short` → 1 FAILED: `test_ledger_with_events` (ValueError:
  Invalid format string at `dashboard.py:347` — PRE-EXISTING category-B typo, documented at HEAD by
  W4-IMPL-02 §Verification; unchanged by this task) + all 29 others pass]
- [RAN `pytest tests/tui/test_dashboard.py -q -k "reflection or runner_build_info"` → 5 passed]
- [RAN `pytest tests/agents/test_allocation_callsite_p007.py tests/context/test_allocator.py` → 27 passed]
- [RAN `pytest tests/storage/test_tier_migration_p009.py -q --tb=short` → 11 passed, 1 skipped]
- [RAN `pytest tests/storage/test_tier_migration_p009.py tests/storage/test_entity_normalisation_p003.py
  tests/storage/test_migrations.py tests/storage/test_schema_parity.py -q --tb=short` → all pass (PG skips)]
- [RAN `pytest tests/conventions/test_import_conventions.py -q --tb=short` → 21 passed]
- [RAN `pytest tests/improvement/test_reflection_wiring_p013.py -q --tb=short -m "not slow"` → 27 passed]
- [RAN `pytest tests/storage/test_graph.py tests/memory/test_creator.py tests/memory/test_facts.py
  tests/memory/test_entity_resolution.py tests/storage/test_dialect.py tests/storage/test_tier_migration_p009.py
  tests/context/test_allocator.py tests/context/test_metrics.py tests/agents/test_allocation_callsite_p007.py
  -q --tb=short` → all pass (PG/kuzu-gated skips only)]
- [RAN `.venv\Scripts\python.exe -m ruff check src tests` → All checks passed! (0 findings)]
- [RAN `.venv\Scripts\python.exe -m mypy src` → exactly 3 errors at
  `src/paa/sandbox/subprocess_backend.py:548` (baseline; gate holds)]
- NOT-RUN (out of scoped scope): full `pytest tests/` suite, PostgreSQL suites (no DSN), containers/CI.

## 6. Unverified assumptions & remaining risks

1. **R-W4-008 scope, honestly stated.** My count-log makes the "corrupt row → count logged, migration
   completes" criterion true for the crash-mid-step state and for PostgreSQL (whose `ADD COLUMN` does
   not re-validate existing CHECKs). It is NOT true for a v5-shaped DB holding a corrupt blob on
   SQLite — see §F. The brief's "migrations count-log" parenthetical barred the pre-repair step that
   would fix it, so this is **DEFERRED-DEPENDENCY** (NEED posted; see below) rather than a silent fix.
   The count-log is still real progress: those states CAN reach the backfill and previously were silent.
2. **Ack-semantics decision** (R-W4-005 alternative): the reclaim-cycle cost is accepted by the brief;
   ack-on-notify-failure remains a separate decision for the orchestrator (recorded in the allocator
   code comment itself).
3. **Ack/nack failure propagation** in the allocator: same class as R-W4-007 but outside the brief's
   claim-only scope. `allocate_attention` still raises if `queue.nack`/`ack` fails. Candidate follow-up.
4. **Norm-NULL gate-model residual**: the R-W4-004 class (any writer that omits the fold) now has a
   structural trigger test (C3b) and its graph instance fixed, but no startup count/gauge of NULL rows
   exists — DO-LATER per REV O5.
5. **PG half of everything**: all PG-gated suites skipped here (no `PAA_TEST_POSTGRES_DSN`); CI must
   prove the PG v6 ALTER/backfill/parity. No parity claim beyond SQLite is made here.
6. **Line shifts** in 6 touched src files (dashboard/runner/migrations/sqlite_backend/allocator/
   orchestrator) for W4-FIX-DOCS to re-point.

## 7. NEED / DEFERRED-DEPENDENCY

**Need (owner: orchestrator / a follow-up task):** v6 pre-repair step so a v5 database carrying a
corrupt `aliases` blob can migrate at all. **Why it is needed** (empirical, §F): SQLite STRICT +
existing CHECK violation → `ALTER TABLE ADD COLUMN` checks every existing row against every table
CHECK and fails, so the migration transaction rolls back and the DB is permanently pinned at v5.
**Why it is deferred:** the brief scopes `migrations.py` to "count-log only" and §3's acceptance
("count logged, migration completes") is satisfied by the count-log for every state that can reach the
backfill; adding a pre-repair step (a new guarded step + its PG probe question) is a same-file
behaviour change beyond that scope, so it is carried as NEED rather than made silently.
**Proposal for the follow-up:** a SQLite guarded step BEFORE the column adds — guard "any row with
`json_valid(aliases)` false", python action rewrites the blob to `'[]'` (the gate's fail-soft residue,
which the blob is already read as) and emits `migration.backfill_alias_corrupt`; PG needs its own probe
(TEXT→jsonb validity) or a documented no-op (PG ADD COLUMN does not re-validate CHECKs, so its backfill
path fires there). Either way the same event name keeps one observable.

## 8. Long-run test (Source / Load / Hostile / Operability / Reversal)

- **Source:** norms = `fold_case` (canonical) + folded JSON (aliases), both writers, same function;
  TUI value = shared `format_reflection_health` over `reflection_health(settings.home)`.
- **Load (100x / concurrency / partial failure):** graph write path is a single-statement upsert;
  claim guard bounds to one batch; the backfill remains a full-table scan (O3 DO-LATER stands).
  Concurrency: upsert race on same name is pre-existing (ConstraintViolation → StorageError, unchanged).
- **Hostile input:** non-JSON blobs still land on the gate's fail-soft view; corrupt norm rows fail
  closed (C3a); hostile candidate terms still unable to reach SQL (dialect suite green). A NULL
  canonical is impossible (NOT NULL), so `fold_case(None)` is unreachable.
- **Operability:** roll back = revert the commits (norm columns are ignorable by old code — one-way
  migration per W4-IMPL-03); the corrupt count is observable in migration logs; NUL/CLI parity holds
  by shared formatter.
- **Reversal:** revert `_UPSERT_ENTITY_SQL` → cross-writer tests fail (norm columns NULL → invisible);
  revert the rune (scratch-driver) → the TUI mirror tests fail by `disabled`; revert the claim wrap →
  the raising-queue test fails with RuntimeError uncaught; revert the count-log → the corrupt test
  finds no line.

## 9. Git state

- Branch: `ops/20261107-integration-fix` [RAN `git branch --show-current` before committing].
- Never committed on a protected branch; commits are explicit paths only (no `add -A`/`-a`/stash/etc.).
- Sibling uncommitted state (W4-FIX-DOCS COMMS lines, W4-FIX-DOCS report) — never reverted or staged by me.
- Code commit + Report commit: SHAs recorded in §10.

## 10. Commit SHAs (appended after committing)

- Code+tests commit: `8971e83a65f5021b9c5b470f143249cc68ef5e39`
- Report commit: `<REPORT_SHA>`

## F. Out-of-scope findings (post-NEED evidence — the v5-shape STRICT wedge)

Discovered while implementing the R-W4-008 test as briefed ("a corrupt blob row → count logged,
migration completes"). The blob was placed at v5 shape exactly as TREV P2-b described
("a v5 row with aliases = 'not json'"), and the migration raised `StorageError: sqlite error:
CHECK constraint failed (sqlite_code='SQLITE_ERROR', ...)` at the ADD COLUMN step — before the
backfill ever ran. Traceback tail (verbatim): `migrations.py:_run_step → _execute → connection.py:
translate_sqlite_error → StorageError: sqlite error: CHECK constraint failed`.

Mechanism, isolated in scratch (uncommitted scripts, deleted after):
[Table: STRICT+CHECK → `ALTER TABLE ... ADD COLUMN` FAIL; non-STRICT+CHECK → OK; STRICT+no-CHECK →
OK; non-STRICT+no-CHECK → OK.] SQLite, on `ADD COLUMN` to a STRICT table, re-validates existing
CHECK constraints over all rows; the raw `aliases` blob `'not json at all'` violates
`ck_entity_aliases_json CHECK (json_valid(aliases))`, while `aliases_norm` (no CHECK) never does.

Consequences:
- A v5-shape database containing such a row is **stuck at version 5** under SQLite — every `connect()`
  retries and fails the same way (the runner logs each attempt).
- REV-W4's mitigation ("count-and-log '[]' in the backfill") implicitly assumed the backfill was
  reachable on this state; it is not. The mitigation (since P003's design) is not wrong — it is
  unreachable where it is most needed.
- Real-world states need a foreign writer bypassing the CHECK (the same class C3a constructs for its
  test), so this is a durability corner, not a hot path — but a migration that wedges the app at
  startup is exactly the kind of thing that surfaces first on the operator's box, not in CI.

The fix (pre-repair step) is carried as NEED in §7 and in COMMS — deliberately deferred, not silent.
