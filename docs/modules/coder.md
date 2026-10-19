# Module: Coder agent (`src/paa/coder/`)

## Module identity

- **Name:** Coder agent (`paa agent`)
- **Path:** `src/paa/coder/`
- **Main responsibility:** A tool-using agent loop over the `Runtime`, gated by permissions and hooks, driven by a model over a constrained-JSON seam.
- **Architectural layer:** Control / edge
- **Entrypoints:** `CoderAgent.build()`, `CoderAgent.ask(prompt)`, `AgentLoop.run()`
- **Public interfaces:** `CoderAgent`, `MUTATING_RUNTIME_TOOLS` (`agent.py`); `AgentLoop`, `ToolCallingModel`, `Tool`, `LoopResult`, `ModelTurn`, `ToolCall` (`loop.py`); `RouterToolCallingModel` (`model_adapter.py`); `ToolPermissionPolicy`, `DEFAULT_MUTATING_TOOLS`, `PermissionMode` (`permissions.py`); `HookRegistry`, `HookEvent` (`hooks.py`); `Session`, `TurnRole` (`session.py`); `CommandRegistry`, `default_registry` (`commands.py`); `build_runtime_tools` (`runtime_tools.py`)
- **Other modules that depend on it:** `cli/coder.py` (`paa agent` command), `runtime.py` (builds Runtime coder uses)

## Purpose and responsibilities

`paa agent` is the one command that assembles `paa.coder` into something
runnable: build a real `Runtime`, let the model see the runtime's ledger through
`coder.runtime_tools`, and gate every call through the permission policy
(`cli/coder.py` module docstring).

The loop is think → act → observe under permissions & hooks:
1. Hook PRE_TOOL_USE (may modify args or block).
2. Permission check (deny authoritative).
3. Human prompt on ASK (injected callback).
4. Execute tool.
5. Hook POST_TOOL_USE (`loop.py:14`).

The model seam is constrained JSON, not native tool calls: `RouterToolCallingModel`
asks the router for a `{text, tool_calls[]}` object conforming to a schema with
tool names in an enum (`model_adapter.py`).

`ModelTurn.tokens` is always 0 on this path — `max_tokens` never trips;
`max_steps` bounds runs (`AGENTS.md:160`).

## Internal implementation

### `agent.py` — CoderAgent
`CoderAgent.build()` composes `Runtime.build()` + `RouterToolCallingModel` +
`AgentLoop` + `ToolPermissionPolicy` + `HookRegistry` + `Session` (`:101`).
`MUTATING_RUNTIME_TOOLS = ("submit_task", "run_task")` — these don't match
`DEFAULT_MUTATING_TOOLS` globs, so they're added explicitly to keep `--mode plan`
read-only (`:64`, ADR-0011 context).
`mode` default `ASK`; no `confirm` callback → auto-*deny* (unattended run stops
at first gated call) (`:134`).

### `loop.py` — AgentLoop
`run(user_message)` drives up to `max_steps` (default 25): model proposes,
text recorded, tool calls handled through `_handle_call` (the safety gate),
then returns on `model_finished` / `token_budget_exhausted` / `max_steps_reached`
(`:142`). Denied calls feed an explanatory tool result back to the model
(`:27`).

### `model_adapter.py` — RouterToolCallingModel
Implements `ToolCallingModel.propose(window, tools)`: serialises offered tools
as JSON, builds `CompletionRequest`, calls `EscalatingModelRouter.complete_structured`
with `turn_schema()` (tool_calls[].name enum restricted to offered names),
parses back into `ModelTurn` (`:161`). Tokens always 0 (`:32`–`:38`).

### `permissions.py` — ToolPermissionPolicy
Precedence: explicit deny (always wins, even under BYPASS) → explicit allow →
explicit ask → mode default (`:172`). `PLAN` mode refuses all mutating tools
(`:186`). `_MUTATING_TOOLS` globs: `write*`, `edit*`, `bash*`, `shell*`,
`apply_patch*`, `*delete*`, `*write*`, `send_*`, `*_commit*` (`:127`).

### `hooks.py` — HookRegistry
`HookEvent`: SESSION_START/END, USER_PROMPT_SUBMIT, PRE_TOOL_USE,
POST_TOOL_USE, SUBAGENT_STOP, STOP (`:39`). First block wins and short-circuits;
hooks that raise are logged and treated as non-blocks (`:95`).

### `session.py` — Session
Append-only JSONL transcript; resume = replay; compaction replaces in-memory
window with SUMMARY turn, on-disk intact (`:84`). `TurnRole`: USER/ASSISTANT/
TOOL_CALL/TOOL_RESULT/SYSTEM/SUMMARY (`:32`).

### `runtime_tools.py` — build_runtime_tools
Seven ledger-facing tools: `check_runtime`, `list_tasks`, `task_status`,
`submit_task`, `run_task`, `read_ledger`, `verify_ledger` (`:47`). They read
the ledger projection (not derived caches) and return JSON with errors as values.

### `commands.py` — slash commands
Built-ins: `/help`, `/clear`, `/model`, `/mode`, `/compact`, `/cost` (`:184`–`:189`).
Custom commands from `.md` files with `$ARGUMENTS`/`$1`..`$9` substitution.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `CoderAgent.ask(prompt)` | prompt string | `LoopResult` | may call submit_task/run_task → writes ledger |
| `AgentLoop.run(user_message)` | user message | `LoopResult` | records session turns; calls tools |
| `ToolPermissionPolicy.evaluate(tool, args)` | tool name + args | `PermissionDecision` | none |
| `build_runtime_tools(rt)` | `Runtime` | dict of tools | none |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `CoderAgent` → `Runtime.build()` | construct | Real runtime to drive | `agent.py:152` |
| `CoderAgent` → `RouterToolCallingModel` | `propose()` | Model seam | `agent.py:174` |
| `RouterToolCallingModel` → `EscalatingModelRouter` | `complete_structured()` | Inference + escalation | `model_adapter.py:176` |
| `AgentLoop` → `ToolPermissionPolicy` | `evaluate()` | Gate every call | `loop.py:224` |
| `AgentLoop` → `HookRegistry` | `fire()` | Hooks pre/post tool | `loop.py:217`, `:246` |
| `AgentLoop` → `Session` | record turns | Transcript persistence | `loop.py:144` |
| runtime tools → `Runtime.submit/run` | drive tasks | Ledger writes | `runtime_tools.py:220`, `:259` |
| `cli/coder.py` → `CoderAgent.build` | compose | `paa agent` CLI | `cli/coder.py:103` |

## Runtime behavior

- `confirm` callback absent → auto-deny (`loop.py:138`).
- `max_tokens` never trips (tokens always 0); `max_steps` is the bound
  (`AGENTS.md:160`, `loop.py:181`).
- `--mode plan` read-only: `MUTATING_RUNTIME_TOOLS` included in mutating set
  (`agent.py:192`).

## Failure behavior

- Runtime built without a model router → `PaaError` raised loudly
  (`agent.py:159`).
- Hook raising → logged, treated as non-block (`hooks.py:95`).
- Tool execution exception → recorded as error result, loop continues
  (`loop.py:239`).
- `PaaError` from agent turn → reported cleanly at CLI (`cli/coder.py:132`).

## Modification impact

- Adding a mutating tool: add to `MUTATING_RUNTIME_TOOLS` or pass via
  `mutating_tools=` to `ToolPermissionPolicy`, otherwise `--mode plan`
  silently exempts it (`agent.py:192`, AGENTS.md coder section).
- Changing tool names: must match policy globs and the `turn_schema` enum
  (`model_adapter.py:79`).
- Tests: `tests/coder/` (91 tests per README).

## Known gaps

- Coder agent not wired into the orchestrator's main task path; no built-in
  file-edit/bash/web/git skills (`README.md:160`).
- `RouterToolCallingModel` is the only native-tool-calling-adjacent class;
  nothing in `models/` supports a `tools` wire field (`AGENTS.md:156`).

## Related documents

- [`docs/coder-agent.md`](../coder-agent.md) — full `paa agent` write-up.
- [`modules/agents.md`](agents.md) — base agent protocol.
- [`modules/models.md`](models.md) — escalation router.
- [`modules/storage.md`](storage.md) — runtime tools read ledger/storage.
- [`interfaces/integrations-and-communication.md`](../interfaces/integrations-and-communication.md) — provider config.
