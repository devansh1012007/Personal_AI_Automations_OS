"""``paa agent`` — drive the runtime with a tool-using model.

This is the only command that assembles :mod:`paa.coder` into something runnable.
Everything it does is composition: build a real
:class:`~paa.runtime.Runtime`, let the model see the runtime's ledger through
:mod:`paa.coder.runtime_tools`, and gate every call through the permission
policy. There is no hidden policy here — the flags map one-to-one onto
:class:`~paa.coder.permissions.ToolPermissionPolicy` arguments.

Provider selection is entirely ``PAA_MODELS__*`` configuration, so this command
never names a vendor. To run against OpenRouter::

    export OPENROUTER_API_KEY=sk-or-...
    export PAA_MODELS__LOCAL_PROVIDER=openrouter
    export PAA_MODELS__LOCAL_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
    export PAA_MODELS__ESCALATION_PROVIDER=none
    paa agent --list-tools                          # catalogue, no tokens spent
    paa agent "which backends are live, and what is open?"

Note the ``LOCAL_PROVIDER`` slot holds a hosted model here. That is a naming
artefact of the router's two-tier shape (first tier / second tier), not a claim
about locality: :class:`~paa.models.base.ModelProvider.tier` still comes from
the registry row, which declares ``openrouter`` as
:class:`~paa.models.base.ModelTier.FRONTIER`, so the privacy accounting and the
``leaves_machine`` flag in any escalation record stay truthful.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from paa.coder.permissions import PermissionMode

__all__ = ["register_agent"]

console = Console()


def register_agent(app: typer.Typer) -> None:
    """Attach the ``agent`` command to the CLI."""

    @app.command("agent")
    def agent(
        prompt: str | None = typer.Argument(
            None, help="What the agent should do or investigate. Required unless --list-tools."
        ),
        mode: str = typer.Option(
            "ask",
            "--mode",
            help="ask | plan (read-only) | accept_edits | bypass.",
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            help="Auto-approve every ASK. Without this the agent stops and waits.",
        ),
        max_steps: int = typer.Option(25, "--max-steps", help="Cap on think→act cycles."),
        allow: list[str] = typer.Option(
            None, "--allow", help="Pre-allow a rule, e.g. `read_ledger`."
        ),
        deny: list[str] = typer.Option(
            None, "--deny", help="Refuse a tool outright. Wins even under --mode bypass."
        ),
        resume: str = typer.Option(
            None, "--resume", help="Resume a transcript by session id."
        ),
        list_tools: bool = typer.Option(
            False, "--list-tools", help="Print the tool catalogue and exit."
        ),
    ) -> None:
        """Run one turn of the tool-using agent against this runtime."""

        try:
            resolved_mode = PermissionMode(mode.lower())
        except ValueError:
            console.print(
                f"[red]unknown mode {mode!r}[/red] — "
                f"expected one of {', '.join(m.value for m in PermissionMode)}"
            )
            raise typer.Exit(1) from None

        if prompt is None and not list_tools:
            console.print("[red]a prompt is required unless --list-tools is given[/red]")
            raise typer.Exit(1)

        async def _do() -> None:
            from paa.coder.agent import CoderAgent
            from paa.coder.session import Session
            from paa.config import get_settings
            from paa.core.errors import PaaError

            settings = get_settings()
            transcript_dir = settings.home / "sessions"
            session = Session.resume(resume, transcript_dir) if resume else None
            if session is not None:
                console.print(f"[dim]resumed session {session.session_id}[/dim]")

            built = await CoderAgent.build(
                settings,
                session=session,
                mode=resolved_mode,
                allow=list(allow or []),
                deny=list(deny or []),
                confirm=always_yes if yes else prompt_yes,
                max_steps=max_steps,
            )
            try:
                router = built.runtime.model_router
                if router is not None:
                    frontier = f", frontier {router.frontier.name}" if router.frontier else ""
                    console.print(
                        f"[dim]model: {router.local.name} / {router.local.model} "
                        f"(tier {router.local.tier.name}){frontier}[/dim]"
                    )

                if list_tools:
                    _print_tools(built.tools)
                    return

                result = await built.ask(prompt or "")
                console.print()
                console.print(result.final_text or "[dim](no final text)[/dim]")
                console.print(
                    f"[dim]{result.steps} steps, {result.tool_calls} tool calls, "
                    f"{result.denied_calls} denied, stopped: {result.stopped_reason}[/dim]"
                )
            except PaaError as exc:
                # A model failure (timeout, provider down, modality blocked) must be
                # reported cleanly rather than dumped as a traceback. The agent's
                # own transcript still records every attempt; this only cleans up the
                # CLI output the operator sees.
                console.print(f"[red]error:[/red] {exc}")
                raise typer.Exit(1) from exc
            finally:
                await built.close()

        asyncio.run(_do())


def _print_tools(tools: dict[str, Any]) -> None:
    table = Table("tool", "description")
    for name, tool in sorted(tools.items()):
        table.add_row(name, str(tool.description).split(". Use")[0].split(". These")[0])
    console.print(table)


async def always_yes(tool: str, arguments: dict[str, Any], reason: str) -> bool:
    """``--yes``: approve a gated call, announcing it so the transcript shows it."""
    console.print(f"[yellow]auto-approved[/yellow] {tool} ({reason})")
    return True


async def prompt_yes(tool: str, arguments: dict[str, Any], reason: str) -> bool:
    """Ask the operator. Runs the blocking read on a worker thread.

    The default for this command is to *ask*, because the tools that matter here
    (``submit_task``, ``run_task``) write to the ledger and can execute work.
    """

    def _ask() -> bool:
        console.print(f"[yellow]permission required[/yellow] {tool} — {reason}")
        console.print(arguments)
        try:
            return input("allow? [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            return False

    return await asyncio.to_thread(_ask)