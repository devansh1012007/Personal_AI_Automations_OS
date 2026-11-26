"""``paa improve`` — the guarded self-improvement loop's front door (C8.1).

One verb, five stages, no shortcuts: the command runs
``improve.reflection-loop`` over a repository the operator names, against
the runtime's own ledger, with the repository's own gate commands as the
deterministic validator and a **human at the terminal** as the gate.
Exit codes follow the observe convention — the subject's fate, not the
observation's:

* ``0`` — the cycle completed (an improvement landed, or the history was
  clean and there was nothing to propose);
* ``1`` — the cycle stopped by its own rules (validation refused the
  proposal, or the human did): a yellow line names the stopped reason,
  and the proposal artifact stays on disk for the operator to read;
* ``2`` — the door refused (no gate commands named, no such repository):
  nothing ran.

The honesty line (ADR-0017) rides the command's own help: prototypical
few-shot classification ≠ gradient meta-learning — this loop is not
meta-learning at all, it is the reflection engine's deterministic
analysis driving a guarded change process, and **no gate in it is ever
model-decided** (SPEC 2 §9): validation is exit codes, the only approval
is a human's.

``paa pipeline run improve.reflection-loop`` is the same cycle through
the registry's front door: :func:`run_improve_pipeline` is the shared
driver (the observe pattern), so both doors journal identically and
leave the same artifacts under ``PAA_HOME/improve/<run-id>/``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from paa.improve.gate import Approver
    from paa.ledger.store import LedgerStore
    from paa.pipelines.base import PipelineResult

console = Console()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def run_improve_pipeline(
    repo: Path,
    *,
    validators: Sequence[Sequence[str]],
    approver: Approver | None = None,
    params: Mapping[str, Any] | None = None,
    artifact_home: Path | None = None,
    ledger: LedgerStore | None = None,
    allow_insecure_signing: bool = False,
) -> tuple[uuid.UUID, PipelineResult]:
    """One ``improve.reflection-loop`` run on its own lineage — the shared driver.

    Both doors (``paa improve`` and
    ``paa pipeline run improve.reflection-loop``) come through here so
    the journal rows, the facade and the artifacts are identical
    whichever front door the operator chose. ``ledger`` injects a
    already-connected store (the conformance runner and the test suite
    drive their own); when it is ``None`` the runtime is built over the
    ambient ``PAA_HOME``, exactly the way the observe driver builds
    theirs. ``artifact_home`` likewise defaults to the runtime home's
    ``improve/`` directory.

    Raises :class:`~paa.core.errors.PaaError` when the repository or the
    gate commands are not usable; the caller decides how that prints.
    """
    from paa.cli.main import _build_runtime
    from paa.config import get_settings
    from paa.core.errors import PaaError
    from paa.core.types import new_correlation_id
    from paa.improve.gate import terminal_approver
    from paa.improve.pipeline import ImproveFacade, ImproveReflectionLoop
    from paa.pipelines.base import PipelineTask
    from paa.pipelines.journal import CallJournal

    repo = repo.resolve()
    if not repo.is_dir():
        raise PaaError(f"the repository to improve was not found: {repo}")
    if not validators:
        raise PaaError(
            "no validation gate commands were named; the guarded loop refuses "
            "to validate nothing — name the repository's own gates "
            "(e.g. --gate \"ruff check src\" --gate \"python -m pytest -q\")"
        )

    settings = get_settings()
    home = Path(artifact_home) if artifact_home is not None else settings.home / "improve"

    rt = None
    store = ledger
    if store is None:
        rt = await _build_runtime()
        store = rt.ledger
    try:
        run_id = new_correlation_id()
        journal = CallJournal(store, run_id)
        await journal.run_started(
            ImproveReflectionLoop.name,
            ImproveReflectionLoop.version,
            run_kind="improve",
        )
        try:
            facade = ImproveFacade(
                ledger=store,
                repo_root=repo,
                artifact_home=home,
                journal=journal,
                validators=validators,
                approver=approver or terminal_approver(who=_terminal_identity()),
                allow_insecure_signing=allow_insecure_signing,
            )
            task = PipelineTask(
                correlation_id=run_id,
                session_id=None,
                goal="self-improve",
                params=dict(params or {}),
            )
            result = await ImproveReflectionLoop().run(task, facade)
        except Exception as exc:
            # A foreign exception's text can carry the repository's paths
            # or a gate command's secrets; name the type, never the text
            # (the observe driver's rule).
            reason = exc.message if isinstance(exc, PaaError) else type(exc).__name__
            await journal.run_failed(reason)
            raise
        await journal.run_completed(
            result.status.value, metrics=dict(result.metrics)
        )
        return run_id, result
    finally:
        if rt is not None:
            await rt.close()


def _terminal_identity() -> str:
    """Who is sitting at the terminal, for the decision artifact."""
    import os

    return os.environ.get("USER") or "terminal-operator"


def _split_gates(gates: Sequence[str]) -> list[list[str]]:
    """Each ``--gate`` value is one command, shell-split into argv.

    Splitting here (not in the validator) keeps the refusal at the door:
    an unparseable command is a usage error before any run is journalled.
    """
    validators: list[list[str]] = []
    for raw in gates:
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            console.print(f"[red]--gate {raw!r} does not shell-split: {exc}[/red]")
            raise typer.Exit(2) from exc
        if not argv:
            console.print(f"[red]--gate {raw!r} is empty[/red]")
            raise typer.Exit(2)
        validators.append(argv)
    return validators


def improve(
    repo: Path = typer.Option(
        Path("."), "--repo", "-r", help="The repository to improve (its own gates run)."
    ),
    gate: list[str] = typer.Option(
        None,
        "--gate",
        help=(
            "A validation gate command, shell-split (repeatable) — the "
            "repository's own gates, e.g. --gate \"ruff check src\"."
        ),
    ),
    window_days: int = typer.Option(
        7, "--window-days", help="The reflection window the engine analyses, in days."
    ),
    validation_timeout_s: int = typer.Option(
        300, "--validation-timeout-s", help="Per-command wall budget for the gates."
    ),
    insecure_fallback_signing: bool = typer.Option(
        False,
        "--insecure-fallback-signing",
        help=(
            "Attest with the symmetric dev fallback when real Ed25519 is "
            "unavailable (honestly labelled; needs paa[attest] otherwise)."
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the run's metrics as JSON."
    ),
) -> None:
    """Run the guarded self-improvement cycle once.

    Reflection over the real ledger, a deterministic patch proposal,
    validation by this repository's own gate commands, a human gate at
    the terminal, and — only then — a commit that lands as an attested
    change bundle behind the host-git integrity gate. Nothing is ever
    auto-applied: a refused or failed cycle leaves the repository
    untouched and the proposal on disk under PAA_HOME/improve/.
    """

    validators = _split_gates(gate or [])

    from paa.core.errors import PaaError

    try:
        run_id, result = _run(
            run_improve_pipeline(
                repo,
                validators=validators,
                params={
                    "window_days": window_days,
                    "validation_timeout_s": validation_timeout_s,
                },
                allow_insecure_signing=insecure_fallback_signing,
            )
        )
    except PaaError as exc:
        console.print(f"[red]improve refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(json.dumps(result.metrics))
    else:
        colour = "green" if result.status.value == "completed" else "yellow"
        console.print(f"[{colour}]{result.status.value}[/{colour}] {result.summary}")
        console.print(f"[dim]stopped: {result.metrics.get('stopped_reason')}[/dim]")
        if result.metrics.get("bundle_dir"):
            console.print(f"[dim]bundle: {result.metrics['bundle_dir']}[/dim]")
        console.print(f"[dim]correlation: {run_id}[/dim]")
    if result.status.value != "completed":
        raise typer.Exit(1)
