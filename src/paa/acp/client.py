"""The PAA ACP client adapter: an external agent, contained and mediated
(C7.6, the T2 front).

The mirror of :mod:`paa.acp.server`. There, PAA *is* the agent and an editor
drives it; here PAA *spawns* an external ACP agent (a coding-agent binary,
or anything that speaks ACP) behind a
:class:`~paa.sandbox.base.SandboxSession` — the same duplex contained-child
contract the sandboxed MCP client (C5.3) and the recorder's harness wrap
(C7.3) use — and mediates the two things an ACP agent can ask of its client:

``session/request_permission``
    The agent's tool-call ask. It is answered by PAA's deterministic
    :class:`~paa.coder.permissions.ToolPermissionPolicy` — the *same* policy
    the coder loop and the recorder's MCP proxy use, never a model, never a
    human on a pipe that has none. An ``ask`` verdict resolves to a
    **denial** recorded as ``denied_unattended`` with the raw decision kept
    in the reason (the recorder proxy's semantics, mirrored exactly); a
    ``deny`` selects the agent's reject option; an ``allow`` selects its
    allow option — and when the agent offered no option of the kind the
    policy's verdict needs, the adapter fails closed (a rejection, or the
    ``cancelled`` outcome when not even that exists), never an approval the
    policy did not grant.

``fs/read_text_file``
    The agent's file read. Answered from the workspace the agent was spawned
    into, through the same jailed, protected reader the server front uses
    (:mod:`paa.acp.fs`) — the containment's client-side half.

Everything mediated is **journalled** through the coder's ADR-0046
tool-call family (``TOOL_CALL_PROPOSED`` → ``DENIED``/``COMPLETED``) on a
per-call lineage, exactly the audit shape the MCP server writes for its own
tool calls — ledger-before-decision, so even a refused call is a fact before
it is a refusal. What is deliberately NOT journalled here: the agent's
streamed transcript. Observing a harness's full flight is the recorder's job
(C7.3); this adapter journals the *boundary* (what it was asked to approve
and serve), which is the part it is accountable for, and does not pretend to
see inside the agent.

v1-minimal: initialize, one session, one turn at a time, streamed updates,
cancel. No ``session/load``, no modes, no elicitation, no terminals — an
agent that asks for those gets ``-32601``, the honest unknown method.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import structlog

from paa.acp.fs import read_scoped_text_file
from paa.acp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_FS_READ_TEXT_FILE,
    METHOD_INITIALIZE,
    METHOD_NOT_FOUND,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_CANCEL,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    PROTOCOL_VERSION,
    AcpError,
    encode_frame,
    error_response,
    result_response,
    text_content,
)
from paa.coder.journal import (
    LedgerToolJournal,
    args_preview_redacted,
    args_sha256_of,
)
from paa.coder.loop import ToolCtx
from paa.coder.permissions import (
    PermissionDecision,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.core.errors import SandboxError
from paa.ledger.events import canonical_json
from paa.ledger.store import LedgerStore
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)

__all__ = ["AcpClient", "UpdateSink"]

log = structlog.get_logger(__name__)

#: How this client renders in the handshake. ``paa-usa`` is the name the MCP
#: client adapter uses for itself; this front names its role instead, so an
#: agent's logs can tell an editor from a mediator.
CLIENT_NAME: Final = "paa-acp-client"

#: Per-call budget on one request/response exchange. The prompt turn's own
#: budget is the caller's (a turn legitimately runs long); this is the
#: handshake/session-exchange clock.
_DEFAULT_TIMEOUT: Final = 10.0

#: Ledger refusal reasons are bounded, exactly as the MCP server bounds its own.
_MAX_REASON_CHARS: Final = 200

#: The verdict for a tool call that carried no name: deny, with the reason
#: stated once. A call the policy cannot classify is refused rather than
#: guessed at — fail closed is the only posture a deterministic boundary
#: can hold.
_UNNAMED_DECISION: Final = PermissionDecision(
    PermissionOutcome.DENY,
    "tool call carried no name: the policy cannot classify it",
    "unnamed",
)

#: An async sink for one streamed ``session/update`` payload (the whole
#: ``params`` object, ``sessionId`` included). ``None`` means "collect
#: nothing" — the mediation journal is independent of the update stream.
UpdateSink = Callable[[dict[str, Any]], Awaitable[None]]


class AcpClient:
    """A mediated ACP conversation with one contained external agent.

    Owns the :class:`~paa.sandbox.base.SandboxSession` and a single
    background read task, exactly like the sandboxed MCP client: requests
    carry monotonically increasing integer ids and resolve through a futures
    table; the agent's own requests (permission asks, file reads) are served
    by handler tasks that answer under the write lock, so a streamed update
    and a permission reply can never interleave bytes.
    """

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        policy: ToolPermissionPolicy,
        ledger: LedgerStore | None = None,
    ) -> None:
        self._command = tuple(command)
        self._policy = policy
        # The journal seam: ``None`` means "mediate without a ledger" (a
        # library caller composing its own evidence); the CLI always passes
        # one, because an unjournalled mediation is exactly the quiet
        # unrecorded action this front exists to prevent.
        self._ledger = ledger
        self._session: SandboxSession | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._update_sinks: dict[str, UpdateSink] = {}
        self._next_id = 0
        self._closed = False
        self._agent_capabilities: dict[str, Any] = {}
        self._write_lock = asyncio.Lock()
        self._buffer = bytearray()
        self._workspace: Path | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(
        self,
        sandbox: Sandbox,
        *,
        workspace: Path | str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        memory_mb: int = 256,
        cpu_cores: float = 0.5,
        allow_network: bool = False,
    ) -> None:
        """Open the agent child as a contained duplex session and start reading.

        The workspace is the agent's cwd jail **and** the root every
        ``fs/read_text_file`` answer is scoped to — one root, both halves of
        the containment. Unlike the MCP client this is required, not
        optional: an ACP agent without a working directory is not a thing
        this front mediates.
        """
        if self._session is not None or self._closed:
            raise RuntimeError("ACP client session was already started or closed")
        self._workspace = Path(workspace)
        spec = SandboxSpec(
            command=self._command,
            workspace_path=self._workspace,
            env=dict(env) if env else {},
            timeout_seconds=timeout_seconds,
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
            allow_network=allow_network,
        )
        self._session = await sandbox.open(spec)
        self._reader_task = asyncio.create_task(self._read_loop())
        log.debug("acp.client.started", command=self._command[0])

    @property
    def workspace(self) -> Path | None:
        """The agent's workspace jail, or ``None`` before :meth:`start`."""
        return self._workspace

    @property
    def agent_capabilities(self) -> dict[str, Any]:
        """The agent's declared capabilities from the handshake (post-init)."""
        return dict(self._agent_capabilities)

    async def close(self) -> SandboxResult | None:
        """Tear the session down — whole-tree kill — and fail pending calls.

        Idempotent. Pending requests are failed with :class:`ConnectionError`
        after the teardown so an awaiting caller unblocks rather than waiting
        out its own timeout.
        """
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            # The task is already unwinding; swallow whatever it raises on join.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        for task in list(self._request_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._request_tasks.clear()
        result = None
        if self._session is not None:
            session, self._session = self._session, None
            result = await session.close()
        self._fail_pending(ConnectionError("ACP client closed"))
        return result

    # -- the conversation ----------------------------------------------------

    async def initialize(
        self, *, timeout_seconds: float = _DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """The ACP handshake: version pinned, capabilities recorded.

        The reply's major version must equal this client's pinned one — an
        agent that answers a different major is an agent this mediator
        cannot honestly drive, and the failure is raised here, at wiring
        time, not discovered mid-conversation.
        """
        from paa import __version__

        reply = await self.call(
            METHOD_INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                # Declared exactly: this client answers fs/read_text_file.
                # No write, no terminal, no elicitation — an agent that asks
                # for those gets the honest -32601.
                "clientCapabilities": {"fs": {"readTextFile": True}},
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            },
            timeout_seconds=timeout_seconds,
        )
        result = _result_of(reply, METHOD_INITIALIZE)
        version = result.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise AcpError(
                INVALID_PARAMS,
                f"the agent speaks ACP major version {version!r}; this mediator "
                f"pins {PROTOCOL_VERSION}",
            )
        capabilities = result.get("agentCapabilities")
        self._agent_capabilities = capabilities if isinstance(capabilities, dict) else {}
        return result

    async def new_session(
        self, *, timeout_seconds: float = _DEFAULT_TIMEOUT
    ) -> str:
        """``session/new`` over the agent's own workspace. Returns the session id."""
        if self._workspace is None:
            raise RuntimeError("start() before new_session()")
        reply = await self.call(
            METHOD_SESSION_NEW,
            {"cwd": str(self._workspace), "mcpServers": []},
            timeout_seconds=timeout_seconds,
        )
        result = _result_of(reply, METHOD_SESSION_NEW)
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpError(INVALID_PARAMS, "session/new returned no sessionId")
        return session_id

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        on_update: UpdateSink | None = None,
        timeout_seconds: float = 60.0,
    ) -> str:
        """Drive one prompt turn to its stop reason.

        ``on_update`` receives every streamed ``session/update`` payload for
        this session while the turn runs. The returned string is the turn's
        ``stopReason`` — ``end_turn`` on success, ``cancelled`` after a
        :meth:`cancel`, whatever the agent reports otherwise. The budget is
        the caller's: a turn legitimately runs long, so it is a parameter
        here rather than a constant the caller cannot widen.
        """
        if on_update is not None:
            self._update_sinks[session_id] = on_update
        try:
            reply = await self.call(
                METHOD_SESSION_PROMPT,
                {"sessionId": session_id, "prompt": [text_content(text)]},
                timeout_seconds=timeout_seconds,
            )
        finally:
            self._update_sinks.pop(session_id, None)
        result = _result_of(reply, METHOD_SESSION_PROMPT)
        stop = result.get("stopReason")
        return stop if isinstance(stop, str) else ""

    async def cancel(self, session_id: str) -> None:
        """Send ``session/cancel``; the in-flight prompt answers ``cancelled``."""
        await self.notify(METHOD_SESSION_CANCEL, {"sessionId": session_id})

    # -- wire primitives -------------------------------------------------------

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a request and await its correlated reply.

        :raises ConnectionError: the client is not running, the pipe is
            broken, or the agent exited before replying.
        :raises TimeoutError: no reply arrived within ``timeout_seconds``.
        """
        if self._closed or self._session is None:
            raise ConnectionError("ACP client is not running")
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._send(
            {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise TimeoutError(
                f"ACP request {method!r} timed out after {timeout_seconds}s"
            ) from None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no reply expected)."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._session is None:
            raise ConnectionError("ACP client is not running")
        async with self._write_lock:
            try:
                await self._session.write(encode_frame(message))
            except SandboxError as exc:
                # The session reports a gone pipe as SandboxError (its
                # contract); this client's contract reports ConnectionError.
                raise ConnectionError("ACP agent pipe is broken") from exc

    # -- read side ---------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Chunked reads in, complete lines out — ACP's NDJSON transport."""
        assert self._session is not None
        session = self._session
        try:
            while True:
                chunk = await session.read()
                if not chunk:
                    break  # EOF: the agent exited (crash or clean shutdown)
                self._buffer += chunk
                while (line := self._pop_line()) is not None:
                    if line:
                        self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead session must not escape and orphan callers
            log.warning("acp.client.read_loop_error", error=str(exc))
        finally:
            self._fail_pending(ConnectionError("ACP agent stream closed"))

    def _pop_line(self) -> str | None:
        """One complete newline-delimited message off the buffer, or ``None``.

        ACP's stdio transport is newline-delimited only (unlike MCP, there is
        no Content-Length mode to autodetect), so the reassembly is a line
        split — the framing *kit* is still shared on the write side
        (:func:`paa.mcp.protocol.encode_frame`); the read side composes the
        session's raw-byte contract with ACP's simpler delimiter.
        """
        newline = self._buffer.find(b"\n")
        if newline < 0:
            return None
        line = bytes(self._buffer[:newline])
        del self._buffer[: newline + 1]
        return line.decode("utf-8", errors="replace").strip()

    def _dispatch(self, payload: str) -> None:
        """Route one decoded message: reply, agent request, or notification."""
        try:
            message: Any = json.loads(payload)
        except json.JSONDecodeError:
            # One bad frame must not kill the stream every other message
            # rides — the same resilience the MCP client's loop holds.
            log.warning("acp.client.malformed_json", preview=payload[:120])
            return
        if not isinstance(message, dict):
            log.warning("acp.client.non_object_message")
            return
        method = message.get("method")
        if method is None:
            message_id = message.get("id")
            future = self._pending.pop(message_id, None) if message_id is not None else None
            if future is not None and not future.done():
                future.set_result(message)
            elif message_id is not None:
                log.warning("acp.client.unknown_response_id", id=message_id)
            return
        if "id" in message:
            # An agent→client request: served by its own task so the read
            # loop keeps flowing (a permission ask must not block the stream
            # that carries the notification that answers it).
            task = asyncio.create_task(self._serve_agent_request(message))
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)
            return
        if method == "session/update":
            params = message.get("params")
            if not isinstance(params, dict):
                return
            sink = self._update_sinks.get(str(params.get("sessionId") or ""))
            if sink is not None:
                task = asyncio.create_task(self._run_update_sink(sink, params))
                self._request_tasks.add(task)
                task.add_done_callback(self._request_tasks.discard)

    async def _run_update_sink(self, sink: UpdateSink, params: dict[str, Any]) -> None:
        """One streamed update to the caller's sink; a sink error is logged,
        never propagated — a renderer must not kill the mediation."""
        try:
            await sink(params)
        except Exception as exc:
            log.warning("acp.client.update_sink_error", error=str(exc))

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -- agent→client requests: the mediated surface --------------------------------

    async def _serve_agent_request(self, message: dict[str, Any]) -> None:
        """Answer one agent request under the write lock. Total over input."""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            if method == METHOD_REQUEST_PERMISSION:
                response = await self._mediate_permission(request_id, params)
            elif method == METHOD_FS_READ_TEXT_FILE:
                response = await self._serve_fs_read(request_id, params)
            else:
                response = error_response(
                    request_id,
                    METHOD_NOT_FOUND,
                    f"this ACP client does not implement {method!r} (v1: "
                    "session/request_permission, fs/read_text_file)",
                )
        except AcpError as exc:
            response = error_response(request_id, exc.code, exc.message, exc.details or None)
        except Exception as exc:
            log.exception("acp.client.request_handler_error", error=str(exc))
            response = error_response(
                request_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}"
            )
        await self._send(response)

    async def _mediate_permission(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """One permission ask: journal, decide, answer. Never a model, never
        a silent approval.

        The journal rows land on a per-call lineage (the MCP server's audit
        shape): ``TOOL_CALL_PROPOSED`` before the decision is acted on, then
        ``DENIED`` (with the ``denied_unattended`` resolution in the reason
        for an ask) or ``COMPLETED`` attesting the outcome that was actually
        sent. What the agent does *after* the decision is its own execution
        — this journal attests the boundary, not the agent's internals.
        """
        tool_call = params.get("toolCall")
        if not isinstance(tool_call, dict):
            raise AcpError(
                INVALID_PARAMS, "session/request_permission requires a 'toolCall' object"
            )
        options = params.get("options")
        if not isinstance(options, list) or not options:
            raise AcpError(INVALID_PARAMS, "session/request_permission requires 'options'")
        raw_input = tool_call.get("rawInput")
        arguments = raw_input if isinstance(raw_input, dict) else {}
        raw_name = tool_call.get("name")
        # Narrowed once, then used everywhere: the policy, the journal's
        # tool label and the redacted preview all need the *same* string.
        if isinstance(raw_name, str) and raw_name:
            tool: str = raw_name
            named = True
        else:
            tool = "unnamed"
            named = False
        call_id = str(tool_call.get("toolCallId") or uuid.uuid4().hex)

        decision = self._policy.evaluate(tool, arguments) if named else _UNNAMED_DECISION

        ctx = ToolCtx(
            session_id=str(params.get("sessionId") or "acp"),
            call_id=call_id,
            tool=tool,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(tool, arguments),
            isolation_level=IsolationLevel.NONE,
        )
        journal = self._journal_for(ctx)
        if journal is not None:
            await journal.before(ctx)  # the attempt is a fact before it is a decision

        outcome, option = _select_permission_option(
            options, allow=decision.outcome is PermissionOutcome.ALLOW
        )
        if decision.outcome is PermissionOutcome.ALLOW:
            payload: dict[str, Any] = {"outcome": {"outcome": outcome, "optionId": option}}
            if journal is not None:
                await journal.after(ctx, ok=True, result_sha=_outcome_sha256(payload))
        elif journal is not None:
            await journal.denied(ctx, _denial_reason(decision)[:_MAX_REASON_CHARS])

        if outcome == "cancelled":
            # No option of the needed kind existed (and for an allow verdict,
            # not even a reject one to fail closed with): the only
            # non-approval vocabulary ACP offers here is the cancelled outcome.
            return result_response(request_id, {"outcome": {"outcome": "cancelled"}})
        return result_response(
            request_id, {"outcome": {"outcome": "selected", "optionId": option}}
        )

    async def _serve_fs_read(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """One agent file read: jailed to the spawn workspace, protected by
        the coder's own rules, journaled like every other mediated call."""
        if self._workspace is None:  # pragma: no cover - start() gates every path here
            raise AcpError(INTERNAL_ERROR, "the client has no workspace to read from")
        arguments = {
            key: params[key] for key in ("path", "line", "limit") if params.get(key) is not None
        }
        ctx = ToolCtx(
            session_id=str(params.get("sessionId") or "acp"),
            call_id=uuid.uuid4().hex,
            tool=METHOD_FS_READ_TEXT_FILE,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(METHOD_FS_READ_TEXT_FILE, arguments),
            isolation_level=IsolationLevel.NONE,
        )
        journal = self._journal_for(ctx)
        if journal is not None:
            await journal.before(ctx)  # a read attempt is a fact before it is served
        try:
            served = read_scoped_text_file(
                self._workspace,
                params.get("path"),
                line=params.get("line"),
                limit=params.get("limit"),
            )
        except AcpError as exc:
            if journal is not None:
                await journal.denied(ctx, exc.message[:_MAX_REASON_CHARS])
            raise  # the refusal reaches the agent as its own JSON-RPC error
        if journal is not None:
            await journal.after(
                ctx,
                ok=True,
                result_sha=hashlib.sha256(served.content.encode("utf-8")).hexdigest(),
            )
        return result_response(request_id, {"content": served.content})

    def _journal_for(self, ctx: ToolCtx) -> LedgerToolJournal | None:
        """The per-call journal, when a ledger was wired.

        The lineage is fresh per call (uuid) — the MCP server's audit shape:
        tool-call vocabulary only, so the boot sweep's coder-only exemption
        holds and no row ever shares a chat session's lineage.
        """
        if self._ledger is None:
            return None
        return LedgerToolJournal(self._ledger, uuid.uuid4())


def _select_permission_option(options: list[Any], *, allow: bool) -> tuple[str, str | None]:
    """Pick the option the verdict needs, failing closed.

    An ``allow`` verdict wants an allow-kind option; a deny wants a reject
    one. If the agent offered none of the wanted kind, an allow verdict
    falls back to a reject option — never approve through a door the agent
    did not open — and a verdict with no usable option at all resolves to
    the ``cancelled`` outcome, the only non-approval answer ACP's permission
    vocabulary has.
    """
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    fallback = ("reject_once", "reject_always") if allow else ()
    for kinds in (wanted, fallback):
        for option in options:
            if isinstance(option, dict) and option.get("kind") in kinds:
                option_id = option.get("optionId")
                if isinstance(option_id, str) and option_id:
                    return "selected", option_id
    return "cancelled", None


def _denial_reason(decision: PermissionDecision) -> str:
    """The journalled reason for a non-allow verdict, ``denied_unattended``
    made explicit for an ask — the recorder proxy's semantics."""
    if decision.outcome is PermissionOutcome.ASK:
        return (
            f"denied_unattended (policy decision: ask — {decision.reason}); the ACP "
            "client has no interactive door: pre-allow it with an --allow rule on "
            "paa acp client"
        )
    return f"denied by policy: {decision.reason}"


def _outcome_sha256(payload: dict[str, Any]) -> str:
    """The result hash a COMPLETED row attests (canonical JSON)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _result_of(reply: dict[str, Any], method: str) -> dict[str, Any]:
    """The result object of a reply, raising the reply's error if any."""
    if "error" in reply:
        error = reply["error"]
        raise AcpError(
            int(error.get("code", INTERNAL_ERROR)),
            str(error.get("message", "agent error")),
        )
    result = reply.get("result")
    if not isinstance(result, dict):
        raise AcpError(INTERNAL_ERROR, f"{method} returned no result object")
    return result
