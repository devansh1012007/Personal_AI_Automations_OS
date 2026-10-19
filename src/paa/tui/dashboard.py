"""Renderers — turn a :class:`CommandResult` and a runtime snapshot into Rich output.

Each ``render_*`` function is pure given its inputs: it draws into a
:class:`rich.console.Console` and returns nothing. Testable with a captured
:class:`rich.console.StringIOConsole`. The runner drives the loop; these are
presentational only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from paa.core.types import ComplexityModality
from paa.ledger.replay import TaskPhase
from paa.tui.snapshot import Snapshot

__all__ = [
    "render_dashboard",
    "render_doctor",
    "render_help",
    "render_ledger",
    "render_status",
    "render_transcript",
]


def _phase_colour(phase: TaskPhase) -> str:
    return {
        TaskPhase.CREATED: "dim",
        TaskPhase.QUEUED: "cyan",
        TaskPhase.HYDRATING: "cyan",
        TaskPhase.PLANNING: "yellow",
        TaskPhase.POLICY_REVIEW: "yellow",
        TaskPhase.EXECUTING: "green",
        TaskPhase.VALIDATING: "green",
        TaskPhase.AWAITING_HUMAN: "magenta",
        TaskPhase.COMMITTED: "bright_green",
        TaskPhase.FAILED: "red",
        TaskPhase.BLOCKED: "bold red",
        TaskPhase.ROLLED_BACK: "red",
        TaskPhase.ABANDONED: "dim red",
    }.get(phase, "white")


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.strftime("%H:%:%M:%S")
    return str(value)


def render_help() -> Panel:
    lines = [
        "paa tui — command reference",
        "",
        "new <goal>          submit a task and let the agent work",
        "drive <id>          drive a queued task to completion right now",
        "status <id>         show the projection of a lineage",
        "ledger <id>         walk the event chain for <id>",
        "gate <id> [--approve|--reject]   clear or reject a human gate",
        "doctor              show which backends are live",
        "help                this message",
        "q / :q / :quit     exit the dashboard",
        "",
        "Tip: ``ledger`` shows every event in chronological order — that's the "
        "'explain why' view.",
    ]
    body = Text("\n".join(lines), style="white")
    return Panel(body, title="[bold]help[/bold]", border_style="cyan")


def render_dashboard(snapshot: Snapshot) -> Panel:
    header = Text("paa — live dashboard", style="bold cyan", justify="center")

    if not snapshot.rows:
        empty = Text("[dim]no active tasks[/dim]", justify="center")
        summary = Text(
            f"mode: {snapshot.runtime_info.get('mode', '?')}   "
            f"vector: {type(snapshot.runtime_info.get('vector_store') or 'n/a').__name__}",
            justify="center",
            style="dim",
        )
        return Panel(Group(header, empty, summary), border_style="cyan")

    table = Table(expand=True, box=None, show_header=True, header_style="bold")
    table.add_column("id", overflow="fold")
    table.add_column("phase", no_wrap=False)
    table.add_column("mod", no_wrap=True)
    table.add_column("att", justify="right")
    table.add_column("tok", justify="right")
    table.add_column("goal", overflow="fold")
    for row in snapshot.rows:
        table.add_row(
            _short_id(row.correlation_id),
            Text(row.phase.value, style=_phase_colour(row.phase)),
            row.modality.value,
            str(row.attempts),
            str(row.tokens_consumed),
            row.goal_preview or "",
        )

    footer = Text(
        "[dim]type a command and press Enter; "
        "live updates refresh on each poll tick.[/dim]",
        justify="center",
    )
    return Panel(Group(header, table, footer), border_style="cyan")


def _short_id(cid: Any) -> str:
    s = str(cid)
    return s[:8] + "…" if len(s) > 11 else s


def render_status(projection: dict[str, Any]) -> Panel:
    phase = projection.get("phase", TaskPhase.CREATED).value
    modality = projection.get("modality", ComplexityModality.STANDARD).value
    steps = f"{len(projection.get('completed_steps', []))}/{len(projection.get('plan_steps', []))}"
    errors = projection.get("errors", []) or []
    error_text = "\n".join(
        f"- {e.get('event_type', '?')}: {e.get('reason', '')}" for e in errors[:5]
    ) or "[dim]none[/dim]"

    content = (
        f"correlation: {projection.get('correlation_id', '-')}\n"
        f"session: {projection.get('session_id') or '-'}\n"
        f"phase: {phase}\n"
        f"modality: {modality}\n"
        f"steps: {steps} done\n"
        f"attempts: {projection.get('attempts', 0)}\n"
        f"tokens consumed: {projection.get('tokens_consumed', 0)}\n"
        f"policy: {projection.get('policy_decision') or '-'}\n"
        f"validation verdict: {projection.get('validation_verdict') or '-'}\n"
        f"awaiting: {projection.get('awaiting_reason') or '-'}\n"
        f"\nerrors:\n{error_text}"
    )
    return Panel(content, title="[bold]status[/bold]", border_style="cyan")


def render_ledger(events: list[Any], correlation_id: str) -> Panel:
    if not events:
        return Panel(
            Text(f"[dim]no events for {correlation_id}[/dim]"), title="[bold]ledger[/bold]"
        )
    lines: list[str] = []
    for i, e in enumerate(events, start=1):
        etype = getattr(e, "event_type", None)
        when = getattr(e, "recorded_at", None)
        when_s = when.strftime("%H:%:%S") if isinstance(when, datetime) else "-"
        agent = getattr(e, "agent_role", "-")
        version = getattr(e, "state_version", "-")
        lines.append(
            f"{i:>2}. v{version:<4} [{etype.value if etype else '?'}] agent={agent:<18} {when_s}"
        )
    body = Text("\n".join(lines))
    return Panel(body, title=f"[bold]ledger[/bold]  {correlation_id[:8]}…", border_style="cyan")


def _type_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    return type(value).__name__ if value else "none"


def render_doctor(info: dict[str, Any]) -> Panel:
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("component", style="cyan")
    table.add_column("status")
    table.add_row("version", info.get("version", "?"))
    table.add_row("home", str(info.get("home", "?")))
    table.add_row("permission mode", info.get("policy_mode", "?"))
    table.add_row("relational", info.get("relational", "?"))
    vector = info.get("vector_store")
    graph = info.get("graph_store")
    queue = info.get("queue")
    vector_label = _type_label(vector)
    graph_label = _type_label(graph)
    queue_label = _type_label(queue)
    model_router = info.get("model_router")
    mr_label = _type_label(model_router)
    table.add_row("vector", vector_label)
    table.add_row("graph", graph_label)
    table.add_row("queue", queue_label)
    table.add_row("model router", mr_label)
    sandbox = info.get("sandbox")
    if sandbox is not None:
        level = getattr(sandbox, "isolation_level", None)
        sb_name = sandbox.name if not isinstance(sandbox, str) else sandbox
        table.add_row(
            "sandbox",
            f"{sb_name} ({getattr(level, 'name', '?')})" if level else sb_name,
        )
    else:
        table.add_row("sandbox", "[dim]none (dry-run)[/dim]")
    open_tasks = len(info.get("open_tasks", []))
    table.add_row("open tasks", str(open_tasks))
    return Panel(table, border_style="cyan")


def render_transcript(lines: list[str], hint: str | None = None) -> Group:
    rendered: list[Any] = []
    for line in lines:
        style = "dim" if any(line.startswith(p) for p in ("> ", "(", "[", "→")) else "white"
        rendered.append(Text(line, style=style))
    if hint is not None:
        rendered.append(Text(hint, style="yellow"))
    if not rendered:
        rendered.append(Text("(no transcript yet)", style="dim italic"))
    return Group(*rendered)
