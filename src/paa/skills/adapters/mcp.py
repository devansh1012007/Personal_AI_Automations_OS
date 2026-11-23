"""An embedded MCP client — ``provider='mcp_server'``.

This is the "self-hosted MCP server to MCP" half of the user's request: the
runtime speaks the Model Context Protocol (JSON-RPC 2.0 over stdio) to a server
it spawns **under a sandbox session** (C5.3), discovers that server's tools, and
exposes each as a :class:`~paa.skills.contracts.SkillContract`.

Containment, honestly
---------------------
The server child is spawned through :meth:`paa.sandbox.base.Sandbox.open` and
speaks over the resulting :class:`~paa.sandbox.base.SandboxSession`:

* the child environment is the :class:`~paa.sandbox.base.SandboxSpec` **explicit
  allowlist** plus the backend's bootstrap set — the host environment is never
  inherited, so an ambient ``ANTHROPIC_API_KEY`` in the parent's ``os.environ``
  cannot reach the server process;
* the child's cwd is the session's resolved workspace jail, a fresh scratch
  directory per session unless the caller supplies one;
* the resource envelope (memory ceiling, CPU share) is whatever the chosen
  backend enforces, and the containment level is the backend's own honest
  :class:`~paa.sandbox.base.IsolationLevel` — a ``SubprocessSandbox`` is
  ``PROCESS`` (same kernel, same user: it stops runaway resources and
  accidental writes outside the workspace, not a hostile syscall), a Docker
  backend would be ``NAMESPACE``;
* teardown is the session's whole-**tree** kill, so an MCP server that spawned
  helper processes leaves no orphan behind.

A backend that cannot open a duplex session (the dry-run backend, Docker and
WSL today) refuses at ``open()`` with a :class:`~paa.core.errors.SandboxError`
naming itself; that refusal propagates — a containment failure is not a
workload failure, and collapsing the two would hide it.

The adapter refuses to run without a sandbox at all (the
:class:`~paa.skills.adapters.claw_hub.ClawHubAdapter` rule): an MCP server is
out-of-process code, and the bare ``create_subprocess_exec`` spawn this module
used to perform — ambient environment inherited, no jail, no ceiling, no tree
kill — was the audit's #1 trust gap. There is no unsandboxed path left to
label, because there is no unsandboxed path.

Policy at the boundary
-----------------------
Every tool crosses the deterministic intersection ``manifest ∩ mandate ∩
policy`` before it can run — the same house gate :mod:`paa.mcp.server` applies
to its own tools and :mod:`paa.skills.usa` applies to skill dispatch:

* **manifest** — each discovered tool's contract declares
  ``required_permissions`` (``PERM_SANDBOX_RUN`` because the tool spawns a
  sandboxed child, ``PERM_NET_EGRESS`` because an MCP server process can reach
  out of process);
* **mandate** — a constructor seam (``mandate_permissions``), ``None`` imposing
  no constraint, tighten-only when present;
* **policy** — the active :class:`~paa.core.types.PermissionMode`'s grants.

A tool that does not survive the intersection is **not exposed** by
:meth:`McpServerAdapter.discover`, and a contract that reaches
:meth:`McpServerAdapter.invoke` anyway (registered under an older policy,
dispatched by a caller that skipped discovery) is refused **before the server
child is ever spawned**: the gate raises
:class:`~paa.core.errors.PermissionDeniedError`, and the refusal is journalled
as a ``TOOL_CALL_DENIED`` ledger row through the coder's
:class:`~paa.coder.journal.LedgerToolJournal` — proof the tool never executed.
When a ledger is bound, every call is journalled in the ADR-0046 order
(``PROPOSED`` before the gate and any effect; ``DENIED`` / ``COMPLETED`` /
``FAILED`` after), so a crash, timeout or cancel mid-call leaves a row the
recovery sweep classifies as an outcome, never as an unknown to repeat.

Transport
---------
MCP frames JSON-RPC messages over stdio in one of two ways, and servers in the
wild use both:

* **newline-delimited JSON** — one compact JSON object per line;
* **``Content-Length`` headers** — an LSP-style header block, a blank line, then
  exactly that many bytes of body.

:class:`MCPClient` **autodetects the framing per message on read** (a line that
begins ``Content-Length:`` switches that message to header framing; anything else
is treated as a whole-line JSON object), so a server may even mix the two. The
write framing is chosen once at construction because the *first* thing we send —
``initialize`` — precedes any reply we could learn the framing from.

The session's :meth:`~paa.sandbox.base.SandboxSession.read` hands back raw
chunks, not lines — the sandbox transports bytes and line framing is the
caller's business — so the client reassembles messages itself, preserving the
autodetect above across chunk boundaries (a body may span chunks; several
messages may share one).

Robustness is the point
-----------------------
A subprocess speaking a line protocol fails in specific, distinct ways, and each
must be handled as its own path rather than collapsed into a generic "error":

* **server crash mid-request** — stdout hits EOF; the read loop fails every
  in-flight request with :class:`ConnectionError` so an awaiting call unblocks
  immediately instead of waiting out its timeout;
* **malformed JSON line** — logged and skipped; one bad line must not kill the
  read loop and orphan every other pending request;
* **response with an unknown id** — logged and dropped; a reply we never asked
  for cannot resolve a request and must not raise;
* **request timeout** — the pending entry is abandoned and :class:`TimeoutError`
  is raised, distinct from a crash because the remedy differs (retry vs. respawn).

Correlation is by JSON-RPC ``id`` through a pending-futures table, so replies may
arrive out of order — which, over a duplex pipe with an async server, they will.

The session's own wall clock is deliberately not armed (``timeout_seconds=None``
by default): a session's silence is not idleness — an MCP server legitimately
waits between requests — so the per-call JSON-RPC budget is the real clock on
each exchange, and the session's memory ceiling and tree-kill hold the line
while it waits. Callers that want a lifetime budget pass one.

Stderr, honestly: the session contract carries no ``read_stderr`` — the backend
captures it in the background (so a chatty child cannot wedge on a full pipe)
and reports it in the final :class:`~paa.sandbox.base.SandboxResult` that
``close()`` returns. This client follows that contract: stderr is a teardown
fact, not a stream it can consume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.coder.journal import (
    LedgerToolJournal,
    args_preview_redacted,
    args_sha256_of,
)
from paa.coder.loop import ToolCtx
from paa.core.errors import PermissionDeniedError, SandboxError, SkillContractError
from paa.core.types import Permission, PermissionMode
from paa.ledger.events import canonical_json
from paa.sandbox.base import SandboxSpec
from paa.skills.adapters.base import SkillAdapter, SkillResult
from paa.skills.contracts import SkillContract, SkillInvocation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from paa.ledger.store import LedgerStore
    from paa.sandbox.base import Sandbox, SandboxResult, SandboxSession
    from paa.skills.adapters.base import SecretProvider

__all__ = ["MCPClient", "McpServerAdapter"]

log = structlog.get_logger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "paa-usa", "version": "4.1.0"}
_DEFAULT_TIMEOUT = 10.0
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9_.\-]+")

#: Ledger refusal reasons are bounded, exactly as the MCP server bounds its own.
_MAX_REASON_CHARS = 200

#: Read-side framing states. ``None`` (line mode) means the next complete line
#: is a payload unless it opens a ``Content-Length`` header block; the other two
#: states are *inside* such a block, held open across chunk boundaries by
#: :meth:`MCPClient._next_payload` until the body's bytes arrive.
_CL_DRAINING_HEADERS = "headers"
_CL_AWAITING_BODY = "body"


class MCPClient:
    """A JSON-RPC 2.0 client over a contained duplex session to one MCP server.

    Owns the :class:`~paa.sandbox.base.SandboxSession` and a single background
    read task. Requests are sent with monotonically increasing integer ids and
    resolved by :meth:`call` through a futures table, so the client is safe to
    drive concurrently. There is no unsandboxed spawn: :meth:`start` requires
    the containment backend, and the child is its session's — env allowlist,
    workspace jail, resource envelope, whole-tree kill on close.
    """

    def __init__(
        self,
        command: tuple[str, ...] | list[str],
        *,
        write_framing: str = "line",
    ) -> None:
        if write_framing not in ("line", "content-length"):
            raise ValueError(
                f"write_framing must be 'line' or 'content-length', got {write_framing!r}"
            )
        self._command = tuple(command)
        self._write_framing = write_framing
        self._session: SandboxSession | None = None
        #: The root child's PID, captured at open and kept after teardown: the
        #: callers that most need it are the post-mortem ones ("is anything
        #: from that tree still running?"), and the session reference is
        #: dropped on close.
        self._pid: int | None = None
        self._workspace: Path | None = None
        self._owns_workspace = False
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False
        #: Read-side reassembly state: the not-yet-complete bytes, plus the
        #: ``Content-Length`` block state when a header block is mid-flight.
        self._buffer = bytearray()
        self._cl_state: str | None = None
        self._cl_length = 0

    # -- lifecycle ---------------------------------------------------------

    async def start(
        self,
        sandbox: Sandbox,
        *,
        workspace: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        memory_mb: int = 256,
        cpu_cores: float = 0.5,
        allow_network: bool = False,
    ) -> None:
        """Open the server child as a contained duplex session and start reading.

        The :class:`SandboxSpec` is built here from the server definition: the
        command is argv (never a shell string), the workspace is the child's
        cwd jail — a fresh scratch directory the client owns and removes on
        close, unless the caller supplies one (then the caller owns it) — and
        ``env`` is the **explicit allowlist** merged over the backend's
        bootstrap set by :func:`~paa.sandbox.base.build_child_env`, never a
        copy of ``os.environ``. A secret reaches the child only by being placed
        in ``env`` deliberately; the adapter resolves its contracts'
        ``env_allowlist`` through the bound secret proxy to build exactly that.

        ``timeout_seconds`` is the session's **lifetime** wall clock and
        defaults to ``None`` (no deadline): an MCP server legitimately waits
        between requests, so the per-call budget on :meth:`call` is the real
        clock on each exchange. The memory ceiling and the tree-kill on close
        hold the line regardless.

        :raises paa.core.errors.SandboxError: the backend refused the session
            (it cannot deliver a duplex child) or could not start the process —
            a containment failure, kept distinct from a workload failure.
        """
        if self._session is not None or self._closed:
            raise RuntimeError("MCP client session was already started or closed")

        if workspace is None:
            ws = Path(tempfile.mkdtemp(prefix="paa-mcp-"))
            self._owns_workspace = True
        else:
            ws = Path(workspace)
        self._workspace = ws

        spec = SandboxSpec(
            command=self._command,
            workspace_path=ws,
            env=dict(env) if env else {},
            timeout_seconds=timeout_seconds,
            memory_mb=memory_mb,
            cpu_cores=cpu_cores,
            allow_network=allow_network,
        )
        try:
            self._session = await sandbox.open(spec)
        except BaseException:
            # A failed open leaves the scratch directory (and nothing else)
            # behind — clean it up before letting the containment failure
            # propagate as itself, not as a workload failure.
            if self._owns_workspace:
                _cleanup(ws)
                self._owns_workspace = False
            raise
        assert self._session is not None
        self._pid = self._session.pid
        self._reader_task = asyncio.create_task(self._read_loop())
        log.debug("skills.mcp.started", command=self._command[0])

    @property
    def workspace(self) -> Path | None:
        """The session's workspace jail, or ``None`` before :meth:`start`."""
        return self._workspace

    @property
    def pid(self) -> int | None:
        """The session's root child PID, or ``None`` before :meth:`start`.

        Kept readable after teardown, deliberately — the orphan check ("is
        anything from that tree still running?") runs after the kill.
        """
        return self._pid

    async def close(self) -> SandboxResult | None:
        """Tear the session down — whole-tree kill — and fail pending calls.

        Idempotent. The session's final
        :class:`~paa.sandbox.base.SandboxResult` is returned (``None`` if no
        session was ever established): it carries the backend's own
        classification of how the tree ended — ``killed_reason="closed"`` when
        the caller closed a live tree, the exit code when the root died on its
        own — which is the fact a caller journals or logs, not a guess. An
        owned scratch workspace is removed with it.

        Pending calls are failed with :class:`ConnectionError` after the
        teardown so an awaiting caller unblocks rather than waiting out its
        own timeout.
        """
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            # The task may end via cancellation or a pipe error; either way it is
            # already being torn down, so swallow whatever it raises on join.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        result: SandboxResult | None = None
        if self._session is not None:
            session, self._session = self._session, None
            result = await session.close()
        if self._owns_workspace and self._workspace is not None:
            _cleanup(self._workspace)
            self._owns_workspace = False
        self._fail_pending(ConnectionError("MCP client closed"))
        return result

    # -- read side ---------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._session is not None
        session = self._session
        try:
            while True:
                chunk = await session.read()
                if not chunk:
                    break  # EOF: the server exited (crash or clean shutdown)
                self._buffer += chunk
                # One chunk may hold several messages, or half of one; keep
                # popping until the buffer holds no complete message.
                while (payload := self._next_payload()) is not None:
                    self._dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead session must not escape and orphan callers
            log.warning("skills.mcp.read_loop_error", error=str(exc))
        finally:
            # Whatever ended the loop, no further replies are coming: unblock
            # every awaiting caller rather than let them wait out their timeouts.
            self._fail_pending(ConnectionError("MCP server stream closed"))

    def _next_payload(self) -> str | None:
        """Pop one complete wire message off the read buffer.

        Returns ``None`` when the buffer holds no complete message yet (the
        caller waits for more chunks), the message text otherwise. Blank lines
        are skipped; a ``Content-Length`` header line switches this message to
        header framing — the remaining header lines are drained, the blank
        separator line is consumed, then exactly ``Content-Length`` bytes of
        body are collected, across chunk boundaries if the body spans them.
        """
        while True:
            if self._cl_state == _CL_AWAITING_BODY:
                if len(self._buffer) < self._cl_length:
                    return None
                needed = self._cl_length
                body = bytes(self._buffer[:needed])
                del self._buffer[:needed]
                self._cl_state = None
                return body.decode("utf-8", errors="replace")

            newline = self._buffer.find(b"\n")
            if newline < 0:
                return None
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            header = line.decode("utf-8", errors="replace").strip()

            if not header:
                if self._cl_state == _CL_DRAINING_HEADERS:
                    # The blank separator line: the body follows.
                    self._cl_state = _CL_AWAITING_BODY
                continue

            if self._cl_state == _CL_DRAINING_HEADERS:
                # A further header line — consumed and ignored, exactly the
                # drain the stream-based reader performed before the session.
                continue

            if header.lower().startswith("content-length:"):
                try:
                    length = int(header.split(":", 1)[1].strip())
                except (IndexError, ValueError):
                    length = -1
                if length < 0:
                    log.warning("skills.mcp.bad_content_length", header=header)
                    # Same treatment the stream-based reader gave a bad header:
                    # hand the dispatcher an unparsable payload so the
                    # malformed-line path skips it and the loop lives on.
                    return ""
                self._cl_length = length
                self._cl_state = _CL_DRAINING_HEADERS
                continue

            # Line framing: the whole line is one JSON object.
            return header

    def _dispatch(self, payload: str) -> None:
        """Route one raw message to its pending future, or drop it."""
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            # Malformed line: skip it. Killing the loop here would orphan every
            # other in-flight request over a single bad frame.
            log.warning("skills.mcp.malformed_json", preview=payload[:120])
            return
        if not isinstance(message, dict):
            log.warning("skills.mcp.non_object_message")
            return

        message_id = message.get("id")
        if message_id is None:
            # A notification or a server-initiated request. This client does not
            # service server requests; ignore rather than misroute.
            return
        future = self._pending.pop(message_id, None)
        if future is None:
            # A reply to a request we never sent (or already timed out on).
            log.warning("skills.mcp.unknown_response_id", id=message_id)
            return
        if not future.done():
            future.set_result(message)

    def _fail_pending(self, exc: Exception) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -- write side --------------------------------------------------------

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 - JSON-RPC per-call budget
    ) -> dict[str, Any]:
        """Send a request and await its correlated reply.

        :raises ConnectionError: the client is not running, the pipe is broken,
            or the server exited before replying.
        :raises TimeoutError: no reply arrived within ``timeout`` seconds. The
            pending slot is abandoned so a late reply is treated as an unknown id
            rather than resolving a caller that has already given up.
        """
        if self._closed or self._session is None:
            raise ConnectionError("MCP client is not running")

        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future

        await self._send(
            {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise TimeoutError(f"MCP request {method!r} timed out after {timeout}s") from None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no reply expected)."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._session is None:
            raise ConnectionError("MCP client is not running")
        data = json.dumps(message)
        if self._write_framing == "content-length":
            frame = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n{data}".encode()
        else:
            frame = (data + "\n").encode("utf-8")
        try:
            await self._session.write(frame)
        except SandboxError as exc:
            # The session reports a gone pipe as SandboxError (its contract);
            # this client's contract reports it as ConnectionError, which the
            # adapter turns into an ok=False sample rather than a raise.
            raise ConnectionError("MCP server pipe is broken") from exc

    # -- MCP handshake -----------------------------------------------------

    async def initialize(self, *, timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any]:  # noqa: ASYNC109
        """Perform the MCP ``initialize`` handshake and confirm readiness."""
        reply = await self.call(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
            timeout=timeout,
        )
        _raise_on_rpc_error(reply, "initialize")
        await self.notify("notifications/initialized")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    async def list_tools(self, *, timeout: float = _DEFAULT_TIMEOUT) -> list[dict[str, Any]]:  # noqa: ASYNC109
        reply = await self.call("tools/list", {}, timeout=timeout)
        _raise_on_rpc_error(reply, "tools/list")
        result = reply.get("result") or {}
        tools = result.get("tools") if isinstance(result, dict) else None
        return list(tools) if isinstance(tools, list) else []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 - JSON-RPC per-call budget
    ) -> dict[str, Any]:
        reply = await self.call(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        _raise_on_rpc_error(reply, f"tools/call:{name}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}


class McpServerAdapter(SkillAdapter):
    """Exposes a self-hosted MCP server's tools as skills.

    Each public operation runs a full session — start, ``initialize``, do the
    work, close — over one contained duplex session, so a discovery and an
    invocation are each self-contained and leave no process behind.

    The policy gate is bound at construction: ``permission_mode`` (the active
    mode whose grants are the policy leg of ``manifest ∩ mandate ∩ policy``;
    ``ASK`` by default, the :class:`~paa.mcp.server.MCPServer` seam's default),
    ``mandate_permissions`` (``None`` imposes no constraint, tighten-only when
    present — the same seam semantics), and ``ledger`` (bound ⇒ every call is
    journalled in the ADR-0046 order; unbound ⇒ no rows, the gate still runs).
    ``sandbox`` is the backend used for discovery and as the fallback for
    invocation; a per-call ``sandbox`` on :meth:`invoke` overrides it.
    """

    def __init__(
        self,
        server_command: tuple[str, ...] | list[str],
        *,
        write_framing: str = "line",
        version: str = "0.1.0",
        sandbox: Sandbox | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        mandate_permissions: frozenset[Permission] | None = None,
        ledger: LedgerStore | None = None,
    ) -> None:
        if not server_command:
            raise ValueError("server_command must name an executable")
        self._server_command = tuple(server_command)
        self._write_framing = write_framing
        self._version = version
        self._sandbox = sandbox
        self._permission_mode = permission_mode
        self._mandate_permissions = mandate_permissions
        self._ledger = ledger

    @property
    def provider(self) -> str:
        return "mcp_server"

    # -- policy ------------------------------------------------------------

    def _grants(self) -> frozenset[Permission]:
        """The ``mandate ∩ policy`` legs of the intersection, tighten-only.

        The manifest leg is each contract's own ``required_permissions``
        (:meth:`SkillContract.missing_permissions` intersects it with what this
        returns). A ``None`` mandate imposes no constraint — the same semantics
        :func:`paa.pipelines.base.compute_grants` states for its constraint
        sources.
        """
        granted = self._permission_mode.granted
        if self._mandate_permissions is not None:
            granted &= self._mandate_permissions
        return granted

    def _journal_ctx(
        self, backend: Sandbox, contract: SkillContract, arguments: Mapping[str, Any]
    ) -> tuple[ToolCtx, uuid.UUID]:
        """The per-call context and lineage a tool-call lineage journals from.

        ``isolation_level`` is the backend's *actual* level (never requested),
        and the lineage is a fresh correlation per call — the same shape
        :class:`~paa.mcp.server.MCPServer` journals session-less tool calls
        with, so each call's rows are recovered and swept independently.
        """
        tool_name = contract.invocation.target
        lineage = uuid.uuid4()
        ctx = ToolCtx(
            session_id=str(lineage),
            call_id=uuid.uuid4().hex,
            tool=tool_name,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(tool_name, arguments),
            isolation_level=backend.isolation_level,
        )
        return ctx, lineage

    # -- sessions ----------------------------------------------------------

    @asynccontextmanager
    async def _session(
        self, backend: Sandbox, *, env: Mapping[str, str] | None = None
    ) -> AsyncIterator[MCPClient]:
        """One contained server session: start, initialize, yield, close.

        The session's wall clock is deliberately unbounded (the per-call
        JSON-RPC budget is the real clock on each exchange; see the module
        docstring); the memory ceiling and the whole-tree kill on close hold
        the containment regardless. ``env`` is the invocation's deliberate
        allowlist — never the ambient environment.
        """
        client = MCPClient(self._server_command, write_framing=self._write_framing)
        await client.start(backend, env=env)
        try:
            await client.initialize()
            yield client
        finally:
            await client.close()

    async def discover(self) -> list[SkillContract]:
        """Handshake, list the server's tools, and map each *granted* one.

        The tool allowlist is ``manifest ∩ mandate ∩ policy``: a tool whose
        ``required_permissions`` are not granted by the bound mode (and
        mandate) is **not exposed** — discovery must never surface a capability
        the runtime would then refuse to dispatch. A skipped tool is logged,
        not hidden: the mode and the missing permissions are both named.
        """
        if self._sandbox is None:
            raise SkillContractError(
                "mcp discovery requires a sandbox; none was provided",
            )
        async with self._session(self._sandbox) as client:
            tools = await client.list_tools()
        granted = self._grants()
        contracts: list[SkillContract] = []
        for tool in tools:
            try:
                contract = self._tool_to_contract(tool)
            except SkillContractError as exc:
                log.warning(
                    "skills.mcp.tool_skipped",
                    tool=tool.get("name") if isinstance(tool, dict) else None,
                    detail=str(exc),
                )
                continue
            missing = contract.missing_permissions(granted)
            if missing:
                log.warning(
                    "skills.mcp.tool_not_granted",
                    tool=contract.skill_name,
                    mode=self._permission_mode.value,
                    missing=[p.value for p in missing],
                )
                continue
            contracts.append(contract)
        return contracts

    def _tool_to_contract(self, tool: dict[str, Any]) -> SkillContract:
        raw_name = tool.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            raise SkillContractError("MCP tool has no name")
        name = _normalise_name(raw_name)

        raw_desc = tool.get("description")
        description = (
            raw_desc.strip()
            if isinstance(raw_desc, str) and len(raw_desc.strip()) >= 20
            else f"MCP tool '{raw_name}' served by a self-hosted MCP server."
        )

        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            input_schema = {"type": "object"}
        output_schema = tool.get("outputSchema")
        if not isinstance(output_schema, dict) or output_schema.get("type") != "object":
            output_schema = {"type": "object"}

        invocation = SkillInvocation(
            kind="mcp_tool",
            target=raw_name,  # the server's exact tool name, not the normalised id
            server_command=self._server_command,
        )
        return SkillContract.parse(
            {
                "skill_name": name,
                "provider": "mcp_server",
                "version": self._version,
                "description": description,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "risk_profile": 0.5,
                # The manifest leg of the intersection. A sandboxed tool always
                # needs PERM_SANDBOX_RUN — the claw_hub rule: an author who
                # forgets it gets a contract that can never be dispatched —
                # and MCP servers reach out of process, so egress is the
                # honest default.
                "required_permissions": [
                    Permission.SANDBOX_RUN.value,
                    Permission.NET_EGRESS.value,
                ],
                "invocation": invocation.model_dump(mode="json"),
            }
        )

    # -- invocation --------------------------------------------------------

    def _invocation_env(
        self, contract: SkillContract, secret_broker: SecretProvider | None
    ) -> dict[str, str]:
        """The deliberate child env: broker-resolved allowlist names only.

        ``invocation.env_allowlist`` names the secrets the server may see; each
        is resolved **through the bound secret proxy** (the host's
        ``PERM_SECRET_READ`` check happens inside the broker, and the value
        redacts itself everywhere but the explicit ``reveal()`` this path
        uses) and lands in the spec's explicit env allowlist — the
        ``SandboxSpec`` docstring's supported way to hand a workload a
        credential it genuinely needs. The parent environment is never a
        source: nothing ambient reaches the child by construction.

        Called only after the policy gate has passed, so a refused call never
        resolves — never even touches — a secret value.
        """
        names = contract.invocation.env_allowlist
        if not names:
            return {}
        if secret_broker is None:
            raise SkillContractError(
                "the invocation names env_allowlist secrets but no secret broker "
                "was bound to this dispatch",
                skill_name=contract.skill_name,
            )
        env: dict[str, str] = {}
        for secret_name in names:
            # ``reveal()`` is the one greppable way out of redaction; this is
            # its deliberate use site, and the value goes nowhere but the spec
            # env — never a log line, never a ledger payload.
            env[secret_name] = secret_broker.get_secret(secret_name).reveal()
        return env

    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - deliberate per-call budget
        secret_broker: SecretProvider | None = None,
    ) -> SkillResult:
        """Call one tool over a fresh contained session and normalise its result.

        ``sandbox`` is the containment backend for this call, falling back to
        the adapter's own bound sandbox; with neither, the call is refused —
        an MCP server is out-of-process code and this repo does not run that
        unsandboxed.

        The deterministic gate runs **before the server child is spawned**: the
        contract's ``required_permissions`` (the manifest leg) must survive
        ``mandate ∩ policy``. A refusal is journalled as a ``TOOL_CALL_DENIED``
        row — proof the tool never executed — and raised as
        :class:`~paa.core.errors.PermissionDeniedError`, the same refusal the
        Unified Skill Adapter raises at its own gate, so the reliability
        optimiser is never fed a policy refusal as a skill failure.
        """
        backend = sandbox if sandbox is not None else self._sandbox
        if backend is None:
            raise SkillContractError(
                "mcp tools require a sandbox; none was provided",
                skill_name=contract.skill_name,
            )
        tool_name = contract.invocation.target
        call_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        started = time.perf_counter()

        # An unformable call costs no lineage: the env-formability check (and
        # the broker's own permission gate, if it fires) precedes the PROPOSED
        # row, exactly as the MCP server resolves sessions before journalling.
        if contract.invocation.env_allowlist and secret_broker is None:
            raise SkillContractError(
                "the invocation names env_allowlist secrets but no secret broker "
                "was bound to this dispatch",
                skill_name=contract.skill_name,
            )

        ctx, lineage = self._journal_ctx(backend, contract, arguments)
        journal = (
            LedgerToolJournal(self._ledger, lineage) if self._ledger is not None else None
        )
        if journal is not None:
            # Ledger-before-effect: the PROPOSED row precedes the gate and any
            # spawn, so a crash after it leaves a row the sweep classifies.
            await journal.before(ctx)

        missing = contract.missing_permissions(self._grants())
        if missing:
            reason = (
                f"permissions not granted in {self._permission_mode.value}: "
                f"{sorted(p.value for p in missing)}"
            )
            if (
                self._mandate_permissions is not None
                and missing != contract.missing_permissions(self._permission_mode.granted)
            ):
                # The mode alone would have granted these: the mandate leg is
                # what bound them out, and the ledger row says so rather than
                # attributing the refusal to the mode.
                reason += " — the mandate allowlist omits them"
            if journal is not None:
                await journal.denied(ctx, reason[:_MAX_REASON_CHARS])
            log.warning(
                "skills.mcp.permission_denied",
                skill=contract.skill_name,
                mode=self._permission_mode.value,
                missing=[p.value for p in missing],
            )
            raise PermissionDeniedError(
                missing=[p.value for p in missing], mode=self._permission_mode.value
            )

        env = self._invocation_env(contract, secret_broker)
        try:
            async with self._session(backend, env=env) as client:
                result = await client.call_tool(tool_name, arguments, timeout=call_timeout)
        except asyncio.CancelledError:
            # The cancel is an outcome, not a silent death: the FAILED row
            # lands before the CancelledError reaches the caller, the session
            # teardown (already run by the context manager) has killed the
            # tree, and the sweep never sees an outcome-less PROPOSED.
            if journal is not None:
                await journal.after(ctx, ok=False, result_sha=None)
            raise
        except TimeoutError as exc:
            if journal is not None:
                await journal.after(ctx, ok=False, result_sha=None)
            return SkillResult(
                ok=False,
                error=f"MCP tool timed out: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except ConnectionError as exc:
            if journal is not None:
                await journal.after(ctx, ok=False, result_sha=None)
            return SkillResult(
                ok=False,
                error=f"MCP server connection failed: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except SkillContractError as exc:
            if journal is not None:
                await journal.after(ctx, ok=False, result_sha=None)
            return SkillResult(
                ok=False,
                error=f"MCP tool returned an error: {exc.message}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        output, is_error = _normalise_tool_result(result)
        result_sha = None if is_error else _result_sha256(output)
        if journal is not None:
            # A COMPLETED row attests the result hash; an ``isError`` result is
            # still a completed *protocol* operation whose tool failed, so it
            # gets FAILED with no result hash — the coder journal's contract.
            await journal.after(ctx, ok=not is_error, result_sha=result_sha)
        if is_error:
            return SkillResult(
                ok=False,
                output=output,
                error="MCP tool reported isError=true",
                latency_ms=latency_ms,
            )
        return SkillResult(ok=True, output=output, latency_ms=latency_ms, exit_code=0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _raise_on_rpc_error(reply: dict[str, Any], context: str) -> None:
    """Turn a JSON-RPC ``error`` object into a :class:`SkillContractError`."""
    error = reply.get("error")
    if error is not None:
        message = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        raise SkillContractError(f"MCP {context} failed: {message}", detail={"code": code})


def _normalise_tool_result(result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Reduce an MCP ``tools/call`` result to ``(output_dict, is_error)``.

    Prefers ``structuredContent`` when the tool provides it — that is already the
    structured output a contract's ``output_schema`` describes. Otherwise the
    ``content`` blocks are carried through under a ``content`` key so nothing is
    lost, and a single text block that itself parses as a JSON object is unwrapped
    (many tools return their real payload as a JSON string in one text block).
    """
    is_error = bool(result.get("isError", False))

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, is_error

    content = result.get("content")
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed, is_error

    return {"content": content if isinstance(content, list) else []}, is_error


def _normalise_name(raw: str) -> str:
    """Coerce an MCP tool name toward ``SKILL_NAME_PATTERN`` (lower, dash-safe)."""
    lowered = raw.strip().lower().replace(" ", "-").replace("/", ".")
    cleaned = _INVALID_NAME_CHARS.sub("-", lowered).strip("-.")
    return cleaned or "mcp-tool"


def _result_sha256(payload: dict[str, Any]) -> str:
    """The result hash a COMPLETED row attests (canonical JSON).

    The same computation :mod:`paa.mcp.server` uses for its own COMPLETED rows,
    so an MCP tool call is attested identically whichever side of the pipe
    journalled it.
    """
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _cleanup(path: Path) -> None:
    """Best-effort scratch-workspace removal (the claw_hub teardown rule)."""
    shutil.rmtree(path, ignore_errors=True)
