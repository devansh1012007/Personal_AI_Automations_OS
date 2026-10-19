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

**Sessions are the operator's memory.** Every run ends by printing the session
id and the two ways to pick that session up again — ``--resume <id>`` and
``--continue`` (the most recent transcript, by mtime). Without that line a
conversation is a one-shot: the next invocation starts a fresh session with a
fresh id and the model has no idea the last one happened.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from paa.coder.permissions import PermissionMode
from paa.core.errors import PaaError
from paa.core.types import ComplexityModality

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
        continue_last: bool = typer.Option(
            False,
            "--continue",
            help="Resume the most recent session instead of starting a new one.",
        ),
        modality: str | None = typer.Option(
            None,
            "--modality",
            help="SIMPLE|STANDARD|COMPLEX|MAX — compared against the escalation floor.",
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

        # The two ways of naming a session are mutually exclusive: ``--resume``
        # names one explicitly, ``--continue`` means "the newest one". Both
        # were checked before anything is built, because a flag typo that
        # costs a runtime build is a flag typo worth catching first.
        if resume and continue_last:
            console.print(
                "[red]--resume and --continue are mutually exclusive[/red] — pick one"
            )
            raise typer.Exit(1)

        # ``_as_modality`` is the same tolerant coercion the runtime tools use
        # on a model's own modality argument, so the CLI and the tools agree
        # on what "complex" means. It returns ``None`` for anything it cannot
        # parse, which for a flag the operator typed is a mistake, not a
        # request to guess.
        from paa.coder.runtime_tools import _as_modality

        resolved_modality = ComplexityModality.STANDARD
        if modality is not None:
            parsed = _as_modality(modality)
            if parsed is None:
                console.print(
                    f"[red]unknown modality {modality!r}[/red] — "
                    f"expected one of {', '.join(m.value for m in ComplexityModality)}"
                )
                raise typer.Exit(1)
            resolved_modality = parsed

        if prompt is None and not list_tools:
            console.print("[red]a prompt is required unless --list-tools is given[/red]")
            raise typer.Exit(1)

        async def _do() -> None:
            from paa.coder.agent import CoderAgent
            from paa.coder.session import Session
            from paa.config import get_settings

            settings = get_settings()
            transcript_dir = settings.home / "sessions"
            session = None
            if resume:
                session = Session.resume(resume, transcript_dir)
                console.print(f"[dim]resumed session {session.session_id}[/dim]")
            elif continue_last:
                # ``Session.resume`` on a missing file returns an *empty*
                # session rather than raising, so the "is there anything to
                # continue?" question has to be asked of the directory, not of
                # the resume itself.
                latest = Session.latest_session(transcript_dir)
                if latest is None:
                    console.print(
                        "[red]no previous session to continue[/red] — "
                        f"no transcripts under {transcript_dir}"
                    )
                    raise typer.Exit(1)
                session = Session.resume(latest, transcript_dir)
                console.print(f"[dim]continued session {session.session_id}[/dim]")

            built = await CoderAgent.build(
                settings,
                session=session,
                mode=resolved_mode,
                allow=list(allow or []),
                deny=list(deny or []),
                confirm=always_yes if yes else prompt_yes,
                max_steps=max_steps,
                modality=resolved_modality,
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
                _render_error(exc, floor=settings.models.escalate_at_or_above.value)
                raise typer.Exit(1) from exc
            finally:
                # Printed from the ``finally`` so it appears on the failure path
                # too: a run that died still had a session, and the id is how
                # the operator picks that transcript up again.
                console.print(
                    f"[dim]session {built.session.session_id}  —  "
                    f"paa agent --resume {built.session.session_id}  |  "
                    f"paa agent --continue[/dim]"
                )
                await built.close()

        asyncio.run(_do())


def _render_error(exc: PaaError, *, floor: str | None = None) -> None:
    """Print a runtime failure as one message plus indented detail bullets.

    :meth:`paa.core.errors.PaaError.__str__` flattens every detail into a
    single parenthesised line, which is unreadable for the failures the model
    router raises: the facts an operator needs (which rule blocked the
    escalation, what the provider actually said) are buried in a wall of
    ``key='value'`` pairs. The structured ``details`` mapping is rendered
    instead.

    Nothing is dropped. The keys rendered here are the ones worth reading
    first; a detail this function does not recognise is still printed, and an
    error carrying none of them falls back to the raw string.
    """
    details = exc.details
    console.print(f"[red]error:[/red] {exc.message}")

    bullets: list[str] = []
    consumed: set[str] = set()

    reasons = details.get("escalation_block_reasons")
    if isinstance(reasons, list) and reasons:
        bullets.append("escalation blocked: " + ", ".join(str(r) for r in reasons))
        consumed.add("escalation_block_reasons")
        # The singular reason is the first entry of the list; showing both
        # would repeat the same rule in two shapes.
        consumed.add("escalation_block_reason")
    elif details.get("escalation_block_reason"):
        bullets.append(f"escalation blocked: {details['escalation_block_reason']}")
        consumed.add("escalation_block_reason")

    unavailable = details.get("escalation_unavailable_reason")
    if unavailable:
        bullets.append(f"escalation unavailable: {unavailable}")
    elif details.get("frontier_configured") is True:
        bullets.append("escalation available: a frontier provider is configured")
    consumed.update({"escalation_unavailable_reason", "frontier_configured"})

    underlying = details.get("underlying_message")
    if underlying:
        kind = details.get("underlying_error")
        suffix = f" ({kind})" if kind else ""
        bullets.append(f"provider failure: {underlying}{suffix}")
        consumed.add("underlying_message")
        consumed.add("underlying_error")

    status = details.get("underlying_status_code")
    if status is not None:
        bullets.append(f"provider status: {status}")
        consumed.add("underlying_status_code")

    provider = details.get("provider")
    if provider:
        bullets.append(f"provider: {provider}")
        consumed.add("provider")

    modality = details.get("modality")
    permission_mode = details.get("permission_mode")
    if modality or permission_mode:
        parts: list[str] = []
        if modality:
            floor_note = f" (floor {floor})" if floor else ""
            parts.append(f"modality {modality}{floor_note}")
        if permission_mode:
            parts.append(f"permission mode {permission_mode}")
        bullets.append(", ".join(parts))
    consumed.update({"modality", "permission_mode"})

    # Any other detail the provider carried (``underlying_body_excerpt`` and
    # friends) is still worth a line.
    for key, value in sorted(details.items()):
        if key.startswith("underlying_") and key not in consumed:
            bullets.append(f"provider {key.removeprefix('underlying_')}: {value}")
            consumed.add(key)

    # ``policy_reason`` repeats the first block reason and ``escalation_allowed``
    # is implied by the block: showing them twice makes the output look like it
    # has more to say than it does.
    consumed.update({"policy_reason", "escalation_allowed"})

    leftover = {k: v for k, v in details.items() if k not in consumed}
    if leftover:
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(leftover.items()))
        bullets.append(f"details: {rendered}")

    if not bullets:
        extra = str(exc)
        if extra != exc.message:
            console.print(f"[dim]{extra}[/dim]")
        return

    for bullet in bullets:
        console.print(f"  [dim]•[/dim] {bullet}")


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
        # The arguments are what the operator is being asked to approve, so
        # they are printed as JSON rather than as a raw dict repr: a
        # parameter-less tool would otherwise show a bare ``{}``, and a
        # parameterised one a ``{'goal': '...'}`` blob.
        if arguments:
            console.print_json(json.dumps(arguments, default=str, sort_keys=True))
        try:
            return input("allow? [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            return False

    return await asyncio.to_thread(_ask)