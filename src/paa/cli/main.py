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
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from paa import __version__
from paa.core.types import ComplexityModality, PermissionMode

app = typer.Typer(
    name="paa",
    help="Personal Autonomous Cognitive Operating System Runtime.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _build_runtime(*, run_recovery: bool = False):
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
            outcome = await rt.run(correlation_id)
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
            head = await rt.ledger.head(correlation_id_uuid(correlation_id))
            if head is None:
                console.print("[red]no such task[/red]")
                raise typer.Exit(1)
            p = await rt.project(correlation_id)
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
            events = await rt.ledger.read_correlation(correlation_id_uuid(correlation_id))
            if not events:
                console.print("[red]no such task[/red]")
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
            ok, problems = await rt.ledger.verify_chain(correlation_id_uuid(correlation_id))
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
            state = await rt.orchestrator.clear_human_gate(
                correlation_id_uuid(correlation_id), approved=approve, note=note
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
            table.add_row("relational", settings.storage.backend_relational)
            table.add_row("vector", type(rt.vector_store).__name__ if rt.vector_store else "none")
            table.add_row("graph", type(rt.graph_store).__name__ if rt.graph_store else "none")
            table.add_row("queue", type(rt.queue).__name__ if rt.queue else "none")
            table.add_row(
                "model router",
                type(rt.model_router).__name__ if rt.model_router else "none",
            )
            worker = rt.agents.get("worker")
            sandbox = getattr(worker, "_sandbox", None)
            if sandbox is not None:
                level = getattr(sandbox, "isolation_level", None)
                table.add_row(
                    "sandbox",
                    f"{sandbox.name} ({getattr(level, 'name', '?')})",
                )
            else:
                table.add_row("sandbox", "none (dry-run)")
            table.add_row("open tasks", str(len(await rt.ledger.open_correlations())))
            console.print(table)
        finally:
            await rt.close()

    _run(_do())


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (must be loopback)."),
    port: int | None = typer.Option(None, help="Bind port."),
) -> None:
    """Start the API and the background daemon over one shared runtime."""

    async def _do() -> None:
        import uvicorn

        from paa.api import create_app
        from paa.config import get_settings
        from paa.daemon import Daemon

        settings = get_settings()
        rt = await _build_runtime(run_recovery=False)
        daemon = Daemon(rt)
        api = create_app(rt)

        await daemon.start()
        config = uvicorn.Config(
            api,
            host=host or settings.api_host,
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


def correlation_id_uuid(value: str):
    import uuid

    try:
        return uuid.UUID(value)
    except ValueError:
        console.print(f"[red]invalid correlation id: {value}[/red]")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
