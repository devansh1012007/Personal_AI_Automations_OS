# Testing and quality

PAA's testing conventions are documented in `AGENTS.md` and `pyproject.toml`.
This document summarizes them and maps test suites to the modules they cover.

## Install and run

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

Verify order (per `AGENTS.md:16`):
```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
.venv/bin/python -m pytest tests/ -q
```

Focused runs:
- `pytest tests/ledger -q` — the ledger spine (71 tests per README)
- `pytest tests/ledger/test_replay.py::test_x -q`
- `pytest -m "not slow"` — exclude slow tests
- `pytest -m chaos` — crash injection
- `pytest -m requires_graph` — graph-extra tests

## Test markers and extras

- `--strict-markers` is on; extras-gated tests use a `pytest.mark.requires_*`
  marker **and** a `skipif`/`importorskip` guard (e.g. `tests/storage/
  test_vector.py`, `tests/api/conftest.py`).
- `asyncio_mode = "auto"` — async tests need no decorator.
- `filterwarnings = ["error::DeprecationWarning:paa.*"]` — a `DeprecationWarning`
  raised from `paa` fails the test. Fix the code, don't silence it.

## Fixtures and isolation

- `paa_home` (`tests/conftest.py`) isolates state into `tmp_path`; fixtures are
  function-scoped and write into `tmp_path`, so order-independence is expected.
  Don't add session-scoped state.
- `snapshotter` is a byte-oriented fake on purpose — `write_text`/`read_text`
  would translate newlines on Windows and report phantom manifest drift.
- Anything that changes `PAA_HOME` (tests) **must** call
  `reset_settings_cache()` (`config.py:495`).
- Tests that chdir must call `reset_dotenv_cache()` (`config.py:502`).
- `PAA_TEST_VERBOSE=1` un-silences structlog.

## Hypothesis property tests

Hypothesis covers invariants that must hold for all inputs:
- gatherer token ceiling (can't be broken),
- replay over arbitrary event orderings (never raises),
- confidence stays in [0,1].

Extend these rather than writing example-based tests for them (`AGENTS.md:92`).

## Crash/chaos tests

Simulate power loss by closing the DB mid-lineage and reopening — committed
transactions survive, uncommitted vanish (`AGENTS.md:94`). `pytest -m chaos`.

## Build status (from README, ~line 208)

| Subsystem | State | Tests |
|---|---|---|
| Core types, config, permissions | tested | via ledger suite |
| Event ledger + replay + recovery | tested | 71 |
| Bounded context engine | tested | 353 |
| Storage (vector, graph, cold lake, queue, relational) | tested | 255 (+5 skipped, Redis) |
| Sandbox | tested | 39 |
| Memory decay + domains + contradiction | tested | 37 |
| Agents (orchestrator, delegation, policy, context, planner, critic, router, worker) | tested | 70 |
| Composition root + end-to-end integration | tested | 16 |
| Deterministic validation | exercised via agents; no dedicated suite | — |
| Model providers + escalation router + platform registry | tested | 71 |
| Memory creator/curator + world model | tested | 76 |
| Unified Skill Adapter (Claw Hub + MCP) | tested | 51 |
| Marketplace (signed packages, 5-gate installer) | tested | 18 |
| Self-improvement + meta-learning | tested | 22 |
| Claude-Code features (permissions, hooks, loop, sessions) | tested | 39 |
| Coder agent (`paa agent`) | tested | 91 |
| FastAPI ingestion + daemon + CLI | tested | 27 |
| Docker deployment + Postgres/MinIO backends | built; live-server run needs a pass | parity + backend |
| Observability | built, **untested** | — |

Total (per README): 1185 collected — 1104 passing, 75 skipped (optional extras
not installed), 6 failing. All six failures are environmental and pre-existing:
the sandbox executes steps with `python`, and on the build machine only
`python3` exists, so worker steps exhaust their retry budget. They fail
identically on a clean checkout.

## Coverage gaps (demonstrable from test structure)

- `tests/validation/` is an empty package — no dedicated validation suite.
- There is no `tests/observability/` — `src/paa/observability/` is exercised
  nowhere (`AGENTS.md:97`).
- Deterministic validation is exercised via agents but has no dedicated suite.

Missing tests ≠ proven defects; these are uncovered areas.

## Lint and type

- `ruff` with `line-length = 100`; `S` (bandit) is on. The `S608`/`S105`
  ignores in `pyproject.toml` are per-file allow-lists for known-safe patterns;
  do not widen them.
- `mypy` with `strict = true` and `warn_unreachable`; `disallow_untyped_defs`
  is relaxed only for `tests.*`.
- `class X(str, enum.Enum)` — not `StrEnum` (UP042 deliberately ignored).
- `CorrelationId`/`SessionId`/`TaskId` are `NewType`s over UUID.

## Notes

- The README's Testing section still shows Windows paths, and its per-subsystem
  test counts are approximate — only the total line was reconciled against a
  real run (`AGENTS.md:144`).
- Six pre-existing test failures are environmental, not regressions.

## Related documents

- [`development/developer-workflow.md`](developer-workflow.md) — setup and workflow.
- [`architecture/overview.md`](../architecture/overview.md) — conventions that differ from defaults.
