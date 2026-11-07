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

from paa.core.types import ComplexityModality, CorrelationId, EventType
from paa.improvement.reflection import reflection_health
from paa.ledger.replay import TaskPhase
from paa.ledger.replay import project as project_lineage
from paa.models.base import redact
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

__all__ = ["build_panel_fields", "run_tui"]

log = structlog.get_logger(__name__)

REFRESH_SECONDS = 2.0
INLINE_PAUSE_SECONDS = 0.25

# PLAN:integration-fix:P006 — bound on how many open lineages a panel scans
# per build. The dashboard rebuilds every 2s tick; an unbounded per-row scan
# would grow without limit as open tasks grow. The counts panel is a single
# bounded COUNT query (see FactRepository.count_facts_by_domain) and is not
# subject to this limit.
_PANEL_SCAN_LIMIT = 50


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
    # REV-W4 R-W4-003: the doctor mirror's three reflection keys. The CLI
    # doctor renders reflection_health(settings.home, enabled=...), and the
    # TUI renders the same helper's output through the shared formatter, so
    # the two surfaces answer about the same home. A settings object without
    # a reflection block (e.g. the injected test fakes) reads as disabled —
    # the ReflectionSettings default — rather than breaking the 2s tick.
    _reflection = getattr(settings, "reflection", None)
    _enabled = getattr(_reflection, "enabled", False)
    _health = reflection_health(settings.home, enabled=bool(_enabled))
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
        "reflection_enabled": _health["enabled"],
        "reflection_last_run_age_days": _health["last_run_age_days"],
        "reflection_stale": _health["stale"],
    }


def _correlation_id_of(head: Any) -> Any:
    """The uuid inside an ``open_correlations`` entry, or the entry itself.

    ``LedgerStore.open_correlations`` returns :class:`CorrelationHead`
    (``head.correlation_id``), while the test fake returns plain id strings.
    Both are accepted so the panel builders work against the real ledger and
    the fakes without a special case per caller.
    """
    return getattr(head, "correlation_id", head)


async def _memory_browser_counts(rt: Any) -> dict[str, Any]:
    """Live-fact counts for the memory browser panel.

    Imported function-locally so the TUI module stays importable without the
    memory stack on the path (the same lazy pattern as the cold-lake factory
    in ``api/app.py``). One bounded aggregate query per call.
    """
    from paa.memory.facts import FactRepository

    if rt.db is None:
        raise RuntimeError("memory browser has no relational backend")
    return await FactRepository(rt.db).count_facts_by_domain()


async def _attention_top(settings: Any, rt: Any) -> list[dict[str, Any]] | None:
    """Live allocator decisions (``ATTENTION_ALLOCATED`` events) ranked by score.

    PLAN:integration-fix:P007b (P-006 -> P-007 fill) — the panel is the
    ledger's own record of what the allocator decided, read back from the
    open lineages (bounded by ``_PANEL_SCAN_LIMIT``, same scan shape as
    :func:`_decision_tracker`). The allocator folds to nothing in
    ``TaskProjection`` (the decision IS the record, ADR-0036), so this reads
    the raw events, not the projection. Rank is the allocator's total order
    (score descending, ``item_id`` ascending); the bound is
    ``AllocatorSettings.max_results`` (default 5). No events -> ``None``,
    which renders "No data yet": an empty allocator is honest-null, never
    a synthesised score. Per-lineage read failures skip that lineage only.

    Freshness (REV R-W5-003): the scan window is the *most recent*
    ``_PANEL_SCAN_LIMIT`` open lineages (``open_correlations`` is
    ``updated_at`` ASC, so the freshest sit at the tail — taking the head
    of the list as before would let the oldest lineages crowd out every
    fresh decision once more than 50 lineages are open). Events within a
    lineage are read in replay order (``state_version`` ASC) and entries
    are deduped by ``item_id`` with later events winning, so an item
    re-allocated on a later tick appears once, with its latest score (not
    its best score).

    Terminal lineages are *not* scanned: only ``is_terminal = 0`` heads are
    returned, so a decision whose lineage later closes (e.g.
    ``MUTATION_COMMITTED``) vanishes from this panel. That is accepted by
    design — the panel shows in-flight attention, not history — and this
    sentence is the contract; read the ledger for the full record.
    """
    allocator_settings = getattr(settings, "allocator", None)
    bound = getattr(allocator_settings, "max_results", 5) if allocator_settings else 5
    bound = int(bound) if isinstance(bound, int) and bound >= 1 else 5
    by_item: dict[str, dict[str, Any]] = {}
    heads = await rt.ledger.open_correlations()
    # Most recent window: open_correlations is updated_at ASC, so the
    # freshest lineages are the tail of the list.
    for head in heads[-_PANEL_SCAN_LIMIT:]:
        cid = _correlation_id_of(head)
        try:
            events = await rt.ledger.read_correlation(cid)
        except Exception as exc:
            log.warning(
                "tui.project_failed", correlation_id=str(cid), error=redact(str(exc))
            )
            continue
        for event in events:
            if getattr(event, "event_type", None) != EventType.ATTENTION_ALLOCATED:
                continue
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            score = payload.get("score")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            item_id = str(payload.get("item_id"))
            # Scan order is oldest -> newest, so overwriting keeps the
            # latest event per item (later events win).
            by_item[item_id] = {
                "item_id": item_id,
                "score": float(score),
                "correlation_id": str(cid),
                "inputs": payload.get("inputs"),
            }
    if not by_item:
        return None
    entries = list(by_item.values())
    entries.sort(key=lambda entry: (-entry["score"], str(entry["item_id"])))
    return entries[:bound]


async def _decision_tracker(rt: Any) -> list[dict[str, Any]]:
    """Recent raw POLICY_* decisions from open lineages.

    Quality/outcome scores do not exist (``hot_serving_evaluation_results``
    is schema-only, never written), so only the raw ``policy_decision`` /
    ``policy_reason`` pair is surfaced; nothing is synthesised. Per-lineage
    projection failures are tolerated (that lineage is skipped), mirroring
    ``_gather_rows``; only a catastrophic ledger failure escapes to the
    per-panel isolation wrapper.
    """
    entries: list[dict[str, Any]] = []
    heads = await rt.ledger.open_correlations()
    for head in heads[:_PANEL_SCAN_LIMIT]:
        cid = _correlation_id_of(head)
        try:
            proj = await project_lineage(rt.ledger, cid)
        except Exception as exc:
            log.warning(
                "tui.project_failed", correlation_id=str(cid), error=redact(str(exc))
            )
            continue
        if proj.policy_decision is None:
            continue
        entries.append(
            {
                "correlation_id": str(cid),
                "policy_decision": proj.policy_decision,
                "policy_reason": proj.policy_reason,
            }
        )
    return entries


async def _autonomy_state(settings: Any, rt: Any) -> dict[str, Any]:
    """Permission mode plus the count of open awaiting-human gates."""
    mode = settings.policy.mode.value
    awaiting_ids: list[str] = []
    heads = await rt.ledger.open_correlations()
    for head in heads[:_PANEL_SCAN_LIMIT]:
        cid = _correlation_id_of(head)
        try:
            proj = await project_lineage(rt.ledger, cid)
        except Exception as exc:
            log.warning(
                "tui.project_failed", correlation_id=str(cid), error=redact(str(exc))
            )
            continue
        if proj.phase == TaskPhase.AWAITING_HUMAN:
            awaiting_ids.append(str(cid))
    return {
        "mode": mode,
        "awaiting_gates": len(awaiting_ids),
        "awaiting_ids": awaiting_ids,
    }


async def _agent_monitor(rt: Any) -> dict[str, Any]:
    """Coarse agent view: loaded agents plus recent roles from open lineages.

    No per-agent counters exist (``hot_serving_agent_registry`` is schema-only,
    never written), so this is deliberately coarse and writes no state. Recent
    roles are the last event's ``agent_role`` per scanned lineage (bounded by
    ``_PANEL_SCAN_LIMIT``); per-lineage read failures are tolerated.
    """
    agents = sorted(str(k) for k in rt.agents)
    recent_roles: list[str] = []
    heads = await rt.ledger.open_correlations()
    for head in heads[:_PANEL_SCAN_LIMIT]:
        cid = _correlation_id_of(head)
        try:
            events = await rt.ledger.read_correlation(cid)
        except Exception as exc:
            log.warning(
                "tui.ledger_failed", correlation_id=str(cid), error=redact(str(exc))
            )
            continue
        label = getattr(events[-1], "agent_role", None) if events else None
        if label and label not in recent_roles:
            recent_roles.append(str(label))
    return {"agents": agents, "recent_roles": recent_roles}


async def build_panel_fields(settings: Any, rt: Any) -> dict[str, Any]:
    """Collect the five P006 panel fields with per-panel isolation.

    PLAN:integration-fix:P006 — one builder, two surfaces. The TUI fills its
    :class:`Snapshot` from this and the API ``/health`` mirrors the same dict
    (additive, same names), so the two surfaces cannot disagree about the
    same runtime fact — the same one-source pattern ``sandbox_report_rows``
    uses for the doctor panel.

    ``attention_top`` reads the allocator's own ledger decisions
    (``ATTENTION_ALLOCATED`` events; see :func:`_attention_top`) and is
    ``None`` exactly when the allocator has decided nothing — a null that
    renders "No data yet" is correct while a computed score would be a
    fake. Each field is collected under its own try/except: a raising
    source empties only its panel (logged, not silent) and never kills
    the 2s tick loop or the health response.
    """
    fields: dict[str, Any] = {
        "attention_top": None,
        "memory_browser_counts": None,
        "decision_tracker": None,
        "autonomy_state": None,
        "agent_monitor": None,
    }
    try:
        fields["attention_top"] = await _attention_top(settings, rt)
    except Exception as exc:
        log.warning(
            "tui.panel_failed", panel="attention_top", error=redact(str(exc))
        )
    try:
        fields["memory_browser_counts"] = await _memory_browser_counts(rt)
    except Exception as exc:
        log.warning(
            "tui.panel_failed",
            panel="memory_browser_counts",
            error=redact(str(exc)),
        )
    try:
        fields["decision_tracker"] = await _decision_tracker(rt)
    except Exception as exc:
        log.warning(
            "tui.panel_failed", panel="decision_tracker", error=redact(str(exc))
        )
    try:
        fields["autonomy_state"] = await _autonomy_state(settings, rt)
    except Exception as exc:
        log.warning(
            "tui.panel_failed", panel="autonomy_state", error=redact(str(exc))
        )
    try:
        fields["agent_monitor"] = await _agent_monitor(rt)
    except Exception as exc:
        log.warning(
            "tui.panel_failed", panel="agent_monitor", error=redact(str(exc))
        )
    return fields


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
            # PLAN:integration-fix:P006 — panels come from the one shared
            # builder so this surface and /health cannot disagree. The builder
            # never raises (per-panel isolation), so a panel source failure
            # refreshes as "No data yet" instead of killing the tick loop.
            panels = await build_panel_fields(settings, rt)
            snapshot = Snapshot(
                rows=rows,
                runtime_info=info,
                attention_top=panels["attention_top"],
                memory_browser_counts=panels["memory_browser_counts"],
                decision_tracker=panels["decision_tracker"],
                autonomy_state=panels["autonomy_state"],
                agent_monitor=panels["agent_monitor"],
            )
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
