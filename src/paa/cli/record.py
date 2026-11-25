"""``paa record`` — the harness-neutral flight recorder's front doors (C7.3).

Three commands, one record format (``paa-record/1``,
:mod:`paa.recorder.record`):

* ``paa record run -- <harness-cmd>`` — the wrap door the spec names
  ``paa record <harness-cmd>``: the harness runs under a
  :class:`~paa.sandbox.base.SandboxSession` for its whole life, every
  effect lands in the hash-chained flight record, and at close the run
  leaves its observe bundle (the T1 floor) and an attested change
  bundle a reviewer can ``paa verify`` on a clean machine.
* ``paa record proxy --run-dir <dir>`` — the MCP door a harness's tool
  calls ride: journalled, decided by the run's policy, executed under
  containment. Spawned by the harness (its MCP config names this
  command; the run's env carries the stanza in ``PAA_RECORD_PROXY_CMD``).
* ``paa record verify <run-dir>`` — the reviewer's door over the chain
  itself: schema, sequence, every link, the summary, the phase.

The exit code of ``run`` is the harness's fate — ``0`` only for a clean
exit — because a crashed harness is a *successful recording* of a
failed subject, and the distinction is the artifact's whole point.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
from rich.console import Console

console = Console()

record_app = typer.Typer(
    help="The harness-neutral flight recorder (C7.3): run, proxy, verify.",
    no_args_is_help=True,
)


class _StdoutSink:
    """The :class:`paa.mcp.protocol.FrameSink` for real stdio.

    Writes through ``sys.stdout.buffer`` — the raw byte stream — so
    text-mode translation can never rewrite a frame. The same class the
    MCP server's CLI uses; duplicated here rather than imported from a
    command module because a CLI's private plumbing is not another
    CLI's contract.
    """

    def __init__(self) -> None:
        self._buffer = sys.stdout.buffer

    def write(self, data: bytes) -> None:
        self._buffer.write(data)

    async def drain(self) -> None:
        self._buffer.flush()


async def _stdin_reader() -> asyncio.StreamReader:
    """A stream reader over stdin, the asyncio way (the mcp serve pattern)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (NotImplementedError, ValueError) as exc:
        raise SystemExit(
            "paa record proxy must be spawned with a pipe on stdin; see "
            "your harness's MCP server configuration."
        ) from exc
    return reader


# ---------------------------------------------------------------------------
# run — the wrap door
# ---------------------------------------------------------------------------


@record_app.command("run")
def record_run(
    command: list[str] = typer.Argument(
        ..., help="The harness command to record, e.g. -- claude."
    ),
    workspace: Path = typer.Option(
        Path("."), "--workspace", "-w", help="The workspace the harness runs in."
    ),
    timeout_s: int = typer.Option(
        3600, "--timeout-s", help="Lifetime budget for the whole recorded session."
    ),
    memory_mb: int = typer.Option(
        1024, "--memory-mb", help="Memory ceiling for the harness tree (MB)."
    ),
    transcript_cap_kb: int = typer.Option(
        2048, "--transcript-cap-kb", help="Per-stream transcript cap (KiB)."
    ),
    goal: str = typer.Option(
        None,
        "--goal",
        help="The mandate's goal for the attested bundle (default: the command line).",
    ),
    mode: str = typer.Option(
        "ask",
        "--mode",
        help="Policy mode for mediated tool calls: ask, accept_edits, plan, bypass.",
    ),
    allow: list[str] = typer.Option(
        None, "--allow", help="Pre-allow rule, e.g. 'bash(git *)' (repeatable)."
    ),
    deny: list[str] = typer.Option(
        None, "--deny", help="Deny rule — always wins over allow (repeatable)."
    ),
    ask: list[str] = typer.Option(
        None, "--ask", help="Force-ask rule (resolves to a recorded denial)."
    ),
    no_attest: bool = typer.Option(
        False, "--no-attest", help="Skip the attested bundle at close."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Do not relay the harness's stdout live."
    ),
    env: list[str] = typer.Option(
        None, "--env", help="An env var for the harness, KEY=VAL (repeatable)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the run's summary as JSON."
    ),
) -> None:
    """Record one harness session: contained, journalled, attested at close."""
    from paa.config import get_settings
    from paa.core.errors import PaaError
    from paa.recorder.core import POLICY_MODES, PolicySpec, RecorderCore, RecordOutcome
    from paa.sandbox import get_sandbox

    child_env: dict[str, str] = {}
    for item in env or []:
        if "=" not in item:
            console.print(f"[red]--env expects KEY=VAL, got {item!r}[/red]")
            raise typer.Exit(2)
        key, _, val = item.partition("=")
        child_env[key] = val

    if mode not in POLICY_MODES:
        console.print(
            f"[red]--mode must be one of {POLICY_MODES}, got {mode!r}[/red]"
        )
        raise typer.Exit(2)

    async def _run() -> RecordOutcome:
        settings = get_settings()
        sandbox = await get_sandbox(settings.sandbox)
        core = RecorderCore(sandbox, home=settings.home)
        return await core.record(
            command,
            workspace=workspace.resolve(),
            timeout_s=float(timeout_s),
            memory_mb=memory_mb,
            transcript_cap_kb=transcript_cap_kb,
            policy=PolicySpec(
                mode=mode,
                allow=tuple(allow or []),
                deny=tuple(deny or []),
                ask=tuple(ask or []),
            ),
            child_env=child_env,
            goal=goal,
            attest=not no_attest,
            # ``--json`` is a machine door: the relayed harness output
            # would corrupt the payload, and the artifacts carry the
            # full transcript either way.
            quiet=quiet or as_json,
        )

    try:
        outcome = asyncio.run(_run())
    except PaaError as exc:
        console.print(f"[red]record refused:[/red] {exc.message}")
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run_id": outcome.run_id,
                    "run_dir": str(outcome.run_dir),
                    "state": outcome.state,
                    "exit_code": outcome.result.exit_code,
                    "events": outcome.record.counts,
                    "observe_bundle": str(outcome.observe_bundle_path),
                    "attest_bundle": (
                        str(outcome.attest_bundle_dir)
                        if outcome.attest_bundle_dir
                        else None
                    ),
                    "attest_error": outcome.attest_error,
                }
            )
        )
    else:
        colour = "green" if outcome.state == "clean_exit" else "yellow"
        console.print(
            f"[{colour}]{outcome.state}[/{colour}] "
            f"exit={outcome.result.exit_code} "
            f"{outcome.result.duration_ms:.0f} ms"
        )
        console.print(f"[dim]record: {outcome.run_dir}[/dim]")
        console.print(f"[dim]observe bundle: {outcome.observe_bundle_path}[/dim]")
        if outcome.attest_bundle_dir is not None:
            console.print(f"[dim]attested bundle: {outcome.attest_bundle_dir}[/dim]")
            console.print(
                f"[dim]verify: paa verify {outcome.attest_bundle_dir}[/dim]"
            )
        elif outcome.attest_error is not None:
            console.print(f"[red]attest failed:[/red] {outcome.attest_error}")

    if outcome.state != "clean_exit":
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# proxy — the MCP door
# ---------------------------------------------------------------------------


@record_app.command("proxy")
def record_proxy(
    run_dir: Path = typer.Option(
        ..., "--run-dir", help="The recorded run's directory (its record.json)."
    ),
) -> None:
    """Serve the run's mediated toolset over MCP stdio (the harness spawns this)."""
    from paa.config import get_settings
    from paa.core.errors import PaaError
    from paa.recorder.proxy import RecordProxy
    from paa.sandbox import get_sandbox

    async def _serve() -> None:
        settings = get_settings()
        sandbox = await get_sandbox(settings.sandbox)
        proxy = RecordProxy(run_dir, sandbox)
        reader = await _stdin_reader()
        await proxy.serve(reader, _StdoutSink())

    try:
        asyncio.run(_serve())
    except PaaError as exc:
        print(f"paa record proxy: {exc.message}", file=sys.stderr)
        raise typer.Exit(2) from exc
    except SystemExit:
        raise
    except Exception as exc:  # a proxy that dies says why, on stderr
        print(f"paa record proxy: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# verify — the reviewer's door over the chain
# ---------------------------------------------------------------------------


@record_app.command("verify")
def record_verify(
    run_dir: Path = typer.Argument(..., help="The recorded run's directory."),
    as_json: bool = typer.Option(False, "--json", help="Print the verdict as JSON."),
) -> None:
    """Verify a flight record: schema, sequence, every chain link, the summary."""

    from paa.recorder.record import verify_record

    verdict = verify_record(run_dir)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "run_dir": str(verdict.run_dir),
                    "ok": verdict.ok,
                    "phase": verdict.phase,
                    "events": verdict.events,
                    "chain_root": verdict.chain_root,
                    "histogram": verdict.histogram,
                    "problems": list(verdict.problems),
                }
            )
        )
    else:
        checks = [
            ("schema", verdict.schema_ok),
            ("sequence", verdict.seq_ok),
            ("chain", verdict.chain_ok),
            ("summary", verdict.summary_ok),
            ("torn tail", not verdict.torn_tail),
            ("finalised", verdict.phase == "final"),
        ]
        for label, ok in checks:
            mark = "[green]ok[/green]" if ok else "[red]FAILED[/red]"
            console.print(f"  {mark}  {label}")
        console.print(
            f"\n{verdict.events} events, chain root "
            f"{(verdict.chain_root or '—')[:16]}…, phase {verdict.phase}"
        )
        for problem in verdict.problems:
            console.print(f"[red]-[/red] {problem}")
    if not verdict.ok:
        raise typer.Exit(1)
