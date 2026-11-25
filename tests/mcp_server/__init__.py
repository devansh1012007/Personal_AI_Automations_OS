"""Tests for the MCP server surface (SPEC 2 §3.1 C4.5).

Protocol conformance (``test_protocol.py``) and the six tools end to end on
the offline echo-model path (``test_tools.py``). No network, no MCP SDK, no
subprocess: the server is driven in-process through
:meth:`paa.mcp.server.MCPServer.handle_message` and, for the framing round
trip, through :meth:`MCPServer.serve` over an in-memory stream pair — the
same shape the client adapter's tests take (a scripted peer), inverted.
"""

from paa.mcp.server import MCPServer  # re-exported for import convenience

__all__ = ["MCPServer"]
