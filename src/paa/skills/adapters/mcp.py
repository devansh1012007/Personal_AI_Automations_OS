"""An embedded MCP client — ``provider='mcp_server'``.

This is the "self-hosted MCP server to MCP" half of the user's request: the
runtime speaks the Model Context Protocol (JSON-RPC 2.0 over stdio) to a server
it launches as a subprocess, discovers that server's tools, and exposes each as a
:class:`~paa.skills.contracts.SkillContract`.

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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.errors import SkillContractError
from paa.skills.adapters.base import SkillAdapter, SkillResult
from paa.skills.contracts import SkillContract, SkillInvocation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from paa.sandbox.base import Sandbox
    from paa.skills.adapters.base import SecretProvider

__all__ = ["MCPClient", "McpServerAdapter"]

log = structlog.get_logger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "paa-usa", "version": "4.1.0"}
_DEFAULT_TIMEOUT = 10.0
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9_.\-]+")


class MCPClient:
    """A JSON-RPC 2.0 client over a persistent stdio pipe to one MCP server.

    Owns the subprocess and a single background read task. Requests are sent with
    monotonically increasing integer ids and resolved by :meth:`call` through a
    futures table, so the client is safe to drive concurrently.
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
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Launch the server subprocess and begin reading its stdout."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        log.debug("skills.mcp.started", command=self._command[0])

    async def close(self) -> None:
        """Terminate the server and cancel the read task. Idempotent."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            # The task may end via cancellation or a pipe error; either way it is
            # already being torn down, so swallow whatever it raises on join.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._proc is not None:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except TimeoutError:  # pragma: no cover - slow shutdown
                    self._proc.kill()
                    await self._proc.wait()
            self._proc = None
        self._fail_pending(ConnectionError("MCP client closed"))

    # -- read side ---------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break  # EOF: the server exited (crash or clean shutdown)
                header = line.decode("utf-8", errors="replace").strip()
                if not header:
                    continue
                if header.lower().startswith("content-length:"):
                    payload = await self._read_content_length(stdout, header)
                    if payload is None:
                        break
                else:
                    payload = header
                self._dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken pipe must not escape and orphan callers
            log.warning("skills.mcp.read_loop_error", error=str(exc))
        finally:
            # Whatever ended the loop, no further replies are coming: unblock
            # every awaiting caller rather than let them wait out their timeouts.
            self._fail_pending(ConnectionError("MCP server stream closed"))

    async def _read_content_length(
        self, stdout: asyncio.StreamReader, first_header: str
    ) -> str | None:
        """Consume an LSP-style header block and return the body text."""
        try:
            length = int(first_header.split(":", 1)[1].strip())
        except (IndexError, ValueError):
            log.warning("skills.mcp.bad_content_length", header=first_header)
            return ""
        # Drain any remaining headers up to the blank separator line.
        while True:
            extra = await stdout.readline()
            if not extra or extra.strip() == b"":
                break
        try:
            body = await stdout.readexactly(length)
        except asyncio.IncompleteReadError:
            return None  # truncated body == stream died
        return body.decode("utf-8", errors="replace")

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
        if self._closed or self._proc is None:
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
        if self._proc is None or self._proc.stdin is None:
            raise ConnectionError("MCP client stdin is not available")
        data = json.dumps(message)
        if self._write_framing == "content-length":
            frame = f"Content-Length: {len(data.encode('utf-8'))}\r\n\r\n{data}".encode()
        else:
            frame = (data + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(frame)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
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
    work, close — over one persistent pipe, so a discovery and an invocation are
    each self-contained and leave no subprocess behind.
    """

    def __init__(
        self,
        server_command: tuple[str, ...] | list[str],
        *,
        write_framing: str = "line",
        version: str = "0.1.0",
    ) -> None:
        if not server_command:
            raise ValueError("server_command must name an executable")
        self._server_command = tuple(server_command)
        self._write_framing = write_framing
        self._version = version

    @property
    def provider(self) -> str:
        return "mcp_server"

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[MCPClient]:
        client = MCPClient(self._server_command, write_framing=self._write_framing)
        await client.start()
        try:
            await client.initialize()
            yield client
        finally:
            await client.close()

    async def discover(self) -> list[SkillContract]:
        """Handshake, list the server's tools, and map each to a contract."""
        async with self._session() as client:
            tools = await client.list_tools()
        contracts: list[SkillContract] = []
        for tool in tools:
            try:
                contracts.append(self._tool_to_contract(tool))
            except SkillContractError as exc:
                log.warning(
                    "skills.mcp.tool_skipped",
                    tool=tool.get("name") if isinstance(tool, dict) else None,
                    detail=str(exc),
                )
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
                # MCP servers reach out of process; egress is the honest default.
                "required_permissions": ["PERM_NET_EGRESS"],
                "invocation": invocation.model_dump(mode="json"),
            }
        )

    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - deliberate per-call budget
        secret_broker: SecretProvider | None = None,
    ) -> SkillResult:
        """Call one tool over a fresh session and normalise its result.

        ``sandbox`` is unused: an MCP server is its own subprocess and provides
        its own isolation boundary, so there is nothing to mount here.
        """
        tool_name = contract.invocation.target
        call_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        started = time.perf_counter()
        try:
            async with self._session() as client:
                result = await client.call_tool(
                    tool_name, arguments, timeout=call_timeout
                )
        except TimeoutError as exc:
            return SkillResult(
                ok=False,
                error=f"MCP tool timed out: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except ConnectionError as exc:
            return SkillResult(
                ok=False,
                error=f"MCP server connection failed: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except SkillContractError as exc:
            return SkillResult(
                ok=False,
                error=f"MCP tool returned an error: {exc.message}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        output, is_error = _normalise_tool_result(result)
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
