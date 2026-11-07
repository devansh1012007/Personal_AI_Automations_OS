<!-- ORCH-DIGEST:START -->
agent: W4-IMPL-02 | domain: P-013 weekly reflection wiring (daemon 4th loop + file last-run + doctor rows) | status: DONE
items: INV-05 Fix A complete (no EventType, no cron); T14 + boundary/hostile/disabled/concurrency/failure-retry + empty-ledger covered by 29 new tests
proposed_tasks: 0 (this is the implementation)
top_risks: crash between playbook write and marker can duplicate one rule on retry (fence preserves human text, run_weekly body outside write-set); TUI runner._build_info outside write-set (mirror renders disabled default — NEED for runner population); docs-lock 12 citations to my files need post-wave re-point
cross_domain_conflicts: cli/main.py doctor region claimed by function (P-001 done); daemon.py loop region sole-owner this wave
out_of_plan_discoveries: 1 (docs-lock shift churn incl. sibling files; crashed-loop process linger reproduced at HEAD f973755)
confidence: high (all scoped suites green; ruff 0; mypy total 3 pre-existing)
code_commit: 620ab30
last_comms_line_read: 2026-11-07T12:53:18Z W4-IMPL-02 -> ALL | FINDING | T-013-P013
<!-- ORCH-DIGEST:END -->

# Implementation report — W4-IMPL-02 (IMPL) — wave 4 · run 20261107-integration-fix

## 0. Scope and method
- Brief: docs/agent-ops/20261107-integration-fix/briefs/W4-IMPL-02.md (T-013-P013). WRITE-SET only:
  src/paa/daemon.py, src/paa/cli/main.py (doctor row only), src/paa/improvement/reflection.py
  (last-run helpers only), src/paa/tui/dashboard.py (doctor mirror row only),
  tests/improvement/test_reflection_wiring_p013.py (new). READ-ONLY: everything else.
  FORBIDDEN: config.py, core/types.py, ledger/*, memory/world_model.py, new EventTypes.
- Required reading done in brief order: STANDING-ORDERS.md; AGENTS.md; 00-mission.md; plan rev5
  §E P13 + §H T14 + §K; W0-INV-05 §P-013 (Fix A, file-based last-run, no REFLECTION_* events);
  W0-INV-01 §P-010 (reflection delegates to WorldModel — respect, do not reintroduce fence writes,
  run_weekly signature unchanged); W1-IMPL-01 (ReflectionSettings declared — CONSUME only);
  W3-FIX-01 (run_weekly per-rule VaultConflictError guard — loop must not unmask it).
- Code verified at HEAD: daemon.py (3 loops, _supervise/_sleep/_maintenance_loop shape),
  reflection.py (run_weekly/apply_to_playbook + W3 guard), cli/main.py doctor (lines 227-279),
  tui/dashboard.py render_doctor (362-406) + runner.py _build_info (outside write-set),
  config.py ReflectionSettings (enabled=False default, lines 774-787) + ensure_directories,
  world_model.py atomic-write + hashes-JSON idiom, tests/improvement/test_improvement.py (22 tests),
  tests/daemon/test_maintenance_loop.py Runtime fixture pattern, tests/cli/test_cli.py doctor tests.
- Branch verified: ops/20261107-integration-fix [RAN git branch --show-current]. HEAD at start: 45b542d.

## Context Report (pre-implementation)
- Root cause: ReflectionEngine.run_weekly exists + tested but has zero production callers
  [RAN grep run_weekly src/ -> definition only]; the RFC §3.1 "once a week" claim is false until
  a scheduler fires it. No last-run state exists anywhere (no REFLECTION_* EventType, no file),
  so /doctor cannot assert staleness. W3 cured the crash mode (per-domain VaultConflictError
  skip); this task schedules the engine safely without unmasking that guard.
- Affected files/consumers: daemon loop cadence + supervision; ReflectionSettings.enabled gate;
  settings.home state file; settings.storage.vault_path playbook target; CLI + TUI doctor rows.
  Downstream: none of the other loops depend on reflection; reflection depends on ledger reads
  (events_since) + vault fence writes only.
- Invariants: ledger-before-side-effect untouched (no new ledger writes; run_weekly writes no
  events); single-daemon philosophy (4th supervised loop, no OS cron — Fix B rejected);
  zero-baseline (ruff 0, mypy total 3 pre-existing); WorldModel remains sole fence writer
  (apply_to_playbook delegation untouched); playbook writes only via run_weekly.
- Risks/edge cases: (a) reflection must not run parallel to curator DECAY prune — shared lock,
  reflection acquires after the maintenance tick releases it; (b) crash between playbook write
  and last-run marker write -> retry appends a duplicate rule (fence preserves human text; one
  duplicate anti-pattern is the worst case — recorded as remaining risk, run_weekly body is
  outside the write-set so dedupe cannot be added here); (c) corrupt/naive/future last-run file
  -> degrade to unknown/due, never crash doctor; (d) disabled flag -> loop never fires, doctor
  shows disabled, engine stays callable; (e) TUI runner._build_info cannot pass reflection keys
  (outside write-set) -> render_doctor mirror renders unknown/disabled-safe defaults + NEED posted.
- Expected behavior: first boot (no state file, enabled) -> due immediately, runs once, writes
  marker; next tick same week -> no-op; 7d exactly -> not due; 7d+1s -> due/stale; doctor prints
  reflection.last_run_age_days + due/disabled; failed run -> no marker -> next tick retries.
- Required tests (must FAIL before, pass after): T14 stale/due + run clears; 7d / 7d+1s boundary
  pair; hostile (empty home, no vault/state file -> due/unknown, no crash, no writes);
  disabled (no fire, doctor disabled, engine callable); concurrency (two ticks -> single append);
  failed tick writes no marker + retry succeeds single append (kill-mid-reflection shape).

## Phase log

### Phase 0 — Tests first (DONE, fail-before shown)
- Created `tests/improvement/test_reflection_wiring_p013.py` (29 tests) before any src change.
- [RAN pytest tests/improvement/test_reflection_wiring_p013.py -q (pre-implementation) -> ImportError: cannot import name 'format_reflection_health' from 'paa.improvement.reflection', 1 collection error] — the tests fail without the fix, as required (the daemon tick, helpers, and doctor rows do not exist yet).

### Phase 1 — `src/paa/improvement/reflection.py`: last-run helpers only (DONE, self-reviewed)
- New: `REFLECTION_INTERVAL_DAYS = 7.0`, `REFLECTION_LAST_RUN_FILENAME = "reflection_last_run.json"`,
  `reflection_last_run_path`, `read_reflection_last_run`, `write_reflection_last_run`,
  `reflection_last_run_age_days`, `reflection_health`, `format_reflection_health` (+ `__all__`).
- File-based per INV-05 (no `REFLECTION_*` EventType): `{last_run_at}` JSON under `Settings.home`
  (PAA_HOME), written atomically (temp + `os.replace`) following the `WorldModel._atomic_write`
  idiom; reads degrade missing/corrupt/wrong-shaped/naive inputs to `None` (unknown/due) with a
  redacted warning, mirroring `WorldModel._load_hashes` (bookkeeping, not truth).
- Inverse boundary: `stale`/`due` use strictly-greater-than-7-days, so exactly 7d is fresh and
  7d+1s is stale (T14 pair). `due` is gated on `enabled`; `stale` describes the data alone;
  unknown is due-but-not-stale. Naive `now` treated as UTC like every stored timestamp.
- `run_weekly` / fence delegation / `apply_to_playbook` / the W3 per-rule guard: untouched.
- Self-review: imports all used (`redact` wired into the two new log lines per P-001 hygiene);
  `reflection_last_run_age_days` is consumed by the daemon delay (no dead helper). No findings.

### Phase 2 — `src/paa/daemon.py`: 4th loop + ordering (DONE, self-reviewed)
- `_reflection_loop` (supervised via `_supervise("reflection", ...)` in `start()`, same try/except
  + `_sleep` shape as `_maintenance_loop`): checks first (no initial sleep — first boot with no
  marker is due immediately, runs once), then sleeps until the next due point or the 1h poll
  (disabled / failed tick), so it fires once per weekly interval, never tight-loops.
- `_run_reflection_tick(now=None) -> bool`: returns False when `ReflectionSettings.enabled` is
  False or not due; otherwise builds `ReflectionEngine(rt.ledger, vault_path=settings.storage.vault_path)`,
  calls `run_weekly(now=when)`, and writes the marker only after the pass completes (crash
  mid-pass leaves no marker → next tick re-runs). Non-conflict failures propagate to the loop,
  which logs `daemon.reflection_failed` (redacted) and continues; the W3 per-rule
  `VaultConflictError` guard inside `run_weekly` is never unmasked.
- Ordering (plan §J): new `asyncio.Lock` (`_curation_lock`) held by `_maintenance_loop` around
  `_run_curation_tick()` and acquired by the reflection pass — never parallel; reflection runs
  after the prune. Pinned by `test_tick_waits_for_the_curation_lock`.
- `PLAN:integration-fix:P013` trace tags at the loop/tick/lock; module docstring now lists 4 loops.
- Self-review: shutdown cancels cleanly (lock acquire is cancellable; `_sleep` wakes on `_stop`);
  no `TYPE_CHECKING` block added (one `datetime` import joined the existing classified block —
  classification tests still pass); no findings.

### Phase 3 — Doctor rows (DONE, self-reviewed)
- CLI (`cli/main.py`, doctor function only): additive row `reflection.last_run_age_days` =
  `format_reflection_health(reflection_health(settings.home, enabled=settings.reflection.enabled))`.
  Read-only (health never writes — pinned by the empty-home hostile test).
- TUI mirror (`dashboard.py`, `render_doctor` + `_reflection_doctor_value` helper): same label,
  same value strings; missing runner keys render `disabled` (matches the `ReflectionSettings`
  default). The pre-existing strftime typo (`dashboard.py:346`, category-B) is untouched.
- Self-review: value strings byte-identical between surfaces (tests assert both); no findings.

### Phase 4 — Verification (DONE)
- See Verification Evidence below. `grep run_weekly src/` → definition (reflection.py) +
  exactly one production caller (daemon.py) + doc mentions (config comment, daemon docstring) + tests.

## Review Audit
- Phase 1: self-reviewed (above) — ruff/mypy scoped clean after `__all__` natural-sort fix.
- Phase 2: self-reviewed — re-read diff: supervision, shutdown, lock ordering, marker-after-run
  all hold; the `age == 7.0 → delay 0` case re-checks once and then fires (real clock crosses
  within ms). No CRITICAL/MAJOR findings.
- Phase 3: self-reviewed — CLI/TUI strings pinned identical by tests; no findings.
- Final diff review: 5 files only (4 src + new test file) in code commit `620ab30`, +779/−3.
  No `config.py`/`ledger/*`/`core/types.py`/`world_model.py` edits; no new EventType; no cron docs.
- Independent review: not run (this session is itself a subagent; no nesting). Self-review above
  substitutes; recommend a fresh-context reviewer before the final gate.

## Verification Evidence [RAN]
- [RAN pytest tests/improvement/test_reflection_wiring_p013.py -> 29 passed] (was ImportError
  collection error pre-implementation — fail-before shown in Phase 0).
- [RAN pytest tests/improvement/ (both files) -> 51 passed: 22 existing unchanged + 29 new].
- [RAN pytest tests/daemon/test_maintenance_loop.py -> 13 passed] (loop + curation behaviour intact).
- [RAN pytest tests/memory/test_dual_vault_p010.py -> 37 passed] (fence delegation intact).
- [RAN pytest tests/cli/test_cli.py -k doctor -> 3 passed] (2 new reflection-row tests + existing doctor tests).
- [RAN pytest tests/api/test_daemon.py -> 5 tests: 4 pass clean; test_a_crashed_loop_is_restarted
  passes (`.` + daemon.stopped in output) but the pytest process does not exit within 90s —
  reproduced identically at HEAD f973755 in a clean worktree with PYTHONPATH override
  (exits=False at 60s), so it is pre-existing on this host, not this change (FINDING posted).]
- [RAN pytest tests/conventions/test_import_conventions.py -> classification/source/runtime/
  annotation/extra + fresh-interpreter sweeps pass; only
  test_the_sweep_covers_every_module_under_the_package fails (139 probes / 140 modules) because
  sibling W4-IMPL-01's untracked src/paa/context/allocator.py is not yet in the probe list —
  not my write-set (FINDING posted; my 4 files import cleanly in the fresh sweep).]
- [RAN pytest tests/tui/test_dashboard.py -> 1 failed (pre-existing category-B
  test_ledger_with_events, ValueError Invalid format string at src/paa/tui/dashboard.py:346,
  unchanged by this task); all other TUI tests pass incl. the 3 new reflection-mirror tests.]
- [RAN pytest tests/conventions/test_docs_references.py -> fails: 12 blank-line citations to my
  4 edited files (line shifts, unavoidable) + siblings' facts.py/dialect.py shifts; needs a
  post-wave doc re-point task (W1/W3 precedent; FINDING posted).]
- [RAN ruff check src tests -> All checks passed (0 findings)].
- [RAN mypy src -> Found 3 errors, all pre-existing src/paa/sandbox/subprocess_backend.py:548;
  scoped mypy on my 4 files -> Success, 0 errors. Total stays 3.]
- [RAN grep run_weekly src/ -> definition + daemon.py caller + doc mentions only; one production caller.]
- NOT-RUN (out of scoped-check scope): full pytest suite, PostgreSQL/S3 live paths, containers/CI.

## Long-run test (Source / Load / Hostile / Operability / Reversal)
- **Source:** authority is the marker file + live settings (`reflection.enabled`, `settings.home`);
  the engine is the sole production caller of `run_weekly`. No second truth: `run_weekly` still
  writes no ledger events.
- **Load (100×):** one weekly pass reads 7 days of events (`events_since`) and writes ≤ N rules
  (N = high-friction domains, small); due-check is one file stat+read per poll; lock hold is the
  pass duration only. Loop sleeps are bounded (≤ 7d remaining, 1h poll) and shutdown-cancellable.
- **Hostile input:** corrupt/wrong-shaped/naive/future marker → unknown/due, never a crash, never
  false-fresh (pinned); vault-is-a-file → tick raises, no marker, retry lands once (pinned);
  empty ledger → pass runs, no playbook write (pinned); empty home → doctor due/unknown, zero writes (pinned).
- **Operability (observe/roll back/migrate):** `daemon.reflection_completed` (domains/rules),
  `daemon.reflection_failed` (redacted), `reflection.last_run_unreadable` (marker degrade) log
  lines + the doctor row (`reflection.last_run_age_days`) are the signals. Rollback: flip
  `PAA_REFLECTION__ENABLED=false` (loop never fires, engine stays callable) or revert the commit;
  marker file can be deleted (next tick is due-immediately, idempotent-gated). No migration.
- **Reversal:** revert daemon/helpers/rows → all 29 new tests fail (no marker, no row, no
  single-append, no lock ordering); revert just the lock → `test_tick_waits_for_the_curation_lock`
  fails; revert just the doctor rows → the 2 CLI + 3 TUI doctor tests fail; widen the stale check
  to `>= 7` → `test_exactly_seven_days_is_not_stale` fails.

## Unverified assumptions & remaining risks
- [ASSUMED] Sequential single-process daemon (existing posture): the `_curation_lock` serialises
  the two asyncio loops, but direct calls to `_run_curation_tick` (tests) do not take the lock —
  production always goes through the loops, and the tick acquires it, so the guarantee holds
  wherever both loops run.
- [ASSUMED] Crash between the playbook write and the marker write duplicates one anti-pattern
  on the retry (the marker is written after the pass by design; `run_weekly` appends rather than
  dedupes). The fence always preserves human text, so the worst case is one duplicate rule next
  week. Deduping inside `run_weekly`/`apply_to_playbook` is outside this write-set — flagged as
  a follow-up, not rebuilt here.
- [ASSUMED] `recorded_at` lands inside the 7-day window in tick tests (real `utc_now` injected
  as `now`, same as existing improvement tests).
- NOT-RUN: full suite, PG/S3, Docker/CI, live Qdrant/Redis paths.
- **NEED (outside write-set):** `src/paa/tui/runner.py::_build_info` does not populate
  `reflection_enabled`/`reflection_last_run_age_days`/`reflection_stale`, so the TUI mirror
  renders `disabled` (the default) until the runner owner wires the keys. The CLI doctor is the
  live surface and works today. (Posted as FINDING context in §5; runner.py is not in this write-set.)
- **FINDING (pre-existing, not this change):** `test_a_crashed_loop_is_restarted` process linger
  reproduced at HEAD f973755 (see Verification Evidence).

## Git State
- Branch: `ops/20261107-integration-fix` [RAN git branch --show-current before committing].
- Code+tests commit (explicit paths only, never `add -A`/`-a`): `620ab30` — 5 files, +779/−3,
  trailers `Task: T-013-P013`, `Agent: W4-IMPL-02`, `Wave: W4`,
  `Report: docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-02.md`.
- This report is committed separately (docs-only) listing `620ab30`.
- Sibling commits landed during my work (W4-IMPL-01/03: `b14228f`, `f973755`) — disjoint files,
  no overlap with my write-set; verified via `git status --short` before committing.
- Forbidden commands never used (no stash/reset/checkout/rebase/merge/push/`--no-verify`).
