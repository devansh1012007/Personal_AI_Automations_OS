# MCP server (`paa mcp serve`)

**Status: built (SPEC 2 §3.1 C4.5).** `paa mcp serve` runs PAA's memory and
chat as six MCP tools over stdio: JSON-RPC 2.0, one NDJSON object per line,
over stdin/stdout. It is designed to be *spawned by* an MCP client (Claude
Code, Codex, Cursor, OpenCode) rather than listened-with — there is no socket,
no bind address, and nothing for the loopback rule to widen. The server lives
in `src/paa/mcp/` (`server.py`, `protocol.py`); the command entry is
`src/paa/cli/mcp.py:120`.

Every tool call is policy-checked (`manifest ∩ mandate ∩ policy`) and
journaled before it executes; refusals are ledger events too. This is the
same boundary every other PAA effect crosses.

## Spawn configuration

Any MCP client that speaks stdio needs exactly three keys: the command, the
args, and (recommended) a token variable in the environment it hands the
child:

```json
{
  "mcpServers": {
    "paa": {
      "command": "paa",
      "args": ["mcp", "serve"],
      "env": { "PAA_MCP_TOKEN": "generate-a-long-random-value" }
    }
  }
}
```

`env` is merged into the child's environment by the harness; only
`PAA_MCP_TOKEN` is PAA-specific (any extra keys pass through untouched).

## Hooking Claude Code

Claude Code registers a stdio MCP server with its own CLI — the spawn block
above in the client's own syntax:

```bash
claude mcp add paa -- paa mcp serve
```

The one-liner carries command and args; the JSON form — `.mcp.json` at a
project root, or the `mcpServers` key of `~/.claude.json` — is the same
block, and is what you reach for when the token env should ride the spawn:

```json
{
  "mcpServers": {
    "paa": {
      "command": "paa",
      "args": ["mcp", "serve"],
      "env": { "PAA_MCP_TOKEN": "generate-a-long-random-value" }
    }
  }
}
```

Token guidance is unchanged by the choice of client: `PAA_MCP_TOKEN` rides
the `env` block and is merged into the child's environment; the *name* is
configurable per "Token auth" below; the value never enters the ledger.
Codex, Cursor, and OpenCode each have their own registration surface, and
this doc does not invent their flags — what every one of them needs is the
same three keys (command, args, env), so the spawn block above is the
portable contract and each client's MCP documentation says where to put it.

## Token auth

- `PAA_MCP__TOKEN_ENV` names the environment variable that holds the token —
  **the name, never the value** (the same indirection every `*_env` setting
  follows; see `MCPSettings` in `docs/operations/configuration-and-environment.md`).
  The default name is `PAA_MCP_TOKEN`; `paa mcp serve --token-env <NAME>`
  overrides it for one invocation.
- When the named variable holds a non-empty value at startup, every client
  must present that token in its `initialize` params (`params.token`) before
  any other method is served. A wrong or missing token is refused with JSON-RPC
  `-32009` (`AUTH_REQUIRED`), and the refusal is journaled without ever
  carrying the value.
- When the variable is unset, the spawn pipe is the trust boundary: the client
  that spawned the server is its only writer. `paa doctor` reports this row
  (`mcp token env`) — set/unset by name only, with the unset case naming the
  boundary (`src/paa/cli/main.py:301`).

The token value never enters the ledger, and no tool payload carries a
`token` key.

## The six tools (contract summary)

| Tool | Input → output | Policy class | Journaled as |
|---|---|---|---|
| `memory_query` | `{fact_id \| alias}` → fact + supersession lineage | read | `MEMORY_RECALL_CONSULTED` (capability `recall`) |
| `memory_search` | `{query, limit}` → ranked facts | read | same |
| `memory_remember` | `{statement, provenance}` → signal id (session-scoped) | write | `MEMORY_SIGNAL_EXTRACTED` via the MemoryCreator |
| `context_bundle` | `{session_id?, query}` → packet + provenance | read | `MEMORY_RECALL_CONSULTED` |
| `chat_send` | `{session_id?, message}` → reply (non-streaming aggregate) | write + model | full chat-turn event set (through ChatService) |
| `sessions_list` | — → session summaries | read | — (the tool-call audit rows still apply) |

Two journaling facts worth knowing exactly (from `src/paa/mcp/server.py`'s
contract header):

1. Every tool call is *also* journaled as a tool call — the coder's
   `TOOL_CALL_PROPOSED` → `TOOL_CALL_COMPLETED` / `TOOL_CALL_FAILED` /
   `TOOL_CALL_DENIED` family — on a per-call lineage with
   `isolation_level=NONE`. For the write tools the PROPOSED row precedes the
   effect, so a crash mid-call is closed as
   `FAILED(reason=interrupted)` by the boot sweep.
2. The memory/chat rows land on the **chat session lineage**, never on the
   tool-call lineage: a lineage that mixes chat vocabulary with tool-call
   vocabulary would defeat both recovery exemptions.

`memory_remember` without a session is refused: it is session-scoped by
contract — pass `session_id` or configure `PAA_MCP__DEFAULT_SESSION`
(`MCPSettings.default_session`).

## Errors

Error codes follow the pipeline spec's JSON-RPC table so one vocabulary serves
both protocols (`src/paa/mcp/protocol.py`): `-32601` unknown method, `-32602`
invalid params, `-32001` budget exceeded, `-32002` permission denied,
`-32006` capability not granted, `-32007` invalid call params, `-32009`
auth required (call before the handshake, or a token mismatch).

## Logs, secrets, and stdout discipline

The command writes protocol frames to stdout and nothing else — every log
line stays off stdout or it corrupts the wire. Tool arguments are attested by
hash with shape-only redacted previews (the N2.5 canary discipline), so a
harness that says "my api key is …" into `chat_send` leaks nothing into the
ledger; the verbatim words live in the cold lake.

## Manual smoke: a harness reads a fact written via `paa chat`

The manual smoke (SPEC 2 §3.4 G-MVP item 3; echo model, fully offline):

```bash
# 1. Write through the chat surface. The turn extracts the fact INLINE: the
#    turn's remember() runs the MemoryCreator inside the turn and writes the
#    MEMORY_SIGNAL_EXTRACTED row itself, so the fact is queryable the moment
#    the turn ends:
paa chat --plain
you: remember that deploys freeze on Fridays
# leave with Ctrl+D

# 2. `paa memory drain` is NOT the materialisation step for chat-written
#    facts — step 1 already extracted them. drain runs one MemoryCreator
#    batch over unprocessed cold-lake signals; after a chat turn it claims
#    nothing (a no-op sweep — worth running as a belt-and-braces check over
#    other channels, expected to report zero claimed):
paa memory drain

# 3. Configure your harness with the spawn block above (or set
#    PAA_MCP_TOKEN in the harness env) and start it. From the harness, call:
#      memory_search { "query": "deploys freeze", "limit": 5 }
#    Expected: the fact, with its originating chat session in the provenance.

# 4. The trail is on the ledger: the consult row lands on the chat session
#    lineage, the tool-call row on its own per-call lineage (see the
#    journaling notes above). Confirm the fact landed:
paa memory facts
```

The scripted form of this smoke is `python scripts/mcp_stdio_smoke.py` —
the executable proof, driving the *command* the way a real MCP client does
rather than the in-process suite: spawn `paa mcp serve` as a child with
pipes over a throwaway `PAA_HOME`, then `initialize` → `tools/list`
(asserting the catalogue carries `memory_remember`, `memory_search`, and
`sessions_list`) → `sessions_list` → `chat_send` ("remember that deploys
freeze on Fridays", echo model) → `memory_remember` against that session →
`memory_search`, which must return the fact written this run. Offline by
design — the echo model, no network, no key — and it fails loudly on any
non-JSON line on stdout, the stdout-pollution failure mode it exists to
catch. Exit 0 with a `SMOKE OK` line on success; a non-zero exit names the
failing step; the server's stderr tail is printed whenever the child wrote
any.

## Related documents

- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_MCP__*` and `PAA_CHAT__*` settings.
- [`interfaces/integrations-and-communication.md`](integrations-and-communication.md) — the boundary inventory this server extends.
- [`modules/cli.md`](../modules/cli.md) — the CLI command table.
- [`docs/plans/2026-11-21-next-spec-2.md`](../plans/2026-11-21-next-spec-2.md) — §3.3 (the contract this implements) and §3.4 (the gate this smoke satisfies).
