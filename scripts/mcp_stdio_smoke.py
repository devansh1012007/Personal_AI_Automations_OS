"""Live MCP stdio round-trip against the real `paa mcp serve` child.

The in-process suite (``tests/mcp_server/``) drives ``MCPServer`` methods
directly; this script drives the *command* the way a real MCP client
(Claude Code, Codex, Cursor, OpenCode) does: spawn ``paa mcp serve`` with
pipes, speak NDJSON JSON-RPC 2.0 over stdin/stdout, and assert the facts
flow. It is the executable form of the manual smoke scripted in
``docs/interfaces/mcp-server.md`` — the one that found the stdout-pollution
bug (unconfigured structlog printed boot logs onto the protocol stream).

Offline by design: the chat side is driven by the echo model, and the fact
is written through the ``memory_remember`` tool, so the whole round-trip
needs no network and no key.

Usage::

    PAA_HOME=/tmp/paa-mcp-smoke python scripts/mcp_stdio_smoke.py

Exit 0 with a SEARCH-HIT line on success; non-zero with the failing step
named on any break.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

#: The server child command. The console script is used when present (the
#: documented spawn block says ``"command": "paa"``); the import-and-run
#: fallback covers environments that installed the package without the
#: entry point.
_COMMAND: tuple[tuple[str, ...], ...] = (
    ("paa", "mcp", "serve"),
    (sys.executable, "-c", "from paa.cli.main import app; app()", "mcp", "serve"),
)

#: Per-request wall clock. Generous on purpose: the child boots a full
#: runtime (SQLite, ledger, memory) before its first reply.
_TIMEOUT = 60.0

#: How long to wait for the child to be gone after ``kill -9``.
_REAP_GRACE = 5.0


class SmokeFailure(RuntimeError):
    """One named step of the smoke failed; the message says which."""


def _server_command() -> tuple[str, ...]:
    for command in _COMMAND:
        if shutil.which(command[0]) is not None or command[0] == sys.executable:
            return command
    raise SmokeFailure("no usable paa entry point on PATH")


class StdioClient:
    """A minimal NDJSON JSON-RPC client over the server child's pipes."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._next_id = 0

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(request) + "\n").encode())
        await self._proc.stdin.drain()

        assert self._proc.stdout is not None
        while True:
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=_TIMEOUT)
            if not raw:
                raise SmokeFailure(
                    f"server closed stdout before answering {method!r} "
                    f"(exit={self._proc.returncode})"
                )
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # The stdout-discipline contract: every line on stdout is a frame.
            # A line that does not parse as JSON-RPC *is* the failure mode this
            # smoke exists to catch, so it fails loudly, not silently.
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SmokeFailure(
                    f"non-JSON line on the protocol stream answering {method!r}: "
                    f"{line[:120]!r}"
                ) from exc
            if not isinstance(message, dict) or message.get("id") != self._next_id:
                continue  # a notification or someone else's reply
            return message

    async def notify(self, method: str) -> None:
        assert self._proc.stdin is not None
        frame = {"jsonrpc": "2.0", "method": method}
        self._proc.stdin.write((json.dumps(frame) + "\n").encode())
        await self._proc.stdin.drain()

    async def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=_REAP_GRACE)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()


async def _round_trip() -> None:
    home = Path(tempfile.mkdtemp(prefix="paa-mcp-smoke-"))
    env = {
        **os.environ,
        "PAA_HOME": str(home),
        # Deterministic sleep-free logs are not needed; stderr just must exist.
        "PAA_OBSERVABILITY__LOG_LEVEL": "INFO",
    }
    command = _server_command()
    print(f"spawn: {' '.join(command)}  (PAA_HOME={home})")
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    client = StdioClient(proc)
    try:
        reply = await client.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "paa-stdio-smoke", "version": "1"},
            },
        )
        _raise_on_rpc(reply, "initialize")
        info = reply.get("result", {}).get("serverInfo", {})
        print(f"initialize ok — server: {info.get('name', '?')} {info.get('version', '')}")
        await client.notify("notifications/initialized")

        reply = await client.call("tools/list", {})
        _raise_on_rpc(reply, "tools/list")
        tools = [t.get("name") for t in reply.get("result", {}).get("tools", [])]
        print(f"tools/list ok — {len(tools)} tools: {', '.join(tools)}")
        for expected in ("memory_remember", "memory_search", "sessions_list"):
            if expected not in tools:
                raise SmokeFailure(f"tool {expected!r} missing from tools/list")

        reply = await client.call(
            "tools/call",
            {
                "name": "sessions_list",
                "arguments": {},
            },
        )
        _raise_on_rpc(reply, "sessions_list")

        reply = await client.call(
            "tools/call",
            {
                "name": "chat_send",
                "arguments": {"message": "remember that deploys freeze on Fridays"},
            },
        )
        _raise_on_rpc(reply, "chat_send")
        result = _tool_payload(reply)
        session_id = result.get("session_id")
        if not session_id:
            raise SmokeFailure(f"chat_send returned no session_id: {str(result)[:200]}")
        print(f"chat_send ok — session {str(session_id)[:8]}…, status {result.get('status')}")

        reply = await client.call(
            "tools/call",
            {
                "name": "memory_remember",
                "arguments": {
                    "session_id": session_id,
                    "statement": "deploys freeze on Fridays",
                    "provenance": {"source": "stdio-smoke"},
                },
            },
        )
        _raise_on_rpc(reply, "memory_remember")
        print("memory_remember ok — statement signalled")

        reply = await client.call(
            "tools/call",
            {
                "name": "memory_search",
                "arguments": {"query": "deploys freeze", "limit": 5},
            },
        )
        _raise_on_rpc(reply, "memory_search")
        blob = json.dumps(reply)
        hit = "friday" in blob.lower()
        print(f"memory_search ok — SEARCH-HIT: {hit}")
        if not hit:
            raise SmokeFailure(
                "memory_search did not return the fact written this run: "
                f"{blob[:400]}"
            )

        await client.close()
        if proc.returncode not in (0, None):
            raise SmokeFailure(f"server exited {proc.returncode} after stdin EOF")
        print("SMOKE OK — the documented Claude Code spawn path works end to end")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        stderr = proc.stderr
        if stderr is not None:
            err = await stderr.read()
            if err:
                tail = err.decode("utf-8", errors="replace").splitlines()[-8:]
                print("--- server stderr (last lines) ---")
                for line in tail:
                    print(f"    {line}")
        shutil.rmtree(home, ignore_errors=True)


def _tool_payload(reply: dict[str, Any]) -> dict[str, Any]:
    """Reduce one ``tools/call`` reply to its payload mapping.

    The same normalisation :class:`paa.skills.adapters.mcp.MCPClient` applies:
    ``structuredContent`` when present, else a single text block that parses
    as a JSON object, else the raw content list.
    """
    result = reply.get("result", {})
    if not isinstance(result, dict):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
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
                    return parsed
    return result


def _raise_on_rpc(reply: dict[str, Any], step: str) -> None:
    error = reply.get("error")
    if isinstance(error, dict):
        raise SmokeFailure(f"{step} returned JSON-RPC error {error.get('code')}: "
                            f"{error.get('message')}")


if __name__ == "__main__":
    try:
        asyncio.run(_round_trip())
    except SmokeFailure as exc:
        print(f"SMOKE FAILED — {exc}")
        sys.exit(1)
