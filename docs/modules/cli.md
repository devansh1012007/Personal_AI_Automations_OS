# Module: CLI (`src/paa/cli/`)

## Module identity

- **Name:** CLI
- **Path:** `src/paa/cli/`
- **Main responsibility:** The `paa` Typer command line — the primary human entry point; every command except `serve` is one-shot (build Runtime, do one thing, print, close).
- **Architectural layer:** Edge
- **Entrypoints:** `cli/main.py:app` (Typer), `cli/coder.py:register_agent` (`paa agent`), `cli/chat.py:app` (`paa chat`), `cli/mcp.py:app` (`paa mcp`)
- **Public interfaces:** `app` (`cli/main.py:46`), `register_agent` (`cli/coder.py:53`)
- **Other modules that depend on it:** `runtime.py` (all commands build `Runtime`), `coder/agent.py` (`paa agent`)

## Purpose and responsibilities

One-shot commands are cheap and always succeed at submission so the request is
durable before any work (`cli/main.py` module docstring). `paa ledger` is the
"explain why it did that" command — walks a task's event chain. `paa doctor`
never overstates backends or isolation (`cli/main.py:13`).

## Commands (`main.py`)

| Command | Line | Behavior |
|---|---|---|
| `version` | 73 | Print `paa {__version__}` |
| `submit` | 79 | Build Runtime; submit; optionally run to completion; print phase/outcome |
| `run` | 123 | Drive a queued task to terminal |
| `status` | 139 | Show replayed `TaskProjection` (phase, steps, attempts, tokens, awaiting/policy reason) |
| `ledger` | 165 | Walk event chain; print chain integrity (verify_chain) |
| `gate` | 211 | Clear a human gate (`--approve/--reject`) |
| `recover` | 238 | Boot sweep; print JSON summary |
| `doctor` | 256 | Report live backends + sandbox isolation level, plus chat model resolution, MCP token env and TUI-extra rows (SPEC 2 §3.1 C4.6) |
| `serve` | 422 | Build Runtime + Daemon + FastAPI app; uvicorn on `api_host:api_port` |
| `mode` | 469 | Show `PAA_POLICY__MODE=<value>` to set |
| `tui` | 485 | Run `run_tui()` |

Sub-apps registered on the root app (same one-shot `Runtime` discipline unless
the command is inherently long-running):

| Command | Home | Behavior |
|---|---|---|
| `paa memory <sub>` | `memory_app` (`cli/main.py:693`) | `facts`/`search`/`signals`/`drain`/`world-model` reads over the memory subsystem |
| `paa chat` | `cli/chat.py:160` | The chat surface (SPEC 2 §3.1 C4.4): Textual TUI, or `--plain` for terminals without the `[tui]` extra; `--session`/`--resume` name a lineage; `--model` overrides `PAA_CHAT__MODEL` |
| `paa mcp serve` | `cli/mcp.py:120` | The stdio MCP server (SPEC 2 §3.1 C4.5): JSON-RPC 2.0 over stdin/stdout, spawned by the MCP client; see [`interfaces/mcp-server.md`](../interfaces/mcp-server.md) |

All one-shot commands use `_build_runtime(run_recovery=False)` (`cli/main.py:103`) —
recovery is opt-in per command (an operator decision, `coder/agent.py:134`);
`serve` also does not run recovery at build (daemon does it at start, `daemon.py:91`).

### `paa agent` (`coder.py`)
`--mode ask|plan|accept_edits|bypass`, `--yes` (auto-approve), `--max-steps`,
`--allow`/`--deny`, `--resume`, `--list-tools`. Confirm callback: `always_yes`
under `--yes`, else `prompt_yes` (blocks on `input`; EOF → deny)
(`cli/coder.py:53`–`:172`).

## Runtime behavior

- `submit` default `run=True` (`:82`).
- `serve` binds loopback only; `api_host` validator enforced at `Settings`
  construction (`config.py:901`).
- Every command closes the `Runtime` in `finally` (`cli/main.py:161`, `:161`, etc.).

## Failure behavior

- Invalid correlation id → `typer.Exit(1)` (`cli/main.py:550`).
- Task not awaiting gate → `PaaError` → printed red, exit 1 (`:228`).
- `paa agent` `PaaError` → `[red]error:[/red]`, exit 1 (`cli/coder.py:194`).
- Unknown mode → printed red, exit 1 (`cli/main.py:521`, `cli/coder.py:100`).

## Modification impact

- Adding a CLI command: add a `@app.command()` in `main.py` (or register in
  `coder.py` for `agent`); each builds `Runtime` via `_build_runtime`.
- Tests: CLI exercised via `tests/cli/` and `tests/api/` (27 tests API per
  README).

## Known gaps

- Batch commands (`paa submit`/`paa run`) print phase lines rather than
  streaming tokens; the chat surface streams reply deltas (`paa chat`).
- `paa doctor` isolation level reflects the backend's own declaration
  (`isolation_level`, never aspirational — `sandbox/base.py:405`).

## Related documents

- [`architecture/overview.md`](../architecture/overview.md) — entry points.
- [`backend/overview.md`](../backend/overview.md) — CLI command table.
- [`modules/coder.md`](coder.md) — `paa agent`.
- [`interfaces/mcp-server.md`](../interfaces/mcp-server.md) — `paa mcp serve` spawn config and tools.
- [`operations/deployment-and-runtime.md`](../operations/deployment-and-runtime.md) — `paa serve`.
