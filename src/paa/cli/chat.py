"""``paa chat`` — the chat surface sub-app (SPEC 2 §3.1 C4.4).

``paa chat`` opens the Textual TUI; ``paa chat --plain`` runs the terminal-only
fallback loop. The two are the same product on the same substrate — same
:class:`~paa.chat.service.ChatService`, same ledger contract, same memory —
and this module is the seam that picks the front-end:

* The TUI lives behind a lazy import **and** an installed-module probe, because
  Textual is the ``[tui]`` extra, not a core dependency: without it the command
  says what to install (or to run ``--plain``) instead of dying with an
  ``ImportError`` traceback.
* ``--plain`` never imports :mod:`paa.tui.chat.app` at all
  (:mod:`paa.tui.chat.plain` is deliberately Textual-free), so the fallback
  works on a bare ``pip install paa``.
* ``--session``/``--resume`` name a session lineage by id; resume is the
  substrate's native re-read, so both spellings are aliases of one option.
* ``--model`` names a registry row for the turn model; ``echo`` (the default)
  is offline and deterministic, so a chat demo needs no network and no key.
* ``--console`` opens the app on the Loop Console screen (C8.5) — loop
  management first (registry panel, install/fork through the gates,
  conformance), with Esc climbing back to the conversation. It is a TUI
  screen, so it refuses the ``--plain`` combination rather than silently
  opening a surface the plain loop does not have.

The module-level ``app`` is the standard typer sub-app export, wired into the
root CLI by the orchestrator (see ``paa.cli.main``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from pathlib import Path

    from paa.config import Settings

__all__ = ["app"]

console = Console()

#: Holds the TUI's runtime-log file handle open for the process lifetime —
#: structlog writes to whatever stream it was last configured with, and a
#: handle that went out of scope would be closed by the interpreter's final
#: GC pass mid-session, turning every later log line into a raised
#: ``ValueError`` on a closed file.
_log_handle: object | None = None

app = typer.Typer(
    name="chat",
    help="Talk to the runtime with persistent memory (TUI; --plain for terminals only).",
    invoke_without_command=True,
)


def _parse_session_id(value: str | None) -> uuid.UUID | None:
    """The operator's session id, or ``None`` to start a fresh lineage.

    A malformed id is refused before anything is built — a flag typo that
    costs a runtime build is a flag typo worth catching first (the same rule
    ``paa agent`` applies to its mutually-exclusive resume flags).
    """
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        console.print(
            f"[red]not a session id:[/red] {value!r} — expected a UUID "
            "(see the ids /sessions prints)"
        )
        raise typer.Exit(1) from None


def _resolved_settings(model: str | None) -> Settings:
    """Process settings with the ``--model`` override applied to the chat slot.

    The copy is detached from the cached settings (``get_settings`` is
    ``lru_cache``d and its return value is shared) so a one-shot command can
    carry an override without writing anything back into the cache.
    """
    from paa.config import get_settings

    base = get_settings()
    if model is None or model == base.chat.model:
        return base
    return base.model_copy(
        update={"chat": base.chat.model_copy(update={"model": model})}
    )


def _check_model(settings: Settings) -> None:
    """Resolve the chat model at wiring time, not mid-conversation.

    A typo must surface as a clean refusal here, the same way
    :func:`paa.chat.loop.build_chat_model` surfaces it to the wiring — not as
    a failed turn after the operator typed a message.
    """
    from paa.chat.loop import build_chat_model
    from paa.models.registry import UnknownProviderError

    try:
        build_chat_model(settings.chat)
    except (UnknownProviderError, ValueError) as exc:
        console.print(
            f"[red]unknown model {settings.chat.model!r}[/red] — {exc}. "
            "Names resolve through the model registry (PAA_MODELS__*); "
            "'echo' always works offline."
        )
        raise typer.Exit(1) from exc


async def _run_tui(
    settings: Settings, session_id: uuid.UUID | None, *, start_on_console: bool = False
) -> None:
    """Build the runtime, wire the chat surface, run the Textual app."""
    from paa.runtime import Runtime
    from paa.tui.chat.app import ChatApp
    from paa.tui.chat.wiring import ChatWiring

    runtime = await Runtime.build(settings)
    try:
        wiring = ChatWiring.from_runtime(runtime, settings=settings.chat)
        _redirect_runtime_logs_to_file(wiring.home)
        chat_app = ChatApp(
            wiring, session_id=session_id, start_on_console=start_on_console
        )
        await chat_app.run_async()
    finally:
        await runtime.close()


def _redirect_runtime_logs_to_file(home: Path | None) -> None:
    """Point structlog at ``PAA_HOME/logs/runtime.log`` before the TUI starts.

    The root CLI callback sends diagnostics to stderr — the right default for
    every one-shot command. A full-screen Textual app is the one surface where
    that is wrong: the app owns the terminal, and an INFO line mid-conversation
    (``memory.signal_processed`` fires every turn) lands raw escape-less text
    into the middle of the painted screen. The file keeps the trace the F12
    drawer promises (the journal stays the chat surface's own event stream;
    this file is the substrate's) without touching the operator's terminal.

    Boot diagnostics printed *before* this redirect already went to stderr —
    visible, honest, and finished before the first paint. No home means no
    redirect: the app degrades to stderr rather than losing its logs.
    """
    global _log_handle
    if home is None:
        return
    from paa.observability.logging import configure_logging

    try:
        logs = home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _log_handle = (logs / "runtime.log").open("a", encoding="utf-8")
    except OSError:
        return  # unwritable home: stderr stays, the turn must not pay for it
    configure_logging(stream=_log_handle)


@app.callback(invoke_without_command=True)
def chat(
    ctx: typer.Context,
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Run the terminal-only surface (no Textual needed; same service, "
        "plain streaming, indented memory rail).",
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        "--resume",
        help="Resume a session lineage by id — the ledger is re-read; the TUI "
        "replays the transcript from the cold lake.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Registry model name backing the turns. Default: PAA_CHAT__MODEL, "
        "which is 'echo' (offline, deterministic).",
    ),
    open_console: bool = typer.Option(
        False,
        "--console",
        help="Open on the Loop Console screen (C8.5): the pipeline registry "
        "(trust, tier, version), install/activate/fork through the real "
        "gates, and the six-check conformance run. Esc returns to the chat.",
    ),
) -> None:
    """Open the chat surface: the TUI by default, or --plain."""
    if ctx.invoked_subcommand is not None:
        # The sub-app has no subcommands today; keep the door honest if one
        # is ever added — the callback must not run its setup for it.
        return

    if open_console and plain:
        console.print(
            "[red]--console is a TUI screen of the chat app[/red] — the plain "
            "loop has no console. Drop --plain, or manage loops with "
            "[bold]paa skills[/bold] and [bold]paa pipeline[/bold] instead."
        )
        raise typer.Exit(2)

    parsed_session = _parse_session_id(session)
    settings = _resolved_settings(model)
    _check_model(settings)

    if plain:
        from paa.tui.chat.plain import run_plain_session

        run_plain_session(settings, session_id=parsed_session)
        return

    if importlib.util.find_spec("textual") is None:
        console.print(
            "[red]the chat TUI needs Textual[/red] — install the [tui] extra: "
            "pip install 'paa[tui]'. Or run [bold]paa chat --plain[/bold] for "
            "the terminal-only surface, which needs nothing extra."
        )
        raise typer.Exit(1)

    try:
        asyncio.run(_run_tui(settings, parsed_session, start_on_console=open_console))
    except KeyboardInterrupt:  # pragma: no cover — the TUI handles Ctrl+C itself
        console.print("[dim]left paa chat[/dim]")
