"""``paa observe`` — run any CLI, watch honestly, classify the aftermath (C7.1).

The T1 front door: ``paa observe -- pytest -q`` runs the harness under
the sandbox's one-shot contract and leaves a complete observation
bundle — manifest diff, git's view, transcript, crash classification —
under ``PAA_HOME/observe/<run-id>/bundle.json``, with the run journalled
on its own lineage as ``builtin.observe`` (``resume="none"``,
``evidence_tier="T1"``), traceable with ``paa pipeline trace``.

The exit code is the observed CLI's fate: ``0`` only for a clean exit.
A crashed or failed harness is a *successful observation* with a failed
subject, and the distinction is the whole point of the artifact — so
the command says ``signalled``/``timed_out``/… on the console and exits
``1``, never pretending the harness succeeded.

``paa pipeline run builtin.observe`` is the same run through the
registry's front door: :func:`run_observe_pipeline` is the shared
driver, so both doors journal identically and leave the same bundle.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    from paa.pipelines.base import PipelineResult

console = Console()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def run_observe_pipeline(
    command: list[str],
    *,
    workspace: Path,
    timeout_s: int,
    memory_mb: int,
    transcript_cap_kb: int,
    child_env: dict[str, str] | None = None,
) -> tuple[uuid.UUID, PipelineResult]:
    """One ``builtin.observe`` run on its own lineage — the shared driver.

    Both doors (``paa observe`` and ``paa pipeline run builtin.observe``)
    come through here so the journal rows, the facade and the bundle are
    identical whichever front door the operator chose. Raises
    :class:`~paa.core.errors.PaaError` when the goal is not a runnable
    command; the caller decides how that prints.
    """
    from paa.cli.main import _build_runtime
    from paa.config import get_settings
    from paa.core.types import new_correlation_id
    from paa.observe.pipeline import ObserveFacade, ObservePipeline
    from paa.pipelines.base import PipelineTask
    from paa.pipelines.journal import CallJournal
    from paa.sandbox import get_sandbox

    settings = get_settings()
    sandbox = await get_sandbox(settings.sandbox)

    rt = await _build_runtime()
    try:
        run_id = new_correlation_id()
        journal = CallJournal(rt.ledger, run_id)
        await journal.run_started(
            ObservePipeline.name,
            ObservePipeline.version,
            run_kind="observe",
            resume="none",
            evidence_tier="T1",
        )
        try:
            facade = ObserveFacade(
                sandbox=sandbox,
                journal=journal,
                workspace=workspace.resolve(),
                bundle_home=settings.home,
                child_env=child_env or None,
            )
            task = PipelineTask(
                correlation_id=run_id,
                session_id=None,
                # The goal is the command line, quoted so an argv word
                # with spaces survives the pipeline's own shlex round
                # trip exactly (``builtin.observe`` splits it back).
                goal=" ".join(shlex.quote(part) for part in command),
                params={
                    "timeout_s": timeout_s,
                    "memory_mb": memory_mb,
                    "transcript_cap_kb": transcript_cap_kb,
                },
            )
            result = await ObservePipeline().run(task, facade)
        except Exception as exc:
            from paa.core.errors import PaaError

            reason = exc.message if isinstance(exc, PaaError) else str(exc)
            await journal.run_failed(reason)
            raise

        await journal.run_completed(
            result.status.value, metrics=dict(result.metrics)
        )
        return run_id, result
    finally:
        await rt.close()


def observe(
    command: list[str] = typer.Argument(
        ..., help="The command to observe, e.g. -- pytest -q."
    ),
    workspace: Path = typer.Option(
        Path("."), "--workspace", "-w", help="The workspace the CLI runs in."
    ),
    timeout_s: int = typer.Option(
        600, "--timeout-s", help="Wall-clock budget for the observed run."
    ),
    memory_mb: int = typer.Option(
        512, "--memory-mb", help="Memory ceiling for the observed child (MB)."
    ),
    transcript_cap_kb: int = typer.Option(
        2048, "--transcript-cap-kb", help="Per-stream transcript cap (KiB)."
    ),
    env: list[str] = typer.Option(
        None, "--env", help="An env var for the child, KEY=VAL (repeatable)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the bundle's summary as JSON."
    ),
) -> None:
    """Observe one command and classify what it left behind."""

    import json

    child_env: dict[str, str] = {}
    for item in env or []:
        if "=" not in item:
            console.print(f"[red]--env expects KEY=VAL, got {item!r}[/red]")
            raise typer.Exit(2)
        key, _, val = item.partition("=")
        child_env[key] = val

    from paa.core.errors import PaaError

    try:
        run_id, result = _run(
            run_observe_pipeline(
                command,
                workspace=workspace,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
                transcript_cap_kb=transcript_cap_kb,
                child_env=child_env,
            )
        )
    except PaaError as exc:
        console.print(f"[red]observe refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(json.dumps(result.metrics))
    else:
        colour = "green" if result.status.value == "completed" else "yellow"
        console.print(
            f"[{colour}]{result.metrics['state']}[/{colour}] "
            f"exit={result.metrics['exit_code']} "
            f"{result.metrics['duration_ms']:.0f} ms"
        )
        console.print(f"[dim]bundle: {result.metrics['bundle_path']}[/dim]")
        console.print(f"[dim]correlation: {run_id}[/dim]")
    if result.status.value != "completed":
        raise typer.Exit(1)
