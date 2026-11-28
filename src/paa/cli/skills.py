"""``paa skills`` — the chat extension surface's operator door (SPEC 2 §8, C8.4).

The verbs follow the flow :mod:`paa.chat.extensions` owns — discover,
install through the five gates, activate — plus the two reads an operator
needs to aim them:

* ``paa skills install <source-dir>`` — install every skill a
  Claw-Hub/OpenClaw-shaped directory offers (``--name`` picks one);
  ``--personal`` reads ``PAA_HOME/skills/personal`` instead of a path.
  Local skill directories are unsigned, so the signature gate refuses
  without ``--allow-unverified`` — the opt-in is the operator's word,
  never this command's default.
* ``paa skills list`` — the capability catalogue a chat turn would see
  (policy-filtered, so a mode-refused capability never reads "live"),
  plus the marketplace's installed-package rows.
* ``paa skills activate <name>`` — (re-)activate an installed skill.
* ``paa skills mcp -- <command>`` — expose a self-hosted MCP server's
  tools as chat capabilities through the sandboxed adapter.

Every command runs a real runtime and leaves its EXTENSION_* rows in the
ledger: the CLI is the operator's hand on the same flow the chat service
serves, not a side door around it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from paa.chat.extensions import ChatExtensionSurface
    from paa.runtime import Runtime

__all__ = ["app"]

console = Console()

app = typer.Typer(
    name="skills",
    help="The chat extension surface: install, list and activate capabilities.",
    no_args_is_help=True,
)


def _surface(runtime: Runtime, *, allow_unverified: bool) -> ChatExtensionSurface:
    """The extension surface over a built runtime.

    The operator decisions the surface cannot make itself are made here,
    from this command's flags: the unsigned-install opt-in and the AST
    scanner. The sandbox, the permission mode and ``PAA_HOME`` come from
    the runtime, the same as every other surface that executes code.
    """
    from paa.chat.extensions import ChatExtensionSurface
    from paa.validation.ast_scanner import AstSecurityScanner

    return ChatExtensionSurface(
        ledger=runtime.ledger,
        db=runtime.db,
        home=runtime.settings.home,
        sandbox=runtime.sandbox,
        scanner=AstSecurityScanner(),
        permission_mode=runtime.settings.policy.mode,
        allow_unverified=allow_unverified,
    )


async def _build_runtime() -> Runtime:
    from paa.runtime import Runtime

    return await Runtime.build()


@app.command("install")
def install(
    source: str | None = typer.Argument(
        None,
        help="A Claw-Hub/OpenClaw-shaped source directory: a hub of skill "
        "subdirectories or one skill directory (the layout 'claw pull' produces).",
    ),
    name: str | None = typer.Option(
        None, "--name", help="Install only the skill with this name."
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help="Read the personal skills dir (PAA_HOME/skills/personal) instead of a path.",
    ),
    allow_unverified: bool = typer.Option(
        False,
        "--allow-unverified",
        help="Local skill directories are unsigned; this opt-in is what the "
        "signature gate needs. The other four gates still run.",
    ),
    no_activate: bool = typer.Option(
        False, "--no-activate", help="Install without activating (activate later by name)."
    ),
) -> None:
    """Install skills through the five-gate installer, then activate them."""
    from paa.core.errors import PaaError

    if source is None and not personal:
        console.print(
            "[red]a source is required:[/red] a directory path, or --personal for "
            "the personal skills dir."
        )
        raise typer.Exit(2)

    async def _run() -> None:
        runtime = await _build_runtime()
        try:
            surface = _surface(runtime, allow_unverified=allow_unverified)
            discovered = await surface.discover(
                source if not personal else None, personal=personal
            )
            if name is not None:
                discovered = [s for s in discovered if s.contract.skill_name == name]
                if not discovered:
                    console.print(f"[red]no skill named {name!r} in that source[/red]")
                    raise typer.Exit(1)
            if not discovered:
                where = "the personal skills dir" if personal else f"{source!r}"
                console.print(f"[dim]no skills discovered in {where}[/dim]")
                return
            for skill in discovered:
                report = await surface.install(skill, activate=not no_activate)
                if report.installed:
                    gate_names = [gate.value for gate in report.install.passed_gates]
                    gate_names += [
                        f"{gate.value} (n/a)" for gate, _ in report.install.na_gates
                    ]
                    gates = ", ".join(gate_names)
                    activation = report.activation
                    pipeline_note = (
                        f" — coding loop: {activation.pipeline}"
                        if activation is not None and activation.pipeline
                        else ""
                    )
                    console.print(
                        f"[green]installed[/green] {report.skill_name} "
                        f"(gates: {gates}){pipeline_note}"
                    )
                    console.print(
                        f"  staged at "
                        f"{surface.staged_dir(report.skill_name, skill.contract.version)}"
                    )
                else:
                    gate = (
                        report.install.refused_at.value
                        if report.install.refused_at is not None
                        else "unknown"
                    )
                    console.print(
                        f"[red]refused[/red] {report.skill_name} at the {gate} gate: "
                        f"{report.install.reason}"
                    )
                    raise typer.Exit(1)
        except PaaError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        finally:
            await runtime.close()

    asyncio.run(_run())


@app.command("list")
def list_skills() -> None:
    """Show the capability catalogue a chat turn sees, and installed packages."""
    from paa.chat.extensions import render_capability_lines

    async def _run() -> None:
        runtime = await _build_runtime()
        try:
            surface = _surface(runtime, allow_unverified=False)
            contracts = await surface.capability_catalogue()
            lines = render_capability_lines(contracts)
            table = Table(
                "capability",
                title=f"chat capability catalogue ({len(lines)} live, "
                f"policy-filtered)",
            )
            for line in lines:
                table.add_row(line)
            console.print(table)

            rows = await runtime.db.fetch_all(
                "SELECT package_name, version, kind, publisher, signature_state "
                "FROM marketplace_packages ORDER BY package_name"
            )
            if rows:
                packages = Table(
                    "package",
                    "version",
                    "kind",
                    "publisher",
                    "signature",
                    title="installed packages (the five-gate record)",
                )
                for row in rows:
                    packages.add_row(
                        str(row["package_name"]),
                        str(row["version"]),
                        str(row["kind"]),
                        str(row["publisher"]),
                        str(row["signature_state"]),
                    )
                console.print(packages)
        finally:
            await runtime.close()

    asyncio.run(_run())


@app.command("activate")
def activate(
    skill_name: str = typer.Argument(..., help="The installed skill to activate."),
) -> None:
    """(Re-)activate an installed skill as a chat capability."""
    from paa.core.errors import PaaError

    async def _run() -> None:
        runtime = await _build_runtime()
        try:
            surface = _surface(runtime, allow_unverified=False)
            try:
                activation = await surface.activate(skill_name)
            except PaaError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            pipeline_note = (
                f" — coding loop: {activation.pipeline}" if activation.pipeline else ""
            )
            console.print(
                f"[green]activated[/green] {activation.skill_name} "
                f"v{activation.version} ({activation.provider}){pipeline_note}"
            )
        finally:
            await runtime.close()

    asyncio.run(_run())


@app.command("mcp")
def mcp(
    server_command: list[str] = typer.Argument(
        ..., help="The MCP server argv, after '--'."
    ),
) -> None:
    """Expose a self-hosted MCP server's tools as chat capabilities."""
    from paa.core.errors import PaaError

    if not server_command:
        console.print("[red]an MCP server command is required[/red]")
        raise typer.Exit(2)

    async def _run() -> None:
        runtime = await _build_runtime()
        try:
            surface = _surface(runtime, allow_unverified=False)
            try:
                activations = await surface.install_mcp_server(server_command)
            except PaaError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            for activation in activations:
                console.print(
                    f"[green]activated[/green] {activation.skill_name} "
                    f"v{activation.version} (mcp_server)"
                )
            if not activations:
                console.print(
                    "[dim]no granted tools: the server exposed none the active "
                    "policy allows[/dim]"
                )
        finally:
            await runtime.close()

    asyncio.run(_run())
