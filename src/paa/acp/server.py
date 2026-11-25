"""The PAA ACP server: PAA's chat loop behind an editor's Agent Client
Protocol connection (C7.6, the T1 front).

``paa acp serve`` is spawned by an ACP client (an editor — Zed is the
reference one) and speaks JSON-RPC 2.0 over stdio, newline-delimited, on the
shared framing kit (:mod:`paa.mcp.protocol`) — the same wire discipline
``paa mcp serve`` states for itself: protocol frames on stdout and nothing
else, diagnostics on stderr.

What the server does with each method (the v1-minimal subset, ADR-0053):

``initialize``
    The handshake: version + capabilities. The reply always carries this
    server's pinned major version (ACP's negotiation contract — a client
    that asked for a different major decides for itself whether to
    disconnect), and ``agentCapabilities`` declares exactly what v1
    supports: no ``loadSession``, no image/audio/embedded-context prompts.
    Streaming is not a capability in ACP — ``session/update`` with
    ``agent_message_chunk`` is baseline — so there is nothing to overclaim.

``session/new``
    Creates a **chat session** (:class:`~paa.chat.service.ChatService`)
    bound to the client's ``cwd``: the ACP session id *is* the chat session
    id, so the ledger lineage and the wire session cannot disagree. ``cwd``
    must name an existing directory (it is the workspace every
    ``fs/read_text_file`` answer is jailed to). ``mcpServers`` is accepted
    and validated for shape only — v1 does not connect MCP servers on the
    editor's behalf, and the handshake advertises no ``mcpCapabilities``,
    so an editor that reads capabilities knows this front does not offer it.

``session/prompt``
    One turn through :meth:`ChatService.send`, with the reply **streamed**
    as ``agent_message_chunk`` notifications as deltas arrive (the chat
    loop's ``on_delta`` seam). The request is answered only when the turn
    ends — with ``end_turn`` on success, ``refusal`` on a failed turn (the
    closest member of ACP's closed stop-reason set; ADR-0053), ``cancelled``
    after a cancel. The prompt runs as its own task so the read loop stays
    free to receive ``session/cancel`` mid-turn — a server that blocked on
    the turn could never be cancelled, and a cancel that cannot arrive is
    not a cancel.

``session/cancel``
    A notification; cancels the session's in-flight turn. Cancellation
    actually stops work: the task is cancelled, the chat loop's shielded
    handler journals ``CHAT_TURN_CANCELLED`` on the session lineage, and the
    prompt request is then answered with the ``cancelled`` stop reason —
    the ACP contract's acknowledgement.

``fs/read_text_file``
    Answered from the session's own ``cwd`` through the shared jailed,
    protected reader (:mod:`paa.acp.fs`) — never a blind disk read.

Unknown methods answer ``-32601``; unknown notifications are silence; a
request before the handshake answers ``-32600``; EOF cancels in-flight
turns and ends the chat sessions (``stopped_reason="acp-eof"``) so the
ledger says what the wire did.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from asyncio import StreamReader
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog

from paa.acp.fs import read_scoped_text_file
from paa.acp.protocol import (
    AGENT_NAME,
    AGENT_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_FS_READ_TEXT_FILE,
    METHOD_INITIALIZE,
    METHOD_NOT_FOUND,
    METHOD_SESSION_CANCEL,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    RESOURCE_NOT_FOUND,
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_REFUSAL,
    AcpError,
    FrameSink,
    agent_message_chunk,
    encode_frame,
    error_response,
    parse_prompt_text,
    read_frame,
    result_response,
    stop_reason_result,
)
from paa.chat.service import ChatService
from paa.core.errors import PaaError
from paa.models.base import redact
from paa.sandbox.base import resolve_workspace

__all__ = ["ACPServer"]

log = structlog.get_logger(__name__)

#: The chat-session scope the server stamps on every session it creates —
#: free-form in the journal payload, distinct from ``"personal"``/``"mcp"``
#: so the ledger shows the session came from the ACP surface.
_ACP_SCOPE: Final = "acp"

#: Why a session ended when the client hung up. The journal's
#: ``CHAT_SESSION_ENDED`` row carries it; the fold's counters ride along.
_EOF_STOPPED_REASON: Final = "acp-eof"


@dataclass
class _AcpSession:
    """One live ACP session: the wire state around one chat session."""

    session_id: str
    chat_id: uuid.UUID
    cwd: Path
    prompt_task: asyncio.Task[None] | None = field(default=None, repr=False)


class ACPServer:
    """The stdio ACP server over one chat service.

    Constructed with the wiring piece (``ChatService`` — built off a runtime
    by the CLI, or explicitly by tests). One instance serves one client
    connection: :meth:`serve` frames NDJSON until end of stream,
    :meth:`handle_message` is the request router the framing loop calls, and
    :meth:`quiesce` awaits in-flight prompt turns (the tests' and the
    shutdown path's synchronization point).
    """

    def __init__(self, *, chat: ChatService) -> None:
        self._chat = chat
        self._sessions: dict[str, _AcpSession] = {}
        self._initialized = False
        self._sink: FrameSink | None = None
        self._sink_lock = asyncio.Lock()
        self._prompt_tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    # -- transport -----------------------------------------------------------

    async def serve(self, reader: StreamReader, sink: FrameSink) -> None:
        """Serve requests from ``reader`` until end of stream.

        Replies and notifications are written through the locked sender, so
        a streaming prompt turn and a concurrent reply can never interleave
        bytes on the wire. EOF tears everything down: in-flight turns are
        cancelled (their ledger rows land shielded) and chat sessions are
        ended, because a client that closed the pipe will not send another
        prompt.
        """
        self._sink = sink
        try:
            while True:
                payload = await read_frame(reader)
                if payload is None:
                    return  # EOF: the client closed the pipe
                response = await self.handle_payload(payload)
                if response is not None:
                    await self._send(response)
        finally:
            await self._shutdown()

    async def handle_payload(self, payload: str) -> dict[str, Any] | None:
        """Parse one raw frame and dispatch it. Parse failures reply ``-32700``."""
        try:
            message: Any = json.loads(payload)
        except json.JSONDecodeError:
            return error_response(None, PARSE_ERROR, "frame is not valid JSON")
        return await self.handle_message(message)

    # -- the request router ----------------------------------------------------

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Route one decoded JSON-RPC message.

        ``None`` means "no frame to write": a notification, or — the one
        asymmetric case — a ``session/prompt``, whose reply is written by
        the turn task when the turn ends (see the module docstring for why
        the router must not await it). Total over any input: a non-object,
        a response object or a malformed notification gets the honest
        protocol answer, never an exception.
        """
        if not isinstance(message, dict):
            return error_response(None, INVALID_REQUEST, "frame is not a JSON object")
        method = message.get("method")
        request_id = message.get("id")
        has_id = "id" in message
        if method is None:
            return error_response(None, INVALID_REQUEST, "request object carries no method")

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                return error_response(request_id, INVALID_PARAMS, "params must be an object")
            return None  # a malformed notification is dropped, not answered

        if not has_id:
            await self._handle_notification(method, params)
            return None  # a notification is acknowledged by silence

        try:
            if method == METHOD_INITIALIZE:
                return result_response(request_id, self._handle_initialize(params))

            if not self._initialized:
                # ACP's own rule: a request before the handshake is an invalid
                # *request* (-32600), not an unknown method and not an auth
                # error — there is no auth surface to have failed.
                return error_response(
                    request_id,
                    INVALID_REQUEST,
                    "request before the initialize handshake: send initialize first",
                )

            if method == METHOD_SESSION_NEW:
                return await self._handle_session_new(request_id, params)
            if method == METHOD_SESSION_PROMPT:
                self._start_prompt(request_id, params)
                return None  # the turn task writes this request's reply
            if method == METHOD_FS_READ_TEXT_FILE:
                return self._handle_fs_read(request_id, params)
        except AcpError as exc:
            return error_response(request_id, exc.code, exc.message, exc.details or None)
        except PaaError as exc:
            return error_response(request_id, INTERNAL_ERROR, redact(str(exc)))
        except Exception as exc:
            log.exception("acp_server.unhandled_error", error=redact(str(exc)))
            return error_response(
                request_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}"
            )
        return error_response(
            request_id,
            METHOD_NOT_FOUND,
            f"unknown method {method!r}; this server serves initialize, session/new, "
            "session/prompt, fs/read_text_file (plus the session/cancel and "
            "session/update notifications)",
        )

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Notifications: ``session/cancel`` is the one with work to do."""
        if method != METHOD_SESSION_CANCEL:
            return  # unknown notifications are acknowledged by silence
        session = self._sessions.get(_session_key(params))
        if session is None or session.prompt_task is None:
            return  # nothing in flight: a cancel of an idle session is a no-op
        session.prompt_task.cancel()

    # -- the methods ------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """The handshake. The version is validated, the reply pins ours.

        Per ACP's negotiation contract the reply carries THIS server's
        pinned major version whether or not the client's matches — the
        client decides what to do about a mismatch. A missing or non-integer
        version is malformed params: "no version" is not a choice.
        """
        version = params.get("protocolVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            raise AcpError(
                INVALID_PARAMS,
                "initialize requires an integer params.protocolVersion (the ACP major "
                f"version; this agent speaks {PROTOCOL_VERSION})",
            )
        self._initialized = True
        log.debug("acp_server.initialized", client_version=version)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            # Declared exactly: no loadSession, no richer prompt content, no
            # MCP connections on the editor's behalf. Streaming is baseline
            # ACP (session/update), not a capability to advertise.
            "agentCapabilities": {"loadSession": False},
            "agentInfo": {"name": AGENT_NAME, "version": AGENT_VERSION},
            "authMethods": [],
        }

    async def _handle_session_new(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a chat session bound to the client's ``cwd``."""
        raw_cwd = params.get("cwd")
        if not isinstance(raw_cwd, str) or not raw_cwd.strip():
            raise AcpError(INVALID_PARAMS, "session/new requires a non-empty 'cwd'")
        try:
            cwd = resolve_workspace(raw_cwd)
        except Exception as exc:
            raise AcpError(
                INVALID_PARAMS, f"session/new cwd is not a usable directory: {exc}"
            ) from exc
        _validate_mcp_servers(params.get("mcpServers"))
        chat_id = await self._chat.create_session(scope=_ACP_SCOPE)
        session_id = str(chat_id)  # the ACP session id IS the chat session id
        self._sessions[session_id] = _AcpSession(
            session_id=session_id, chat_id=chat_id, cwd=cwd
        )
        return result_response(request_id, {"sessionId": session_id})

    def _handle_fs_read(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Serve one jailed, protected file read from the session's cwd."""
        session = self._require_session(params)
        served = read_scoped_text_file(
            session.cwd,
            params.get("path"),
            line=params.get("line"),
            limit=params.get("limit"),
        )
        result: dict[str, Any] = {"content": served.content}
        if served.truncated:
            # ACP's result carries only ``content``; the truncation marker
            # rides ``_meta`` — the protocol's own extensibility slot — so a
            # capped read is marked, never silent.
            result["_meta"] = {"truncated": True}
        return result_response(request_id, result)

    # -- the prompt turn ---------------------------------------------------------

    def _start_prompt(self, request_id: Any, params: dict[str, Any]) -> None:
        """Validate and launch one prompt turn as its own task.

        Validation failures raise here and reach the wire as errors from
        :meth:`handle_message`; a valid prompt returns and the *task* writes
        both the streamed updates and the final response, leaving the read
        loop free for ``session/cancel``.
        """
        session = self._require_session(params)
        if session.prompt_task is not None and not session.prompt_task.done():
            raise AcpError(
                INVALID_PARAMS,
                "a prompt is already in flight for this session; send session/cancel "
                "first (ACP v1 serves one turn per session at a time)",
            )
        text = parse_prompt_text(params.get("prompt"))
        task = asyncio.create_task(
            self._run_prompt(session, request_id, text),
            name=f"acp-prompt:{session.session_id[:8]}",
        )
        session.prompt_task = task
        self._prompt_tasks.add(task)
        task.add_done_callback(self._prompt_tasks.discard)

    async def _run_prompt(self, session: _AcpSession, request_id: Any, text: str) -> None:
        """Drive one turn; stream its deltas; answer the request at the end."""
        message_id = uuid.uuid4().hex

        async def on_delta(delta: str) -> None:
            await self._send(agent_message_chunk(session.session_id, message_id, delta))

        try:
            turn = await self._chat.send(session.chat_id, text, on_delta=on_delta)
        except asyncio.CancelledError:
            # The chat loop has already journaled CHAT_TURN_CANCELLED
            # (shielded) and re-raised — the cancellation did real work. The
            # ACP acknowledgement is the prompt response itself.
            await self._answer(
                session, request_id, result_response(request_id, stop_reason_result(STOP_CANCELLED))
            )
            return
        except AcpError as exc:
            await self._answer(
                session, request_id, error_response(request_id, exc.code, exc.message)
            )
            return
        except PaaError as exc:
            # A refused turn is not an internal error: the chat journal has
            # the FAILED row; the wire gets the honest stop reason.
            log.warning("acp_server.turn_refused", error=redact(str(exc)))
            await self._answer(
                session, request_id, result_response(request_id, stop_reason_result(STOP_REFUSAL))
            )
            return
        except Exception as exc:
            log.exception("acp_server.turn_error", error=redact(str(exc)))
            await self._answer(
                session,
                request_id,
                error_response(request_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}"),
            )
            return
        stop = STOP_END_TURN if turn.status == "completed" else STOP_REFUSAL
        await self._answer(
            session, request_id, result_response(request_id, stop_reason_result(stop))
        )

    async def _answer(
        self, session: _AcpSession, request_id: Any, response: dict[str, Any]
    ) -> None:
        """Write one prompt request's reply, unless the connection is closing."""
        if self._closing:
            return  # the client hung up; writing to a dead pipe helps nobody
        await self._send(response)

    # -- shared helpers -----------------------------------------------------------

    def _require_session(self, params: dict[str, Any]) -> _AcpSession:
        """The session a session-scoped request names, or ``-32002``.

        ACP's unknown-session rule: ``-32002`` with ``data.sessionId`` set
        to the requested id — the caller can tell an unknown session from a
        malformed one.
        """
        session_id = _session_key(params)
        session = self._sessions.get(session_id)
        if session is None:
            raise AcpError(
                RESOURCE_NOT_FOUND,
                f"no session {session_id!r} on this connection",
                sessionId=session_id,
            )
        return session

    async def _send(self, message: dict[str, Any]) -> None:
        """Write one frame under the lock — interleaved bytes corrupt the wire."""
        if self._sink is None:
            return
        async with self._sink_lock:
            self._sink.write(encode_frame(message))
            await self._sink.drain()

    async def quiesce(self) -> None:
        """Await every in-flight prompt turn (tests and shutdown)."""
        tasks = [t for t in self._prompt_tasks if not t.done()]
        for task in tasks:
            await task

    async def _shutdown(self) -> None:
        """EOF teardown: cancel turns, end sessions — once, best-effort."""
        if self._closing:
            return
        self._closing = True
        for session in self._sessions.values():
            if session.prompt_task is not None and not session.prompt_task.done():
                session.prompt_task.cancel()
        await self.quiesce()
        for session in self._sessions.values():
            try:
                await self._chat.end_session(session.chat_id, stopped_reason=_EOF_STOPPED_REASON)
            except Exception as exc:  # teardown is best-effort by contract
                log.warning(
                    "acp_server.session_end_failed",
                    session_id=session.session_id,
                    error=redact(str(exc)),
                )
        self._sessions.clear()


def _session_key(params: dict[str, Any]) -> str:
    """The ``sessionId`` param as a string, or the empty string.

    An absent or non-string id is the *empty* key, which names no session —
    so a malformed id reaches the caller as ``-32002``/``-32602`` through
    the same path a well-formed unknown one does, never a KeyError.
    """
    session_id = params.get("sessionId")
    return session_id if isinstance(session_id, str) else ""


def _validate_mcp_servers(raw: Any) -> None:
    """Shape-check ``mcpServers``; v1 connects none of them.

    Accepted-and-validated-for-shape rather than ignored-silently: a client
    that mis-declares its own config should hear about it now, not wonder
    later why no server connected. The functional gap (no MCP connections on
    this front) is declared in the handshake and recorded in ADR-0053.
    """
    if raw is None:
        return  # tolerated absent: v1 treats it as an empty list
    if not isinstance(raw, list):
        raise AcpError(INVALID_PARAMS, "mcpServers must be an array")
    for entry in raw:
        if not isinstance(entry, dict):
            raise AcpError(INVALID_PARAMS, "each mcpServers entry must be an object")
        if not isinstance(entry.get("name"), str) or not entry.get("name"):
            raise AcpError(INVALID_PARAMS, "each mcpServers entry requires a 'name'")
        if not isinstance(entry.get("command"), str) or not entry.get("command"):
            raise AcpError(INVALID_PARAMS, "each mcpServers entry requires a 'command'")
        args = entry.get("args")
        if args is not None and not isinstance(args, list):
            raise AcpError(INVALID_PARAMS, "mcpServers 'args' must be an array")
