"""PAA's MCP server surface: PAA memory and chat as tools for external harnesses.

SPEC 2 §3.1 C4.5. :mod:`paa.mcp.protocol` is the hand-rolled wire vocabulary
(JSON-RPC 2.0, NDJSON framing, the pinned protocol version, the error-code
table aligned with the pipeline platform's); :mod:`paa.mcp.server` is the
server itself — six policy-checked, ledger-journaled tools over
:class:`~paa.chat.service.ChatService` and the memory repositories. The CLI
(``paa mcp serve``) lives in :mod:`paa.cli.mcp` and is the only thing that
builds a runtime and opens the stdio pipes.

Deliberately SDK-free: the framing and the handshake are this repo's own
implementation of the same protocol :mod:`paa.skills.adapters.mcp` speaks
as a *client* — the server is that framing inverted, so the loopback
conversation is symmetric and a conformance test can drive both ends
in-process. See the plan's §10 drift mitigation (hand-rolled NDJSON,
version pinned in the handshake, no SDK dependency).
"""

from paa.mcp.protocol import (
    AUTH_REQUIRED,
    BUDGET_EXCEEDED,
    CAPABILITY_NOT_GRANTED,
    INTERNAL_ERROR,
    INVALID_CALL_PARAMS,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PERMISSION_DENIED,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    McpError,
)
from paa.mcp.server import TOOL_MANIFEST, MCPServer, ToolSpec

__all__ = [
    "AUTH_REQUIRED",
    "BUDGET_EXCEEDED",
    "CAPABILITY_NOT_GRANTED",
    "INTERNAL_ERROR",
    "INVALID_CALL_PARAMS",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PERMISSION_DENIED",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOL_MANIFEST",
    "MCPServer",
    "McpError",
    "ToolSpec",
]
