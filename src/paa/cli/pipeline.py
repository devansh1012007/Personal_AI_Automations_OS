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

from paa.pipelines.overlay import Overlay, OverlayError, load_overlay, tightened_limits
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
                  f"runtime {d.requires_runtime}  runnable={row.runnable}  "
                  f"resume={d.resume}  evidence={d.evidence_tier}")
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


def _parse_sets(sets: list[str]) -> dict[str, str]:
    """Split ``key=value`` options; the declaration decides the coercion.

    Coercing here would guess a type the declaration may disagree with —
    a ``str`` param whose value happens to be ``"true"`` would silently
    become a bool. Splitting first and coercing in :func:`_validate_sets`
    against the declared type keeps ``--set`` on the same closed contract
    the overlay loader enforces.
    """
    out: dict[str, str] = {}
    for item in sets:
        if "=" not in item:
            console.print(f"[red]--set expects key=value, got {item!r}[/red]")
            raise typer.Exit(1)
        key, _, raw = item.partition("=")
        out[key] = raw
    return out


def _validate_sets(
    sets: dict[str, Any], declaration: PipelineDeclaration
) -> dict[str, Any]:
    """Coerce to the declared types under the same tighten-only rules.

    Returns the typed params rather than merely validating them: a value
    that survives this function is one the runner may actually use.
    """
    out: dict[str, Any] = {}
    for key, raw in sets.items():
        spec = declaration.params.get(key)
        if spec is None:
            console.print(
                f"[red]{declaration.name} declares no param {key!r}[/red]"
            )
            raise typer.Exit(1)
        if not isinstance(raw, str):
            console.print(f"[red]param {key!r} must be given as key=value[/red]")
            raise typer.Exit(1)
        if spec.type == "int":
            try:
                value: Any = int(raw)
            except ValueError:
                console.print(f"[red]param {key!r} must be an int[/red]")
                raise typer.Exit(1) from None
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
        elif spec.type == "bool":
            if raw.lower() not in ("true", "false"):
                console.print(f"[red]param {key!r} must be true or false[/red]")
                raise typer.Exit(1)
            value = raw.lower() == "true"
        elif spec.type == "str":
            value = raw
        else:  # declarations are built from the closed type set; kept honest
            console.print(f"[red]param {key!r} has an unknown type[/red]")
            raise typer.Exit(1)
        out[key] = value
    return out


def _refuse_unhonoured_overlay(
    overlay: Overlay | None,
    pipeline: str,
    *,
    params: frozenset[str] = frozenset(),
    slots: frozenset[str] = frozenset(),
    limits: frozenset[str] = frozenset(),
) -> None:
    """Refuse overlay sections this one-shot runner cannot actually apply.

    An overlay that is validated, announced as applied and then ignored
    is worse than no overlay: the operator believes a budget or a slot
    changed when it did not. The runner names exactly what it honours;
    anything else is a loud refusal before a runtime is built.
    """
    if overlay is None:
        return
    refused: list[str] = []
    for section, honoured in (
        ("params", params),
        ("slots", slots),
        ("limits", limits),
    ):
        extra = set(getattr(overlay, section)) - set(honoured)
        if extra:
            refused.append(f"{section}: {', '.join(sorted(extra))}")
    if overlay.capabilities:
        refused.append("capabilities: " + ", ".join(sorted(overlay.capabilities)))
    if refused:
        console.print(
            f"[red]{pipeline} cannot honour this overlay:[/red] " + "; ".join(refused)
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
    gate: list[str] = typer.Option(
        None,
        "--gate",
        help=(
            "A validation gate command, shell-split (repeatable) — the "
            "improve loop's deterministic validator."
        ),
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

    # ``--gate`` is the improve loop's own option, the same way ``--mode``
    # and ``--yes`` are the coder loop's: named on the shared door, refused
    # for every pipeline whose validator it is not.
    if gate and row.name != "improve.reflection-loop":
        console.print(
            f"[red]--gate is the improve loop's validation gate list;[/red] "
            f"{row.name} has no deterministic validation gates"
        )
        raise typer.Exit(1)

    declaration = row.declaration
    overlay: Overlay | None = None
    if overlay_path is not None:
        try:
            overlay = load_overlay(overlay_path, declaration)
        except OverlayError as exc:
            console.print(f"[red]overlay refused at load:[/red] {exc.message}")
            raise typer.Exit(1) from exc
        # What each one-shot runner can actually honour, stated once.
        # Chat applies its params, its one slot and max_tokens; the coder
        # loop applies params; the task path honours no overlay at all.
        honoured: dict[
            str, tuple[frozenset[str], frozenset[str], frozenset[str]]
        ] = {
            "builtin.chat": (
                frozenset(declaration.params),
                frozenset({"context_provider"}),
                frozenset({"max_tokens"}),
            ),
            "coder.tool-loop": (
                frozenset(declaration.params),
                frozenset(),
                frozenset(),
            ),
            # The T1 adapter honours its three declared knobs and nothing
            # else. Its capability ask and slot set are both empty, so an
            # overlay that tries to open either is refused as a widening.
            "builtin.observe": (
                frozenset(declaration.params),
                frozenset(),
                frozenset(),
            ),
            # The guarded loop honours its two declared knobs (the
            # reflection window and the validation wall budget) and
            # nothing else — its capability ask is commit-only.
            "improve.reflection-loop": (
                frozenset(declaration.params),
                frozenset(),
                frozenset(),
            ),
        }
        h_params, h_slots, h_limits = honoured.get(
            row.name, (frozenset(), frozenset(), frozenset())
        )
        _refuse_unhonoured_overlay(
            overlay, row.name, params=h_params, slots=h_slots, limits=h_limits
        )
        if (
            row.name == "builtin.chat"
            and overlay.slots.get("context_provider") == "none"
        ):
            console.print(
                "[red]builtin.chat cannot run with context_provider='none'[/red]"
            )
            raise typer.Exit(1)

    params: dict[str, Any] = dict(
        _validate_sets(_parse_sets(set_param or []), declaration)
    )
    if overlay is not None:
        params.update(overlay.params)
        # Announce only after every refusal — overlay sections and --set
        # ranges alike — has passed. An aborted run must not claim an
        # overlay was applied.
        console.print(f"[dim]overlay applied: {sorted(overlay.slots) or 'no slots'}, "
                      f"{sorted(overlay.limits) or 'no limits'} tightened[/dim]")

    if row.name == "builtin.plan-execute-critique":
        _run(_run_task_pipeline(row.name, goal, allow))
    elif row.name == "builtin.chat":
        _run(_run_chat_pipeline(goal, params, overlay, declaration))
    elif row.name == "coder.tool-loop":
        _run(_run_coder_pipeline(goal, params, mode, yes))
    elif row.name == "builtin.observe":
        _run(_run_observe_pipeline(goal, params))
    elif row.name == "improve.reflection-loop":
        _run(_run_improve_pipeline(goal, params, gate))
    else:  # unreachable while the builtins are the five above
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


async def _run_chat_pipeline(
    goal: str,
    params: dict[str, Any],
    overlay: Overlay | None,
    declaration: PipelineDeclaration,
) -> None:
    """One chat turn, offline-capable, journaled as a pipeline run.

    The one-shot driver writes the ``PIPELINE_RUN_*`` lifecycle rows on
    the fresh session's lineage — the host's job, not the service's: a
    multi-turn conversation is a session (its vocabulary is
    ``CHAT_TURN_*``), and only a pipeline-shaped invocation carries the
    pipeline-vocabulary wrapper. Two such runs are two lineages, each
    recoverable on its own.

    Validated params, tightened limits and the resolved slot are applied
    *here*, before the service is built — never inside the loader, and
    never announced without being honoured. A turn that fails writes
    ``PIPELINE_RUN_FAILED``, not a false ``COMPLETED``: the ledger must
    not claim a run succeeded when the model did not answer.
    """
    from paa.chat.loop import ChatLoop
    from paa.chat.service import ChatService
    from paa.cli.main import _build_runtime
    from paa.models.base import redact
    from paa.pipelines.base import PipelineStatus
    from paa.pipelines.journal import CallJournal
    from paa.pipelines.slots import resolve_context_provider

    def _declared_int(key: str) -> int | None:
        value = params.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            console.print(f"[red]{key!r} must be an int[/red]")
            raise typer.Exit(1)
        return value

    rt = await _build_runtime()
    try:
        base = rt.settings.chat
        update: dict[str, Any] = {}
        recall_k = _declared_int("recall_k")
        if recall_k is not None:
            update["recall_k"] = min(base.recall_k, recall_k)
        last_k = _declared_int("last_k_turns")
        if last_k is not None:
            update["last_k_turns"] = min(base.last_k_turns, last_k)

        # A --set and an overlay may only tighten what the operator
        # configured: the min() is the arithmetic half of tighten-only.
        budgets = [
            budget
            for budget in (
                base.token_budget,
                _declared_int("tokens_budget"),
                tightened_limits(declaration.limits, overlay).max_tokens
                if overlay is not None
                else None,
            )
            if budget is not None and budget > 0
        ]
        if budgets:
            update["token_budget"] = min(budgets)

        provider = None
        if overlay is not None:
            ref = overlay.slots.get("context_provider")
            if ref is not None:
                try:
                    provider = resolve_context_provider(ref)
                except ValueError as exc:
                    console.print(f"[red]context slot refused:[/red] {exc}")
                    raise typer.Exit(1) from exc

        service = ChatService.from_runtime(
            rt, settings=base.model_copy(update=update), context_provider=provider
        )
        session_id = await service.create_session()
        # Printed before the turn: even a failed run needs its lineage id
        # so the operator can `paa pipeline trace` it.
        console.print(f"[dim]session: {session_id}[/dim]")
        journal = CallJournal(rt.ledger, session_id)
        await journal.run_started(ChatLoop.name, ChatLoop.version)
        try:
            result = await service.send(session_id, goal)
        except Exception as exc:
            reason = redact(f"{type(exc).__name__}: {exc}")
            await journal.run_failed(reason)
            console.print(f"[red]run failed:[/red] {reason}")
            raise typer.Exit(1) from exc
        if result.status != PipelineStatus.COMPLETED.value:
            reason = result.error or f"chat turn ended with status {result.status!r}"
            await journal.run_failed(reason)
            console.print(f"[red]turn failed:[/red] {reason}")
            raise typer.Exit(1)
        await journal.run_completed(
            "completed",
            metrics={"turn_id": result.turn_id, "tokens": result.tokens_consumed},
        )
        console.print(result.reply)
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


async def _run_observe_pipeline(goal: str, params: dict[str, Any]) -> None:
    """One ``builtin.observe`` run through the registry's own door.

    The goal is the observed command line (the pipeline splits it); the
    knobs come from the declaration's params. This is the same driver
    ``paa observe`` uses, so both doors journal identically and leave
    the same T1 bundle — first-class in the registry is a fact, not a
    promise (spec §6).
    """
    import shlex

    from paa.cli.observe import run_observe_pipeline

    run_id, result = await run_observe_pipeline(
        shlex.split(goal),
        workspace=Path("."),
        timeout_s=int(params.get("timeout_s", 600)),
        memory_mb=int(params.get("memory_mb", 512)),
        transcript_cap_kb=int(params.get("transcript_cap_kb", 2048)),
    )
    colour = "green" if result.status.value == "completed" else "yellow"
    console.print(f"[{colour}]{result.metrics['state']}[/{colour}] {result.summary}")
    console.print(f"[dim]bundle: {result.metrics['bundle_path']}[/dim]")
    console.print(f"[dim]correlation: {run_id}[/dim]")
    if result.status.value != "completed":
        raise typer.Exit(1)


async def _run_improve_pipeline(
    goal: str, params: dict[str, Any], gates: list[str] | None
) -> None:
    """One ``improve.reflection-loop`` run through the registry's door.

    The goal is the repository to improve; the knobs come from the
    declaration's params; the gates come from ``--gate``. This is the
    same driver ``paa improve`` uses, so both doors journal identically
    and leave the same artifacts (the observe pattern, restated for the
    loop whose operator inputs — gate commands, a human approver — do
    not fit a typed-param runner on their own).
    """
    from paa.cli.improve import _split_gates, run_improve_pipeline
    from paa.core.errors import PaaError

    validators = _split_gates(gates or [])
    try:
        run_id, result = await run_improve_pipeline(
            Path(goal),
            validators=validators,
            params=params,
        )
    except PaaError as exc:
        console.print(f"[red]improve refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    colour = "green" if result.status.value == "completed" else "yellow"
    console.print(f"[{colour}]{result.status.value}[/{colour}] {result.summary}")
    console.print(f"[dim]stopped: {result.metrics.get('stopped_reason')}[/dim]")
    if result.metrics.get("bundle_dir"):
        console.print(f"[dim]bundle: {result.metrics['bundle_dir']}[/dim]")
    console.print(f"[dim]correlation: {run_id}[/dim]")
    if result.status.value != "completed":
        raise typer.Exit(1)


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

        try:
            resolved = uuid.UUID(correlation_id)
        except ValueError as exc:
            console.print(f"[red]not a UUID: {correlation_id!r}[/red]")
            raise typer.Exit(1) from exc
        rt = await _build_runtime()
        try:
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
