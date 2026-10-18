# The coder agent — `paa agent`

`src/paa/coder/` was a standalone Claude-Code-shaped subsystem: permissions,
hooks, a tool-calling loop, transcripts. It knew nothing about this runtime.
`coder/agent.py` is what wires it to one — the piece that makes the directory
runnable.

Everything below is composition, not new machinery. A real
`Runtime` is built, the model sees that runtime's ledger through
`coder/runtime_tools.py`, and every call is gated by
`coder/permissions.py`. There is no hidden policy: the CLI flags map one-to-one
onto `ToolPermissionPolicy` arguments.

## The four new pieces

| File | What it is |
|---|---|
| `coder/model_adapter.py` | `RouterToolCallingModel` — adapts `EscalatingModelRouter` to the `ToolCallingModel` protocol the loop codes against. Also `turn_schema()` and the system prompt. |
| `coder/runtime_tools.py` | `build_runtime_tools(rt)` — the seven tools below. |
| `coder/agent.py` | `CoderAgent.build()` — the composition root. |
| `cli/coder.py` | `paa agent` — typer command, confirm callbacks, `--list-tools`. |

The pieces stayed decoupled on purpose. `coder/loop.py`, `permissions.py`,
`hooks.py` and `session.py` were untouched apart from exporting
`DEFAULT_MUTATING_TOOLS` and accepting a `mutating_tools` argument; everything
new is additive.

## The model seam is constrained JSON, not native tool calls

`ModelProvider` exposes `complete_structured(prompt, schema, **kwargs)` and
nothing in `models/` has a `tools` wire field. Rather than bolt one on, the
adapter asks the router for an object shaped like:

```json
{"text": "...", "tool_calls": [{"name": "check_runtime", "arguments": {}}]}
```

with tool names in an `enum` of the currently-registered tools. The provider's
existing validate-and-repair loop is the safety net. Adding native tool calling
would be an optimisation *inside that one class* and changes nothing else.

Two consequences worth knowing:

- **`ModelTurn.tokens` is always 0 on this path** — `complete_structured`
  returns a validated dict and drops usage numbers. `AgentLoop(max_tokens=...)`
  therefore never trips. Bound runs with `max_steps`, which the CLI does.
- **Reasoning models need token headroom.** `CompletionRequest.max_tokens`
  defaults to 1024, and a reasoning model spends its budget on `reasoning`
  before emitting any `content`. `CoderAgent.build` passes
  `model_max_tokens=2048` by default; raise it for long answers.

## The tools

| Tool | Mutates? | What it does |
|---|---|---|
| `check_runtime` | no | Which backends, provider and sandbox are actually live; open task count |
| `list_tasks` | no | Non-terminal lineages, oldest first |
| `task_status` | no | One lineage's phase, plan progress, attempts, token spend, policy/awaiting reason — derived by replay, not cached |
| `read_ledger` | no | One lineage's event chain in replay order |
| `verify_ledger` | no | Hash-chain verification: digests recompute, `prev_hash` links, `state_version` dense and monotonic |
| `submit_task` | **yes** | Submit to the orchestrator, return the correlation id. Records, does not execute |
| `run_task` | **yes** | Drive a submitted task to a terminal state |

`submit_task` and `run_task` are the only mutating tools, and neither name
matches any of the conventional filesystem/shell globs in
`DEFAULT_MUTATING_TOOLS` (`read_*`, `list_*`, `ls`, `cat`, …). Left alone,
`--mode plan` would have silently exempted the two tools that actually change
state, which is backwards. `CoderAgent.build` therefore passes
`mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)`. **A new
mutating tool needs adding to that tuple or it becomes read-only by accident.**

## Permissions and confirmation

The coder has its own `PermissionMode` — lowercase `ask | accept_edits | plan |
bypass` — which is a **different enum** from the runtime's
`core.types.PermissionMode` (`AUTO | ASK | SUPERVISED | LOCKDOWN | SAFE |
HIGH_RISK`). Do not confuse them; they are both in scope on the same command
line (`--mode` takes the coder's, `PAA_POLICY__MODE` takes the runtime's).

With no `confirm` callback the loop **auto-denies** rather than auto-approving,
so an unattended run stops at the first gated call instead of quietly
proceeding. `--yes` installs `always_yes`, which prints what it approved so the
transcript shows it. An explicit deny rule wins even under `--mode bypass`.

## Configuration

Provider selection is entirely `PAA_MODELS__*`; the command names no vendor.
With `config.dotenv_value` (ADR-0020) the key can live in `.env`:

```bash
OPENROUTER_API_KEY=sk-or-...
PAA_MODELS__LOCAL_PROVIDER=openrouter
PAA_MODELS__LOCAL_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
PAA_MODELS__ESCALATION_PROVIDER=none
```

`LOCAL_PROVIDER` names the router's *first slot*, not a locality claim. The
`openrouter` registry row declares `tier = FRONTIER`, so `leaves_machine` in any
escalation record stays honest even though the slot holds a hosted model.

```bash
paa agent --list-tools                  # catalogue, spends no tokens
paa agent --mode plan --yes "which backends are live?"
```

## Testing

`tests/coder/` is offline and fast — no provider, no network. The pattern is
`CoderAgent.build(settings, model=ScriptedModel(...))`, which builds a *real*
runtime (real ledger, real policy, real tools) with only the model faked. That
is what makes the permission and tool assertions meaningful: the loop,
permissions, hooks and tools under test are the production ones.

`tests/integration/conftest.py::ScriptedModel` is the reference implementation —
it answers by schema shape and records every call, which is how the "no model
in the security loop" guarantee is asserted.

## Known limitations

- `--resume` restores a transcript but not in-flight tool state; a resumed run
  starts from the last recorded turn.
- The seven tools are read/submit/run shaped. There is no filesystem tool: this
  agent drives the *runtime*, not the working tree. That is deliberate — the
  workspace/validation layer is a separate surface with its own gates.
- Token accounting is not surfaced, for the reason in the seam section.
