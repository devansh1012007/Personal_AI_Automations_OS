# Mission — 20261106-memory-remembering-agent

- Branch: `ops/20261106-memory-remembering-agent`
- Base SHA: `eb6606e24d33f56b1dd7d2d811110b77940b8318` (main)
- Plan: `C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md` (APPROVED 2026-11-06, Decisions D1–D6)
- Goal: C01–C07 wiring (embed sink, pending drain, touch-on-recall, curator schedule, extractor counters, config hygiene, e2e proof) so the agent remembers across sessions.
- Non-goals: no DB schema change, no LLM reranking, no replay job, no new public API. Forbidden edits: direct writes to hot tables, new LIKE patterns outside `storage/relational/like.py`, new per-file ruff ignores, `StrEnum`, secrets in config.

## Commands (Windows host)

- Setup: `uv venv --python 3.12` + `uv pip install -e ".[dev]"` (DONE: 57 pkgs, `paa import OK`, CPython 3.12.12)
- Gate order (AGENTS.md): `.venv\Scripts\python.exe -m ruff check src tests` · `-m mypy src` · `-m pytest tests/ -q`
- Scoped test: `.venv\Scripts\python.exe -m pytest <path> -q` · single test: `... <path>::<test> -q`

## Baseline (captured 2026-11-06, before any change, no agents running)

- Ruff: `All checks passed!` (1 pre-existing warning: invalid `# noqa` directive `src/paa/storage/relational/migrations.py:542` — not a finding, not ours)
- Mypy: **3 errors pre-existing** `src/paa/sandbox/subprocess_backend.py:548` — `os.killpg/getpgid/SIGKILL` missing on win32 (host/platform). Zero-baseline gate is defined for Linux CI; on this Windows host expect exactly these 3 until fixed upstream. Do NOT fix in this run (out of scope, hotspot-adjacent).
- Pytest: **12 failures, all pre-existing, none memory-related** (Windows/host):
  - `tests/sandbox/test_docker_backend.py` ×9: `os.getuid/geteuid` missing (win32), `Path` backslash vs POSIX expectations, HOME/tmpdir env not matching Linux assumptions
  - `tests/tui/test_dashboard.py::test_ledger_with_events`: `ValueError: Invalid format string` at `src/paa/tui/dashboard.py:160` (`when.strftime("%H:%:%S")`) — genuine pre-existing code bug, out of scope
  - `tests/validation/test_workspace.py` ×2: `test_crlf_and_lf_hash_differently` (Windows newline translation), `test_an_unreadable_file_is_skipped_not_fatal` (`os.geteuid` missing)
  - Suite was run with 900s timeout; output reached 100% + summary, then the shell timed out. Counts above from the short-summary; may need a longer timeout on final gate.
- Pre-existing uncommitted changes: none (`git status --porcelain` clean at preflight).

## Hotspots (single-writer)

- `src/paa/storage/relational/schema_sqlite.sql` + `schema_postgres.sql` (parity lock) — NOT touched per plan (no schema change).
- `pyproject.toml`, CI config, `docker-compose.yml` — not touched.
- `src/paa/config.py` (MemorySettings) — C06 only, single writer in its wave.

## Assumptions (A-nnn)

- `A-001`: `VectorEmbeddingSink` + `active_facts` collection + `pending_embeddings/mark_embedded` API are complete and correct — verified by C01/T01 wave, not assumed for later waves.
- `A-002`: No deploy cron schedules `MemoryCurator` (Q1) — verify by grep in Wave 0 before C04.
