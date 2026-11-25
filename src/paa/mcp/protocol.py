"""Wire vocabulary for the PAA MCP server: framing, JSON-RPC errors, versions.

C4.5 of SPEC 2 §3.1. This module is the hand-rolled protocol half of
``paa mcp serve`` — deliberately SDK-free (the plan's §10 drift mitigation:
the framing is already proven client-side in
:mod:`paa.skills.adapters.mcp`, and this server is its inversion, so the
two ends cannot drift apart without a test failing).

Protocol version
----------------
``2024-11-05`` — the exact version :data:`paa.skills.adapters.mcp._PROTOCOL_VERSION`
pins on the client side, so a loopback conversation between PAA's own client
adapter and this server is symmetric by construction. Per the MCP spec's
initialize contract, a client that requests a *different* version gets a
successful reply carrying THIS server's pinned version (the client then
decides to disconnect); a client that sends no version at all is refused as
invalid params, because "no version" is a malformed handshake, not a choice.

Framing
-------
**NDJSON is the write framing**: one compact JSON object per line, UTF-8,
``\\n`` terminated — matching the client adapter's ``write_framing="line"``
default, which is also what every harness config (Claude Code, Codex,
Cursor, OpenCode) speaks. On **read** the server autodetects per message
exactly as :class:`paa.skills.adapters.mcp.MCPClient` does: a line beginning
``Content-Length:`` switches that message to LSP-style header framing, and
anything else is treated as one whole-line JSON object — so a client
configured for either framing works, while everything this server emits is
NDJSON.

JSON-RPC error codes
--------------------
The standard reserved codes, then the pipeline platform's wire vocabulary
(the table frozen in :mod:`paa.pipelines.base` as ``wire_code`` attributes)
reused verbatim so one error vocabulary serves both protocols. ``-32009`` is
this server's one new code, in the server-defined range the JSON-RPC spec
reserves (``-32000`` .. ``-32099``), taking the first slot the pipeline table
leaves free.

======  =======================  ===============================================
Code    Name                     Raised by / mapped from
======  =======================  ===============================================
-32700  parse                    a frame was not valid JSON
-32600  request                  valid JSON but not a JSON-RPC request object
-32601  method                   unknown method (``initialize``, ``tools/list``,
                                 ``tools/call`` and ``ping`` are the surface)
-32602  params                   schema-violating params, an unknown tool name
                                 in ``tools/call``, or a session id that names
                                 no chat session
-32603  internal                 an unexpected exception (message redacted); a
                                 generic :class:`~paa.core.errors.PaaError` the
                                 table does not name also lands here
-32001  BUDGET_EXCEEDED          :class:`~paa.core.errors.BudgetExceededError`
-32002  PERMISSION_DENIED        :class:`~paa.core.errors.PermissionDeniedError`
                                 — the policy gate's refusal (journaled as
                                 ``TOOL_CALL_DENIED``, never silent)
-32006  CAPABILITY_NOT_GRANTED    :class:`~paa.pipelines.base.
                                 CapabilityNotGrantedError` — the tool is not in
                                 the granted capability set
-32007  INVALID_CALL_PARAMS      :class:`~paa.pipelines.base.
                                 InvalidCallParamsError`
-32009  AUTH_REQUIRED            unauthenticated request: a call before the
                                 ``initialize`` handshake completed, or a
                                 handshake whose token did not match. Refused
                                 BEFORE any tool executes and journaled
                                 (``POLICY_BLOCKED`` on a refusal lineage)
======  =======================  ===============================================

Tool *execution* failures are deliberately NOT JSON-RPC errors: per the MCP
convention, a dispatched tool that fails returns a result with
``isError=true`` (the request itself was valid); only protocol-level faults
(auth, policy, params, unknown method) take the error-object path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from typing import Any, Final, Protocol

from paa import __version__
from paa.core.errors import BudgetExceededError, PaaError, PermissionDeniedError
from paa.pipelines.base import (
    CapabilityNotGrantedError,
    GateRejectedError,
    InvalidCallParamsError,
)

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
    "FrameSink",
    "McpError",
    "encode_frame",
    "error_response",
    "read_frame",
    "result_response",
    "wire_code_for",
]

#: The MCP protocol revision this server speaks. Pinned to the same string the
#: client adapter pins, per the module docstring.
PROTOCOL_VERSION: Final = "2024-11-05"

SERVER_NAME: Final = "paa-mcp"
SERVER_VERSION: Final = __version__

# -- JSON-RPC 2.0 reserved codes -------------------------------------------

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

# -- Pipeline-aligned server codes (see the module docstring table) ---------

BUDGET_EXCEEDED: Final = -32001
PERMISSION_DENIED: Final = -32002
CAPABILITY_NOT_GRANTED: Final = -32006
INVALID_CALL_PARAMS: Final = -32007
#: PAA extension: unauthenticated request. First free slot after the pipeline
#: table's ``-32001..-32008`` run; within the JSON-RPC server-defined range.
AUTH_REQUIRED: Final = -32009

#: GateRejectedError's frozen wire code, kept in the map for completeness.
_GATE_REJECTED: Final = -32003


class McpError(PaaError):
    """A protocol-level fault with its JSON-RPC code attached.

    Raised by the server's request handlers;
    :meth:`paa.mcp.server.MCPServer.handle_message` renders it as the
    response's ``error`` object. Carrying the code on the exception (rather
    than mapping at each raise site) keeps every handler's error path uniform.
    """

    def __init__(self, code: int, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = code


def wire_code_for(exc: Exception) -> int:
    """The JSON-RPC code for a runtime exception, per the module table.

    Known pipeline vocabulary maps to its frozen ``wire_code``; everything
    else is an internal error with a redacted message — a PaaError subclass
    the table does not name is still a server fault worth surfacing as such,
    never a crash of the read loop.
    """
    if isinstance(exc, BudgetExceededError):
        return BUDGET_EXCEEDED
    if isinstance(exc, PermissionDeniedError):
        return PERMISSION_DENIED
    if isinstance(exc, CapabilityNotGrantedError):
        return CAPABILITY_NOT_GRANTED
    if isinstance(exc, InvalidCallParamsError):
        return INVALID_CALL_PARAMS
    if isinstance(exc, GateRejectedError):
        return _GATE_REJECTED
    return INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Framing (the client adapter's framing, inverted)
# ---------------------------------------------------------------------------


class FrameSink(Protocol):
    """Where encoded frames go: a :class:`asyncio.StreamWriter`-shaped hole.

    Structural on purpose — the stdio path passes a real
    :class:`asyncio.StreamWriter`, and the tests pass an in-memory recorder
    without a transport.
    """

    def write(self, data: bytes) -> None: ...

    def drain(self) -> Awaitable[None]: ...


def result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """A JSON-RPC 2.0 success reply, echoing the request's id verbatim."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A JSON-RPC 2.0 error reply. ``data`` carries structured context only.

    Callers must never place a secret in ``data`` — the ledger's redaction
    rules apply to this surface too, and an error object is a ledger-adjacent
    payload (the refusal is usually journaled beside it).
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def encode_frame(message: dict[str, Any]) -> bytes:
    """One NDJSON frame: compact JSON, UTF-8, newline-terminated.

    This is the server's write framing — always NDJSON, per the module
    docstring (it matches the client adapter's ``write_framing="line"``
    default, so a loopback conversation is symmetric without negotiation).
    """
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    return (line + "\n").encode("utf-8")


async def read_frame(reader: asyncio.StreamReader) -> str | None:
    """Read one framed message from the stdio peer; ``None`` at end of stream.

    Per-message framing autodetect, mirroring the client adapter's read loop:
    a line beginning ``Content-Length:`` switches to header framing (drain the
    remaining headers to the blank separator line, then read exactly that many
    bytes); anything else is one NDJSON line. Blank lines are skipped, so a
    peer that terminates lines over-eagerly cannot inject empty requests.
    """
    while True:
        line = await reader.readline()
        if not line:
            return None  # EOF: the client closed the pipe (cleanly or by crash)
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if text.lower().startswith("content-length:"):
            return await _read_content_length(reader, text)
        return text


async def _read_content_length(
    reader: asyncio.StreamReader, first_header: str
) -> str | None:
    """Consume an LSP-style header block and return the body text."""
    try:
        length = int(first_header.split(":", 1)[1].strip())
    except (IndexError, ValueError):
        # A malformed length cannot be honoured; skip the block the same way
        # the client adapter does rather than killing the read loop.
        return ""
    while True:
        extra = await reader.readline()
        if not extra or extra.strip() == b"":
            break
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None  # truncated body == the stream died mid-frame
    return body.decode("utf-8", errors="replace")
