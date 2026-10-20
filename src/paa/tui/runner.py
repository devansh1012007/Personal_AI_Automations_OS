"""The interactive driver — polls the runtime and renders the dashboard.

What the runner owns:

* the :class:`Runtime` (built once at start, closed once at exit)
* a :class:`rich.console.Console` (to stdout)
* one command line at a time read from stdin via a worker thread so the asyncio
  loop is never blocked on terminal input

Every iteration:

1. gather rows + info into a :class:`Snapshot`, render the dashboard
2. read one command line
3. dispatch it (pure) then execute any side effect (``new`` / ``drive`` /
   ``gate``) against the runtime
4. render inline panels for ``status`` / ``ledger`` / ``doctor`` / ``help``
5. sleep a beat before repeating

Side effects produce transcript lines (``text = await _execute(...)``); display
results are :class:`rich.console.Group` panels returned from a helper. Both get
printed between dashboard redraws, so the operator always sees fresh state when
they type a command.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from rich.console import Console
from rich.text import Text

from paa.core.types import ComplexityModality, CorrelationId
from paa.ledger.replay import project as project_lineage
from paa.sandbox.base import collect_sandbox_facts
from paa.tui.dashboard import (
    render_dashboard,
    render_doctor,
    render_help,
    render_ledger,
    render_status,
)
from paa.tui.dispatcher import CommandKind, CommandResult, dispatch
from paa.tui.snapshot import CorrelationRow, Snapshot

__all__ = ["run_tui"]

log = structlog.get_logger(__name__)

REFRESH_SECONDS = 2.0
INLINE_PAUSE_SECONDS = 0.25


async def _gather_rows(rt: Any) -> list[CorrelationRow]:
    rows: list[CorrelationRow] = []
    try:
        for cid in await rt.ledger.open_correlations():
            try:
                proj = await project_lineage(rt.ledger, cid)
            except Exception as exc:  # pragma: cover - surface in .errors
                log.warning("tui.project_failed", correlation_id=str(cid), error=str(exc))
                continue
            mod = proj.modality or ComplexityModality.STANDARD
            request = proj.request or {}
            goal = request.get("goal", "") or "" if isinstance(request, dict) else str(request)[:60]
            rows.append(
                CorrelationRow(
                    correlation_id=CorrelationId(cid),
                    phase=proj.phase,
                    modality=mod,
                    attempts=proj.attempts,
                    tokens_consumed=proj.tokens_consumed,
                    created_at=(
                        proj.created_at.strftime("%H:%M:%S")
                        if proj.created_at
                        else None
                    ),
                    goal_preview=goal + ("…" if len(goal) == 60 else ""),
                )
            )
    except Exception as exc:  # pragma: no no cover - defensive
        log.warning("tui.rows_failed", error=str(exc))
    return rows


async def _build_info(settings: Any, rt: Any) -> dict[str, Any]:
    sandbox = rt.sandbox
    # The TUI and the CLI must not disagree about the same fact. The CLI
    # probes the backend before it renders the isolation level, because the
    # level is derived from the probe; the TUI has to do the same or it
    # reports a stale "not probed" forever.
    #
    # The collector is the *same* one the CLI uses, so a failed diagnostic is
    # normalised identically on both surfaces. Doing it here instead would
    # put the two back on the "one of them lies" footing that the shared
    # renderer exists to prevent — and would let a backend that breaks its
    # own contract take the TUI down.
    sandbox_facts: dict[str, Any] = {}
    if sandbox is not None:
        sandbox_facts = await collect_sandbox_facts(sandbox)
        if sandbox_facts.get("diagnostic_error"):
            log.warning(
                "tui.sandbox_diagnostics_failed", error=sandbox_facts["diagnostic_error"]
            )
    open_tasks = [str(cid) for cid in await rt.ledger.open_correlations()]
    return {
        "home": str(settings.home),
        "policy_mode": settings.policy.mode.value,
        # The live substrate, not the configured one — see the note on the
        # doctor's relational row.
        "relational": type(rt.db).__name__,
        "vector_store": rt.vector_store,
        "graph_store": rt.graph_store,
        "queue": rt.queue,
        "model_router": rt.model_router,
        "sandbox": sandbox,
        "sandbox_facts": sandbox_facts,
        "open_tasks": open_tasks,
    }


async def _read_line(stdin: Any) -> str | None:
    """Read one line from stdin; return None on EOF (Ctrl-D).

    ``stdin`` is either a callable returning one line (tests inject a reader;
    production uses :func:`input`) or ``None`` to read from the real terminal.
    The blocking read runs on a worker thread so the asyncio loop is not held.
    """
    if callable(stdin):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(stdin), timeout=REFRESH_SECONDS * 10
            )
        except TimeoutError:
            return None
    loop = asyncio.get_running_loop()

    def _real_read() -> str | None:
        try:
            return input("[dim]paa>[/dim] ")
        except EOFError:
            return None

    try:
        line = await asyncio.wait_for(
            loop.run_in_executor(None, _real_read), timeout=REFRESH_SECONDS * 10
        )
    except TimeoutError:
        return None
    return line if line is not None else None


async def _execute(rt: Any, result: CommandResult, transcript: list[str]) -> str | None:
    """Run a side-effecting command; append transcript lines; return text or None."""
    args = result.args
    cid_raw = args.get("correlation_id")

    match result.kind:
        case CommandKind.NEW if "goal" in args:
            goal = args["goal"]
            cid = await rt.submit(goal, modality=ComplexityModality.STANDARD)
            transcript.append(f"[green]submitted[/green] {cid} for: {goal}")
            return f"submitted {cid}\n{goal}"
        case CommandKind.DRIVE:
            if not cid_raw:
                return "[red]drive requires an id[/red]"
            outcome = await rt.run(_parse_correlation_id(cid_raw))
            colour = "green" if getattr(outcome, "ok", False) else "yellow"
            transcript.append(f"[{colour}]ran[/colour] {cid_raw} -> {outcome.phase.value}")
            err = getattr(outcome, "error", None)
            if err:
                transcript.append(f"[red]error:[/red] {err}")
            return f"ran {cid_raw} -> {outcome.phase.value}"
        case CommandKind.GATE:
            assert cid_raw is not None, "gate requires a correlation id"
            approved: bool | None = args.get("approved") == "y"
            note: str | None = None
            if "note=" in args:
                note = args["note="][len("note="):]
            state = await rt.orchestrator.clear_human_gate(
                _parse_correlation_id(cid_raw), approved=approved, note=note
            )
            transcript.append(f"[green]gate cleared[/green] {cid_raw} approved={approved}")
            return f"gate cleared: {state.phase.value}"
        case CommandKind.HELP:
            transcript.append(result.note or "help shown")
            return None
        case CommandKind.QUIT:
            return "quitting"
        case _:
            raise ValueError(f"unhandled kind: {result.kind}")


async def _display_panels(rt: Any, result: CommandResult, transcript: list[str]) -> list[Any]:
    """Build Rich panels for purely-display commands."""
    panels: list[Any] = []
    kind = result.kind

    if kind == CommandKind.DOCTOR:
        info = await _build_info(rt.settings, rt)
        panels.append(render_doctor(info))
    elif kind == CommandKind.STATUS:
        cid = result.args.get("correlation_id")
        if cid:
            proj = await _latest_projection(rt, cid)
            if proj is not None:
                panels.append(render_status(proj))
    elif kind == CommandKind.LEDGER:
        cid = result.args.get("correlation_id")
        if cid:
            events = await _latest_events(rt, cid)
            if events:
                panels.append(render_ledger(events, cid))
    elif kind == CommandKind.HELP:
        panels.append(render_help())
    else:
        panels.append(Text("(no extra view)", style="dim italic"))
    return panels


async def _latest_projection(rt: Any, cid: str) -> dict[str, Any] | None:
    try:
        proj = await project_lineage(rt.ledger, _parse_correlation_id(cid))
        return dict(proj.model_dump())
    except Exception as exc:
        log.warning("tui.status_failed", correlation_id=cid, error=str(exc))
        return None


async def _latest_events(rt: Any, cid: str) -> list[Any]:
    try:
        return list(await rt.ledger.read_correlation(_parse_correlation_id(cid)))
    except Exception as exc:  # pragma: cover - defensive
        log.warning("tui.ledger_failed", correlation_id=cid, error=str(exc))
        return []


def _parse_correlation_id(value: str) -> CorrelationId:
    try:
        return CorrelationId(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"invalid correlation id: {value!r}") from exc


async def run_tui(*, stdin: Any = None, rt: Any = None) -> None:
    """Run the interactive dashboard until the operator quits.

    If ``rt`` is not provided, a real :class:`Runtime` is built from settings.
    A runtime can be injected for tests (in which case ``Runtime.build`` is
    never invoked — useful for keeping test runs offline).
    """
    from paa.config import get_settings, reset_settings_cache

    settings = get_settings()
    reset_settings_cache()

    if rt is None:
        from paa.runtime import Runtime

        rt = await Runtime.build(run_recovery=False)
    console = Console()
    try:
        transcript: list[str] = []
        while True:
            rows = await _gather_rows(rt)
            info = await _build_info(settings, rt)
            snapshot = Snapshot(rows=rows, runtime_info=info)
            console.print(render_dashboard(snapshot))

            raw = await _read_line(stdin)
            if raw is None:
                break

            try:
                result = dispatch(raw, snapshot)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                await asyncio.sleep(INLINE_PAUSE_SECONDS)
                continue

            text = await _execute(rt, result, transcript)
            if text:
                console.print(Text(text, style="green"))

            for panel in await _display_panels(rt, result, transcript):
                console.print(panel)

            if result.kind is CommandKind.QUIT:
                break

            await asyncio.sleep(INLINE_PAUSE_SECONDS)
    finally:
        await rt.close()


# Late import kept at module scope so tests that import runner without a real
# Runtime still succeed; run_tui builds it lazily unless rt is injected.
