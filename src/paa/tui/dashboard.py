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
from paa.sandbox.base import sandbox_report_rows
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

    # PLAN:integration-fix:P006 — the five named panels always render (even
    # when there are no open tasks), one sub-panel per field. ``None`` (or an
    # empty container) renders "No data yet" — that empty state is the T07
    # acceptance criterion, not a placeholder. Every user-controlled string
    # below is wrapped in Text so it is data, never markup: a Panel built
    # from a str (or a table cell that is a str) parses markup, so "[red]"
    # text changes style and a bare "[/red]" raises MarkupError and kills the
    # dashboard loop. Panel *titles* are fixed field names, never data.
    panels: list[Any] = [
        _panel_attention(snapshot.attention_top),
        _panel_memory(snapshot.memory_browser_counts),
        _panel_decisions(snapshot.decision_tracker),
        _panel_autonomy(snapshot.autonomy_state),
        _panel_agents(snapshot.agent_monitor),
    ]

    if not snapshot.rows:
        empty = Text("[dim]no active tasks[/dim]", justify="center")
        summary = Text(
            f"mode: {snapshot.runtime_info.get('mode', '?')}   "
            f"vector: {type(snapshot.runtime_info.get('vector_store') or 'n/a').__name__}",
            justify="center",
            style="dim",
        )
        return Panel(Group(header, empty, summary, *panels), border_style="cyan")

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
            # goal_preview is caller-supplied text: Text makes it literal
            # (the markup=False precedent from W1 P-001 at render time). A
            # str cell would parse "[red]" as style and crash on "[/red]".
            Text(row.goal_preview or ""),
        )

    footer = Text(
        "[dim]type a command and press Enter; "
        "live updates refresh on each poll tick.[/dim]",
        justify="center",
    )
    return Panel(Group(header, table, *panels, footer), border_style="cyan")


def _is_empty_panel(value: Any) -> bool:
    return value is None or (isinstance(value, (list, tuple, dict)) and len(value) == 0)


def _panel_no_data(name: str) -> Panel:
    return Panel(Text("No data yet", style="dim"), title=name, border_style="cyan")


def _cell_text(value: Any) -> Text:
    # ``-`` for null mirrors _fmt's empty rendering; Text keeps hostile
    # strings literal (see render_dashboard).
    return Text("-" if value is None else str(value))


def _panel_attention(value: Any) -> Panel:
    name = "attention_top"
    if _is_empty_panel(value):
        return _panel_no_data(name)
    if isinstance(value, dict):
        # A {id: score} mapping: rank by score descending when numeric so
        # the row order is the attention order, not the dict order.
        def _score(item: tuple[str, Any]) -> float:
            try:
                return float(item[1])
            except (TypeError, ValueError):
                return 0.0

        items = sorted(value.items(), key=_score, reverse=True)
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("rank", justify="right")
        table.add_column("id", overflow="fold")
        table.add_column("score", justify="right")
        for rank, (cid, score) in enumerate(items, start=1):
            table.add_row(str(rank), _cell_text(cid), _cell_text(score))
        return Panel(table, title=name, border_style="cyan")
    if isinstance(value, (list, tuple)):
        # Ranked rows: list order *is* the ranking (P-07 fills this). Dict
        # entries render one row per key; scalars render one row each.
        if value and isinstance(value[0], dict):
            columns: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    for key in item:
                        if key not in columns:
                            columns.append(key)
            table = Table(show_header=True, header_style="bold", expand=True)
            table.add_column("rank", justify="right")
            for key in columns:
                table.add_column(key, overflow="fold")
            for rank, item in enumerate(value, start=1):
                row: list[Any] = [str(rank)]
                if isinstance(item, dict):
                    for key in columns:
                        row.append(_cell_text(item.get(key)))
                else:
                    # Mixed list (first entry was a dict, this one is not):
                    # a malformed panel must not crash the loop.
                    row.append(_cell_text(item))
                    row.extend([_cell_text(None)] * (len(columns) - 1))
                table.add_row(*row)
            return Panel(table, title=name, border_style="cyan")
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("rank", justify="right")
        table.add_column("value", overflow="fold")
        for rank, item in enumerate(value, start=1):
            table.add_row(str(rank), _cell_text(item))
        return Panel(table, title=name, border_style="cyan")
    return Panel(Text(str(value)), title=name, border_style="cyan")


def _counts_table(title: str, counts: dict[Any, Any]) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column(title, overflow="fold")
    table.add_column("count", justify="right")
    # Deterministic total order: count descending, then name ascending, so
    # the rendered ranking is reproducible across runs and substrates.
    # Non-numeric counts sort as 0 rather than raising.
    def _order(key: Any) -> tuple[float, str]:
        raw = counts[key]
        count = float(raw) if isinstance(raw, (int, float)) else 0.0
        return (-count, str(key))

    for key in sorted(counts, key=_order):
        table.add_row(_cell_text(key), _cell_text(counts[key]))
    return table


def _panel_memory(value: Any) -> Panel:
    name = "memory_browser_counts"
    if _is_empty_panel(value):
        return _panel_no_data(name)
    if isinstance(value, dict) and ("total" in value or "by_domain" in value):
        total = value.get("total", 0)
        by_domain = value.get("by_domain") or {}
        by_status = value.get("by_status") or {}
        chunks: list[Any] = [Text(f"total: {total}")]
        if isinstance(by_domain, dict) and by_domain:
            chunks.append(_counts_table("domain", by_domain))
        if isinstance(by_status, dict) and by_status:
            chunks.append(_counts_table("status", by_status))
        return Panel(Group(*chunks), title=name, border_style="cyan")
    if isinstance(value, dict):
        # A plain {domain: count} mapping from a different caller.
        return Panel(_counts_table("domain", value), title=name, border_style="cyan")
    return Panel(Text(str(value)), title=name, border_style="cyan")


def _panel_decisions(value: Any) -> Panel:
    name = "decision_tracker"
    if _is_empty_panel(value):
        return _panel_no_data(name)
    if isinstance(value, (list, tuple)):
        table = Table(show_header=True, header_style="bold", expand=True)
        table.add_column("id", overflow="fold")
        table.add_column("decision", overflow="fold")
        table.add_column("reason", overflow="fold")
        for item in value:
            if isinstance(item, dict):
                table.add_row(
                    _cell_text(item.get("correlation_id")),
                    _cell_text(item.get("policy_decision")),
                    _cell_text(item.get("policy_reason")),
                )
            else:
                table.add_row(_cell_text(item), _cell_text(None), _cell_text(None))
        return Panel(table, title=name, border_style="cyan")
    return Panel(Text(str(value)), title=name, border_style="cyan")


def _panel_autonomy(value: Any) -> Panel:
    name = "autonomy_state"
    if _is_empty_panel(value):
        return _panel_no_data(name)
    if isinstance(value, dict):
        mode = value.get("mode", "?")
        awaiting = value.get("awaiting_gates", 0)
        ids = value.get("awaiting_ids") or []
        chunks: list[Any] = [Text(f"mode: {mode}")]
        chunks.append(Text(f"awaiting gates: {awaiting}"))
        if isinstance(ids, (list, tuple)) and ids:
            for cid in ids:
                chunks.append(Text(f"  {_short_id(cid)}"))
        return Panel(Group(*chunks), title=name, border_style="cyan")
    return Panel(Text(str(value)), title=name, border_style="cyan")


def _panel_agents(value: Any) -> Panel:
    name = "agent_monitor"
    if _is_empty_panel(value):
        return _panel_no_data(name)
    if isinstance(value, dict):
        agents = value.get("agents") or []
        roles = value.get("recent_roles") or []
        chunks: list[Any] = []
        if isinstance(agents, (list, tuple)):
            chunks.append(Text(f"agents: {', '.join(str(a) for a in agents) or 'none'}"))
        else:
            chunks.append(_cell_text(agents))
        if isinstance(roles, (list, tuple)) and roles:
            chunks.append(Text(f"recent roles: {', '.join(str(r) for r in roles)}"))
        return Panel(Group(*chunks), title=name, border_style="cyan")
    if isinstance(value, (list, tuple)):
        single: list[Any] = [Text(f"agents: {', '.join(str(a) for a in value) or 'none'}")]
        return Panel(Group(*single), title=name, border_style="cyan")
    return Panel(Text(str(value)), title=name, border_style="cyan")


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
    facts = info.get("sandbox_facts") or {}
    if sandbox is not None:
        # The same formatter the CLI uses, so the two surfaces of one runtime
        # cannot disagree about the same host.
        rows = sandbox_report_rows(facts)
        if rows:
            for label, value in rows:
                table.add_row(label, value)
        else:
            # A backend that reported no facts at all still has a name and a
            # level; showing nothing would look like "no sandbox". The level
            # is compared against ``None`` and not for truthiness:
            # ``IsolationLevel.NONE`` is 0, and 0 is falsy.
            level = getattr(sandbox, "isolation_level", None)
            sb_name = sandbox.name if not isinstance(sandbox, str) else sandbox
            table.add_row(
                "sandbox",
                f"{sb_name} ({getattr(level, 'name', '?')})" if level is not None else sb_name,
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
