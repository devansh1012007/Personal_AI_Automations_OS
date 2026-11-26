"""``paa acp`` — the ACP fronts (SPEC 2 §6, C7.6).

Two commands, one wire vocabulary (:mod:`paa.acp.protocol`), one framing kit
(:mod:`paa.mcp.protocol`):

* ``paa acp serve`` — the **T1** front: a stdio Agent Client Protocol
  server over PAA's chat loop, spawned by an editor (Zed is the reference
  ACP client). Initialize, ``session/new`` bound to the editor's cwd,
  streamed ``session/update`` chunks, working cancel, a scoped
  ``fs/read_text_file``.
* ``paa acp client -- <agent-cmd>`` — the **T2** front: spawn an external
  ACP agent behind a :class:`~paa.sandbox.base.SandboxSession`, drive one
  turn with streamed updates, and mediate — the agent's permission asks are
  decided by the deterministic tool policy (an ``ask`` resolves to a
  recorded ``denied_unattended``), its file reads are served from the
  workspace jail, everything mediated lands in the ledger.

Both commands write protocol frames to stdout and nothing else when they are
speaking a protocol (``serve``); the client renders a human-readable
transcript, because its stdout is a *display*, not a wire.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    from paa.chat.extensions import ChatExtensionSurface
    from paa.runtime import Runtime

console = Console()

app = typer.Typer(
    name="acp",
    help="Agent Client Protocol v1: serve PAA to editors, or mediate an external agent.",
    no_args_is_help=True,
)

#: The policy modes the client front accepts (the recorder's own set — one
#: vocabulary for every mediated surface).
_POLICY_MODES = ("ask", "accept_edits", "plan", "bypass")


class _StdoutSink:
    """The :class:`paa.mcp.protocol.FrameSink` for real stdio.

    Writes through ``sys.stdout.buffer`` — the raw byte stream — so
    text-mode translation can never rewrite a frame. The same discipline
    ``paa mcp serve`` and ``paa record proxy`` state for their own writes.
    """

    def __init__(self) -> None:
        self._buffer = sys.stdout.buffer

    def write(self, data: bytes) -> None:
        self._buffer.write(data)

    async def drain(self) -> None:
        self._buffer.flush()


async def _stdin_reader() -> asyncio.StreamReader:
    """A stream reader over stdin, the asyncio way (the mcp serve pattern)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (NotImplementedError, ValueError) as exc:
        raise SystemExit(
            "paa acp serve must be spawned with a pipe on stdin; see your "
            "editor's ACP agent configuration."
        ) from exc
    return reader


@app.command("serve")
def serve() -> None:
    """Serve PAA's chat loop over stdio as an ACP agent (JSON-RPC 2.0, NDJSON)."""
    from paa.acp.server import ACPServer
    from paa.chat.service import ChatService
    from paa.runtime import Runtime

    async def _serve() -> None:
        runtime = await Runtime.build()
        try:
            server = ACPServer(
                chat=ChatService.from_runtime(
                    runtime, extensions=_extension_surface(runtime)
                )
            )
            await server.serve(await _stdin_reader(), _StdoutSink())
        finally:
            await runtime.close()

    asyncio.run(_serve())


def _extension_surface(runtime: Runtime) -> ChatExtensionSurface | None:
    """The extension surface the ACP-served chat carries (C8.4).

    An editor's session is a chat session like any other host's, so it
    sees the same capability catalogue a ``paa chat`` turn would:
    installed skills and MCP-tool rows, policy-filtered, dispatchable
    through the ``@use-skill`` convention. The surface is the one
    ``paa skills`` constructs over the same live runtime — same ledger,
    same db, same sandbox, same journal — with the one operator decision
    this front cannot make itself settled conservatively:
    ``allow_unverified=False``, because the ACP door is a *consumer* of
    installed capabilities, not an installer — installing through an
    editor session would be the signature gate's opt-in made by somebody
    other than the operator, so the front does not offer it. Installs
    happen through ``paa skills install``; the editor sees what that
    installed, and nothing more.

    The runtime's own policy still governs every dispatch: a capability
    the mode would refuse never reaches the prompt, and a directive for
    one that slipped past a mid-turn mode change is refused at the USA's
    gate and journalled ``TOOL_CALL_DENIED``.
    """
    from paa.cli.skills import _surface

    return _surface(runtime, allow_unverified=False)


@app.command("client")
def client(
    agent_command: list[str] = typer.Argument(
        ..., help="The external ACP agent argv, after '--'."
    ),
    workspace: Path = typer.Option(
        Path.cwd(),
        "--workspace",
        help="The agent's workspace jail: its cwd, and the root its file reads are scoped to.",
    ),
    prompt: str = typer.Option(
        ..., "--prompt", help="The one prompt turn to drive (v1: one turn per run)."
    ),
    timeout_s: float = typer.Option(
        120.0, "--timeout", help="Budget on the prompt turn, seconds."
    ),
    mode: str = typer.Option(
        "ask",
        "--mode",
        help="Policy mode for mediated tool calls: ask, accept_edits, plan, bypass.",
    ),
    allow: list[str] = typer.Option(
        None, "--allow", help="Pre-allow rule, e.g. 'bash(git *)' (repeatable)."
    ),
    deny: list[str] = typer.Option(
        None, "--deny", help="Deny rule — always wins over allow (repeatable)."
    ),
    ask: list[str] = typer.Option(
        None, "--ask", help="Force-ask rule (resolves to a recorded denial)."
    ),
) -> None:
    """Drive one mediated prompt turn through an external ACP agent (the T2 front)."""
    from paa.acp.client import AcpClient
    from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
    from paa.config import get_settings
    from paa.core.errors import PaaError
    from paa.runtime import Runtime
    from paa.sandbox import get_sandbox

    if not agent_command:
        console.print("[red]no agent command: paa acp client -- <agent-cmd>[/red]")
        raise typer.Exit(2)
    if mode not in _POLICY_MODES:
        console.print(f"[red]--mode must be one of {_POLICY_MODES}, got {mode!r}[/red]")
        raise typer.Exit(2)
    policy = ToolPermissionPolicy(
        mode=PermissionMode(mode),
        allow=list(allow or []),
        deny=list(deny or []),
        ask=list(ask or []),
    )

    async def _run() -> str:
        settings = get_settings()
        sandbox = await get_sandbox(settings.sandbox)
        # The runtime is built for the ledger the mediation journals into —
        # an unjournalled boundary is the thing this front exists to prevent.
        runtime = await Runtime.build()
        try:
            acp = AcpClient(agent_command, policy=policy, ledger=runtime.ledger)
            await acp.start(sandbox, workspace=workspace.resolve())

            async def render(update: dict[str, Any]) -> None:
                _render_update(console, update)

            await acp.initialize()
            session_id = await acp.new_session()
            stop = await acp.prompt(
                session_id, prompt, on_update=render, timeout_seconds=timeout_s
            )
            await acp.close()
            return stop
        finally:
            await runtime.close()

    try:
        stop = asyncio.run(_run())
    except (PaaError, ConnectionError, TimeoutError, RuntimeError) as exc:
        console.print(f"[red]acp client failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    colour = "green" if stop == "end_turn" else "yellow"
    console.print(f"[{colour}]stop reason: {stop or 'none'}[/{colour}]")


def _render_update(out: Console, update: dict[str, Any]) -> None:
    """One streamed ``session/update`` payload, rendered for a human.

    The mediation's journal is the ledger's business; this rendering is the
    operator's live view — message chunks inline, tool calls as titled
    rows, anything else as a dim one-liner (v1 emits message chunks only,
    but an agent's future variants must not render as silence).
    """
    inner = update.get("update")
    if not isinstance(inner, dict):
        return
    kind = inner.get("sessionUpdate")
    if kind == "agent_message_chunk":
        content = inner.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        if isinstance(text, str) and text:
            out.print(text, end="", markup=False, highlight=False)
            if text.endswith(("\n", " ")):
                out.print()
        return
    if kind in ("tool_call", "tool_call_update"):
        title = inner.get("title") or inner.get("toolCallId") or kind
        status = inner.get("status")
        suffix = f" [dim]({status})[/dim]" if status else ""
        out.print(f"[cyan]tool:[/cyan] {title}{suffix}", markup=True)
        return
    out.print(f"[dim]{kind}[/dim]", markup=True)
