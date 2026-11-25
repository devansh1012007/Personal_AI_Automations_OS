# ADR-0053: ACP v1 minimal — the server and the mediated client on the shared framing kit

## Status

Accepted

*Recorded 2026-11-25 by Wave 7 task C7.6 (SPEC 2 §6, the last Wave 7 row)
over the Wave 7 tree. It stands on ADR-0045 (the frozen coder Workspace API
whose jail-then-protect order the file answers reuses), ADR-0046 (the
tool-call journal family the mediated client writes), and the framing kit
decision the MCP surfaces already follow. ADR-0047 is unaffected; this
record covers a different wire.*

## Context

SPEC 2 §6 C7.6 pulls ROADMAP H2.1 and H2.6 forward into Wave 7: **ACP v1**,
honesty-fixed. Two fronts are asked for, and they pull in opposite
directions. The **server** front (PAA as the agent behind an editor — Zed is
the reference ACP client) is reach: the chat loop becomes drivable from a
real editor without a new trust model. The **client** front (PAA spawns an
*external* ACP agent and mediates it) is the H2.6 subject: the boundary
measurement finally has something to measure, because an external agent's
permission asks and file reads cross a wire PAA owns.

The protocol itself is the risk. ACP is a third wire in this tree (after
MCP's two directions), it moves faster than this repository, and this
environment has **no web access** — the live agentclientprotocol.com spec
could not be consulted while implementing. A full-fidelity ACP
implementation written from memory would be a claim this tree cannot back:
every field name, every error code, every notification shape would be
asserted without a reference to check against. And a third framing
implementation (a third encoder, a third reader, a third result/error
envelope) would be the drift the SPEC 2 §10 mitigation exists to prevent —
the recorder proxy already learned this lesson the cheap way, by importing
the MCP kit verbatim.

The same security question the MCP server answered (ADR-0047's neighbours)
returns here with a sharper edge: an editor spawns `paa acp serve` and hands
it a `cwd`; an external agent spawned by `paa acp client` asks its client to
read files. Both fronts answer file-read requests, and both must refuse the
same paths the coder's own tools refuse, or the ACP surface becomes a
sideways door around the protected-path rules.

## Decision

1. **v1-minimal, stated on the wire.** The implemented subset is
   `initialize`, `session/new`, `session/prompt`, the `session/cancel`
   notification, `session/update` streaming (v1 emits
   `agent_message_chunk` only), and `fs/read_text_file`. Everything else
   answers `-32601` with the honest "not in this build" message; unknown
   notifications are silence; a request before the handshake is `-32600`.
   The version is pinned in the handshake: `protocolVersion` is the ACP
   **major integer** (not MCP's date string), the reply always carries this
   server's pinned version (ACP's negotiation contract — the client decides
   about a mismatch), a missing or non-integer version is malformed params,
   and the client front treats an agent that answers a different major as
   fatal at wiring time. Capabilities are declared exactly: no
   `loadSession`, no richer prompt content, no MCP connections on the
   editor's behalf (`mcpServers` is validated for shape and connected to
   nothing).

2. **One framing kit, three fronts.** `src/paa/acp/protocol.py` is the wire
   *vocabulary* (methods, updates, stop reasons, ACP's own error codes) —
   the framing is **imported, not reimplemented**: `encode_frame`,
   `read_frame`, `result_response` and `error_response` come from
   `src/paa/mcp/protocol.py`, the same codebase the MCP server, the MCP
   client and the recorder proxy speak. The JSON-RPC 2.0 reserved codes are
   the kit's own objects (imports, not same-valued copies), so the surfaces
   cannot drift without an import error. ACP's own additions that *collide*
   with PAA's MCP table in meaning (`-32000` authentication required,
   `-32002` resource not found) are defined here with ACP's names rather
   than reused — no module ever sees both meanings at once, because the
   surfaces are separate processes over separate pipes.

3. **Stdio only, no listening socket.** Both fronts speak
   newline-delimited JSON-RPC over the pipes their parent gave them; the
   server never opens a port, so the attack surface of "running the ACP
   server" is the process boundary, not the network. The discipline is the
   MCP server's own: protocol frames on stdout and nothing else,
   diagnostics on stderr. There is no auth surface (`authMethods: []`) —
   there is nothing to authenticate to over an inherited pipe.

4. **File reads answer through the coder's own rules.** One helper
   (`src/paa/acp/fs.py`) serves both fronts: jail the path against the
   workspace root, *then* judge the canonical path against the protected
   rules (top-level `.git`, any-depth `.env*`, `PAA_HOME`) — ADR-0045's
   frozen order, reused rather than restated. Absolute paths only, the read
   capped at the coder tools' own byte cap with truncation marked
   (`_meta.truncated`, never silent), binary content refused. One
   deliberate inversion: in ACP proper `fs/read_text_file` runs agent →
   client; the v1 *server* also answers it, from the session's own scoped
   workspace — the read is jailed either way, and an editor that did not
   offer the capability gets a scoped answer rather than a dead method.

5. **The client front mediates, journals, and fails closed.** The external
   agent runs behind a `SandboxSession` (whole-tree kill on close, the C5.2
   duplex contract) with a **required** workspace that is its cwd jail and
   the root of its file answers — one root, both halves of the containment.
   Permission asks are decided by the deterministic
   `ToolPermissionPolicy` — the same policy the coder loop and the
   recorder's proxy use, never a model, never a human on a pipe that has
   none. An unattended `ask` resolves to a **denial** recorded as
   `denied_unattended` with the raw decision kept; an allow selects the
   agent's own allow option, a deny its reject option; when the agent
   offered no option of the needed kind the adapter fails closed (rejection,
   or the `cancelled` outcome when not even that exists) — never an approval
   the policy did not grant. A call with no tool name is denied without a
   policy lookup. Everything mediated lands in the ledger through the
   ADR-0046 tool-call family (`TOOL_CALL_PROPOSED` → `DENIED`/`COMPLETED`)
   on a per-call lineage. What is deliberately *not* journalled: the agent's
   streamed transcript — observing a full flight is the recorder's job
   (C7.3); this front attests the boundary it is accountable for.

6. **Cancellation is real, and the ledger says what the wire did.** A prompt
   turn runs as its own task so the read loop stays free to receive
   `session/cancel` mid-turn; a cancel stops work (the chat loop journals
   `CHAT_TURN_CANCELLED`, shielded) and the prompt request is answered with
   the `cancelled` stop reason. A failed turn maps to `refusal` — the
   closest honest member of ACP's closed stop-reason set. The ACP session
   id *is* the chat session id, so ledger lineage and wire session cannot
   disagree; EOF cancels in-flight turns and ends the sessions with
   `stopped_reason="acp-eof"`. Prompt content is text blocks only — the
   ACP baseline's `resource_link` is refused as a stated v1 deviation (a
   content type this front cannot honour must fail the request, not vanish
   from the conversation).

## Consequences

Costs: an editor that wants the unimplemented half of ACP (session load,
modes, elicitation, tool-call streaming, richer content) hears `-32601`
until v2; an external agent that leans on elicitation or terminal access is
unusable under the mediated client; and the loopback symmetry that pins the
wire (this server, this client adapter, plus a scripted fake agent over
real pipes) is self-referential by construction — the strongest evidence
this tree can produce offline, and it is not third-party evidence.

The honesty line, stated rather than implied. The wire surface is **this
repository's own minimal subset, pending validation against the live
agentclientprotocol.com spec** — no web access existed in the implementing
environment, so field-level exactness is pinned by the loopback tests, not
by the reference implementation, and a conformance pass against the real
spec (and real agents) is a required follow-up before anyone calls this
"ACP" without the "v1-minimal" qualifier. Streaming granularity is bounded
by the ChatService turn shape: the `on_delta` seam carries reply text, so
`agent_message_chunk` is the only update v1 emits — tool calls and plan
entries would be claims about machinery this front does not drive. **Zed
itself is untested** — the done-when's "one ACP client drives a session
with streamed output and working cancel" is met by the loopback pair, not
by the editor that motivated the front. And the H2.6 boundary measurement
still needs what only a real run provides: an actual external agent, over
an actual repository, with the written share of its writes and commands
that were visible through the protocol versus only through the sandbox —
v1 journals the boundary (asks, reads) and leaves the agent's unmediated
workspace effects to the sandbox, which is exactly the gap that measurement
exists to quantify.

To move back: the scope widens method by method (each new method carries
its own tests and its own honesty about what the front actually drives),
but the two structural decisions — the shared framing kit and the jailed,
policy-mediated file answers — should not move at all; reverting either
reopens the drift and the sideways door this record exists to close.
