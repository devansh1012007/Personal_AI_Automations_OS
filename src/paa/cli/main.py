"""The ``paa`` command line — the first way a human drives the runtime.

Every command except ``serve`` is one-shot: it builds a :class:`Runtime`, does
one thing, prints a result, and closes cleanly. ``serve`` is the long-running
path — it stands up the API and the background daemon over a single shared
runtime.

Design choices worth knowing:

* **``paa ledger`` is the "explain why it did that" command.** It walks a task's
  event chain, which is the DoD's explainability requirement made usable from a
  terminal.
* **``paa doctor`` never lies about isolation or backends.** It reports what is
  actually live (embedded vs server, real sandbox vs subprocess), because a
  status command that overstates the security posture is worse than none.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

from paa import __version__
from paa.cli.coder import register_agent
from paa.core.types import ComplexityModality, PermissionMode
from paa.sandbox.base import collect_sandbox_facts, sandbox_report_rows

if TYPE_CHECKING:
    from paa.config import Settings
    from paa.runtime import Runtime

app = typer.Typer(
    name="paa",
    help="Personal Autonomous Cognitive Operating System Runtime.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _build_runtime(*, run_recovery: bool = False) -> Runtime:
    # Imported here rather than at module scope: the runtime pulls in the
    # agent hierarchy, the ledger and the storage stack, and a CLI that only
    # prints its version should not pay for that. The annotation names the
    # class through a ``TYPE_CHECKING`` import, so the runtime import stays
    # lazy — the name is needed to *type* the return, not to run the
    # function.
    from paa.runtime import Runtime

    return await Runtime.build(run_recovery=run_recovery)


# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the runtime version."""
    console.print(f"paa {__version__}")


@app.command()
def submit(
    goal: str = typer.Argument(..., help="What you want the runtime to do."),
    modality: str | None = typer.Option(None, help="SIMPLE|STANDARD|COMPLEX|MAX"),
    run: bool = typer.Option(True, help="Run to completion now (else just queue)."),
    agent: str | None = typer.Option(None, help="Target a specific agent, bypassing routing."),
) -> None:
    """Submit a task and (by default) run it to a terminal state."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            mod = ComplexityModality(modality.upper()) if modality else None
            cid = await rt.submit(goal, modality=mod, target_agent=agent)
            console.print(f"[bold]correlation:[/bold] {cid}")
            if run:
                outcome = await rt.run(cid)
                colour = "green" if outcome.ok else "yellow"
                console.print(f"[{colour}]phase: {outcome.phase.value}[/{colour}]")
                if outcome.error:
                    console.print(f"[red]error:[/red] {outcome.error}")
            else:
                console.print("[dim]queued (run with the daemon or `paa run <id>`)[/dim]")
        finally:
            await rt.close()

    _run(_do())


@app.command()
def run(correlation_id: str = typer.Argument(..., help="A queued task's id.")) -> None:
    """Drive an already-submitted task to a terminal state."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            resolved = await _resolve_task(rt, correlation_id)
            outcome = await rt.run(resolved)
            console.print(f"phase: {outcome.phase.value}")
        finally:
            await rt.close()

    _run(_do())


@app.command()
def status(correlation_id: str = typer.Argument(...)) -> None:
    """Show the current replayed state of a task."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            resolved = await _resolve_task(rt, correlation_id)
            p = await rt.project(resolved)
            table = Table(show_header=False, box=None)
            table.add_row("phase", p.phase.value)
            table.add_row("modality", p.modality.value)
            table.add_row("steps", f"{len(p.completed_steps)}/{len(p.plan_steps)} done")
            table.add_row("attempts", str(p.attempts))
            table.add_row("tokens", str(p.tokens_consumed))
            if p.awaiting_reason:
                table.add_row("awaiting", p.awaiting_reason)
            if p.policy_reason:
                table.add_row("policy", p.policy_reason)
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@app.command()
def ledger(correlation_id: str = typer.Argument(...)) -> None:
    """Walk a task's event chain — the 'explain why it did that' view."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            resolved = await _resolve_task(rt, correlation_id)
            events = await rt.ledger.read_correlation(resolved)
            if not events:
                # A lineage with a head but no events is an anomaly, not a
                # missing task: the head row is written in the same
                # transaction as the events, so this cannot happen without a
                # damaged ledger. Say which of the two it is.
                console.print("[red]no events recorded for this task[/red]")
                raise typer.Exit(1)
            table = Table("v", "event", "agent", "when")
            for e in events:
                table.add_row(
                    str(e.state_version),
                    e.event_type.value,
                    e.agent_role or "-",
                    e.recorded_at.strftime("%H:%M:%S"),
                )
            console.print(table)
            ok, problems = await rt.ledger.verify_chain(resolved)
            colour = "green" if ok else "red"
            detail = "ok" if ok else str(problems)
            console.print(f"chain integrity: [{colour}]{detail}[/{colour}]")
        finally:
            await rt.close()

    _run(_do())


@app.command()
def gate(
    correlation_id: str = typer.Argument(...),
    approve: bool = typer.Option(False, "--approve/--reject", help="Clear or reject the gate."),
    note: str | None = typer.Option(None),
) -> None:
    """Clear a task parked on a human-attestation gate."""

    async def _do() -> None:
        from paa.core.errors import PaaError

        rt = await _build_runtime()
        try:
            resolved = await _resolve_task(rt, correlation_id)
            state = await rt.orchestrator.clear_human_gate(
                resolved, approved=approve, note=note
            )
            console.print(f"phase: {state.phase.value}")
        except PaaError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        finally:
            await rt.close()

    _run(_do())


@app.command()
def recover() -> None:
    """Run the post-crash boot sweep and report what it did."""

    async def _do() -> None:
        rt = await _build_runtime(run_recovery=False)
        try:
            report = await rt.boot_recovery()
            if report is None:
                console.print("[dim]no recovery engine configured[/dim]")
                return
            console.print(json.dumps(report.summary(), indent=2))
        finally:
            await rt.close()

    _run(_do())


@app.command()
def doctor() -> None:
    """Report which backends and sandbox are actually live. Never overstates."""

    async def _do() -> None:
        from paa.config import get_settings

        settings = get_settings()
        rt = await _build_runtime()
        try:
            table = Table("component", "status")
            table.add_row("version", __version__)
            table.add_row("home", str(settings.home))
            table.add_row("permission mode", settings.policy.mode.value)
            # The live substrate, not the configured one. This row used to
            # print ``settings.storage.backend_relational`` — the *setting* —
            # so a deployment configured for Postgres reported "postgres"
            # while the process was using a local SQLite file. A diagnostic
            # that reads a setting instead of the object it describes is a
            # diagnostic that lies, and this one lies about the substrate
            # the ledger is stored in.
            table.add_row("relational", type(rt.db).__name__)
            table.add_row("vector", type(rt.vector_store).__name__ if rt.vector_store else "none")
            table.add_row("graph", type(rt.graph_store).__name__ if rt.graph_store else "none")
            table.add_row("queue", type(rt.queue).__name__ if rt.queue else "none")
            table.add_row(
                "model router",
                type(rt.model_router).__name__ if rt.model_router else "none",
            )
            sandbox = rt.sandbox
            if sandbox is not None:
                # ORDERING IS LOAD-BEARING. ``diagnose()`` is what asks the
                # Docker daemon whether the requested OCI runtime is
                # registered, and the isolation level is derived from that
                # answer. Reading the level first would print a stale
                # snapshot — "NAMESPACE" — on the row directly above
                # "runsc (registered)", which is how a diagnostic ends up
                # contradicting itself.
                facts = await _sandbox_facts(sandbox)
                rows = sandbox_report_rows(facts)
                if rows:
                    for label, value in rows:
                        table.add_row(label, value)
                else:
                    table.add_row("sandbox", "none (dry-run)")
            else:
                table.add_row("sandbox", "none (dry-run)")
            table.add_row("open tasks", str(len(await rt.ledger.open_correlations())))
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


async def _sandbox_facts(sandbox: Any) -> dict[str, Any]:
    """The backend's self-report, for :func:`sandbox_report_rows` to render.

    A thin wrapper over :func:`collect_sandbox_facts`, so the CLI and the TUI
    normalise a failed diagnostic the same way — the difference between the
    two surfaces is how they *draw* the facts, not how they obtain them.
    """
    return await collect_sandbox_facts(sandbox)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (must be loopback)."),
    port: int | None = typer.Option(None, help="Bind port."),
) -> None:
    """Start the API and the background daemon over one shared runtime."""

    async def _do() -> None:
        import uvicorn

        from paa.api import create_app
        from paa.daemon import Daemon

        settings = _settings_or_refuse()
        # The bind address is settled *before* anything is built or started.
        # A refused bind must not have opened the database, started the
        # daemon, or left a half-started server behind to be cleaned up.
        bind_host = _resolve_bind_host(host, settings)

        rt = await _build_runtime(run_recovery=False)
        daemon = Daemon(rt)
        api = create_app(rt)

        await daemon.start()
        config = uvicorn.Config(
            api,
            host=bind_host,
            port=port or settings.api_port,
            log_level=settings.observability.log_level.lower(),
        )
        server = uvicorn.Server(config)
        console.print(
            f"[green]paa serving[/green] on http://{config.host}:{config.port} "
            f"(daemon running)"
        )
        try:
            await server.serve()
        finally:
            await daemon.stop()
            await rt.close()

    try:
        _run(_do())
    except KeyboardInterrupt:
        console.print("\n[dim]shutting down[/dim]")


@app.command()
def mode(
    new_mode: str = typer.Argument(..., help="ask|auto|supervised|lockdown|safe|high_risk"),
) -> None:
    """Show how to set the permission mode (persisted via env/.env)."""
    try:
        resolved = PermissionMode(new_mode.upper())
    except ValueError:
        console.print(f"[red]unknown mode {new_mode!r}[/red]")
        raise typer.Exit(1) from None
    console.print(
        f"Set [bold]PAA_POLICY__MODE={resolved.value}[/bold] in your environment "
        f"or .env to run in {resolved.value} mode."
    )


@app.command()
def tui() -> None:
    """Run the interactive dashboard until you quit."""
    from paa.tui.runner import run_tui

    asyncio.run(run_tui())


def correlation_id_uuid(value: str) -> uuid.UUID:
    """The canonical UUID for a task id, or a clean refusal.

    A UUID has two spellings — the hyphenated one and the 32-character hex
    one — and the ledger stores the hyphenated form. Every command that
    takes a task id therefore coerces its argument *once*, here, and uses
    the resolved UUID for every lookup it makes afterwards. A command that
    handed the raw argument to one lookup and the canonical one to another
    answered the same id two different ways.
    """
    try:
        return uuid.UUID(value)
    except ValueError:
        console.print(f"[red]invalid correlation id: {value}[/red]")
        raise typer.Exit(1) from None


async def _resolve_task(rt: Runtime, correlation_id: str) -> uuid.UUID:
    """The canonical id of a task that exists.

    The one place a task id is turned into a lineage: the argument is
    coerced once, and a lineage that does not exist is reported as "no such
    task" rather than left to the replay.

    The replay's own refusal — "cannot replay an empty history without an
    initial projection" — is an *invariant*, not a message an operator can
    act on, and it is what a lineage-less id used to produce. The head
    projection is the cheap existence check: one primary-key lookup, and it
    is written in the same transaction as the events, so a lineage with
    events always has one.
    """
    resolved = correlation_id_uuid(correlation_id)
    if await rt.ledger.head(resolved) is None:
        console.print("[red]no such task[/red]")
        raise typer.Exit(1)
    return resolved


def _settings_or_refuse() -> Settings:
    """The process settings, or a clean refusal.

    ``Settings`` refuses a non-loopback ``api_host`` unless the escape hatch
    is set, and it does so by *raising*: a configuration error the operator
    caused is not a crash, and a traceback for "you set PAA_API_HOST to a
    public address" tells them less than the message the validator wrote.
    The validator's own wording is kept, because it names the field and the
    override.
    """
    from pydantic import ValidationError

    from paa.config import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        detail = "; ".join(str(err.get("msg", "")) for err in exc.errors())
        console.print(f"[red]invalid configuration:[/red] {detail}")
        raise typer.Exit(1) from exc


def _resolve_bind_host(host: str | None, settings: Settings) -> str:
    """The address the ingestion API will bind, refused unless it is loopback.

    ``--host`` and ``Settings.api_host`` are two spellings of one decision —
    the address this process listens on — and the RFC's "no public edge
    ingestion" rule is a property of the *bind*, not of the field the bind
    was spelled in. The settings model guards the field; the flag reached the
    same socket without ever passing that guard, which made the easier path
    to a public listener the unguarded one.

    So the flag goes through the same validator, and the same escape hatch
    (``PAA_ALLOW_NON_LOOPBACK=1``, read as ``Settings.allow_non_loopback``):
    a deliberate non-loopback bind is still possible, it just has to be
    deliberate through *both* doors.
    """
    from pydantic import ValidationError

    from paa.config import Settings

    resolved = host or settings.api_host
    if resolved == settings.api_host or settings.allow_non_loopback:
        # Either the very address the settings were built with — which the
        # validator has already vetted, since a non-loopback ``api_host``
        # cannot survive construction without the escape hatch — or the
        # escape hatch itself, which is the operator saying so on purpose.
        return resolved

    try:
        # The rule itself rather than a copy of it: building the settings
        # with the candidate address runs ``Settings._enforce_loopback``,
        # the only place that knows which addresses count as loopback. The
        # escape hatch is forced off because it was handled above.
        Settings(api_host=resolved, allow_non_loopback=False)
    except ValidationError:
        console.print(
            f"[red]refusing to bind {resolved!r}: the ingestion API is loopback-only.[/red] "
            "Set PAA_ALLOW_NON_LOOPBACK=1 to bind a non-loopback address deliberately."
        )
        raise typer.Exit(1) from None
    return resolved


#: ``paa agent`` lives in its own module because it is the one command that
#: assembles :mod:`paa.coder` into something runnable; registering it here keeps
#: the composition in ``coder/`` and the CLI surface here.
register_agent(app)


memory_app = typer.Typer(
    name="memory",
    help="Read and query the memory subsystem (facts, search, signals, drain, world-model).",
)


@memory_app.command("facts")
def facts(
    entity: str = typer.Argument(..., help="Entity name to list live facts for."),
) -> None:
    """List live facts for an entity, resolved by name then queried by id."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            from paa.memory.facts import FactRepository

            repo = FactRepository(rt.db)
            resolved = await repo.resolve_entity(entity)
            if resolved is None:
                console.print("[dim]no facts[/dim]")
                return
            rows = await repo.query_facts(entity_id=resolved.id)
            if not rows:
                console.print("[dim]no facts[/dim]")
                return
            table = Table(
                "entity",
                "predicate",
                "object",
                "confidence",
                "importance",
                "domain",
                title=f"facts for {entity}",
            )
            for f in rows:
                table.add_row(
                    f.entity_name or f.entity_id,
                    f.predicate,
                    f.object_value,
                    f"{f.confidence:.2f}",
                    f"{f.importance:.2f}",
                    f.domain,
                )
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@memory_app.command("search")
def search(
    text: str = typer.Argument(
        ..., help="Text to match against fact predicates and object values."
    ),
) -> None:
    """Keyword search over fact predicates and object values."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            from paa.memory.facts import FactRepository

            repo = FactRepository(rt.db)
            try:
                rows = await repo.search_facts_by_text(text)
            except ValueError as exc:
                console.print(f"[red]invalid search text:[/red] {exc}")
                raise typer.Exit(1) from exc
            if not rows:
                console.print("[dim]no matches[/dim]")
                return
            table = Table(
                "entity",
                "predicate",
                "object",
                "confidence",
                "importance",
                "domain",
                title=f"search: {text}",
            )
            for f in rows:
                table.add_row(
                    f.entity_name or f.entity_id,
                    f.predicate,
                    f.object_value,
                    f"{f.confidence:.2f}",
                    f"{f.importance:.2f}",
                    f.domain,
                )
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@memory_app.command("signals")
def signals(
    status: list[str] = typer.Option(
        None, "--status", "-s", help="Filter by sync_status (repeatable)."
    ),
) -> None:
    """List cold-lake signals and their processing status."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append("sync_status IN (" + ", ".join("?" for _ in status) + ")")
                params.extend(status)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            rows = await rt.db.fetch_all(
                "SELECT id, channel, external_id, sync_status, received_at, processed_at "
                "FROM cold_lake_signals "
                + where
                + " ORDER BY received_at DESC, id DESC LIMIT 50",
                params,
            )
            if not rows:
                console.print("[dim]no signals[/dim]")
                return
            table = Table(
                "id",
                "channel",
                "external_id",
                "status",
                "received_at",
                "processed_at",
                title="cold-lake signals",
            )
            for r in rows:
                table.add_row(
                    str(r["id"])[:8],
                    str(r["channel"]),
                    str(r["external_id"] or "-"),
                    str(r["sync_status"]),
                    str(r["received_at"]),
                    str(r["processed_at"] or "-"),
                )
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@memory_app.command("drain")
def drain(
    limit: int = typer.Option(
        25, "--limit", help="Max signals to claim in this batch."
    ),
) -> None:
    """Run one MemoryCreator batch over unprocessed cold-lake signals."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            from paa.memory.creator import MemoryCreator

            creator = MemoryCreator(rt.db)
            report = await creator.run_batch(limit=limit)
            summary = report.summary()
            table = Table(
                "claimed",
                "processed",
                "ignored",
                "malformed",
                "quarantined",
                "errors",
                "facts_written",
                title="drain batch report",
            )
            table.add_row(
                str(summary["claimed"]),
                str(summary["processed"]),
                str(summary["ignored"]),
                str(summary["malformed"]),
                str(summary["quarantined"]),
                str(summary["errors"]),
                str(summary["facts_written"]),
            )
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@memory_app.command("world-model")
def world_model(
    doc: str = typer.Argument(..., help="Belief document filename (e.g. current_focus.md)."),
) -> None:
    """Print the managed section of a belief document."""

    async def _do() -> None:
        rt = await _build_runtime()
        try:
            from paa.memory.world_model import BeliefDocument, WorldModel

            try:
                belief_doc = BeliefDocument(doc)
            except ValueError:
                console.print(f"[red]no such belief document: {doc}[/red]")
                raise typer.Exit(1) from None
            wm = WorldModel(rt.settings.storage.vault_path)
            text = wm.read_managed(belief_doc)
            console.print(text or "[dim]no managed section[/dim]")
        finally:
            await rt.close()

    _run(_do())


app.add_typer(memory_app, name="memory")


if __name__ == "__main__":
    app()
