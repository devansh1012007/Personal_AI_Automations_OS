"""ACP v1 — PAA as the agent behind editors, and as the mediator in front of
external agents (SPEC 2 Wave 7, C7.6).

Two fronts, one framing kit (:mod:`paa.mcp.protocol` — the same codebase the
MCP server, the MCP client and the recorder proxy speak):

* :mod:`paa.acp.server` — the **T1** front: an Agent Client Protocol *server*
  over stdio, so an editor (Zed, or any ACP client) spawns PAA as its agent
  and drives PAA's chat loop with streaming output, working cancel and a
  scoped ``fs/read_text_file``.
* :mod:`paa.acp.client` — the **T2** front: an ACP *client* adapter that
  spawns an external ACP agent behind a
  :class:`~paa.sandbox.base.SandboxSession` and mediates its permission asks
  through PAA's deterministic tool policy, exactly the way the recorder's MCP
  proxy mediates a harness's tool calls (C7.3).

The v1-minimal scope, the shared-framing decision, the security posture and
the honesty residuals are recorded in ADR-0053
(docs/adr/0053-acp-v1-minimal.md).
"""
