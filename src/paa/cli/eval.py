"""``paa eval`` — the cross-harness eval harness's front doors (C7.4).

Two commands over the same task set and adapter registry:

* ``paa eval tasks`` — list the repository's eval tasks (what the
  harness can score, and what each task's deterministic probes are);
* ``paa eval run`` — run the selected task x harness matrix inside
  PAA's evidence boundary, score it with machine checks only, print
  the Rich comparison table, and write the canonical JSON report under
  ``PAA_HOME/eval/<eval-id>/report.json`` (``--json`` copies it to a
  path of your choosing).

The exit code is the *tool's* fate, not the subjects': a completed
matrix exits 0 even when every row fails, because a failing harness is
a successful evaluation of a failing harness — the report is the
product, and the recorder CLI holds the same line. A refusal (unknown
task id, unknown harness, an empty selection) exits 2 and names the
known ids.

The reproduction line the report carries is built here, from the
options as given, so re-running it selects exactly what the report
scored — the determinism guarantee's entry point.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from paa.evals.runner import EvalReport

console = Console()

eval_app = typer.Typer(
    help="The cross-harness eval harness (C7.4): same tasks, N harnesses, machine-scored.",
    no_args_is_help=True,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _mark(status: str) -> str:
    """One check's cell in the table: green ok, dim n/a, red the breach."""
    if status == "pass":
        return "[green]ok[/green]"
    if status == "not_applicable":
        return "[dim]n/a[/dim]"
    return f"[red]{status}[/red]"


# ---------------------------------------------------------------------------
# tasks — the listing door
# ---------------------------------------------------------------------------


@eval_app.command("tasks")
def eval_tasks() -> None:
    """List the repository's eval tasks: id, kind, intent, probes."""
    from paa.core.errors import PaaError
    from paa.evals.tasks import load_tasks, task_set_root

    try:
        root = task_set_root()
        tasks = load_tasks(root)
    except PaaError as exc:
        console.print(f"[red]eval refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    console.print(f"[dim]task set: {root}[/dim]")
    table = Table(title=f"{len(tasks)} eval task(s)")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("intent", overflow="fold")
    table.add_column("probes", no_wrap=True)
    for task in tasks:
        probes = []
        if task.forbidden_created:
            probes.append(f"-create:{len(task.forbidden_created)}")
        if task.forbidden_changed:
            probes.append(f"-change:{len(task.forbidden_changed)}")
        table.add_row(
            task.task_id,
            task.kind,
            task.intent,
            " ".join(probes) or "-",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# run — the matrix door
# ---------------------------------------------------------------------------


@eval_app.command("run")
def eval_run(
    tasks: list[str] = typer.Option(
        None, "--tasks", help="Task id to run (repeatable); default: every task."
    ),
    harnesses: list[str] = typer.Option(
        None,
        "--harnesses",
        help="Harness adapter id to run (repeatable); default: every registered adapter.",
    ),
    all_tasks: bool = typer.Option(
        False, "--all", help="Every task x every registered harness (the explicit form)."
    ),
    timeout_s: int = typer.Option(
        300, "--timeout-s", help="Wall-clock budget per pair."
    ),
    json_path: Path | None = typer.Option(
        None,
        "--json",
        help="Also write the report JSON to this path (it always lands under PAA_HOME).",
    ),
) -> None:
    """Run the task x harness matrix and score it with machine checks only."""

    from paa.core.errors import PaaError
    from paa.evals.harnesses import get_adapter, known_adapters
    from paa.evals.runner import EvalRunner
    from paa.evals.tasks import load_tasks, task_set_root

    if all_tasks and (tasks or harnesses):
        console.print("[red]--all refuses company:[/red] name it alone, or name selections")
        raise typer.Exit(2)

    try:
        everything = load_tasks(task_set_root())
        selected = tuple(t for t in everything if not tasks or t.task_id in tasks)
        missing = sorted(set(tasks or ()) - {t.task_id for t in selected})
        if missing:
            known = ", ".join(t.task_id for t in everything)
            console.print(
                f"[red]unknown task id(s): {', '.join(missing)}[/red] — known tasks: {known}"
            )
            raise typer.Exit(2)
        if not selected:
            console.print("[red]no tasks selected[/red] — pass --tasks <id> or --all")
            raise typer.Exit(2)

        wanted = list(harnesses) if harnesses else [a.harness_id for a in known_adapters()]
        adapters = [get_adapter(harness_id) for harness_id in wanted]
        if not adapters:
            console.print(
                "[red]no harnesses selected[/red] — pass --harnesses <id> or --all"
            )
            raise typer.Exit(2)
    except PaaError as exc:
        console.print(f"[red]eval refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    # The reproduction line: the invocation, spelled out explicitly, so
    # re-running it selects exactly what this report scored.
    argv = ["paa", "eval", "run"]
    if all_tasks:
        argv.append("--all")
    for task_id in sorted(t.task_id for t in selected):
        argv.extend(("--tasks", task_id))
    for harness_id in wanted:
        argv.extend(("--harnesses", harness_id))

    async def _matrix() -> EvalReport:
        from paa.cli.main import _build_runtime
        from paa.config import get_settings
        from paa.sandbox import get_sandbox

        settings = get_settings()
        sandbox = await get_sandbox(settings.sandbox)
        rt = await _build_runtime()
        try:
            runner = EvalRunner(sandbox, home=settings.home, ledger=rt.ledger)
            return await runner.run_matrix(
                selected,
                adapters,
                argv=argv,
                timeout_s=float(timeout_s),
            )
        finally:
            await rt.close()

    try:
        report = _run(_matrix())
    except PaaError as exc:
        console.print(f"[red]eval refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    table = Table(title="Eval report")
    table.add_column("task", style="cyan", no_wrap=True)
    table.add_column("harness", style="magenta", no_wrap=True)
    table.add_column("mediation", no_wrap=True)
    table.add_column("tests", no_wrap=True)
    table.add_column("bundle", no_wrap=True)
    table.add_column("intent", no_wrap=True)
    table.add_column("forbidden", no_wrap=True)
    table.add_column("overall", no_wrap=True)
    for row in report.rows:
        by_name = row.verdict.by_name()
        marks = [
            _mark(by_name[name].status.value if name in by_name else "missing")
            for name in (
                "tests_pass",
                "bundle_verifies",
                "diff_matches_intent",
                "forbidden_effects",
            )
        ]
        overall = row.verdict.overall.value
        table.add_row(
            row.verdict.task,
            row.verdict.harness,
            row.verdict.mediation,
            *marks,
            f"[green]{overall}[/green]" if overall == "pass" else f"[red]{overall}[/red]",
        )
    console.print(table)

    summary = report.to_json()["summary"]
    console.print(
        f"{summary['passed']}/{summary['rows']} rows passed; "
        f"scoring sha256 {report.scoring_sha256()[:16]}…"
    )
    console.print(f"[dim]report: {report.report_path or '(not written)'}[/dim]")
    console.print(f"[dim]reproduce: {report.reproduce}[/dim]")

    if json_path is not None:
        report.write(json_path)


__all__ = ["eval_app"]
