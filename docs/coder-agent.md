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
| `coder/runtime_tools.py` | `build_runtime_tools(rt)` — the tools below. |
| `coder/agent.py` | `CoderAgent.build()` — the composition root. |
| `cli/coder.py` | `paa agent` — typer command, confirm callbacks, session flags (`--resume`, `--continue`), `--modality`, `--list-tools`. |

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
| `recall_facts` | no | Long-term memory lookup — facts the daemon extracted, not a transcript replay |
| `submit_task` | **yes** | Submit to the orchestrator, return the correlation id. Records, does not execute |
| `run_task` | **yes** | Drive a submitted task to a terminal state |
| `remember_fact` | **yes** | Write one assertion into long-term memory. Rule-based extraction, no model call |

`submit_task`, `run_task` and `remember_fact` are the mutating tools, and none
of those names matches any of the conventional filesystem/shell globs in
`DEFAULT_MUTATING_TOOLS` (`read_*`, `list_*`, `ls`, `cat`, …). Left alone,
`--mode plan` would have silently exempted the tools that actually change
state, which is backwards. `CoderAgent.build` therefore passes
`mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS)`. **A new
mutating tool needs adding to that tuple or it becomes read-only by accident.**

The read-only half of the catalogue is standing-allowed in ASK mode
(`READ_ONLY_TOOLS`), so a permission prompt fires only for a call that
*changes state*. Prompting for `check_runtime({})` is friction that trains an
operator to approve everything, including the calls that matter.

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

## Flags

| Flag | Default | What it does |
|---|---|---|
| `--mode` | `ask` | `ask \| plan \| accept_edits \| bypass` — the coder's `PermissionMode`, not the runtime's |
| `--yes` | off | Auto-approve every ASK, and print what was approved |
| `--max-steps` | `25` | Cap on think→act cycles. The real bound: `ModelTurn.tokens` is always 0, so the token budget never trips |
| `--allow` / `--deny` | — | Pre-allow / refuse a tool. Deny wins even under `--mode bypass` |
| `--resume <id>` | — | Resume a transcript by session id |
| `--continue` | off | Resume the most recent session instead of starting a new one |
| `--modality` | `STANDARD` | `SIMPLE\|STANDARD\|COMPLEX\|MAX` — compared against the escalation floor |
| `--list-tools` | off | Print the tool catalogue and exit without spending a token |

`--resume` and `--continue` are mutually exclusive: one names a session
explicitly, the other means "the newest one". Asking for both is an error, and
it is an error *before* the runtime is built.

## Sessions

A session is a JSON Lines transcript under `$PAA_HOME/sessions`, and it is the
agent's memory of a conversation. Every run ends by printing the session id
and both ways to pick that transcript up again:

```
session 5c26dc48101d4d039facb90aaab1033c  —  paa agent --resume 5c26dc48101d4d039facb90aaab1033c  |  paa agent --continue
```

`--continue` resolves to the transcript with the newest mtime
(`Session.latest_session`). It is *not* the last session id alphabetically:
ids are UUID hex, so the lexicographic order of `Session.list_sessions` is
random with respect to time. `--continue` with no transcript to continue is a
clean message and exit 1, not a traceback.

Without that line each invocation was a one-shot: a fresh session id, an empty
window, and no memory of the previous conversation.

## Escalation and `--modality`

The agent's model calls go through the same escalation router as everything
else, and the router's floor is `PAA_MODELS__ESCALATE_AT_OR_ABOVE` (default
`COMPLEX`). The agent used to ask for `STANDARD` every time, so escalation
could never fire from this command and the only visible symptom was an
unreadable "escalation is not permitted" blob. `--modality` is the lever:

```bash
paa agent --modality COMPLEX "plan the migration"
```

**Raising the modality only matters if a frontier provider is configured.**
With `PAA_MODELS__ESCALATION_PROVIDER=none` there is nowhere to escalate *to*,
and the error says so (`escalation unavailable: no_frontier_configured`).
`--modality` below the floor changes nothing except that the failure becomes
explicit.

The failure is now rendered as a diagnosis rather than a dict dump:

```
error: local provider failed and escalation is not permitted
  • escalation blocked: modality_below_escalation_floor:COMPLEX
  • escalation unavailable: no_frontier_configured
  • provider failure: model endpoint returned HTTP 429 (ModelUnavailableError)
  • provider status: 429
  • provider: openrouter
  • modality STANDARD (floor COMPLEX), permission mode ASK
```

Every rule that blocked the escalation is listed, not just the first one: an
operator told only "below the floor" fixes the floor, re-runs, and meets the
permission-mode rule as if it were a new bug.

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
paa agent --continue "what did we just decide?"
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

The CLI surface is tested in two places, split by what the test needs:
`tests/coder/test_agent.py` holds the validations that must fire *before*
anything is built (a bad `--mode`, `--modality`, or `--resume`/`--continue`
combination), and `tests/cli/test_cli.py` holds the ones that load settings and
run the agent for real — session continuation, the transcript growing, and the
escalation-failure rendering.

## Known limitations

- `--resume` restores a transcript but not in-flight tool state; a resumed run
  starts from the last recorded turn.
- The tool catalogue is read/submit/run/memory shaped. There is no filesystem
  tool: this agent drives the *runtime*, not the working tree. That is
  deliberate — the workspace/validation layer is a separate surface with its
  own gates.
- Token accounting is not surfaced, for the reason in the seam section.
