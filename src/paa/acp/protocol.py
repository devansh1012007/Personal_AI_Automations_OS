"""Wire vocabulary for the ACP surfaces: methods, updates, errors, versions.

C7.6 of SPEC 2 §6. This module is to :mod:`paa.acp.server` and
:mod:`paa.acp.client` what :mod:`paa.mcp.protocol` is to the MCP server, the
MCP client and the recorder proxy: **the one place the wire vocabulary
lives**, so the two ACP fronts cannot drift apart without a test failing.

The framing itself is NOT here, on purpose. ACP's stdio transport is
newline-delimited JSON-RPC 2.0 — exactly the NDJSON write framing and the
per-message read autodetect :mod:`paa.mcp.protocol` already implements — so
this module *imports* :func:`~paa.mcp.protocol.encode_frame`,
:func:`~paa.mcp.protocol.read_frame`, :func:`~paa.mcp.protocol.result_response`
and :func:`~paa.mcp.protocol.error_response` and re-exports them. One framing
codebase, three fronts (MCP, pipeline, ACP) — the SPEC 2 §10 mitigation, and
the rule the recorder proxy already follows.

Protocol version
----------------
``1`` — the ACP **major** version integer (``protocolVersion`` in the
handshake is a number, not MCP's date string). ACP's negotiation contract: a
client that requests a different major version still gets a *successful*
reply carrying this server's pinned version, and the client decides whether
to disconnect. A handshake with no version at all is malformed params.

The v1-minimal subset
---------------------
Implemented here (everything else answers ``-32601``, the honest "not in
this build" rather than a guess):

======================  ===========  ==========================================
Method                  Direction    v1 behaviour
======================  ===========  ==========================================
``initialize``          client→agent version + capability exchange
``session/new``         client→agent create a chat session bound to ``cwd``
``session/prompt``      client→agent one turn; updates stream first, the
                                     response (with its ``stopReason``) lands
                                     when the turn ends
``session/cancel``      client→agent notification; cancels the in-flight
                                     turn, which then answers ``cancelled``
``session/update``      agent→client streamed notifications (v1 emits
                                     ``agent_message_chunk`` only)
``fs/read_text_file``   agent→client v1 serves it *from the session's own
                                     scoped workspace* (see ADR-0053 for the
                                     deliberate direction note)
``session/request_``    agent→client client-front only: the mediated
``permission``                      permission ask (T2)
======================  ===========  ==========================================

Error codes
-----------
The JSON-RPC 2.0 reserved codes are shared with the MCP kit verbatim (same
numbers, one import). ACP's own additions differ from PAA's MCP table in
meaning for two numbers — ``-32000`` is *authentication required* (on the MCP
surface that slot is a PAA extension) and ``-32002`` is *resource not found*
(on the MCP surface it is the policy denial) — so they are defined here with
ACP's names rather than reused, and no module ever sees both meanings at
once: the surfaces are separate processes over separate pipes.

======  =======================  ===============================================
Code    Name                     Raised by
======  =======================  ===============================================
-32700  parse                    a frame was not valid JSON
-32600  invalid request          not a request object, or a request that
                                 arrived before the ``initialize`` handshake
-32601  method not found         a method outside the v1 subset above
-32602  invalid params           malformed params, a bad ``cwd``, a
                                 non-text content block, a protected or
                                 out-of-scope path in ``fs/read_text_file``
-32603  internal                 an unexpected exception (message redacted)
-32800  request cancelled        the JSON-RPC-level cancellation code (v1
                                 answers cancels with the ``cancelled`` stop
                                 reason instead; kept for completeness)
-32000  auth required            v1 never raises it (no auth surface); the
                                 client-front validates the agent's handshake
                                 against it
-32002  resource not found       a session id that names no live session, or
                                 (client front) a file that does not exist
======  =======================  ===============================================
"""

from __future__ import annotations

from typing import Any, Final

from paa import __version__
from paa.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    FrameSink,
    McpError,
    encode_frame,
    error_response,
    read_frame,
    result_response,
)

__all__ = [
    "AGENT_NAME",
    "AGENT_VERSION",
    "AUTH_REQUIRED",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_FS_READ_TEXT_FILE",
    "METHOD_INITIALIZE",
    "METHOD_NOT_FOUND",
    "METHOD_REQUEST_PERMISSION",
    "METHOD_SESSION_CANCEL",
    "METHOD_SESSION_NEW",
    "METHOD_SESSION_PROMPT",
    "NOTIFY_SESSION_UPDATE",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "REQUEST_CANCELLED",
    "RESOURCE_NOT_FOUND",
    "STOP_CANCELLED",
    "STOP_END_TURN",
    "STOP_REFUSAL",
    "UPDATE_AGENT_MESSAGE_CHUNK",
    "AcpError",
    "FrameSink",
    "agent_message_chunk",
    "encode_frame",
    "error_response",
    "notification",
    "parse_prompt_text",
    "read_frame",
    "result_response",
    "stop_reason_result",
    "text_content",
]

#: The ACP **major** protocol version this tree speaks. An integer, per the
#: ACP schema — not MCP's date string. Pinned so a loopback conversation
#: between this server and this client adapter is symmetric by construction,
#: the same property the MCP kit pins for its two fronts.
PROTOCOL_VERSION: Final = 1

AGENT_NAME: Final = "paa-acp"
AGENT_VERSION: Final = __version__

# -- methods (client → agent) ----------------------------------------------

METHOD_INITIALIZE: Final = "initialize"
METHOD_SESSION_NEW: Final = "session/new"
METHOD_SESSION_PROMPT: Final = "session/prompt"

# -- notifications (client → agent) ----------------------------------------

#: A notification: no id, no reply. Cancels the session's in-flight turn.
METHOD_SESSION_CANCEL: Final = "session/cancel"

# -- notifications (agent → client) ----------------------------------------

#: A notification carrying one :data:`UPDATE_*` payload under
#: ``params.update``. v1 emits ``agent_message_chunk`` only — the chat loop
#: streams reply deltas, and tool calls/plan entries would be claims about
#: machinery this front does not drive.
NOTIFY_SESSION_UPDATE: Final = "session/update"
UPDATE_AGENT_MESSAGE_CHUNK: Final = "agent_message_chunk"

# -- methods (agent → client) ----------------------------------------------

#: The agent asks the client to read a file in the client's environment.
#: ACP's own direction; PAA's v1 *server* also answers it from the session's
#: scoped workspace (ADR-0053 records why that inversion is safe here).
METHOD_FS_READ_TEXT_FILE: Final = "fs/read_text_file"

#: The agent asks the client's user for permission. The T2 client front
#: answers it with the deterministic policy's verdict, never a model's.
METHOD_REQUEST_PERMISSION: Final = "session/request_permission"

# -- JSON-RPC 2.0 reserved codes (the shared kit's own objects) --------------
#
# The reserved range is one vocabulary across all three fronts: these names
# are the *imports* at the top of this module, not same-valued copies, so the
# surfaces cannot drift apart without an import error. ACP's own additions
# below deliberately break that rule — see their comments.

# -- ACP's own codes (see the module table for the MCP-surface collisions) --

#: Authentication is required first. v1 has no auth surface; the client
#: front treats it as a fatal handshake answer from a foreign agent.
AUTH_REQUIRED: Final = -32000
#: A referenced resource (session, file) does not exist.
RESOURCE_NOT_FOUND: Final = -32002
#: JSON-RPC-level cancellation. Cancels are answered with the ``cancelled``
#: stop reason instead (the ACP prompt-turn contract), so v1 never raises it.
REQUEST_CANCELLED: Final = -32800

# -- stop reasons (the session/prompt result vocabulary) --------------------

STOP_END_TURN: Final = "end_turn"
STOP_CANCELLED: Final = "cancelled"
#: A *failed* chat turn maps here — the closest honest member of ACP's
#: closed stop-reason set for "the agent could not continue" (ADR-0053).
STOP_REFUSAL: Final = "refusal"


class AcpError(McpError):
    """A protocol-level ACP fault with its JSON-RPC code attached.

    The MCP kit's :class:`~paa.mcp.protocol.McpError` already carries the
    code and the details; this subclass exists so an ACP raise site reads as
    one and so the two surfaces' faults stay distinguishable in a traceback.
    """


# ---------------------------------------------------------------------------
# Payload builders — the shapes both fronts write, in one place
# ---------------------------------------------------------------------------


def text_content(text: str) -> dict[str, Any]:
    """One ACP text content block."""
    return {"type": "text", "text": text}


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC notification frame (no id — never answered)."""
    return {"jsonrpc": "2.0", "method": method, "params": params}


def agent_message_chunk(session_id: str, message_id: str, text: str) -> dict[str, Any]:
    """One ``session/update`` frame carrying an ``agent_message_chunk``.

    ``messageId`` is opaque to the client; v1 uses the chat turn id, so every
    chunk of one turn shares it and a new turn means a new message.
    """
    return notification(
        NOTIFY_SESSION_UPDATE,
        {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                "messageId": message_id,
                "content": text_content(text),
            },
        },
    )


def stop_reason_result(stop_reason: str) -> dict[str, Any]:
    """The ``session/prompt`` result: a stop reason, nothing else."""
    return {"stopReason": stop_reason}


def parse_prompt_text(prompt: Any) -> str:
    """The user text of a ``session/prompt`` ``prompt`` array, or refuse.

    v1 accepts ``text`` blocks only. The ACP baseline also mandates
    ``resource_link``; refusing it here is a stated v1 deviation (ADR-0053)
    rather than a silent drop — a content type this front cannot honour must
    fail the request, not vanish from the conversation.
    """
    if not isinstance(prompt, list) or not prompt:
        raise AcpError(INVALID_PARAMS, "session/prompt requires a non-empty prompt array")
    parts: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            raise AcpError(INVALID_PARAMS, "prompt blocks must be objects")
        if block.get("type") != "text":
            raise AcpError(
                INVALID_PARAMS,
                f"prompt content type {block.get('type')!r} is not supported in "
                "ACP v1 (text only; see ADR-0053)",
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise AcpError(INVALID_PARAMS, "text content blocks require a string 'text'")
        parts.append(text)
    return "\n".join(parts)
