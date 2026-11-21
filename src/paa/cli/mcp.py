"""``paa mcp`` — the MCP server sub-app (SPEC 2 §3.1 C4.5).

``paa mcp serve`` runs the stdio MCP server: JSON-RPC 2.0, one NDJSON object
per line, over stdin/stdout, designed to be *spawned by* an MCP client
(Claude Code, Codex, Cursor, OpenCode) rather than listened-with. The
module-level ``app`` is the standard typer sub-app export, wired into the
root CLI by the orchestrator (see ``paa.cli.main``).

Token
-----
``--token-env`` names the environment variable that holds the token (never
the token itself — config and flags carry *names* of secrets, values stay in
the environment, the same rule every ``*_env`` setting follows). When the
named variable holds a non-empty value, the client must present that token
in its ``initialize`` params (``params.token``) before any method answers;
when it is unset, the spawn pipe is the trust boundary and no token is
required. Refused handshakes are journaled (``POLICY_BLOCKED``) and never
carry the token value.

The command writes protocol frames to stdout and nothing else — every log
line and every library print must stay off stdout or it corrupts the wire.
"""

from __future__ import annotations

import asyncio
import os
import sys

import typer

from paa.config import MCPSettings, get_settings

__all__ = ["app"]

app = typer.Typer(
    name="mcp",
    help="Expose PAA memory and chat as MCP tools (stdio; spawn me from your harness).",
    no_args_is_help=True,
)


class _StdoutSink:
    """The :class:`paa.mcp.protocol.FrameSink` for real stdio.

    Writes through ``sys.stdout.buffer`` — the raw byte stream — so text-mode
    translation can never rewrite a frame (the same discipline the fake MCP
    server in ``tests/skills/test_mcp.py`` states for its own writes).
    """

    def __init__(self) -> None:
        self._buffer = sys.stdout.buffer

    def write(self, data: bytes) -> None:
        self._buffer.write(data)

    async def drain(self) -> None:
        self._buffer.flush()


async def _resolve_token(token_env: str | None) -> str | None:
    """The configured token value, or ``None`` to run unauthenticated.

    The environment wins; the process ``.env`` is the fallback (the same
    two-step every provider key resolves with — see
    :func:`paa.config.dotenv_value`). Only the *name* of the variable is ever
    configured or logged; the value goes nowhere but the comparison.
    """
    from paa.config import dotenv_value

    name = token_env if token_env else get_settings().mcp.token_env
    value = os.environ.get(name) or dotenv_value(name)
    return value or None


async def _stdin_reader() -> asyncio.StreamReader:
    """A stream reader over stdin, the asyncio way.

    ``connect_read_pipe`` is POSIX plumbing; on a loop that cannot wrap stdin
    (or when stdin is a console rather than a pipe) the failure is fatal and
    said plainly — a server that cannot hear its client must refuse to start,
    not start deaf.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except NotImplementedError as exc:  # pragma: no cover - platform-specific
        raise SystemExit(
            "paa mcp serve requires a stdin pipe this event loop can read "
            "(POSIX). Run under WSL2 on Windows."
        ) from exc
    except ValueError as exc:  # stdin is a console, not a pipe
        raise SystemExit(
            "paa mcp serve must be spawned with a pipe on stdin; see your "
            "harness's MCP server configuration."
        ) from exc
    return reader


async def _serve(token_env: str | None, mcp_settings: MCPSettings) -> None:
    """Build the runtime, open the stdio pipes, serve until EOF."""
    from paa.mcp.server import MCPServer
    from paa.runtime import Runtime

    runtime = await Runtime.build()
    try:
        server = await MCPServer.from_runtime(
            runtime,
            mcp_settings=mcp_settings,
            expected_token=await _resolve_token(token_env),
        )
        await server.serve(await _stdin_reader(), _StdoutSink())
    finally:
        await runtime.close()


@app.command("serve")
def serve(
    token_env: str | None = typer.Option(
        None,
        "--token-env",
        help="NAME of the env var holding the MCP token (never the token itself). "
        "Defaults to PAA_MCP__TOKEN_ENV's value, then PAA_MCP_TOKEN. When the "
        "named variable is set, clients must present it in initialize; when "
        "unset, the spawn pipe is the trust boundary.",
    ),
) -> None:
    """Serve PAA memory + chat over stdio as an MCP server (JSON-RPC 2.0, NDJSON)."""
    asyncio.run(_serve(token_env, get_settings().mcp))
