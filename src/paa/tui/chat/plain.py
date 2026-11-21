"""``paa chat --plain`` — the no-Textual fallback loop.

SPEC 2 §3.2's chat surface, degraded on purpose: this module drives the **same**
:class:`~paa.chat.service.ChatService` the TUI drives, over the **same**
:class:`~paa.tui.chat.wiring.ChatWiring` (breaker on the model seam, signals on
the cold lake, ledger under everything), and it never imports Textual — not at
module scope, not anywhere — so ``paa chat --plain`` works with the ``[tui]``
extra not installed. The ledger cannot tell which surface drove a turn; the
differences are presentation only:

* replies stream as plain text through the terminal, no markdown render;
* the memory rail prints as indented dim lines after each completed turn;
* Ctrl+C follows the TUI's Esc contract, not the usual REPL one: the first
  interrupt stops the streaming turn (the service writes
  ``CHAT_TURN_CANCELLED``, shielded — this surface only has to *cancel*), and
  the REPL keeps running; Ctrl+C at the prompt or Ctrl+D leaves;
* the slash set is the subset that makes sense without screens
  (:data:`PLAIN_SLASH_COMMANDS`); an unknown slash is a one-line error;
* the transcript is *not* replayed on resume — the plain surface says so in
  that case instead of pretending. The lineage, the cold lake and the facts
  carry everything; the TUI replays them, this loop does not.

Shape: the asyncio world runs on a dedicated loop in a daemon thread and the
REPL runs synchronously on the main thread. That split is what makes the
contract above work without touching signal handlers: ``input()`` runs on the
main thread (where :mod:`readline` editing is active), the service streams from
the loop thread while the operator's thread waits on the turn, and a Ctrl+C
raises :class:`KeyboardInterrupt` in the main thread — interrupting either the
prompt or the wait, which is exactly the two cases the contract distinguishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Final

from rich.console import Console

from paa import __version__
from paa.chat.loop import build_chat_model
from paa.chat.session import SessionNotFoundError
from paa.core.errors import PaaError
from paa.memory.facts import FactRepository
from paa.models.registry import UnknownProviderError
from paa.pipelines.base import PipelineStatus
from paa.tui.chat.errors import ErrorKind, classify, microcopy
from paa.tui.chat.journal import Journal, journal_for_home
from paa.tui.chat.render import fmt_tokens, sanitize
from paa.tui.chat.wiring import ChatWiring

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from paa.chat.service import TurnResult
    from paa.config import Settings
    from paa.memory.facts import Fact
    from paa.runtime import Runtime

__all__ = ["PLAIN_SLASH_COMMANDS", "run_plain_session"]

#: The prompt the operator types after. Two roles, two labels — the same role
#: words the TUI's stream labels lines with.
_PROMPT: Final = "you: "

#: The assistant's streaming label, printed once per turn before the deltas.
_REPLY_LABEL: Final = "assistant: "

#: The slash set that makes sense without screens. Each entry is dispatched in
#: :meth:`PlainChat._dispatch_slash`; anything else is a one-line error.
PLAIN_SLASH_COMMANDS: Final[dict[str, str]] = {
    "/new": "start a fresh session",
    "/sessions": "list sessions (resume with --session next time)",
    "/memory": "memory snapshot — the substrate's own numbers",
    "/model": "show or set the model (echo is the offline default)",
    "/help": "list the plain-mode slash commands",
    "/quit": "leave paa chat",
}

#: Facts shown in the plain memory rail per turn (the TUI rail's own bound).
_RAIL_FACT_LIMIT: Final[int] = 12

#: Statement width the rail truncates at — the TUI rail's own bound, so both
#: surfaces abbreviate the same fact the same way.
_RAIL_STATEMENT_WIDTH: Final[int] = 34

#: readline history bound, when the platform provides readline.
_HISTORY_LINES: Final[int] = 500


def _prime_readline() -> bool:
    """Enable readline line-editing for ``input()`` when the platform has it.

    Importing :mod:`readline` is what wires the GNU hooks into ``input()`` —
    there is nothing to call for the editing itself; the one call here bounds
    the history so a long session cannot eat memory. Returns whether the
    editing is active (Windows without pyreadline: it is not).
    """
    try:
        import readline
    except ImportError:  # pragma: no cover — platform-specific
        return False
    with contextlib.suppress(Exception):
        readline.set_history_length(_HISTORY_LINES)
    return True


class PlainChat:
    """The ``--plain`` surface: one service, one loop thread, no Textual.

    All substrate work runs on the loop thread (:meth:`_submit`); the main
    thread only reads input, waits on futures, and prints. Every print goes
    through one of the bounded helpers, never a bare ``print`` — the same
    "no raw traceback, bounded text" rule the TUI applies.
    """

    def __init__(
        self,
        wiring: ChatWiring,
        loop: asyncio.AbstractEventLoop,
        *,
        session_id: uuid.UUID | None = None,
        journal: Journal | None = None,
        console: Console | None = None,
    ) -> None:
        self._wiring = wiring
        self._loop = loop
        self._session_id = session_id
        self._journal = journal if journal is not None else journal_for_home(wiring.home)
        self._console = console if console is not None else Console(markup=False)
        self._turn_fut: Future[TurnResult] | None = None
        self._stop = False
        self._reply_open = False

    # -- entry -------------------------------------------------------------------

    def run(self) -> None:
        """Set the session up, then serve turns until Ctrl+C / Ctrl+D / /quit."""
        self._journal.log(
            "ui.started",
            surface="plain",
            version=__version__,
            model=self._wiring.settings.model,
            policy=self._wiring.permission_mode.value,
        )
        readline_active = _prime_readline()
        try:
            self._submit(self._setup_session()).result()
            self._print_header(readline_active)
            while not self._stop:
                try:
                    line = input(_PROMPT)
                except EOFError:  # Ctrl+D — the quiet way out
                    break
                except KeyboardInterrupt:  # Ctrl+C at the prompt — leave
                    break
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    self._dispatch_slash(text)
                else:
                    self._run_turn(text)
        finally:
            self._note(
                f"left — session {self._short_id()} stays on the ledger; resume "
                "with paa chat --session <id>"
            )
            self._journal.log("ui.exit", surface="plain")

    # -- loop bridge -------------------------------------------------------------

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        """Run one coroutine on the loop thread; the caller waits on the result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _short_id(self) -> str:
        return str(self._session_id)[:8] if self._session_id else "?"

    # -- session -----------------------------------------------------------------

    async def _setup_session(self) -> None:
        """Create or resume the session (same degrade as the TUI's setup)."""
        if self._session_id is None:
            self._session_id = await self._wiring.service.create_session()
            self._journal.log(
                "chat.session_created", session_id=str(self._session_id)
            )
            return
        try:
            record = await self._wiring.service.resume_session(self._session_id)
        except SessionNotFoundError as exc:
            # A bad --resume id must not brick the loop: say what happened,
            # start fresh — the same degrade :meth:`ChatApp.setup_session` takes.
            self._session_id = await self._wiring.service.create_session()
            self._note(
                f"could not resume — {exc.message}. Started a new session instead; "
                "check the id with /sessions."
            )
            self._journal.log(
                "chat.session_created", session_id=str(self._session_id)
            )
            return
        self._note(
            f"resumed session {self._short_id()} — "
            f"{record.turns_proposed} turn(s) on the ledger; the plain surface "
            "does not replay transcripts (the cold lake keeps them verbatim)"
        )
        self._journal.log("chat.session_resumed", session_id=str(self._session_id))

    async def _new_session(self) -> None:
        """/new — one lineage, one header line (the TUI's wording)."""
        sid = await self._wiring.service.create_session()
        self._session_id = sid
        self._journal.log("chat.session_created", session_id=str(sid))
        self._note(
            f"session {str(sid)[:8]} started — isolated; semantic facts are global "
            "with provenance, this session's turns stay inside it"
        )

    # -- turns -------------------------------------------------------------------

    def _run_turn(self, text: str) -> None:
        """Drive one turn from the main thread: submit, wait, print outcomes.

        Ctrl+C while waiting cancels the turn (the service records
        CHAT_TURN_CANCELLED shielded on the loop thread) and the REPL
        continues; the wait itself survives the interrupt, so the cancel is
        awaited to completion before the prompt returns.
        """
        session_id = self._session_id
        if session_id is None:
            return
        if self._turn_fut is not None and not self._turn_fut.done():
            self._note(
                "already streaming — one turn at a time. Press Ctrl+C to stop "
                "the current turn first."
            )
            return
        if self._wiring.resilient.breaker_open:
            self._note(
                "model circuit open — 3 consecutive failures reached. Next: "
                "/model echo for the offline path, or restart paa chat."
            )
            return
        self._journal.log("chat.turn_submitted", session_id=str(session_id))
        turn = self._submit(self._drive(text))
        self._turn_fut = turn
        try:
            try:
                turn.result()
                return
            except KeyboardInterrupt:
                # First interrupt during a stream: stop the turn, keep the REPL.
                turn.cancel()
                with contextlib.suppress(BaseException):
                    turn.result()  # awaited: the service's shielded cancel completes
                self._end_reply()
                self._note(microcopy(ErrorKind.CANCELLED, asyncio.CancelledError()))
                self._journal.log("chat.turn_cancelled", session_id=str(session_id))
                return
        except asyncio.CancelledError:
            # The loop thread cancelled the task (the Ctrl+C path above from a
            # second signal) — same note, same ledger row.
            self._end_reply()
            self._note(microcopy(ErrorKind.CANCELLED, asyncio.CancelledError()))
            self._journal.log("chat.turn_cancelled", session_id=str(session_id))
            return
        except Exception as exc:
            self._end_reply()
            kind = classify(exc)
            self._note(microcopy(kind, exc))
            self._journal.log(
                "chat.turn_error",
                kind=kind.value,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        finally:
            self._turn_fut = None

    async def _drive(self, text: str) -> TurnResult:
        """The turn coroutine (loop thread): send, then print the outcome lines."""
        session_id = self._session_id
        if session_id is None:
            raise PaaError("no session yet — setup has not run")
        self._write_raw(_REPLY_LABEL)
        try:
            result = await self._wiring.service.send(
                session_id, text, on_delta=self._make_sink()
            )
        except asyncio.CancelledError:
            raise  # the service already recorded CHAT_TURN_CANCELLED, shielded
        except Exception as exc:
            self._end_reply()
            self._note(microcopy(classify(exc), exc))
            self._journal.log(
                "chat.turn_error",
                kind=classify(exc).value,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._end_reply()
        if result.status == PipelineStatus.COMPLETED.value:
            self._journal.log(
                "chat.turn_completed",
                session_id=str(session_id),
                turn_id=result.turn_id,
                recalled=len(result.recalled_fact_ids),
                tokens=result.tokens_consumed,
            )
            self._note(
                f"recalled {len(result.recalled_fact_ids)} fact(s) · signals "
                f"{len(result.signal_ids)} · budget {result.bundle_tokens}/"
                f"{result.token_ceiling} tokens"
            )
            await self._print_rail(result)
        else:
            self._journal.log(
                "chat.turn_failed",
                session_id=str(session_id),
                turn_id=result.turn_id,
                reason=result.error,
            )
            self._note(f"✗ turn failed — {result.error or 'unknown reason'}")
        return result

    def _make_sink(self) -> Callable[[str], Awaitable[None]]:
        """The delta sink: plain text straight to the terminal, no throttle.

        The TUI buffers ~30 ms of deltas to spare its render loop; a terminal
        file write is its own buffer, so the plain surface prints every chunk
        as it arrives (still neutralised at the door — see :func:`sanitize`).
        """

        async def sink(delta: str) -> None:
            self._write_raw(sanitize(delta))

        return sink

    async def _print_rail(self, result: TurnResult) -> None:
        """The memory rail, plain form: indented dim lines after the turn.

        The same content the TUI's :class:`~paa.tui.chat.widgets.MemoryRail`
        renders — consulted facts with confidence and times cited, captured
        signals, the budget line — computed from the same substrate reads.
        """
        facts: list[Fact] = []
        repo = FactRepository(self._wiring.db)
        for fact_id in result.recalled_fact_ids[:_RAIL_FACT_LIMIT]:
            try:
                fact = await repo.get_fact(fact_id)
            except Exception:
                fact = None
            if fact is not None:
                facts.append(fact)
        lines = ["consulted  —  none served"] if not facts else ["consulted"]
        for fact in facts:
            statement = fact.statement.replace("\n", " ")
            if len(statement) > _RAIL_STATEMENT_WIDTH:
                statement = statement[: _RAIL_STATEMENT_WIDTH - 1] + "…"
            line = f"▸ {statement}  {fact.confidence:.2f}"
            if fact.use_count:
                line += f" · cited {fact.use_count}\u00d7"
            lines.append(line)
        if result.signal_ids:
            lines.append("captured this turn")
            for signal_id in result.signal_ids:
                lines.append(f"▸ signal {signal_id[:12]}… → extracted")
        lines.append(
            f"budget {fmt_tokens(result.bundle_tokens)}/"
            f"{fmt_tokens(result.token_ceiling)} tokens"
        )
        for line in lines:
            self._print_dim(f"  {line}")

    # -- slash commands ------------------------------------------------------------

    def _dispatch_slash(self, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        self._journal.log(
            "ui.slash", surface="plain", command=command, argument=argument
        )
        if command == "/new":
            self._submit(self._new_session()).result()
        elif command == "/sessions":
            self._submit(self._show_sessions()).result()
        elif command == "/memory":
            self._submit(self._show_memory()).result()
        elif command == "/model":
            self._model_command(argument)
        elif command == "/help":
            self._print_help()
        elif command == "/quit":
            self._stop = True
        else:
            self._note(
                f"unknown command '{command}' — not in the plain set. /help "
                "lists what this build supports."
            )

    async def _show_sessions(self) -> None:
        """/sessions — every session folded from the ledger, newest first."""
        records = await self._wiring.service.list_sessions()
        if not records:
            self._note("no sessions on the ledger yet — say something, or /new.")
            return
        for record in records:
            fence = "isolated" if not record.cross_session_episodic else "cross-session"
            cancelled = (
                f", {record.turns_cancelled} cancelled" if record.turns_cancelled else ""
            )
            failed = f", {record.turns_failed} failed" if record.turns_failed else ""
            started = (
                record.created_at.strftime("%Y-%m-%d %H:%M UTC")
                if record.created_at
                else "unknown"
            )
            self._console.print(
                f"▸ session {str(record.session_id)[:12]}…  [{record.status}]  {fence}",
                markup=False,
            )
            self._console.print(
                f"  model:{record.model or 'unknown'} · turns "
                f"{record.turns_proposed} ({record.turns_completed} ok{failed}"
                f"{cancelled}) · {fmt_tokens(record.tokens_consumed)} tok · "
                f"started {started}",
                markup=False,
            )

    async def _show_memory(self) -> None:
        """/memory — the snapshot the TUI dashboard renders, as lines."""
        kpis, funnel, curator, facts = await self._wiring.memory_snapshot()
        self._console.print("MEMORY — the substrate's own numbers", markup=False)
        self._print_dim(f"  {kpis}")
        self._print_dim(f"  {funnel}")
        self._print_dim(f"  {curator}")
        if not facts:
            self._print_dim(
                "  no facts yet — turns that state durable things fill this"
            )
            return
        for fact in facts[:_RAIL_FACT_LIMIT]:
            self._print_dim(
                f"  ▸ {fact.id[:12]}  conf {fact.confidence:.2f} · "
                f"cited {fact.use_count}\u00d7  {fact.statement[:60]}"
            )
        if len(facts) > _RAIL_FACT_LIMIT:
            self._print_dim(f"  … and {len(facts) - _RAIL_FACT_LIMIT} more (hot tables)")

    def _model_command(self, argument: str) -> None:
        """/model — show the model, or set it for subsequent turns."""
        if not argument:
            note = (
                "  (offline, deterministic)"
                if self._wiring.settings.model == "echo"
                else ""
            )
            self._note(
                f"model: {self._wiring.settings.model}{note} — set with /model "
                "<name> (e.g. /model echo); the change applies from the next "
                "turn, session and memory unchanged"
            )
            return
        try:
            build_chat_model(self._wiring.settings.model_copy(update={"model": argument}))
        except (UnknownProviderError, ValueError) as exc:
            self._note(microcopy(ErrorKind.MODEL_PERMANENT, exc))
            return
        try:
            self._wiring = self._wiring.with_model(argument)
        except Exception as exc:
            self._note(microcopy(ErrorKind.MODEL_PERMANENT, exc))
            return
        self._wiring.resilient.reset()
        self._note(
            f"model set to {argument} — subsequent turns use it. The session, "
            "its facts and the ledger are unchanged."
        )
        self._journal.log("ui.model_set", surface="plain", model=argument)

    def _print_help(self) -> None:
        """/help — the plain set, no more (honesty beats a borrowed list)."""
        self._note("commands (plain surface):")
        for name, help_text in PLAIN_SLASH_COMMANDS.items():
            self._print_dim(f"  {name}  —  {help_text}")

    # -- output --------------------------------------------------------------------

    def _print_header(self, readline_active: bool) -> None:
        fence = (
            "isolated"
            if not self._wiring.settings.cross_session_episodic
            else "cross-session episodic ON"
        )
        self._console.print(
            f"paa chat — plain surface · session {self._short_id()} · {fence} · "
            f"model:{self._wiring.settings.model} · "
            f"policy:{self._wiring.permission_mode.value}",
            markup=False,
        )
        hints = (
            "type a message; /help for commands; Ctrl+C stops a streaming "
            "reply; Ctrl+D leaves"
        )
        if not readline_active:
            hints += " (no readline on this terminal)"
        self._print_dim(hints)

    def _write_raw(self, text: str) -> None:
        """Stream text without a newline (reply deltas). Never raises."""
        try:
            self._console.file.write(text)
            self._console.file.flush()
            self._reply_open = True
        except (OSError, ValueError):
            # A closed stdout must not kill the turn: the ledger keeps the
            # reply whatever the terminal does.
            self._reply_open = False

    def _end_reply(self) -> None:
        """Close the streaming line (one newline; none if it self-closed)."""
        if self._reply_open:
            self._write_raw("\n")
        self._reply_open = False

    def _print_dim(self, text: str) -> None:
        """One indented, dim line — the rail's and hints' house shape."""
        with contextlib.suppress(OSError, ValueError):
            self._console.print(text, style="dim", markup=False)

    def _note(self, text: str) -> None:
        """One system note line — sanitized, like the TUI's system lines."""
        self._end_reply()
        self._print_dim(sanitize(text))


def run_plain_session(
    settings: Settings,
    *,
    session_id: uuid.UUID | None = None,
    journal: Journal | None = None,
    console: Console | None = None,
) -> None:
    """Run the plain chat REPL to completion (Ctrl+C at prompt / Ctrl+D / /quit).

    Builds the runtime **on the loop thread** (the service's connections are
    loop-bound, so the loop that drives the turns must be the loop that built
    them), serves the REPL on the caller's thread, and always shuts the
    runtime down before returning. ``KeyboardInterrupt`` from a prompt-time
    Ctrl+C is consumed here — leaving is the normal way out; a second
    interrupt during a stream re-raises to the caller.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=loop.run_forever, name="paa-chat-plain", daemon=True
    )
    thread.start()
    runtime: Runtime | None = None
    try:
        runtime = asyncio.run_coroutine_threadsafe(
            _build_runtime(settings), loop
        ).result()
        wiring = ChatWiring.from_runtime(runtime, settings=settings.chat)
        chat = PlainChat(
            wiring, loop, session_id=session_id, journal=journal, console=console
        )
        with contextlib.suppress(KeyboardInterrupt):
            chat.run()
            # A second Ctrl+C during a stream lands here (the first one
            # cancelled the turn and was consumed): leaving is the outcome.
    finally:
        if runtime is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(runtime.close(), loop).result(
                    timeout=30
                )
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


async def _build_runtime(settings: Settings) -> Runtime:
    """Build the runtime on the loop thread (the plain surface's CLI path)."""
    from paa.runtime import Runtime

    return await Runtime.build(settings)
