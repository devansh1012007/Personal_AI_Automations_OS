# 00-mission — 20261107-integration-fix (orchestrator-owned)

## Scope
Execute plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` (rev 5, READY FOR APPROVAL; operator directed implementation 2026-11-07) — integrate the partially/non-integrated parts of Doc A (Attention Allocation) and Doc B (Core RFC) against live `src/paa`, safety-first, ADR-gated, no verbatim adoption of contradicting formulas/behaviours.

Plan items P-001..P-014 = plan P01..P14. Trace tag in code: `PLAN:integration-fix:Pxx`.

## Repo / branch
- Branch: `ops/20261107-integration-fix` (created from `ops/20261106-memory-remembering-agent` @ `88e648b`)
- BASE_SHA: `88e648bf88b187c9cb0007f07e4019a079daf2d5`
- Pre-existing uncommitted changes (never touch, never commit): `docs/agent-ops/20261106-memory-remembering-agent/briefs/DOCS-README-01.md` (untracked)

## Commands (AGENTS.md, Windows venv)
- Lint: `.venv\Scripts\python.exe -m ruff check src tests`
- Type: `.venv\Scripts\python.exe -m mypy src`
- Tests: `.venv\Scripts\python.exe -m pytest tests/ -q`
- Focused: `pytest tests/ledger -q`, `pytest -m "not slow"`, `pytest -m chaos`, `pytest -m requires_graph`, `pytest -m requires_postgres` (needs `PAA_TEST_POSTGRES_DSN`), `pytest -m requires_s3`
- Set `PAA_TEST_VERBOSE=1` to un-silence structlog.

## Baseline (captured before any change; 2026-11-07)
- `ruff check src tests` → **0 findings** (pass)
- `mypy src` → **3 errors** at `src/paa/sandbox/subprocess_backend.py:548` (`os.killpg`, `os.getpgid`, `signal.SIGKILL` — POSIX symbols in the `else` of `IS_WINDOWS`, checked on Windows host; CI Linux gate unaffected). Pre-existing, NOT this run's regression (see ledger A-001). Gate = these 3 stay 3, never grow.
- `pytest tests/ -q` → **in progress** (background shell `sh_1b34c2db9001xYK7394FMYZiGw`); record pass/fail counts here when done. Pre-existing failures must be listed by id.

## Hotspots (single-writer, wave-gated)
- `src/paa/storage/relational/schema_sqlite.sql` + `schema_postgres.sql` (parity lock — same commit, both files)
- `src/paa/storage/relational/migrations.py` (`SCHEMA_VERSION` bump)
- `src/paa/core/types.py` (`EventType`, `AgentRole`)
- `src/paa/ledger/replay.py`, `ledger/recovery.py` (`_PHASE_MAP`/`_NO_PHASE_CHANGE`/`TERMINAL_EVENTS`)
- `src/paa/config.py` (Settings surface, P14 sole owner)
- `pyproject.toml` (extras/markers)
- `docs/adr/README.md` (ADR index)
- `.github/workflows/ci.yml`

## Assumptions (A-nnn)
- A-001: mypy 3 errors pre-existing (evidence: git log 4a0f761/03f635a predate this run; branch untouched).
- A-002: plan rev5 file:line claims re-verified against HEAD 88e648b by investigators (plan staleness rule requires it).
- A-003: `pytest` baseline completes on Windows host; gated suites (`requires_postgres/s3`) skip without env — skips are not failures.
- A-004: R1–R3 (operator checks) automated per D12 — CI-shaped tests, not manual gates.

## Out of scope
Verbatim Doc A formula adoption; full21 specialists; new web frontend; ledger invariant changes; `git push`/merge unless operator asks; touching the pre-existing untracked file above.
