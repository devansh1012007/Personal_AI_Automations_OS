"""``paa thread`` — the local-first control plane's front doors (C7.5).

Five verbs over one thread record and one ledger lineage
(:mod:`paa.thread`):

* ``paa thread new <title> [--workspace PATH] [--worktree]
  [--harness NAME]`` — create a thread: mandate from the effective
  policy, ``MANDATE_GRANTED`` + ``THREAD_CREATED`` on the thread's own
  lineage, the record file under ``PAA_HOME/threads/``. ``--worktree``
  re-points the workspace at a fresh ``git worktree`` of the given
  repository, on the thread's own branch under ``PAA_HOME`` — the
  operator's checkout stays untouched.
* ``paa thread run <id> [--harness NAME] [--record]`` — one segment:
  handoff packet assembled and hashed, ``THREAD_SEGMENT_STARTED`` before
  the harness spawns, the bound command contained (plainly, or through
  the recorder's whole evidence boundary with ``--record``),
  ``THREAD_SEGMENT_ENDED`` after.
* ``paa thread swap <id> --harness NAME`` — the demo verb: record the
  harness transition, then run under the new binding. Context intact is
  checkable, not asserted: each segment carries the hash of the packet
  it started from, and the next packet's inputs include the previous
  segment's ledger rows.
* ``paa thread show <id>`` — the mandate summary, the segments with
  harness names, packet hashes and evidence pointers.
* ``paa thread list`` — every thread, oldest first.

``paa ledger <thread-id>`` is the other half of the same surface: the
thread id *is* the correlation id, so the ordinary ledger walk shows the
unbroken chain and the mandate, with no thread-specific flag.

Exit codes follow the observe/record rule: ``0`` only for a clean exit —
a crashed harness is a *successfully recorded* segment of a failed stint,
and that distinction is the artifact's whole point.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from paa.runtime import Runtime
    from paa.thread.engine import SegmentOutcome, ThreadEngine
    from paa.thread.record import ThreadRecord

console = Console()

thread_app = typer.Typer(
    help="The local-first control plane (C7.5): one thread, many harnesses.",
    no_args_is_help=True,
)

#: How much of a plainly-contained harness's captured stdout the CLI
#: relays after the run. The recorder path relays live (its own door);
#: this cap is for the T1 path, where the full transcript lives only in
#: the operator's terminal — an unbounded dump would make a chatty
#: harness unreadable, and the interesting end is the end.
_STDOUT_TAIL_CHARS = 2000


def _tail(text: str, limit: int = _STDOUT_TAIL_CHARS) -> str:
    """The last ``limit`` characters of ``text``, marked when cut."""
    if len(text) <= limit:
        return text
    return f"[…{len(text) - limit} chars cut]\n" + text[-limit:]


def _short(identifier: str) -> str:
    """An id short enough for a table cell; the full id rides beside it."""
    return identifier[:8]


class _RuntimeMemorySource:
    """Recalled facts as handoff candidates, over the runtime's store.

    The same door ``paa memory search`` reads: the bounded keyword scan
    the coder agent's ``recall_facts`` falls back to, over the thread's
    title. Confidence/importance are clamped into the unit interval
    before they reach the gatherer's validation — a decayed value outside
    ``[0, 1]`` is a memory-substrate bug this adapter must not crash on;
    the packet degrades to fewer elements, which the gatherer's floors
    already handle honestly.
    """

    def __init__(self, db: Any) -> None:
        from paa.memory.facts import FactRepository

        self._repo = FactRepository(db)

    async def elements(self, thread: ThreadRecord) -> list[Any]:
        from paa.context.gatherer import ContextElement

        facts = await self._repo.search_facts_by_text(thread.title, limit=8)
        return [
            ContextElement(
                id=f"fact:{fact.id}",
                content=fact.statement,
                relevance=1.0,
                confidence=min(1.0, max(0.0, fact.confidence)),
                importance=min(1.0, max(0.0, fact.importance)),
                memory_domain=fact.domain or "semantic",
            )
            for fact in facts
        ]


async def _build_engine() -> tuple[ThreadEngine, Runtime]:
    """The engine over a real runtime: ledger, sandbox, memory source.

    One construction path for every mutating verb so they cannot drift:
    the runtime owns the ledger and the fact store, the sandbox comes
    from the settings, and the memory half is the adapter above.
    Returns ``(engine, runtime)`` — the caller closes the runtime.
    """
    from paa.cli.main import _build_runtime
    from paa.config import get_settings
    from paa.sandbox import get_sandbox
    from paa.thread.engine import ThreadEngine

    settings = get_settings()
    sandbox = await get_sandbox(settings.sandbox)
    rt = await _build_runtime()
    engine = ThreadEngine(
        ledger=rt.ledger,
        home=rt.settings.home,
        sandbox=sandbox,
        memory_source=_RuntimeMemorySource(rt.db),
    )
    return engine, rt


def _mandate_line(record: ThreadRecord) -> str:
    """The one-line mandate summary ``show`` renders, from the grant payload."""
    asserted = record.mandate.get("requester_asserted")
    if not isinstance(asserted, dict):
        asserted = {}
    scope = record.mandate.get("scope")
    if not isinstance(scope, dict):
        scope = {}
    return (
        f"basis={record.mandate.get('basis', '?')} "
        f"requester={asserted.get('kind', '?')}:{asserted.get('id', '?')} "
        f"policy={record.mandate.get('policy_label', '?')} "
        f"workspace={scope.get('workspace') or '-'}"
    )


def _print_outcome(outcome: SegmentOutcome, *, as_json: bool, verb: str) -> None:
    """Render one segment outcome — run's and swap's shared tail.

    The machine door (``--json``) prints only the summary; the human door
    relays the harness's captured streams first (bounded), then the
    segment's fate, its packet hash and its evidence pointer.
    """
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "thread_id": outcome.thread.id,
                    "segment_id": outcome.segment.segment_id,
                    "harness": outcome.segment.harness,
                    "packet_sha256": outcome.segment.packet_sha256,
                    "packet_path": str(outcome.packet_path),
                    "exit_code": outcome.exit_code,
                    "timed_out": outcome.timed_out,
                    "duration_ms": round(outcome.duration_ms, 3),
                    "record_run_dir": (
                        str(outcome.record_run_dir) if outcome.record_run_dir else None
                    ),
                }
            )
        )
        return
    if outcome.stdout:
        console.print(_tail(outcome.stdout).rstrip())
    if outcome.stderr:
        console.print(f"[dim]{_tail(outcome.stderr).rstrip()}[/dim]")
    colour = "green" if outcome.exit_code == 0 and not outcome.timed_out else "yellow"
    state = "timed_out" if outcome.timed_out else f"exit={outcome.exit_code}"
    if verb == "swap":
        head = f"swapped to {outcome.segment.harness}, segment {state}"
    else:
        head = f"segment {state} harness={outcome.segment.harness}"
    console.print(f"[{colour}]{head}[/{colour}] {outcome.duration_ms:.0f} ms")
    console.print(
        f"[dim]packet: {_short(outcome.segment.packet_sha256)}… "
        f"({outcome.packet_path})[/dim]"
    )
    if outcome.record_run_dir is not None:
        console.print(f"[dim]record: {outcome.record_run_dir}[/dim]")


def _exit_code_of(outcome: SegmentOutcome) -> int:
    """``0`` only for a clean, untimed-out exit — the observe/record rule."""
    return 0 if outcome.exit_code == 0 and not outcome.timed_out else 1


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


@thread_app.command("new")
def thread_new(
    title: str = typer.Argument(..., help="What this thread is working on."),
    workspace: Path = typer.Option(
        Path("."), "--workspace", "-w", help="The workspace the thread runs in."
    ),
    worktree: bool = typer.Option(
        False,
        "--worktree",
        help="Create a git worktree of the workspace's repository, on the "
        "thread's own branch under PAA_HOME, and run there.",
    ),
    harness: str = typer.Option(
        None, "--harness", help="Default harness for run/swap when not passed."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the thread as JSON."),
) -> None:
    """Create a thread: mandate, ledger lineage, record file."""

    async def _do() -> ThreadRecord:
        engine, rt = await _build_engine()
        try:
            return await engine.create_thread(
                title=title,
                workspace=workspace,
                policy=rt.settings.policy,
                default_harness=harness,
                worktree=worktree,
            )
        finally:
            await rt.close()

    from paa.core.errors import PaaError

    try:
        record = asyncio.run(_do())
    except PaaError as exc:
        console.print(f"[red]thread refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "id": record.id,
                    "title": record.title,
                    "workspace": record.workspace,
                    "status": record.status.value,
                    "mandate": _mandate_line(record),
                    "default_harness": record.default_harness,
                }
            )
        )
    else:
        console.print(f"[bold]thread:[/bold] {record.id}")
        console.print(f"[dim]title: {record.title}[/dim]")
        console.print(f"[dim]workspace: {record.workspace}[/dim]")
        console.print(f"[dim]mandate: {_mandate_line(record)}[/dim]")
        console.print(f"[dim]ledger: paa ledger {record.id}[/dim]")


# ---------------------------------------------------------------------------
# run + swap
# ---------------------------------------------------------------------------


@thread_app.command("run")
def thread_run(
    thread_id: str = typer.Argument(..., help="The thread to run a segment on."),
    harness: str = typer.Option(
        None, "--harness", help="The binding to run (default: the thread's)."
    ),
    record: bool = typer.Option(
        False,
        "--record",
        help="Run under the flight recorder: SandboxSession, hash-chained "
        "record, observe bundle, the proxy door for mediated tool calls.",
    ),
    timeout_s: int = typer.Option(
        600, "--timeout-s", help="Wall-clock budget for the segment."
    ),
    memory_mb: int = typer.Option(
        512, "--memory-mb", help="Memory ceiling for the harness tree (MB)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the segment as JSON."),
) -> None:
    """Run one segment: handoff, contained harness, close."""

    async def _do() -> SegmentOutcome:
        engine, rt = await _build_engine()
        try:
            return await engine.run_segment(
                thread_id,
                harness=harness,
                record=record,
                timeout_s=float(timeout_s),
                memory_mb=memory_mb,
                # ``--json`` is a machine door: the recorder's live relay
                # would corrupt the payload, and the artifacts carry the
                # full transcript either way (the record CLI's own rule).
                quiet=as_json,
            )
        finally:
            await rt.close()

    from paa.core.errors import PaaError

    try:
        outcome = asyncio.run(_do())
    except PaaError as exc:
        console.print(f"[red]thread refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    _print_outcome(outcome, as_json=as_json, verb="run")
    raise typer.Exit(_exit_code_of(outcome))


@thread_app.command("swap")
def thread_swap(
    thread_id: str = typer.Argument(..., help="The thread to swap."),
    harness: str = typer.Option(..., "--harness", help="The binding to swap to."),
    record: bool = typer.Option(
        False, "--record", help="Run the new segment under the flight recorder."
    ),
    timeout_s: int = typer.Option(
        600, "--timeout-s", help="Wall-clock budget for the new segment."
    ),
    memory_mb: int = typer.Option(
        512, "--memory-mb", help="Memory ceiling for the harness tree (MB)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the segment as JSON."),
) -> None:
    """Swap the harness mid-thread: record the transition, run the new hand."""

    async def _do() -> SegmentOutcome:
        engine, rt = await _build_engine()
        try:
            return await engine.swap(
                thread_id,
                harness=harness,
                record=record,
                timeout_s=float(timeout_s),
                memory_mb=memory_mb,
                quiet=as_json,
            )
        finally:
            await rt.close()

    from paa.core.errors import PaaError

    try:
        outcome = asyncio.run(_do())
    except PaaError as exc:
        console.print(f"[red]thread refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    _print_outcome(outcome, as_json=as_json, verb="swap")
    raise typer.Exit(_exit_code_of(outcome))


# ---------------------------------------------------------------------------
# show + list
# ---------------------------------------------------------------------------


@thread_app.command("show")
def thread_show(
    thread_id: str = typer.Argument(..., help="The thread to show."),
    as_json: bool = typer.Option(False, "--json", help="Print the record as JSON."),
) -> None:
    """Show a thread: mandate, segments, packet hashes, evidence pointers."""
    from paa.config import get_settings
    from paa.core.errors import PaaError
    from paa.thread.record import load_thread

    try:
        record = load_thread(get_settings().home, thread_id)
    except PaaError as exc:
        console.print(f"[red]thread refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(
            json.dumps({"schema": "paa-thread/1", **record.model_dump(mode="json")})
        )
        return

    console.print(f"[bold]thread:[/bold] {record.id}")
    console.print(f"[dim]title: {record.title}[/dim]")
    console.print(f"[dim]workspace: {record.workspace}[/dim]")
    console.print(f"[dim]status: {record.status.value}[/dim]")
    console.print(f"[dim]mandate: {_mandate_line(record)}[/dim]")
    console.print(f"[dim]ledger: paa ledger {record.id}[/dim]")
    table = Table(
        "segment", "harness", "started", "ended", "exit", "packet", "evidence"
    )
    for segment in record.segments:
        table.add_row(
            _short(segment.segment_id),
            segment.harness,
            segment.started_at,
            segment.ended_at or "[yellow]open[/yellow]",
            "-" if segment.exit_code is None else str(segment.exit_code),
            f"{segment.packet_sha256[:8]}…",
            segment.record_run_dir or "-",
        )
    console.print(table)


@thread_app.command("list")
def thread_list(
    as_json: bool = typer.Option(False, "--json", help="Print the threads as JSON."),
) -> None:
    """List every thread, oldest first."""
    from paa.config import get_settings
    from paa.thread.record import list_threads

    # No runtime and no asyncio: listing reads the records' own directory,
    # and a command that only reads files should not pay for a ledger.
    records = list_threads(get_settings().home)

    if as_json:
        console.print_json(
            json.dumps(
                [
                    {
                        "id": record.id,
                        "title": record.title,
                        "status": record.status.value,
                        "workspace": record.workspace,
                        "segments": len(record.segments),
                    }
                    for record in records
                ]
            )
        )
        return

    table = Table("thread", "title", "status", "segments", "workspace")
    for record in records:
        table.add_row(
            _short(record.id),
            record.title,
            record.status.value,
            str(len(record.segments)),
            record.workspace,
        )
    console.print(table)
    if not records:
        console.print("[dim]no threads — create one: paa thread new <title>[/dim]")
