# Module: CLI (`src/paa/cli/`)

## Module identity

- **Name:** CLI
- **Path:** `src/paa/cli/`
- **Main responsibility:** The `paa` Typer command line — the primary human entry point; every command except `serve` is one-shot (build Runtime, do one thing, print, close).
- **Architectural layer:** Edge
- **Entrypoints:** `cli/main.py:app` (Typer), `cli/coder.py:register_agent` (`paa agent`)
- **Public interfaces:** `app` (`cli/main.py:38`), `register_agent` (`cli/coder.py:53`)
- **Other modules that depend on it:** `runtime.py` (all commands build `Runtime`), `coder/agent.py` (`paa agent`)

## Purpose and responsibilities

One-shot commands are cheap and always succeed at submission so the request is
durable before any work (`cli/main.py` module docstring). `paa ledger` is the
"explain why it did that" command — walks a task's event chain. `paa doctor`
never overstates backends or isolation (`cli/main.py:13`).

## Commands (`main.py`)

| Command | Line | Behavior |
|---|---|---|
| `version` | 67 | Print `paa {__version__}` |
| `submit` | 73 | Build Runtime; submit; optionally run to completion; print phase/outcome |
| `run` | 102 | Drive a queued task to terminal |
| `status` | 118 | Show replayed `TaskProjection` (phase, steps, attempts, tokens, awaiting/policy reason) |
| `ledger` | 144 | Walk event chain; print chain integrity (verify_chain) |
| `gate` | 179 | Clear a human gate (`--approve/--reject`) |
| `recover` | 206 | Boot sweep; print JSON summary |
| `doctor` | 224 | Report live backends + sandbox isolation level |
| `serve` | 289 | Build Runtime + Daemon + FastAPI app; uvicorn on `api_host:api_port` |
| `mode` | 336 | Show `PAA_POLICY__MODE=<value>` to set |
| `tui` | 352 | Run `run_tui()` |

All one-shot commands use `_build_runtime(run_recovery=False)` (`cli/main.py:53`) —
recovery is opt-in per command (an operator decision, `coder/agent.py:124`);
`serve` also does not run recovery at build (daemon does it at start, `daemon.py:64`).

### `paa agent` (`coder.py`)
`--mode ask|plan|accept_edits|bypass`, `--yes` (auto-approve), `--max-steps`,
`--allow`/`--deny`, `--resume`, `--list-tools`. Confirm callback: `always_yes`
under `--yes`, else `prompt_yes` (blocks on `input`; EOF → deny)
(`cli/coder.py:53`–`:172`).

## Runtime behavior

- `submit` default `run=True` (`:64`).
- `serve` binds loopback only; `api_host` validator enforced at `Settings`
  construction (`config.py:628`).
- Every command closes the `Runtime` in `finally` (`cli/main.py:95`, `:111`, etc.).

## Failure behavior

- Invalid correlation id → `typer.Exit(1)` (`cli/main.py:372`).
- Task not awaiting gate → `PaaError` → printed red, exit 1 (`:180`).
- `paa agent` `PaaError` → `[red]error:[/red]`, exit 1 (`cli/coder.py:194`).
- Unknown mode → printed red, exit 1 (`cli/main.py:343`, `cli/coder.py:100`).

## Modification impact

- Adding a CLI command: add a `@app.command()` in `main.py` (or register in
  `coder.py` for `agent`); each builds `Runtime` via `_build_runtime`.
- Tests: CLI exercised via `tests/cli/` and `tests/api/` (27 tests API per
  README).

## Known gaps

- No streaming output to the terminal while a task runs (`README.md:161`).
- `paa doctor` isolation level reflects the backend's own declaration
  (`isolation_level`, never aspirational — `sandbox/base.py:404`).

## Related documents

- [`architecture/overview.md`](../architecture/overview.md) — entry points.
- [`backend/overview.md`](../backend/overview.md) — CLI command table.
- [`modules/coder.md`](coder.md) — `paa agent`.
- [`operations/deployment-and-runtime.md`](../operations/deployment-and-runtime.md) — `paa serve`.
