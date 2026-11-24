"""``paa pipeline`` — the registry's window: list, inspect, run, trace.

The C6.5 / P4-lite surface over :mod:`paa.pipelines.registry`:

* ``list`` — every registered pipeline (builtin + local) with its trust
  level; off-contract local manifests are named, not hidden;
* ``inspect`` — one pipeline's manifest-shaped declaration: the
  capability ask, the limits, the typed params, the slots;
* ``run`` — drive one **runnable** (in-process) pipeline once. Local
  manifests are listed and inspected but not executed: running an
  out-of-process pipeline is P3's RPC transport, and the refusal says
  so rather than pretending. Selection is explicit and mandate-scoped
  (``--allow``), never scored;
* ``trace`` — one lineage's pipeline-shaped rows: ``PIPELINE_RUN_*``
  and ``PIPELINE_CALL_*`` for a pipeline run, ``CHAT_TURN_*`` for a
  chat session, with an honest fallback for task lineages (the
  builtin's runs are recorded in the task vocabulary — the
  pipeline-vocabulary integration is the RPC transport's work, and the
  trace says which vocabulary it is reading, not a generic "events").
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from paa.pipelines.overlay import OverlayError, load_overlay
from paa.pipelines.registry import (
    PipelineRegistry,
    PipelineSelectionError,
    select,
)
from paa.pipelines.slots import PipelineDeclaration

app = typer.Typer(help="List, inspect, run and trace pipelines.")
console = Console()

_PIPELINE_EVENT_TYPES = {
    "PIPELINE_RUN_STARTED",
    "PIPELINE_RUN_COMPLETED",
    "PIPELINE_RUN_FAILED",
    "PIPELINE_RUN_DIVERGED",
    "PIPELINE_CALL_PROPOSED",
    "PIPELINE_CALL_COMPLETED",
    "PIPELINE_CALL_FAILED",
    "CHAT_TURN_PROPOSED",
    "CHAT_TURN_COMPLETED",
    "CHAT_TURN_FAILED",
    "CHAT_TURN_CANCELLED",
}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# list / inspect
# ---------------------------------------------------------------------------


@app.command("list")
def list_pipelines() -> None:
    """Every registered pipeline, with its trust level."""
    registry = PipelineRegistry.default()
    table = Table("name", "version", "trust", "runnable", "description")
    for row in registry.list():
        table.add_row(
            row.name,
            row.version,
            row.trust,
            "yes" if row.runnable else "no (P3)",
            row.declaration.description,
        )
    console.print(table)
    for name, reason in registry.local_errors.items():
        console.print(f"[red]off-contract manifest:[/red] {name} — {reason}")


@app.command()
def inspect(
    name: str = typer.Argument(..., help="A registered pipeline's name."),
) -> None:
    """One pipeline's declaration: the ask, the limits, the params, the slots."""
    registry = PipelineRegistry.default()
    row = registry.get(name)
    if row is None:
        known = ", ".join(sorted(r.name for r in registry.list())) or "(none)"
        console.print(f"[red]no registered pipeline named {name!r}[/red] — {known}")
        raise typer.Exit(1)
    d: PipelineDeclaration = row.declaration
    console.print(f"[bold]{d.name}[/bold] {d.version}  trust={row.trust}  "
                  f"runtime {d.requires_runtime}  runnable={row.runnable}")
    if d.description:
        console.print(f"[dim]{d.description}[/dim]")

    if d.capabilities:
        table = Table("capability", "ask")
        for key, value in d.capabilities.items():
            table.add_row(key, repr(value))
        console.print("capabilities:", table)
    if d.params:
        table = Table("param", "type", "default", "range", "description")
        for key, spec in d.params.items():
            lo = "" if spec.minimum is None else str(spec.minimum)
            hi = "" if spec.maximum is None else str(spec.maximum)
            rng = f"{lo}..{hi}" if (lo or hi) else "any"
            table.add_row(key, spec.type, repr(spec.default), rng, spec.description)
        console.print("params:", table)
    if d.slots:
        table = Table("slot", "interface", "default")
        for slot in d.slots.values():
            table.add_row(slot.name, slot.interface, slot.default)
        console.print("slots:", table)
    limits = d.limits
    console.print(
        "limits: "
        + ", ".join(
            f"{key}={getattr(limits, key)}"
            for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs")
        )
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _parse_sets(sets: list[str]) -> dict[str, Any]:
    """``key=value`` options into a dict, ints as ints."""
    out: dict[str, Any] = {}
    for item in sets:
        if "=" not in item:
            console.print(f"[red]--set expects key=value, got {item!r}[/red]")
            raise typer.Exit(1)
        key, _, raw = item.partition("=")
        try:
            out[key] = int(raw)
        except ValueError:
            out[key] = raw
    return out


def _validate_sets(sets: dict[str, Any], declaration: PipelineDeclaration) -> None:
    """The same tighten-only rules the overlay loader enforces on params."""
    for key, value in sets.items():
        spec = declaration.params.get(key)
        if spec is None:
            console.print(
                f"[red]{declaration.name} declares no param {key!r}[/red]"
            )
            raise typer.Exit(1)
        if spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                console.print(f"[red]param {key!r} must be an int[/red]")
                raise typer.Exit(1)
            if spec.minimum is not None and value < spec.minimum:
                console.print(
                    f"[red]param {key!r}={value} is below the declared "
                    f"minimum {spec.minimum}[/red]"
                )
                raise typer.Exit(1)
            if spec.maximum is not None and value > spec.maximum:
                console.print(
                    f"[red]param {key!r}={value} is above the declared "
                    f"maximum {spec.maximum}[/red]"
                )
                raise typer.Exit(1)


@app.command()
def run(
    name: str = typer.Argument(..., help="The pipeline to run."),
    goal: str = typer.Option(..., "--goal", help="The goal or message to drive."),
    set_param: list[str] = typer.Option(
        None, "--set", help="A param override, key=value (tighten-only rules apply)."
    ),
    overlay_path: Path | None = typer.Option(
        None, "--overlay", help="A tighten-only overlay file to apply."
    ),
    allow: list[str] = typer.Option(
        None, "--allow", help="The mandate's pipeline scope (repeatable)."
    ),
    mode: str = typer.Option(
        "ask", "--mode", help="ask | plan | accept_edits | bypass (coder loop)."
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Auto-approve permission prompts (the coder loop)."
    ),
) -> None:
    """Run one in-process pipeline once. Local manifests refuse (P3)."""

    registry = PipelineRegistry.default()
    try:
        row = select(name, registry=registry, allowed=allow or None)
    except PipelineSelectionError as exc:
        console.print(f"[red]{exc.message}[/red]")
        raise typer.Exit(1) from exc

    if not row.runnable:
        console.print(
            f"[red]{row.name} is a local manifest[/red] — listed and inspected, "
            "not executed: running an out-of-process pipeline is the RPC "
            "transport's work (P3)."
        )
        raise typer.Exit(1)

    declaration = row.declaration
    overlay = None
    if overlay_path is not None:
        try:
            overlay = load_overlay(overlay_path, declaration)
        except OverlayError as exc:
            console.print(f"[red]overlay refused at load:[/red] {exc.message}")
            raise typer.Exit(1) from exc
        console.print(f"[dim]overlay applied: {sorted(overlay.slots) or 'no slots'}, "
                      f"{sorted(overlay.limits) or 'no limits'} tightened[/dim]")

    sets = _parse_sets(set_param or [])
    _validate_sets(sets, declaration)
    params: dict[str, Any] = dict(sets)
    if overlay is not None:
        params.update(overlay.params)

    if row.name == "builtin.plan-execute-critique":
        _run(_run_task_pipeline(row.name, goal, allow))
    elif row.name == "builtin.chat":
        _run(_run_chat_pipeline(goal, params))
    elif row.name == "coder.tool-loop":
        _run(_run_coder_pipeline(goal, params, mode, yes))
    else:  # unreachable while the builtins are the three above
        console.print(f"[red]no run path for {row.name!r}[/red]")
        raise typer.Exit(1)


async def _run_task_pipeline(name: str, goal: str, allow: list[str] | None) -> None:
    """The builtin task pipeline: the runtime's own submit → run path."""
    from paa.cli.main import _build_runtime
    from paa.core.errors import PaaError
    from paa.ledger.mandate import explicit_mandate

    rt = await _build_runtime()
    try:
        mandate = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=rt.settings.policy,
            pipelines=list(allow) if allow else None,
        )
        try:
            cid = await rt.submit(goal, mandate=mandate)
        except PaaError as exc:
            console.print(f"[red]mandate refused:[/red] {exc.message}")
            raise typer.Exit(1) from exc
        console.print(f"[bold]correlation:[/bold] {cid}")
        outcome = await rt.run(cid)
        colour = "green" if outcome.ok else "yellow"
        console.print(f"[{colour}]phase: {outcome.phase.value}[/{colour}]")
        if outcome.error:
            console.print(f"[red]error:[/red] {outcome.error}")
    finally:
        await rt.close()


async def _run_chat_pipeline(goal: str, params: dict[str, Any]) -> None:
    """One chat turn, offline-capable, journaled as a pipeline run.

    The one-shot driver writes the ``PIPELINE_RUN_*`` lifecycle rows on
    the fresh session's lineage — the host's job, not the service's: a
    multi-turn conversation is a session (its vocabulary is
    ``CHAT_TURN_*``), and only a pipeline-shaped invocation carries the
    pipeline-vocabulary wrapper. Two such runs are two lineages, each
    recoverable on its own.
    """
    from paa.chat.loop import ChatLoop
    from paa.chat.service import ChatService
    from paa.cli.main import _build_runtime
    from paa.pipelines.journal import CallJournal

    rt = await _build_runtime()
    try:
        service = ChatService.from_runtime(rt)
        session_id = await service.create_session()
        journal = CallJournal(rt.ledger, session_id)
        await journal.run_started(ChatLoop.name, ChatLoop.version)
        result = await service.send(session_id, goal)
        await journal.run_completed(
            "completed",
            metrics={"turn_id": result.turn_id, "tokens": result.tokens_consumed},
        )
        console.print(result.reply)
        console.print(f"[dim]session: {session_id}[/dim]")
    finally:
        await rt.close()


async def _run_coder_pipeline(
    goal: str, params: dict[str, Any], mode: str, yes: bool
) -> None:
    """One coder tool-loop run on the coder CLI's own wiring.

    ``CoderAgent.build`` builds the model, the tools and the session
    exactly as ``paa agent`` does; the difference is the driver —
    :class:`~paa.coder.pipeline.CoderToolLoopPipeline` on a
    host-minted facade, with a :class:`CallJournal` making the run a
    pipeline-shaped, traceable lineage.
    """
    from paa.cli.coder import always_yes, prompt_yes
    from paa.coder.agent import CoderAgent
    from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
    from paa.coder.pipeline import CoderLoopFacade, CoderToolLoopPipeline
    from paa.coder.session import Session
    from paa.config import get_settings
    from paa.core.types import new_correlation_id
    from paa.pipelines.base import PipelineTask
    from paa.pipelines.journal import CallJournal

    try:
        resolved_mode = PermissionMode(mode.lower())
    except ValueError:
        console.print(f"[red]unknown mode {mode!r}[/red]")
        raise typer.Exit(1) from None

    settings = get_settings()
    session = Session(session_id=str(uuid.uuid4()), transcript_dir=settings.home / "sessions")
    built = await CoderAgent.build(
        settings, session=session, mode=resolved_mode,
        confirm=always_yes if yes else prompt_yes,
    )
    try:
        run_id = new_correlation_id()
        journal = CallJournal(built.runtime.ledger, run_id)
        await journal.run_started(
            CoderToolLoopPipeline.name, CoderToolLoopPipeline.version
        )
        facade = CoderLoopFacade(
            model=built.model,
            tools=built.tools,
            session=built.session,
            run_id=run_id,
            call_journal=journal,
            permissions=ToolPermissionPolicy(mode=resolved_mode),
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal=goal, params=params
        )
        result = await CoderToolLoopPipeline().run(task, facade)
        await journal.run_completed(
            result.status.value, metrics=dict(result.metrics)
        )
        colour = "green" if result.status.value == "completed" else "yellow"
        console.print(f"[{colour}]{result.status.value}[/{colour}] {result.summary}")
        console.print(f"[dim]correlation: {run_id}[/dim]")
        console.print(f"[dim]session: {built.session.session_id}[/dim]")
    finally:
        await built.runtime.close()


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


@app.command()
def trace(
    correlation_id: str = typer.Argument(
        ..., help="A pipeline run's lineage id (or a chat session id)."
    ),
) -> None:
    """One lineage's pipeline-shaped rows, with their outcomes."""

    async def _do() -> None:
        from paa.cli.main import _build_runtime

        rt = await _build_runtime()
        try:
            resolved = uuid.UUID(correlation_id)
            events = await rt.ledger.read_correlation(resolved)
            if not events:
                console.print(f"[red]no lineage recorded for {correlation_id}[/red]")
                raise typer.Exit(1)

            rows = [
                e for e in events if e.event_type.value in _PIPELINE_EVENT_TYPES
            ]
            table = Table("v", "event", "detail")
            for e in rows:
                p = e.payload
                if e.event_type.value.startswith("PIPELINE_CALL"):
                    detail = (
                        f"seq {p.get('seq')} {p.get('method')} "
                        f"{(p.get('fingerprint') or '')[:12]}"
                    )
                elif e.event_type.value.startswith("PIPELINE_RUN"):
                    detail = str(p.get("pipeline") or p.get("status") or "")
                else:
                    detail = str(p.get("turn_id") or "")
                table.add_row(str(e.state_version), e.event_type.value, detail)
            if rows:
                console.print(table)
            else:
                # The honest fallback: the builtin's task lineages record
                # their runs in the task vocabulary, not the pipeline one.
                console.print(
                    "[dim]no PIPELINE_* or CHAT_TURN_* rows on this lineage; "
                    "the builtin task pipeline records its runs as task events "
                    "(CONTEXT_HYDRATED, PLAN_COMPILED, ... MUTATION_COMMITTED) — "
                    "see `paa ledger` for the full chain[/dim]"
                )
        finally:
            await rt.close()

    _run(_do())
