# Developer workflow

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Optional extras:
```bash
uv pip install -e ".[vector]"    # Qdrant embedded, else numpy brute force
uv pip install -e ".[graph]"     # KuzuDB, else SQLite recursive CTE
uv pip install -e ".[embeddings]"# real embeddings, else hash embedder
uv pip install -e ".[api]"       # FastAPI ingestion
```

No Docker, no server, no GPU required (`README.md:48`).

## State root

`PAA_HOME` defaults to `~/.paa`, deliberately outside the repo so `git clean`
can't eat the ledger (`config.py:41`). Tests override it via `tmp_path` and
`reset_settings_cache()`.

## Daily commands

```bash
# Run everything
.venv/bin/python -m pytest tests/ -q

# Ledger spine only
.venv/bin/python -m pytest tests/ledger -q

# Crash/chaos injection
.venv/bin/python -m pytest -m chaos -q

# Lint
.venv/bin/python -m ruff check src tests

# Typecheck
.venv/bin/python -m mypy src

# Doctor: which backends/sandbox are actually live
.venv/bin/paa doctor
```

## CLI entry points

| Command | Purpose |
|---|---|
| `paa version` | Print runtime version |
| `paa submit "goal"` | Submit and run a task to completion |
| `paa run <id>` | Drive a queued task to terminal |
| `paa status <id>` | Show replayed task state |
| `paa ledger <id>` | Walk a task's event chain (explainability) |
| `paa gate <id> --approve/--reject` | Clear a human-attestation gate |
| `paa recover` | Post-crash boot sweep |
| `paa doctor` | Report live backends/sandbox |
| `paa serve [host] [port]` | API + background daemon |
| `paa agent "prompt"` | Tool-using coder loop |
| `paa tui` | Interactive dashboard |
| `paa mode <mode>` | Show how to set permission mode (`PAA_POLICY__MODE=...`) |

## Driving the runtime programmatically

```python
async with await Runtime.build() as rt:
    cid = await rt.submit("summarise my notes")
    outcome = await rt.run(cid)
```

For tests, inject a scripted model via `model_adapter=` (`runtime.py:237`);
the `ScriptedModel` in `tests/integration/conftest.py` is the reference
implementation (`AGENTS.md:108`).

## How to trace a feature

1. Find the entry point (CLI `cli/main.py`, API `api/app.py`, or
   `Runtime.submit`/`Runtime.run`).
2. Follow into `ChiefOrchestrator` (`agents/orchestrator.py`), which writes the
   ledger event before doing the work.
3. Identify the handling agent (planner/policy/worker/critic/context builders/
   router) and what it calls downstream.
4. Note what is persisted (ledger event, recovery mark, queue message) vs
   derived (context packet, projection, indexes).
5. Follow errors: every `PaaError` carries structured `details` written to the
   ledger; `TERMINAL_EVENTS` close a lineage (`core/types.py:132`).

## Conventions to follow

- `class X(str, enum.Enum)`, not `StrEnum` (UP042 ignored).
- Pass `.value` explicitly; enums round-trip through pydantic and SQLite TEXT.
- `CorrelationId`/`SessionId`/`TaskId` are `NewType`s over UUID — don't collapse
  them.
- Docstrings are long and load-bearing: they carry the RFC section or
  `SPEC DEVIATION (docs/adr/NNNN)` rationale. A deviation from the RFC without
  an ADR is a review failure.
- Comments explain *why*, not *what* (`AGENTS.md:124`).
- Payload hashing uses `ledger/events.py::canonical_json`; any change breaks
  the hash chain (treat as data migration).
- Adding an `EventType` means touching `core/types.py`, `ledger/replay.py`, and
  `agents/orchestrator.py` (`AGENTS.md:41`).
- Never import a heavy dependency (qdrant, kuzu, sentence-transformers, redis,
  psycopg, boto3) at module scope in a core path.

## Adding a configuration option

- If it's a `Settings` field: add to `config.py` with `PAA_*` prefix and `__`
  delimiter; document default/consumer/fallback.
- If it's configured by name (e.g. a provider API key): use `dotenv_value` and
  register `api_key_env` on the `ProviderSpec` row (`registry.py`).

## What to avoid

- Don't commit secrets or keys.
- Don't add new files to the `S608`/`S105` per-file allow-lists.
- Don't treat a README claim or a directory name as evidence a feature exists;
  verify against source.
- Don't overwrite existing documentation without inspecting it first.

## Related documents

- [`development/testing-and-quality.md`](testing-and-quality.md) — test conventions.
- [`AGENTS.md`](../AGENTS.md) — authoritative runtime conventions.
- [`pyproject.toml`](../pyproject.toml) — lint/type/test config.
