<!-- ORCH-DIGEST:START -->
agent: W3-IMPL-03 | domain: P-006 (T-006) 5 TUI panels + additive /health | status: DONE
items: 5/5 Fix A fields done (snapshot+dashboard+runner+API+counts) + 10 new tests green; 1 known pre-existing strftime failure quoted
proposed_tasks: 1 (T-006-P006)
top_risks: (1) attention_top stays NULL until P-07 W4 (done — always None); (2) per-panel isolation vs 2s tick loop (done — build_panel_fields + 2 tests); (3) pre-existing strftime failure does not mask new failures (proved by counts 39+43=82); (4) single-builder-source drift between TUI and API (done — both call build_panel_fields)
cross_domain_conflicts: none (disjoint from W3-IMPL-01/02/04; api/app.py + facts.py touch sites claimed 2026-11-07T10:35:27Z; siblings committed cda40f5/ dd7c3d6 / d06011a without touching write-set)
out_of_plan_discoveries: FINDING _gather_rows/_build_info pass CorrelationHead where ids expected (pre-existing; new builders use _correlation_id_of correctly) — reported, not fixed, see §4
confidence: high — 10/10 new tests pass, tui+api 81/82 pass (only known category-B strftime), ruff 0 scoped, mypy scoped 0 / total stays 3 pre-existing
last_comms_line_read: 2026-11-07T11:05:00Z W3-IMPL-02 -> ORCH | DONE | T-010-P010 [READ docs/agent-ops/20261107-integration-fix/COMMS.md:92]
<!-- ORCH-DIGEST:END -->

# Implementation report — W3-IMPL-03 — P-006 5 TUI panels + additive /health (T-006-P006)

## 0. Scope and method

Task T-006-P006 (plan P06 / test T07): implement Fix A exactly — 5 named nullable `Snapshot` fields
(`attention_top`, `memory_browser_counts`, `decision_tracker`, `autonomy_state`, `agent_monitor`),
populate what exists from `runner._gather_rows`/`_build_info` sources, `attention_top=None` always,
render each as own sub-panel with `null -> "No data yet"`, mirror same 5 fields additively on
`GET /health`, ONE new read-only aggregate query in `FactRepository`, per-panel isolation.

WRITE-SET (only these, per brief §4 + ownership/W3.tsv): `src/paa/tui/snapshot.py`,
`src/paa/tui/dashboard.py`, `src/paa/tui/runner.py`, `src/paa/api/app.py`, `src/paa/api/schemas.py`,
`src/paa/memory/facts.py` (counts query ONLY), `tests/tui/test_dashboard.py` (append),
`tests/api/test_health_p006.py` (new), `tests/memory/test_counts_p006.py` (new).
FORBIDDEN: `dispatcher.py`, `config.py`, `core/types.py`, `ledger/*`, `context/*`, dashboard.py:160
strftime fix. Read-only otherwise. Branch `ops/20261107-integration-fix` verified at start [RAN].

- Required reading done in order: STANDING-ORDERS.md, AGENTS.md, 00-mission.md, plan rev5
  (§E P06, §H T07, §J, §K/D11, pre-mortem 8), W0-INV-04 §P-006 (Fix A authoritative),
  W1-IMPL-02 (redaction closed + `markup=False` precedent), report template, code at HEAD.
- Code verified at HEAD (need to fill line evidence as work proceeds; placeholders to be updated
  with exact line numbers after edits).
- Code commit first, then this report in separate docs-only commit listing code SHA. Trailers
  `Task: T-006-P006`, `Agent: W3-IMPL-03`, `Wave: W3`, `Report: <this path>`.

### Context summary (root cause / entry points)

Doc A panels were never wired: `Snapshot` at HEAD had exactly 2 fields (`rows`,
`runtime_info` [VERIFIED `git show HEAD:src/paa/tui/snapshot.py` — no panel
fields]); `render_dashboard` rendered one task table plus a `no active tasks`
empty branch [VERIFIED `src/paa/tui/dashboard.py:83-118` at HEAD]; no dispatcher
panel commands exist (D11 — panels belong in the render path, not behind new
`CommandKind`s [VERIFIED W0-INV-04 §P-006]). Three panels lack live sources
(`hot_serving_agent_registry` / `hot_serving_evaluation_results` are schema-only,
never written) — the honest `null -> "No data yet"` IS the T07 criterion.
Chosen approach is INV-04 Fix A exactly; no architectural deviation, no new
frontend, no new auth surface.
W1 pre-mortem 8 check [VERIFIED]: drain/redaction path redacts
(`src/paa/memory/creator.py:910` `redact(str(exc))`, `:1550` `error=redact(str(exc))`,
`src/paa/memory/touch.py:46` `redact(str(exc))`); panels render no exception
strings at all (only data fields), and failure logs follow the existing
`_gather_rows` `error=str(exc)` precedent — no new unredacted secret surface.

### Invariants preserved

- `Snapshot(rows=...)` constructors stay green: 5 new fields default `None`
  (types left open `Any` so P-07 can fill `attention_top` without a signature
  change).
- API additive only: `HealthResponse` old keys (`status`, `version`, `backends`,
  `open_tasks`) untouched; 5 new keys default `None`.
- Per-panel isolation: a raising source empties only its field, never the 2s
  tick loop or `/health` (no 500).
- `attention_top` always `None` this wave (P-07 W4 allocator not landed).
- No `dispatcher.py` / `config.py` / `core/types.py` / `ledger/*` / `context/*`
  changes; no dashboard strftime fix.
- Trace tag `PLAN:integration-fix:P006` in snapshot.py and all new code.

## 1. Implementation

Code resumed after server restart: prior instance's in-tree edits (uncommitted)
were verified file-by-file against the brief and committed as-is (no gaps found
needing code changes; all scoped checks already green). See code commit
`dd1df98f68c66aff6b01f88608d8a7d986398ff4` (9 files, +700/-6).

### Phase 1 — Snapshot 5 nullable fields (`src/paa/tui/snapshot.py`)

[VERIFIED `src/paa/tui/snapshot.py:7-9,50-61`] Five fields
`attention_top`, `memory_browser_counts`, `decision_tracker`, `autonomy_state`,
`agent_monitor`, all `Any = None`, with `PLAN:integration-fix:P006` tag.
Existing `rows=`-only constructors unchanged; no `CommandKind` added.

### Phase 2 — Shared builder + per-panel isolation (`src/paa/tui/runner.py`)

[VERIFIED `src/paa/tui/runner.py:132-271`] New public `build_panel_fields(settings, rt)`
— one builder, two surfaces (same one-source pattern `sandbox_report_rows`
uses for the doctor panel). `attention_top` hard `None` (P-07 W4). Other four
each under own try/except (logged via `log.warning("tui.panel_failed")`, never
silent, never killing the tick loop):
- `memory_browser_counts` → `_memory_browser_counts` [VERIFIED `:143-154`]:
  one bounded aggregate query via `FactRepository.count_facts_by_domain()`.
- `decision_tracker` → `_decision_tracker` [VERIFIED `:157-185`]: raw
  `policy_decision` + `policy_reason` from `project_lineage` projections
  (quality/outcome scores do not exist and are never synthesised); per-lineage
  failures tolerated, mirroring `_gather_rows`.
- `autonomy_state` → `_autonomy_state` [VERIFIED `:188-206`]:
  `settings.policy.mode.value` + count/list of `TaskPhase.AWAITING_HUMAN` gates.
- `agent_monitor` → `_agent_monitor` [VERIFIED `:209-230`]: `sorted(rt.agents)`
  + last event `agent_role` per lineage (coarse only, no new state).
`_correlation_id_of` [VERIFIED `:132-140`] extracts `.correlation_id` from
`CorrelationHead` (or passes through plain ids from test fakes).
`_PANEL_SCAN_LIMIT = 50` [VERIFIED `:56-61`] bounds per-build scans; the counts
panel is a single `COUNT` query and is not subject to the limit.
`run_tui` fills `Snapshot` from the builder [VERIFIED `:419-432`].

### Phase 3 — Counts query (`src/paa/memory/facts.py` ONLY)

[VERIFIED `src/paa/memory/facts.py:1095-1130`] `count_facts_by_domain(*,
include_superseded=False)` — one `GROUP BY memory_domain, embedding_status`
query; per-domain/per-status rollups aggregated in Python. Empty table returns
`{"total": 0, "by_domain": {}, "by_status": {}}` (zeros, not crash/missing key).
Superseded excluded by default, mirroring `count_facts`. No schema change, no
`LIKE` (helper not needed), no new state.

### Phase 4 — Dashboard render (`src/paa/tui/dashboard.py`)

[VERIFIED `src/paa/tui/dashboard.py:83-137`] All 5 panels always render (even
with no open tasks), one sub-panel per field. `_is_empty_panel` (None or empty
container) → `_panel_no_data` renders `Text("No data yet")` — the T07 criterion.
Non-null renders ranked rows: attention = list order (P-07 ranking; dict
`{id: score}` ranked by score descending) with rank column; memory = `total`
plus domain/status count tables (deterministic order: count desc, name asc);
decisions = id/decision/reason rows; autonomy = `mode` + `awaiting gates` +
short ids; agents = `agents: ...` + `recent roles: ...` (empty agents renders
`none`). `goal_preview` changed from `str` cell to `Text(...)` so hostile
markup is literal; every panel cell uses `_cell_text` → `Text` (never parses
markup). Panel titles are fixed field names, never data. DECISION: `Text` at
render time (W1 `markup=False` precedent) over a new sanitiser — reuse the
existing pattern; ANSI escapes pass through as documented (see hostile test).
INVARIANT: malformed panel shapes (mixed list, scalar, plain mapping) render
without crashing the loop — every branch has a fallback `Text(str(value))`.

### Phase 5 — API mirror (`src/paa/api/app.py` + `src/paa/api/schemas.py`)

[VERIFIED `src/paa/api/app.py:73-104`, `src/paa/api/schemas.py:140-147`]
`HealthResponse` gains the same 5 fields (all `Any | None = None`). `/health`
calls the same `build_panel_fields(runtime.settings, runtime)` (imported
function-locally so the API module's import time does not pull the TUI) and
returns the dict additively. Old keys untouched. No new endpoint, no new auth.

### Phase 6 — Tests (append-only + 2 new files)

[VERIFIED] `tests/tui/test_dashboard.py` (append only — existing tests
untouched): 5 P006 tests (all-null → 5× `No data yet`; populated → ranked
rows + numeric counts + decision/autonomy/agent rows; 2-null partial render;
hostile markup/ANSI literal; builder isolation with exploding DB).
`tests/api/test_health_p006.py` (new): additive lock (old keys `status`,
`version`, `backends.queue`, `open_tasks` + 5 new keys; `attention_top is None`;
autonomy/agents/counts populated) and isolation (counts raising → 200 with
`memory_browser_counts is None`, others render).
`tests/memory/test_counts_p006.py` (new): empty table → zeros; per-domain/status
counts (3 facts, 2 semantic + 1 episodic, 2 pending + 1 indexed); superseded
excluded by default / included with flag.

## 2. Verification (gate-report shape)

- New tests [RAN `uv run python -m pytest <10 new tests> -v` → `10 passed, 1 warning in 0.96s`]
  (5 dashboard + 2 health + 3 counts). The 1 warning is FastAPI/Starlette
  `httpx` deprecation from TestClient — not a failure.
- TUI suite [RAN `uv run python -m pytest tests/tui -q` → `38 passed, 1 failed`;
  dots `......F................................` = 39 collected]:
  `FAILED tests/tui/test_dashboard.py::test_ledger_with_events - ValueError: Invalid format string`
  at `src/paa/tui/dashboard.py:346` (`"%H:%:%S"` strftime typo in `render_ledger`).
  This is the KNOWN baseline category-B failure (00-mission.md:33: dashboard.py:160
  typo + `:59` typo). NOT fixed per brief ("do not fix dashboard.py:160 and do
  not hide it"); proves by counts it is the ONLY failure.
- API suite [RAN `uv run python -m pytest tests/api -q` → 43 dots, no FAILED line;
  collect-only = 43 (api 13+5+2+23)]. The shell timed out waiting for process exit
  after 100% (TestClient/daemon threads linger — harness hang, not a test failure);
  a narrower 3-file run returned normally. Counts quoted from `--collect-only`.
- Combined bag [RAN `uv run python -m pytest tests/tui tests/api -q` → 82 dots,
  `FAILED` exactly once (the same known `test_ledger_with_events`)].
- Scoped regression: [RAN `tests/memory/test_counts_p006.py +
  tests/memory/test_facts.py` → all pass (27-ish dots); counts alone `3 passed`].
- Lint/type [RAN `uv run python -m ruff check <9 write-set paths>` → `All checks passed!`;
  `uv run python -m ruff check src/paa/tui src/paa/api src/paa/memory/facts.py ...`
  → `All checks passed!`; `uv run python -m mypy <6 src files>` →
  `Success: no issues found in 6 source files`; `uv run python -m mypy src` →
  exactly 3 pre-existing errors at `src/paa/sandbox/subprocess_backend.py:548`
  (`killpg`/`getpgid`/`SIGKILL` POSIX symbols on Windows host), matching
  00-mission.md:22 baseline — total stays 3, never grows].
- Fail-before proof [RAN `git show HEAD:src/paa/tui/snapshot.py | Select-String …`
  / facts.py / schemas.py` → no matches for `attention_top` or
  `count_facts_by_domain` at HEAD]: at HEAD the new tests error by construction
  (`Snapshot(attention_top=…)` → `TypeError: unexpected kwarg`; `.count_facts_by_domain()`
  → `AttributeError`; `/health` missing the 5 keys). No stash/checkout used
  (forbidden); symbolic proof + 10/10 pass after.
- Write-set discipline [RAN `git diff --name-only` before commit showed only
  write-set files among modified; `git branch --show-current` →
  `ops/20261107-integration-fix`]:
  no dispatcher/config/core/types/ledger/context changes; dashboard strftime
  untouched (still fails as quoted).

## 3. Long-run answers (source / load / hostile / operability / reversal)

- Source: one builder (`build_panel_fields`) feeds both surfaces (TUI `Snapshot`
  + `/health`), so TUI and API cannot disagree about the same fact — same
  pattern as `sandbox_report_rows`/`render_doctor`. Counts come from the
  single aggregate query; tracker/autonomy/monitor come from ledger projections
  + runtime fields. No mirrored computation.
- Load (N+1 check on counts): the counts panel is ONE `GROUP BY … COUNT(*)` query
  per build (never per-row), explicitly documented in the docstring; empty-table
  case is also one query. Panel scans are bounded (`_PANEL_SCAN_LIMIT = 50`),
  so per-tick work is O(50 lineages), not O(open tasks). Residual: `_agent_monitor`
  reads full `read_correlation` per lineage (then takes `[-1]`) — correct but
  heavier than `latest_event`; bounded by the same 50 cap; revisit if tick
  latency matters. The whole build runs every 2s tick; `attention_top` is
  constant `None` (no work).
- Hostile (ANSI): dashboard uses `Text(...)` cells everywhere, so `[red]…[/red]`
  in `goal_preview`/`policy_reason`/counts keys renders literally (pinned by
  `test_p006_hostile_strings_render_literally_without_markup_crash` asserting
  `"[red]hacked[/red]" in out`). Bare `[/red]` no longer risks `MarkupError`
  killing the loop. ANSI escape sequences pass through (documented passthrough,
  not stripped — W1 FINDING); the test asserts render succeeds and
  `"hacked"`/`"pwned"` survive. Counts query takes no user input (static SQL,
  no LIKE) so no injection surface.
- Operability (panel isolation): each panel builder is wrapped in its own
  try/except in `build_panel_fields` (logged, returns `None` for that field);
  `run_tui` therefore refreshes a failing panel as `No data yet` instead of
  killing the tick loop, and `/health` stays 200 with that key null (both
  pinned by tests: `test_p006_panel_builder_isolates_a_raising_source` and
  `test_health_stays_200_when_a_panel_source_fails`). Empty vs failure are
  distinguishable only in logs (`tui.panel_failed` with panel name + redacted-context
  error) — consider a health detail key later; out of scope.
  Rollback: additive fields only; reverting any single panel file degrades
  cleanly (defaults → `No data yet`). Migration: none (no schema change).
- Reversal (which test fails if the fix is reverted?): reverting `snapshot.py`
  fails 4 dashboard panel tests (`TypeError` on kwargs; `No data yet` count
  5→0); reverting `runner.py` fails the builder-isolation test + both API health
  tests (missing `build_panel_fields` / `ImportError`); reverting `facts.py`
  fails 3 counts tests (`AttributeError`) and the first health test (counts
  `None`→500 or missing); reverting `schemas.py` fails health (missing keys);
  reverting `dashboard.py` fails all 4 render tests. `attention_top=None` is
  asserted in health + builder tests — any fake score fails them.

## 4. Out-of-scope findings (reported, not fixed)

- FINDING-F1 (pre-existing, NOT fixed per write-set discipline): `_gather_rows`
  and `_build_info` pass `CorrelationHead` objects where correlation ids are
  expected. `_gather_rows` does `CorrelationId(cid)` where `cid` is a head
  [VERIFIED `src/paa/tui/runner.py:78` vs `store.py:72-96` `CorrelationHead`
  has no `__str__`/`__eq__` to uuid, `correlation_id` is a uuid slot]; with the
  real ledger the task table would be empty (the `except` swallows it) while
  the fake ledgers in tests return plain strings so tests pass.
  `_build_info` does `str(cid)` for `open_tasks` [VERIFIED `:115`] — same
  mismatch. New panel builders use `_correlation_id_of` correctly. Recommended
  follow-up: apply `_correlation_id_of` in `_gather_rows`/`_build_info` (one-line
  each) under a separate scoped task. Not fixed here to avoid scope drift.
- FINDING-F2 (pre-existing, brief explicitly forbids fixing): `render_ledger`
  strftime typo `"%H:%:%S"` [VERIFIED `dashboard.py:346` fails with `ValueError:
  Invalid format string`; 00-mission.md:33 cites `:160` + `:59`]. All 5 new
  dashboard tests are green alongside it; quoted as the sole failure in §2.
- No model involvement: panels read ledger projections + fact counts only;
  no model call, no new auth, no dispatcher change — D11 holds.

## 5. Commits and Git state

- Branch [RAN `git branch --show-current` → `ops/20261107-integration-fix`].
- Code+tests commit (explicit paths only, never `add -A`):
  `dd1df98f68c66aff6b01f88608d8a7d986398ff4` — `P-006: 5 named TUI panels +
  additive /health mirror (Fix A)`, 9 files, +700/-6, trailers
  `Task: T-006-P006`, `Agent: W3-IMPL-03`, `Wave: W3`,
  `Report: docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-03.md`.
- Report commit (docs-only, separate, lists code SHA):
  this commit — `Report W3-IMPL-03 (T-006-P006): P-006 panels DONE; code commit dd1df98f68c66aff6b01f88608d8a7d986398ff4`,
  1 file.
- Working tree after code commit still held other agents' shared files
  (`01-work-ledger.md`, `COMMS.md` — ORCH-committed at wave boundaries) and
  generated `uv.lock`; none touched or committed by this agent. Forbidden
  commands never used (no stash/reset/checkout/rebase/push/`--no-verify`).

## 6. Unverified assumptions and remaining risks

- NOT-RUN (needs shared env, per brief): full `pytest tests/ -q`, containers,
  PostgreSQL/S3 backends. Postgres `GROUP BY` dialect for the counts query is
  assumed portable (static SQL, no engine idiom); SQLite-verified only.
  Server-variant counts parity is a CI concern.
- Fake-ledger optimism: `_gather_rows`/`_build_info` behaviour against the real
  `CorrelationHead` ledger is covered only by FINDING-F1 analysis, not by an
  integration test (no server bind per brief). Panel builders are head-safe by
  construction (`_correlation_id_of` + `as_uuid` uuid), but only fake-string
  paths are test-exercised.
- `attention_top` shape is deliberately `Any` — when P-07 lands, its ranking
  contract must be pinned by tests (current tests pin list-order ranking, not
  the allocator's score semantics).
- API-harness hang: `pytest tests/api -q` prints 100%/43-pass but the process
  lingers (shell timeout). Not a test regression; flagged so the orchestrator
  does not misread it as a hang failure. Needs shared-env investigation (likely
  TestClient/daemon thread teardown).
- `build_panel_fields` caller passes `rt.db` via `FactRepository(rt.db)` —
  assumes `Runtime.db` is the same backend used for facts (true today); a null
  `db` raises `RuntimeError` → isolated to `None`, not a crash.
