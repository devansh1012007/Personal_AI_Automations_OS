# W7-IMPL-07 — C7.6: ACP v1, the server + mediated client

**Task:** SPEC 2 §6 row C7.6 (H2.1/H2.6) — minimal Agent Client Protocol
server (PAA behind editors: streaming, cancel, working directory) + ACP
client adapter (external agent mediated) on the shared duplex framing kit.
**Status:** landed as v1-minimal; the decision record is ADR-0053.

## What landed

* `src/paa/acp/protocol.py` — the ACP wire vocabulary on top of the
  shared framing kit (`paa.mcp.protocol` `read_frame`/`encode_frame` —
  one framing codebase, three fronts: MCP server, MCP client, ACP):
  `initialize` (protocol version pinned in the handshake),
  `session/new`, `session/prompt`, streaming `session/update`
  notifications, `session/cancel`, `fs/read_text_file`; JSON-RPC error
  envelopes with one vocabulary of reserved codes.
* `src/paa/acp/server.py` — the T1 front: an ACP server over stdio.
  An editor spawns PAA as its agent; prompts drive the chat service;
  turn output streams as ordered `session/update` chunks; cancel is
  wired to the loop's cancellation machinery (a real stop, not an ACK);
  teardown on EOF cleans sessions.
* `src/paa/acp/client.py` — the T2 front: spawns an external ACP agent
  behind a `SandboxSession` and mediates its permission asks through
  PAA's deterministic tool policy — the recorder-proxy pattern (C7.3)
  applied to ACP; unattended asks are recorded refusals.
* `src/paa/acp/fs.py` — `fs/read_text_file` scoped by the coder
  protected-path rules (`.git`, `.env*` refused), never a blind read.
* `src/paa/cli/acp.py` — `paa acp serve` and `paa acp client -- <cmd>`;
  registered in `cli/main.py`.
* `docs/adr/0053-acp-v1-minimal.md` — scope, shared-framing decision,
  security posture, honesty residuals; indexed in `docs/adr/README.md`.
* `tests/acp/` — 90 tests: framing round-trip through the shared kit,
  dispatch + error envelopes, handshake pinning, the **streaming
  acceptance** (a client drives initialize → session/new →
  session/prompt over real subprocess pipes and receives multiple
  `session/update` chunks before the stop), the **cancel acceptance**
  (in-flight turn cancelled: ACKed, chunks actually stop, session stays
  usable, children reaped), client mediation (policy-violating call
  refused + journalled; benign passes; unattended ask → recorded
  refusal), fs scoping.

## Design decisions

* **Reuse, do not fork, the framing.** ACP frames are the MCP kit's
  frames; the only ACP-owned vocabulary is the method table.
* **stdio only.** No listening socket, no token surface — the editor
  spawns the agent, so the trust model is the process pipe, same as
  `paa mcp serve`.

## Gates

`pytest tests/acp` 90 passed; `tests/chat tests/skills` seams green
(365 passed across the four areas with conventions); ruff 0 and mypy 0
on `src/paa/acp`, `src/paa/cli/acp.py`.

## Honest residuals

The wire surface is the repo's own minimal subset, pending validation
against the live agentclientprotocol.com spec (no web access in this
environment — recorded in ADR-0053). Streaming granularity is bounded by
the chat turn's shape. Zed itself is untested offline; the in-suite
client stands in for it. The H2.6 boundary measurement now has its
subject (the mediated client) but the measurement itself is the
operator's run, not this wave's artifact.
