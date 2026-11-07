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
- `pytest tests/ -q` → **14 failed / 3,900+ passed / 289 skipped** (foreground run, `tests/` ~3,915 collected; exact pass count not captured — rerun at first gate on final tree for exact numbers). Failure list reproduced verbatim below; all classify as pre-existing (see categories A–D below the list). No failure touches any file a plan item owns, except `tests/coder/test_runtime_tools.py::TestRememberAndRecall::test_remember_then_recall_round_trips` which asserts the OLD note string that FINAL-01 changed — that test is genuinely stale (category D) and P-04's brief must update it deliberately, not silently.

### The 14 baseline failures (authoritative, 2026-11-07)

| # | Test | Category |
|---|---|---|
| 1 | `tests/cli/test_cli.py::TestAgentSessions::test_continue_resumes_the_most_recent_transcript` | A host (Windows line-ending/transcript) |
| 2 | `tests/coder/test_runtime_tools.py::TestRememberAndRecall::test_remember_then_recall_round_trips` | D genuinely stale — asserts pre-FINAL-01 note text; update deliberately in P-04 task |
| 3 | `tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank` | C pre-existing — cites blank lines in `docs/plans and audits/w6-w7-w9.md` (run folder docs/plan dir not excluded... actually file is `docs/plans and audits/w6-w7-w9.md`; pre-existing run trail, not this run's). Pre-existing; NOT mine to fix unless directed. |
| 4–10 | `tests/sandbox/test_docker_backend.py` (7 tests: user/workspace-owner, host_path ×5, volume_argument, home_and_tmpdir) | A host (Docker daemon absent on Windows host — tests fail not skip) |
| 11 | `tests/tui/test_dashboard.py::test_ledger_with_events` | B code bug — `dashboard.py:160` `"%H:%:%S"` strftime typo (also `:59` `"%H:%:%M:%S"`); documented out-of-scope in prior run; P-06 implementer will trip over that file but must not fix outside scope unless directed |
| 12–13 | `tests/validation/test_workspace.py::test_crlf_and_lf_hash_differently`, `::test_an_unreadable_file_is_skipped_not_fatal` | A host (Windows CRLF; unreadable-file semantics differ as Administrator) |

Categories: **A host** = environment/host-dependent, not code regressions; **B code bug** = real defect, documented pre-existing; **C pre-existing doc defect** = fails at HEAD before any of this run's changes; **D genuinely stale test** = repo drifted (code changed, assertion didn't) — one item, and it sits in P-04's blast radius.

### Spot-check results (orchestrator, Wave 0 QA — all claims held)
- `effective_confidence` 11 hits in 5 files exactly as INV-01 claimed. ✓
- `workers:alloc`/`WORKERS_ALLOC`: only `base.py:60` + docstring example — zero producers/consumers. ✓
- `STATE_ROLLBACK_TRIGGERED` already wired (replay:166, recovery, reflection). ✓ (P-11 terminal sub-claim MISCLASSIFIED confirmed.)
- `_touch_recalled`: 4 defs (api:346, cli:699, coder:886, context_agents:403) + 8 call sites = 12 hits. ✓ (plan's old `note:807`/`context_agents:197` line refs stale — INV-04 line evidence is authoritative.)
- `total_memory_cost` / `TOTAL_MEMORY_COST`: 0 hits. ✓
- ADR glob: max file `0035-…`; README index last row `0035`. ADR 0036–0043 free. ✓
- `Dockerfile.embed`: absent. `docker-compose.yml:67,86`: `image: paa/runtime:4.1`. ✓
- `redact` at `models/base.py:256`, `redact_detail:278` (INV-04 said :243-295 — actual def lines :256/:278; substance confirmed).
- `runtime.py:481` unredacted `error=str(exc)`; `:474` redacted. ✓
- `replay.py:190/291` `_NO_PHASE_CHANGE` present. ✓
- HEAD: briefs pinned `88e648b`; live HEAD is `c15b665` (docs-only delta; `git diff 88e648b..HEAD -- src/ tests/` = empty — source identical). All file:line claims hold at both SHAs.

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
