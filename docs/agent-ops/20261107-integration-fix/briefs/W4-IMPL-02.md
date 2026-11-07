# Brief — W4-IMPL-02 (IMPL) · wave 4 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-013 (T-013) — weekly reflection wiring (daemon 4th loop) + file-based last-run tracking + `/doctor` healthcheck (`reflection.last_run_age_days` ≤7d when enabled). Why: the engine is implemented + tested but never scheduled; the doc claim "once a week" is false until wired. W3 cured the crash mode (`run_weekly` now guards `VaultConflictError` per-rule) — this task schedules it safely.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; single-daemon philosophy; observability; settings cache `reset_settings_cache`)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline; A-005 autonomous)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P13 row (cron/scheduler step + `/doctor` healthcheck asserting last run ≤7d; Settings `ReflectionSettings.enabled`) + §H T14 + §K (`reflection.last_run_age_days` observability)
5. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-05.md` §P-013 (root cause: engine never called; Fix A recommended — daemon 4th loop supervised like others, ledger/file last-run, doctor row; Fix B cron rejected (first external scheduler); file-based last-run preferred over EventType (no `REFLECTION_*` events exist — a ledger-scan staleness query needs invented events) + T14 shape + scheduler-vs-maintenance ordering note), `W0-INV-01.md` §P-010 (reflection.py now delegates to WorldModel — respect the delegation, do not reintroduce direct fence writes; `run_weekly` signature unchanged).
6. W-wave evidence: `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-01.md` (`ReflectionSettings` declared — CONSUME only); `reports/w3-review/W3-FIX-01.md` (`run_weekly` conflict guard — your loop must not unmask it).
7. Template: implementation report ORCH-DIGEST first.
8. Code at HEAD (verify line numbers yourself): `src/paa/daemon.py` (the 3 existing loops — shape of a supervised loop: interval, touch ordering, graceful shutdown), `src/paa/improvement/reflection.py` (`run_weekly`, `apply_to_playbook`, per-rule guard), `src/paa/cli/main.py` (doctor table rows — additive row; also `render_doctor` in `tui/dashboard.py` mirror — check both, keep them in step per AGENTS convention), `src/paa/config.py` `ReflectionSettings` (read-only), existing last-run/state-file idioms (glob for any existing `.paa`-state JSON writers to follow — e.g. how checkpoints/snapshots persist small JSON; `ledger/store.py::save_snapshot` or filesystem JSON pattern), P-009's DECAY-tail ordering note (`plan §J: tier sweeper ordered after curator DECAY phase` — your weekly loop runs AFTER maintenance, not parallel to its prune).

## 3. Task and acceptance criteria
Task T-013-P013. Specifically (INV-05 Fix A):
- Daemon 4th loop in `daemon.py`: weekly interval (exact spacing strategy: check how the existing loops space ticks — follow it; the loop must fire ONCE per interval, not tight-loop; first boot with no last-run file = due immediately, but only once). Supervised like the others (stop-on-shutdown, errors logged + tick continues). Ordered AFTER the maintenance/curator sweep in the same daemon iteration (no parallel prune race). Gated by `ReflectionSettings.enabled` (CONSUME only; disabled → loop never fires, doctor shows disabled). Crash mid-reflection → next tick re-runs idempotently (fence/idempotency already in `run_weekly` — assert via test, do not rebuild).
- Last-run state: FILE-based (INV-05 out-of-plan discovery — no `REFLECTION_*` events exist; inventing an EventType for this SURFACE task would make it DEEP and is rejected). Where: follow the existing small-state-JSON idiom you find (PAA_HOME subpath; `config.py` `_ensure_directories`? — read it; if a state-file helper exists, reuse it; else a `reflection_last_run.json` under the home dir with `{last_run_at}` provenance). Concurrency: two ticks same week → playbook appends once (marker-fenced block already handles — assert via the existing test pattern).
- CLI `doctor` (and TUI `render_doctor` mirror — keep them in step): additive row printing `reflection.last_run_age_days` + due/disabled state. Staleness: no last-run file → report as due/unknown (not a crash, not ≤7d); exactly 7d → not stale; 7d+1s → stale (T14 boundary pair). `paa reflection` CLI command (type?): the loop + doctor is the deliverable; a manual-run CLI is optional — add one ONLY if it is ≤20 lines following the existing CLI pattern (a `reflection` subcommand invoking `run_weekly` once); otherwise record as deferred with the exact shape. Do NOT add OS-cron docs (Fix B rejected).
- `ReflectionSettings`: CONSUME only (no config.py edits).
- Tests (must FAIL before, pass after): T14 — due (last run 8d ago) → doctor reports stale; run clears it (last_run_age ≤7); boundary 7d not-stale / 7d+1s stale; hostile: no vault → report produced, no playbook write, no crash; disabled → doctor shows disabled, engine still callable; concurrency: two ticks → single append. New `tests/improvement/test_reflection_wiring_p013.py` (loop timing tested with injected/monkeypatched clock + state file, never real sleeps).
- Trace tag `PLAN:integration-fix:P013` at the loop.
- Acceptance: `grep run_weekly src/` shows exactly one production caller (the daemon loop) + tests; `/doctor` prints the age row when enabled; T14 green with fail-before shown; `tests/improvement/test_improvement.py` 22 green unchanged; ruff 0, mypy total 3; no `config.py`/`ledger/*` edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/daemon.py`, `src/paa/cli/main.py` (doctor row only), `src/paa/improvement/reflection.py` (last-run helpers only — do not touch fence delegation or run_weekly loop body), `src/paa/tui/dashboard.py` (doctor mirror row only), `tests/improvement/test_reflection_wiring_p013.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `src/paa/core/types.py`, `src/paa/ledger/*`, `src/paa/memory/world_model.py` (W3-owned delegation), new EventTypes.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `daemon.py` (shared daemon file; only daemon-loop writer this wave) and `cli/main.py` doctor area (P-001's file — different functions, sequence by function).

## 5. Parallel siblings this wave
W4-IMPL-01 (allocator — `context/allocator.py`, `types.py`, `replay.py`, `creator.py`, `orchestrator.py`), W4-IMPL-03 (non-ASCII v6 — SQL/migrations/`like.py`/`dialect.py`/`facts.py`). Disjoint; you own daemon + reflection + doctor rows.

## 6. Skip list and known traps
- "Cron/scheduler step" assumes infra that doesn't exist — the daemon 4th loop is the W3-delegated decision (INV-05 Fix A). Do not add OS cron docs or a second scheduler.
- No `REFLECTION_*` EventType, no ledger-scan last-run (SURFACE constraint). File-based only.
- The weekly tick must not run parallel to curator DECAY prune (ordering note) and must survive `VaultConflictError` per-rule (W3 cure — verify the loop surfaces it as logged, never as a daemon crash).
- The TUI dashboard strftime failure (category-B) is NOT yours — if your doctor-mirror touches nearby lines, quote the unchanged failure by test id.
- Baseline categories A/B/D untouched.

## 7. Environment and tests
Scoped only: your new test file + `tests/improvement/test_improvement.py` + `tests/cli/` doctor tests if any + ruff/mypy scoped. Tmp fixtures only (injected clock, temp PAA_HOME). No full suite, no real sleeps, no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-013-P013`, `Agent: W4-IMPL-02`, `Wave: W4`, `Report: docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-02.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers incl. kill-mid-reflection + flag-off observability). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
