"""``paa chat`` — the ChatApp: turn driving and the command surface.

The app owns four things and nothing else:

1. **Composition** — the :class:`~paa.tui.chat.wiring.ChatWiring` it is
   handed (built in :mod:`paa.tui.chat.wiring`, the textual-free module the
   plain loop shares, re-exported here for callers): a
   :class:`~paa.chat.service.ChatService` built from the runtime's substrate,
   with the model seam wrapped in
   :class:`~paa.tui.chat.resilience.ResilientChatModel` (retry + breaker live
   *inside* the seam, so a retry never writes a second ledger row).
2. **Turn driving** — one streaming task at a time; deltas throttle through a
   ~30 ms flush; Esc cancels the task, which is exactly how
   ``CHAT_TURN_CANCELLED`` gets written (the service records it on
   cancellation, shielded — the UI only has to *cancel*).
3. **The command surface** — Ctrl+P palette over all actions and the slash
   set; every command is wired to something real, and the ones the service
   cannot support yet say so honestly instead of pretending.
4. **Observability** — a :class:`~paa.tui.chat.journal.Journal` (rotated file
   under ``PAA_HOME/logs/chat.log`` plus the F12 ring) and inline microcopy
   from :mod:`paa.tui.chat.errors`; raw tracebacks never reach the screen.

Accessibility notes, stated honestly: complete keyboard operability and two
registered themes (default + high contrast, F2) are implemented here. Textual
has no screen-reader bridge in this version; the closest affordances — a
completion notification on turn *completion* (never per token) and a
text-first render (every state names itself in words and glyphs) — are what
the app actually provides.

The read-only screens (ledger, memory, policy) read the substrate the app
itself built — ``wiring.ledger``, ``wiring.db`` — the same data
:class:`~paa.chat.service.ChatService` writes. Writes go through the service
only; no screen mutates memory or the ledger.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

import structlog
from textual.app import App
from textual.binding import Binding, BindingType
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import RichLog

from paa import __version__
from paa.chat.context_bundle import build_context_bundle
from paa.chat.loop import build_chat_model
from paa.chat.service import ChatService, TurnResult
from paa.chat.session import CHAT_CHANNEL, SessionNotFoundError
from paa.config import ChatSettings
from paa.core.errors import PaaError
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.memory.facts import Fact, FactRepository
from paa.models.registry import UnknownProviderError
from paa.pipelines.base import PipelineStatus
from paa.tui.chat.console import ConsoleService
from paa.tui.chat.errors import ErrorKind, classify, microcopy
from paa.tui.chat.journal import Journal, journal_for_home
from paa.tui.chat.render import sanitize
from paa.tui.chat.screens import (
    ChatScreen,
    ConsoleScreen,
    ContextModal,
    DoctorModal,
    FactDetailModal,
    LedgerScreen,
    LogDrawer,
    MemoryScreen,
    PolicyScreen,
    SessionsScreen,
)
from paa.tui.chat.theme import (
    CHAT_THEME_NAME,
    HC_THEME_NAME,
    chat_theme,
    chat_theme_hc,
)
from paa.tui.chat.widgets import (
    ROLE_ASSISTANT,
    ROLE_USER,
    MemoryRail,
    RailFact,
)
from paa.tui.chat.wiring import ChatWiring

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["SLASH_COMMANDS", "ChatApp", "ChatWiring"]

log = structlog.get_logger(__name__)

#: Streaming flush cadence: ~30 ms per the spec's throttle budget.
_STREAM_FLUSH_SECONDS: Final[float] = 0.03

#: Facts shown in the memory rail per turn (bounded rail, honest list).
_RAIL_FACT_LIMIT: Final[int] = 12

#: Transcript window loaded on resume (newest). The whole lineage stays in
#: the ledger and the cold lake; the screen loads a bounded recent window so
#: a 10k-turn resume does not have to materialise all of it to be usable.
_RESUME_WINDOW: Final[int] = 500

#: The slash set — each entry is wired in :meth:`ChatApp._dispatch_slash`.
SLASH_COMMANDS: Final[dict[str, str]] = {
    "/new": "start a fresh session",
    "/sessions": "list sessions — switch, resume, archive",
    "/memory": "memory dashboard (Ctrl+M)",
    "/ledger": "this session's event chain (Ctrl+L)",
    "/policy": "grants and the denial log (Ctrl+K)",
    "/console": "the Loop Console — registry, install, fork, conformance (Ctrl+B)",
    "/context": "the assembled context packet, verbatim",
    "/compact": "toggle compact density",
    "/fork": "branch a session from a turn (not yet in this build)",
    "/export": "write the transcript to a markdown file",
    "/model": "show or set the model (echo is the offline default)",
    "/doctor": "chat-surface checks",
    "/help": "list the slash commands",
    "/quit": "leave paa chat",
}


# ---------------------------------------------------------------------------
# The command palette
# ---------------------------------------------------------------------------


def _palette_actions(chat: ChatApp) -> list[tuple[str, str, Callable[[], Any]]]:
    """(display, help, callable) triples — the palette's whole surface.

    The palette invokes entries through ``call_later``, which awaits coroutine
    callables, so ``/quit`` may be the app's async ``action_quit`` directly.
    The turn-driving entries stay synchronous closures and hand their async
    work to :meth:`run_worker` from inside.
    """
    return [
        ("sessions — list, switch, archive", "/sessions", chat.action_show_sessions),
        ("memory dashboard", "/memory", chat.action_show_memory),
        ("ledger — this session's event chain", "/ledger", chat.action_show_ledger),
        ("policy — grants and denials", "/policy", chat.action_show_policy),
        (
            "loops console — registry, install, fork, conformance",
            "/console",
            chat.action_show_console,
        ),
        (
            "new session",
            "/new",
            lambda: chat.run_worker(chat.new_session(), exclusive=True),
        ),
        (
            "context packet — verbatim",
            "/context",
            lambda: chat.run_worker(chat.show_context(), exclusive=True),
        ),
        ("toggle compact density", "/compact", chat.toggle_compact),
        (
            "export transcript",
            "/export",
            lambda: chat.run_worker(chat.export_transcript(None), exclusive=True),
        ),
        (
            "doctor — surface checks",
            "/doctor",
            lambda: chat.run_worker(chat.show_doctor(), exclusive=True),
        ),
        ("toggle high-contrast theme", "F2", chat.action_toggle_theme),
        ("log drawer", "F12", chat.show_log_drawer),
        ("quit", "/quit", chat.action_quit),
    ]


class ChatCommandProvider(Provider):
    """Ctrl+P — fuzzy palette over every chat action (the slash set + screens)."""

    async def discover(self) -> Hits:
        for display, help_text, action in _palette_actions(_as_chat(self.app)):
            yield DiscoveryHit(display, action, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, help_text, action in _palette_actions(_as_chat(self.app)):
            score = matcher.match(display)
            if score > 0:
                yield Hit(score, matcher.highlight(display), action, help=help_text)


def _as_chat(app: App[Any]) -> ChatApp:
    """The app, narrowed to :class:`ChatApp` (the only host of this provider).

    Textual's :class:`~textual.command.Provider` hands its children an
    ``App[object]`` (command providers are app-agnostic), while :class:`ChatApp`
    is an ``App[None]``; the parameter is the app-agnostic shape and the body
    is the one place the narrowing is asserted.
    """
    return cast("ChatApp", app)


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


class ChatApp(App[None]):
    """``paa chat`` — one header bar, the stream, the memory rail, the prompt."""

    CSS = """
    #chat-header { dock: top; }
    #chat-body { height: 1fr; }
    #msg-stream { width: 1fr; }
    #rail { width: 36; }
    #prompt {
        dock: bottom; height: 1; border: none; padding: 0 1;
        background: transparent; color: $foreground;
    }
    #prompt:focus { background: $surface; }
    .utility-title { dock: top; padding: 0 1; color: $trust; text-style: bold; }
    #chain-badge { dock: top; padding: 0 1; height: 1; color: $trust; }
    #ledger-filter { border: none; padding: 0 1; }
    #memory-kpis, #memory-funnel, #memory-curator { padding: 0 1; color: $memory; }
    #policy-grants { padding: 0 1; color: $session; }
    #policy-denials-title { padding: 0 1; color: $denial; }
    #policy-clearance-note { padding: 0 1; color: $foreground-muted; }
    #sessions-title { padding: 0 1; }
    #context-title, #fact-title { color: $trust; padding: 0 1; }
    #context-label, #context-provenance, #fact-body, #fact-lineage {
        padding: 0 1; color: $foreground;
    }
    #context-json { padding: 0 1; color: $trust; }
    #console-title { dock: top; padding: 0 1; color: $trust; text-style: bold; }
    #console-pipelines, #console-skills { height: 1fr; padding: 0 1; }
    #console-skills-title { padding: 0 1; color: $session; }
    #console-forks { padding: 0 1; color: $foreground-muted; }
    #console-status { padding: 0 1; color: $foreground; }
    """

    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = {
        ChatCommandProvider
    }

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+t", "toggle_rail", "memory rail"),
        Binding("ctrl+m", "show_memory", "memory"),
        Binding("ctrl+l", "show_ledger", "ledger"),
        Binding("ctrl+k", "show_policy", "policy"),
        Binding("ctrl+b", "show_console", "loops console"),
        Binding("ctrl+o", "show_sessions", "sessions"),
        Binding("f12", "log_drawer", "logs"),
        Binding("f2", "toggle_theme", "theme", show=False),
        Binding("escape", "escape", "stop / back", show=False, priority=True),
    ]

    def __init__(
        self,
        wiring: ChatWiring,
        *,
        session_id: uuid.UUID | None = None,
        journal: Journal | None = None,
        start_on_console: bool = False,
    ) -> None:
        super().__init__()
        self.register_theme(chat_theme())
        self.register_theme(chat_theme_hc())
        # The chat theme is the app's identity, set here rather than on_mount:
        # the app's own CSS ($trust, $memory, …) only resolves against these
        # variables, so an app mounted before any theme switch would parse its
        # stylesheet against the Textual defaults and drop the rules.
        self.theme = CHAT_THEME_NAME
        self._wiring = wiring
        self._session_id = session_id
        self.journal = journal if journal is not None else journal_for_home(wiring.home)
        self._start_on_console = start_on_console
        self._console: ConsoleService | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._last_result: TurnResult | None = None
        self._stream_buf: list[str] = []
        self._last_flush: float = 0.0
        self._draft_timer: Timer | None = None
        self._pending_draft: str = ""

    # -- wiring views ---------------------------------------------------------

    @property
    def wiring(self) -> ChatWiring:
        return self._wiring

    @property
    def service(self) -> ChatService:
        return self._wiring.service

    @property
    def settings(self) -> ChatSettings:
        return self._wiring.settings

    @property
    def session_id(self) -> uuid.UUID:
        if self._session_id is None:
            raise PaaError("no session yet — setup has not run")
        return self._session_id

    @property
    def isolated(self) -> bool:
        """The session fence: episodic turns stay inside the session."""
        return not self._wiring.settings.cross_session_episodic

    @property
    def policy_mode(self) -> str:
        return self._wiring.permission_mode.value

    @property
    def console_service(self) -> ConsoleService:
        """The Loop Console's service, built once from the wiring (C8.5).

        A view over the same substrate the chat surface writes through:
        the registry, the extension surface's ledger and database, the
        runtime's sandbox for the install gates. Built lazily so a
        session that never opens the console pays nothing for it.
        """
        if self._console is None:
            self._console = ConsoleService(
                ledger=self._wiring.ledger,
                db=self._wiring.db,
                home=self._wiring.home,
                permission_mode=self._wiring.permission_mode,
                sandbox=self._wiring.sandbox,
            )
        return self._console

    # -- screens ----------------------------------------------------------------

    def get_default_screen(self) -> Screen[None]:
        return ChatScreen()

    @property
    def chat_screen(self) -> ChatScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, ChatScreen):
                return screen
        return None

    def back_to_chat(self) -> None:
        """Pop every utility screen until the chat is on top; focus the prompt."""
        while len(self.screen_stack) > 1 and not isinstance(self.screen, ChatScreen):
            self.pop_screen()
        chat = self.chat_screen
        if chat is not None:
            chat.prompt.focus()

    def _show(self, screen: Screen[None]) -> None:
        """Push a utility screen, replacing any already-pushed one."""
        if not isinstance(self.screen, (ChatScreen, ModalScreen)):
            self.pop_screen()
        self.push_screen(screen)
        self.journal.log("ui.screen_open", screen=type(screen).__name__)

    # -- app events ---------------------------------------------------------------

    def on_mount(self) -> None:
        self.journal.log(
            "ui.started",
            version=__version__,
            model=self._wiring.settings.model,
            policy=self.policy_mode,
        )
        if self._start_on_console:
            # ``paa chat --console``: the operator asked for loop management
            # first — push the console over the chat screen, exactly as the
            # binding does, and let Esc climb back to the conversation.
            self._show(ConsoleScreen())

    async def on_unmount(self) -> None:
        """Session state autosaves (spec §3.2 crash-safety)."""
        self._save_draft_now()

    # -- session lifecycle -----------------------------------------------------

    async def setup_session(self) -> None:
        """Create or resume the session, load the transcript, set the header.

        Runs as a worker from :meth:`ChatScreen.on_mount`.
        """
        chat = self.chat_screen
        if chat is None:
            return
        if self._session_id is None:
            sid = await self._wiring.service.create_session()
            self._session_id = sid
            self.journal.log("chat.session_created", session_id=str(sid))
        else:
            try:
                await self._wiring.service.resume_session(self._session_id)
                self.journal.log(
                    "chat.session_resumed", session_id=str(self._session_id)
                )
            except SessionNotFoundError as exc:
                # A bad --resume id must not brick the app: say what happened,
                # start fresh. The microcopy names the next action.
                self._session_id = await self._wiring.service.create_session()
                self.system_line(
                    f"could not resume — {exc.message}. Started a new session "
                    "instead; check the id with /sessions."
                )

        transcript = await self._load_transcript()
        if transcript:
            chat.stream.load_history(transcript)
        self._refresh_header()
        self._restore_draft()
        chat.stream.scroll_to_end()

    async def _load_transcript(self) -> list[tuple[str, str]]:
        """Reconstruct the conversation from the cold lake (the verbatim home)."""
        signals = self._wiring.signals
        if signals is None or self._session_id is None:
            return []
        wanted = str(self._session_id)
        found: list[tuple[str, str]] = []
        try:
            async for signal in signals.iter_by_channel(
                CHAT_CHANNEL, newest_first=True, limit=_RESUME_WINDOW * 2
            ):
                try:
                    payload: Any = json.loads(signal.raw_payload)
                except ValueError:
                    continue
                if not isinstance(payload, dict) or payload.get("session_id") != wanted:
                    continue
                role = payload.get("role")
                text = payload.get("text")
                if isinstance(text, str) and role in (ROLE_USER, ROLE_ASSISTANT):
                    found.append((str(role), text))
                if len(found) >= _RESUME_WINDOW:
                    break
        except Exception as exc:
            self.journal.log("chat.transcript_load_failed", error=str(exc))
            return []
        found.reverse()
        return found

    async def new_session(self) -> None:
        """/new — one lineage, one header refresh, a clean stream."""
        sid = await self._wiring.service.create_session()
        self._session_id = sid
        self._last_result = None
        chat = self.chat_screen
        if chat is not None:
            chat.stream.clear()
            chat.rail.set_consulted([])
            chat.rail.set_captured([])
            chat.rail.set_budget(0, 0)
        self._refresh_header()
        self.system_line(
            f"session {str(sid)[:8]} started — isolated; semantic facts are global "
            "with provenance, this session's turns stay inside it"
        )
        self.journal.log("chat.session_created", session_id=str(sid))

    async def switch_session(self, session_id: uuid.UUID) -> None:
        """Switch the live session (resume is the substrate's native re-read)."""
        try:
            await self._wiring.service.resume_session(session_id)
        except SessionNotFoundError as exc:
            self._report_error(exc)
            return
        self._session_id = session_id
        self._last_result = None
        chat = self.chat_screen
        if chat is not None:
            chat.stream.clear()
            transcript = await self._load_transcript()
            chat.stream.load_history(transcript)
            chat.stream.scroll_to_end()
        self._refresh_header()
        self.system_line(
            f"resumed session {str(session_id)[:8]} — lineage re-read from the ledger"
        )
        self.journal.log("chat.session_resumed", session_id=str(session_id))

    async def archive_session(self, record: Any) -> None:
        """Archive = CHAT_SESSION_ENDED with the fold's own counters (service API)."""
        await self._wiring.service.end_session(record.session_id, stopped_reason="archived")
        self.system_line(
            f"archived session {str(record.session_id)[:8]} — CHAT_SESSION_ENDED "
            "recorded; resume still works (it is a re-read)"
        )
        self.journal.log("chat.session_archived", session_id=str(record.session_id))
        if isinstance(self.screen, SessionsScreen):
            await self.screen.refresh_sessions()

    def _refresh_header(self) -> None:
        chat = self.chat_screen
        if chat is None or self._session_id is None:
            return
        chat.header.set_session(
            session_id=self._session_id,
            isolated=self.isolated,
            model=self._wiring.settings.model,
            policy_mode=self.policy_mode,
        )

    # -- turns -------------------------------------------------------------------

    async def handle_input(self, text: str) -> None:
        """One submitted line: slash command or a chat turn."""
        if text.startswith("/"):
            await self._dispatch_slash(text)
            return
        await self.send_turn(text)

    async def send_turn(self, text: str) -> None:
        """Drive one turn: user block, streaming block, then the drive task."""
        chat = self.chat_screen
        if chat is None or self._session_id is None:
            return
        if self._turn_task is not None and not self._turn_task.done():
            self.system_line(
                "already streaming — one turn at a time. Press Esc to stop the "
                "current turn first."
            )
            return
        if self._wiring.resilient.breaker_open:
            self.system_line(
                "model circuit open — 3 consecutive failures reached. Next: "
                "/doctor (resets it) or /model echo for the offline path."
            )
            return
        stream = chat.stream
        stream.append_message(ROLE_USER, text)
        stream.append_message(ROLE_ASSISTANT, "", streaming=True)
        self._stream_buf.clear()
        self._last_flush = time.monotonic()
        self.journal.log("chat.turn_submitted", session_id=str(self._session_id))
        self._turn_task = asyncio.create_task(self._drive_turn(text))

    async def _drive_turn(self, text: str) -> None:
        """The turn task: send with the throttled sink, then render the outcome."""
        chat = self.chat_screen
        if chat is None or self._session_id is None:
            return
        try:
            result = await self._wiring.service.send(
                self._session_id, text, on_delta=self._make_sink()
            )
        except asyncio.CancelledError:
            self._flush_stream()
            chat.stream.mark_cancelled()
            self.system_line(
                "⏹ stopped — CHAT_TURN_CANCELLED recorded in the ledger; the "
                "partial reply is attested there, not lost"
            )
            self.journal.log("chat.turn_cancelled", session_id=str(self._session_id))
            self._announce("turn stopped", "you cancelled this turn (Esc)")
            return
        except Exception as exc:
            self._flush_stream()
            chat.stream.mark_cancelled()
            self._report_error(exc)
            return
        self._flush_stream()
        self._last_result = result
        if result.status == PipelineStatus.COMPLETED.value:
            chat.stream.finalize_streaming(result.reply)
            await self._update_rail(result)
            self.system_line(
                f"recalled {len(result.recalled_fact_ids)} fact(s) · signals "
                f"{len(result.signal_ids)} · budget {result.bundle_tokens}/"
                f"{result.token_ceiling} tokens"
            )
            self.journal.log(
                "chat.turn_completed",
                session_id=str(self._session_id),
                turn_id=result.turn_id,
                recalled=len(result.recalled_fact_ids),
                tokens=result.tokens_consumed,
            )
            self._announce("assistant reply ready", result.reply[:80])
        else:
            chat.stream.finalize_streaming("")
            self.system_line(f"✗ turn failed — {result.error or 'unknown reason'}")
            self.journal.log(
                "chat.turn_failed",
                session_id=str(self._session_id),
                turn_id=result.turn_id,
                reason=result.error,
            )

    def _make_sink(self) -> Any:
        """The throttled delta sink: buffer, flush at ~30 ms."""

        async def sink(delta: str) -> None:
            self._stream_buf.append(delta)
            now = time.monotonic()
            if now - self._last_flush >= _STREAM_FLUSH_SECONDS:
                self._flush_stream()

        return sink

    def _flush_stream(self) -> None:
        chat = self.chat_screen
        if chat is None or not self._stream_buf:
            return
        chat.stream.update_streaming("".join(self._stream_buf))
        self._last_flush = time.monotonic()

    async def _update_rail(self, result: TurnResult) -> None:
        """Rail: consulted facts (confidence, times cited), signals, budget."""
        chat = self.chat_screen
        if chat is None:
            return
        rail = chat.rail
        facts: list[RailFact] = []
        repo = FactRepository(self._wiring.db)
        for fact_id in result.recalled_fact_ids[:_RAIL_FACT_LIMIT]:
            try:
                fact = await repo.get_fact(fact_id)
            except Exception:
                fact = None
            if fact is not None:
                facts.append(
                    RailFact(
                        fact_id=fact.id,
                        statement=fact.statement,
                        confidence=fact.confidence,
                        cited=fact.use_count,
                    )
                )
        rail.set_consulted(facts)
        rail.set_captured(list(result.signal_ids))
        rail.set_budget(result.bundle_tokens, result.token_ceiling)

    def _report_error(self, exc: Exception) -> None:
        """Inline microcopy for any turn failure — never a traceback."""
        kind = classify(exc)
        self.system_line(microcopy(kind, exc))
        self.journal.log(
            "chat.turn_error", kind=kind.value, error=f"{type(exc).__name__}: {exc}"
        )
        if kind is ErrorKind.MODEL_RETRYABLE:
            # The breaker counts failures inside the seam; mirror its state.
            self.system_line(
                "model failures so far: "
                f"{self._wiring.resilient.consecutive_failures} (circuit opens at "
                "3) — /doctor resets it"
            )

    def _announce(self, title: str, message: str) -> None:
        """Turn-level notification (completion, not per token)."""
        self.notify(message, title=title, timeout=3, severity="information")

    # -- system lines and drafts ------------------------------------------------

    def system_line(self, text: str) -> None:
        """One dim note in the stream (recalls, denials, journal notes)."""
        chat = self.chat_screen
        if chat is not None:
            chat.stream.system_line(sanitize(text))

    def note_draft(self, text: str) -> None:
        """Debounced (250 ms) draft persistence — input survives a crash."""
        self._pending_draft = text
        if self._draft_timer is not None:
            self._draft_timer.stop()
        self._draft_timer = self.set_timer(0.25, self._save_draft_now)

    def _draft_path(self) -> Path | None:
        home = self._wiring.home
        if home is None:
            return None
        return home / "ui" / "chat-draft.json"

    def _save_draft_now(self) -> None:
        path = self._draft_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_id": str(self._session_id) if self._session_id else None,
                "draft": self._pending_draft,
                "saved_at": datetime.now(UTC).isoformat(),
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            self.journal.log("ui.draft_save_failed")

    def _restore_draft(self) -> None:
        path = self._draft_path()
        chat = self.chat_screen
        if path is None or chat is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        current = str(self._session_id) if self._session_id else ""
        if not payload.get("session_id") or str(payload.get("session_id")) == current:
            draft = payload.get("draft")
            if isinstance(draft, str) and draft:
                chat.prompt.value = draft

    # -- slash commands -----------------------------------------------------------

    async def _dispatch_slash(self, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        self.journal.log("ui.slash", command=command, argument=argument)
        if command == "/new":
            await self.new_session()
        elif command == "/sessions":
            self.action_show_sessions()
        elif command == "/memory":
            self.action_show_memory()
        elif command == "/ledger":
            self.action_show_ledger()
        elif command == "/policy":
            self.action_show_policy()
        elif command == "/console":
            self.action_show_console()
        elif command == "/context":
            await self.show_context()
        elif command == "/compact":
            self.toggle_compact()
        elif command == "/fork":
            self.system_line(
                "fork — not yet in this build: the chat service has no fork API "
                "(sessions are ledger lineages; forking needs a lineage-copy "
                "primitive). Next: /new starts a fresh session."
            )
        elif command == "/export":
            await self.export_transcript(argument or None)
        elif command == "/model":
            await self.model_command(argument)
        elif command == "/doctor":
            await self.show_doctor()
        elif command == "/help":
            lines = [
                f"{name}  —  {help_text}" for name, help_text in SLASH_COMMANDS.items()
            ]
            self.system_line("commands:\n" + "\n".join(lines))
        elif command == "/quit":
            self.exit()
        else:
            self.system_line(
                f"unknown command '{command}' — not in the slash set. Type /help "
                "to list what this build supports."
            )

    def toggle_compact(self) -> None:
        """/compact — density toggle for power users."""
        chat = self.chat_screen
        if chat is None:
            return
        compact = not chat.stream.compact
        chat.stream.set_compact(compact)
        self.system_line(f"{'compact' if compact else 'comfortable'} density")

    async def show_context(self) -> None:
        """/context — the assembled packet, verbatim (trust through observability).

        The service builds the packet per turn inside the loop and does not
        return it; what is shown here is the packet *reconstructed* from the
        ledger's record of the last completed turn (its recalled fact ids, the
        cold lake's turns, the same persona and ceiling) with the same pure
        bundle builder. The label says exactly that.
        """
        settings = self._wiring.settings
        repo = FactRepository(self._wiring.db)
        result = self._last_result
        facts: list[dict[str, Any]] = []
        if result is not None:
            for fact_id in result.recalled_fact_ids:
                try:
                    fact = await repo.get_fact(fact_id)
                except Exception:
                    fact = None
                if fact is not None:
                    facts.append(
                        {
                            "id": fact.id,
                            "statement": fact.statement,
                            "confidence": fact.confidence,
                            "importance": fact.importance,
                            "memory_domain": fact.domain,
                        }
                    )
        turns = await self._recent_turns(settings.last_k_turns)
        bundle = build_context_bundle(
            persona=settings.persona,
            recalled_facts=facts,
            recent_turns=turns,
            token_ceiling=result.token_ceiling if result is not None else None,
        )
        provenance_lines = [f"facts packed: {len(bundle.provenance)}"]
        for item in bundle.provenance:
            provenance_lines.append(
                f" ▸ {item.fact_id[:12]}  conf {item.confidence:.2f}  {item.statement[:60]}"
            )
        provenance_lines.append(f"history lines packed: {len(bundle.turns)}")
        label = (
            "reconstructed with the same bundle builder from the ledger's record "
            "of the last completed turn"
            if result is not None
            else "no completed turn yet — this is the packet skeleton the next turn would pack"
        )
        self.push_screen(
            ContextModal(
                label=label,
                packet_json=bundle.rendered_json(),
                provenance="\n".join(provenance_lines),
            )
        )

    async def _recent_turns(self, k: int) -> list[dict[str, str]]:
        """This session's last-k turns from the cold lake (oldest first)."""
        signals = self._wiring.signals
        if signals is None or self._session_id is None or k <= 0:
            return []
        wanted = str(self._session_id)
        found: list[dict[str, str]] = []
        try:
            async for signal in signals.iter_by_channel(
                CHAT_CHANNEL, newest_first=True, limit=k * 2
            ):
                try:
                    payload: Any = json.loads(signal.raw_payload)
                except ValueError:
                    continue
                if not isinstance(payload, dict) or payload.get("session_id") != wanted:
                    continue
                role = payload.get("role")
                text = payload.get("text")
                if isinstance(text, str) and role in (ROLE_USER, ROLE_ASSISTANT):
                    found.append({"role": str(role), "text": text})
                if len(found) >= k:
                    break
        except Exception as exc:
            self.journal.log("chat.recent_turns_failed", error=str(exc))
            return []
        found.reverse()
        return found

    async def export_transcript(self, path_arg: str | None) -> None:
        """/export — the transcript as markdown; the stream is the source."""
        chat = self.chat_screen
        if chat is None or self._session_id is None:
            return
        home = self._wiring.home
        try:
            if path_arg:
                path = Path(path_arg).expanduser()
                path.parent.mkdir(parents=True, exist_ok=True)
            elif home is not None:
                exports = home / "exports"
                exports.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                path = exports / f"chat-{str(self._session_id)[:8]}-{stamp}.md"
            else:
                self.system_line(
                    "export failed — no PAA_HOME and no path given. Next: "
                    "/export <path>"
                )
                return
            lines = [f"# paa chat transcript — session {self._session_id}", ""]
            entries = chat.stream.messages()
            for role, text in entries:
                if role == "system":
                    lines.append(f"*{text}*")
                elif role == ROLE_USER:
                    lines.append(f"**user:** {text}")
                else:
                    lines.append(f"**assistant:** {text}")
                lines.append("")
            path.write_text("\n".join(lines), encoding="utf-8")
            self.system_line(f"exported {len(entries)} entries → {path}")
            self.journal.log("ui.export", path=str(path))
        except OSError as exc:
            self.system_line(microcopy(ErrorKind.STORAGE, exc))

    async def model_command(self, argument: str) -> None:
        """/model — show the model (echo first) or set it for subsequent turns."""
        if not argument:
            note = (
                "  (offline, deterministic)"
                if self._wiring.settings.model == "echo"
                else ""
            )
            self.system_line(
                f"model: {self._wiring.settings.model}{note} — set with /model "
                "<name> (e.g. /model echo); the change applies from the next "
                "turn, session and memory unchanged"
            )
            return
        try:
            build_chat_model(self._wiring.settings.model_copy(update={"model": argument}))
        except (UnknownProviderError, ValueError) as exc:
            self.system_line(microcopy(ErrorKind.MODEL_PERMANENT, exc))
            return
        try:
            self._wiring = self._wiring.with_model(argument)
        except Exception as exc:
            self.system_line(microcopy(ErrorKind.MODEL_PERMANENT, exc))
            return
        self._wiring.resilient.reset()
        self._refresh_header()
        self.system_line(
            f"model set to {argument} — echoed here first; subsequent turns use "
            "it. The session, its facts and the ledger are unchanged."
        )
        self.journal.log("ui.model_set", model=argument)

    async def show_doctor(self) -> None:
        """/doctor — the checks this surface can actually run, honestly labelled."""
        rows: list[tuple[str, str, str]] = [
            ("version", "✓", __version__),
            (
                "home",
                "✓" if self._wiring.home else "△",
                str(self._wiring.home) if self._wiring.home else "unset — logs ring-only",
            ),
            ("policy mode", "✓", self.policy_mode),
        ]
        try:
            build_chat_model(self._wiring.settings)
            rows.append(
                ("model", "✓", f"{self._wiring.settings.model} resolves on the chat seam")
            )
        except (UnknownProviderError, ValueError) as exc:
            rows.append(("model", "✗", str(exc)))
        try:
            count = await self._wiring.ledger.count()
            rows.append(("ledger", "✓", f"reachable — {count} events"))
        except Exception as exc:
            rows.append(("ledger", "✗", f"unreachable — {exc}"))
        if self._wiring.signals is None:
            rows.append(
                ("cold lake", "△", "degraded — episodic recall off, turns still work")
            )
        else:
            rows.append(("cold lake", "✓", "signal repository available"))
        rows.append(("tui extra", "✓", "textual installed (the chat TUI is running)"))
        rows.append(
            ("full paa doctor", "△", "not in this build — lands with the CLI doctor task")
        )
        self.push_screen(DoctorModal(rows))
        self._wiring.resilient.reset()

    # -- read-only screen data ----------------------------------------------------

    async def session_events(self) -> list[LedgerEvent]:
        """The session lineage, in ledger order (the Ledger screen's source)."""
        return await self._wiring.ledger.read_correlation(self.session_id)

    async def verify_chain(self) -> tuple[bool, list[str]]:
        """The cheap local hash-chain check (the badge's only source)."""
        return await self._wiring.ledger.verify_chain(self.session_id)

    async def denial_log(self) -> list[LedgerEvent]:
        """Refusals on this session's lineage: recall fences + failed turns."""
        events = await self.session_events()
        denied: list[LedgerEvent] = []
        for event in events:
            if event.event_type is EventType.MEMORY_RECALL_CONSULTED:
                if event.payload.get("refused") is True:
                    denied.append(event)
            elif event.event_type is EventType.CHAT_TURN_FAILED:
                denied.append(event)
        return denied

    @staticmethod
    def denial_reason(event: LedgerEvent) -> str:
        """The rule that fired, from the payload (what + why is the payload's job)."""
        payload = event.payload
        reason = payload.get("refusal_reason") or payload.get("reason") or "policy refusal"
        return f"{event.event_type.value}: {reason}"

    async def memory_snapshot(self) -> tuple[str, str, str, list[Fact]]:
        """KPI strip, funnel, curator line, and the fact list — real numbers only.

        The query set lives on the wiring (one computation for both surfaces);
        the screen only formats what it shows.
        """
        return await self._wiring.memory_snapshot()

    async def open_fact_detail(self, fact: Fact) -> None:
        """The fact detail modal: fields + everything the substrate exposes."""
        repo = FactRepository(self._wiring.db)
        body_lines = [
            f"statement: {fact.statement}",
            f"confidence: {fact.confidence:.3f} (initial {fact.initial_confidence:.3f})",
            f"importance: {fact.importance:.2f} · domain: {fact.domain} · "
            f"cited: {fact.use_count}\u00d7",
            f"created: {fact.created_at:%Y-%m-%d %H:%M} · last served: "
            f"{fact.last_queried_at:%Y-%m-%d %H:%M}",
        ]
        if fact.source_signal_id:
            body_lines.append(f"source signal: {fact.source_signal_id}")
        lineage_lines: list[str] = []
        try:
            chain = await repo.supersession_chain(fact.id)
        except Exception:
            chain = []
        if chain:
            lineage_lines.append("supersession chain (oldest first):")
            lineage_lines.extend(f" ← {fid}" for fid in chain[:-1])
        if fact.source_signal_id:
            try:
                siblings = await repo.facts_by_source_signal(fact.source_signal_id)
                if siblings:
                    lineage_lines.append("facts from the same signal:")
                    lineage_lines.extend(
                        f" · {s.id[:12]}  {s.statement[:60]}" for s in siblings
                    )
            except Exception as exc:
                log.warning("chat_tui.sibling_facts_unavailable", error=str(exc))
        lineage_lines.append("ledger rows naming this fact:")
        try:
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            rows = await self._wiring.ledger.events_since(
                epoch,
                event_types=[
                    EventType.MEMORY_SIGNAL_EXTRACTED,
                    EventType.CHAT_TURN_COMPLETED,
                    EventType.TIER_DEMOTED,
                    EventType.TIER_ARCHIVED,
                ],
                limit=10_000,
            )
            named = 0
            for row in rows:
                if fact.id in self._event_fact_ids(row.payload):
                    lineage_lines.append(
                        f" · {row.event_type.value} v{row.state_version} "
                        f"(session {str(row.correlation_id)[:8]})"
                    )
                    named += 1
            if named == 0:
                lineage_lines.append(" · none in the scanned window (10k rows)")
        except Exception as exc:
            lineage_lines.append(f" · ledger scan failed: {exc}")
        self.push_screen(
            FactDetailModal(
                title=f"fact {fact.id[:12]}",
                body="\n".join(body_lines),
                lineage="\n".join(lineage_lines),
            )
        )

    @staticmethod
    def _event_fact_ids(payload: dict[str, Any]) -> set[str]:
        """Every fact id a payload names (scalar + list forms)."""
        ids: set[str] = set()
        scalar = payload.get("fact_id")
        if isinstance(scalar, str):
            ids.add(scalar)
        for key in ("facts_written", "fact_ids"):
            value = payload.get(key)
            if isinstance(value, list):
                ids.update(str(item) for item in value if isinstance(item, str))
        return ids

    # -- bound actions -------------------------------------------------------------

    def action_toggle_rail(self) -> None:
        chat = self.chat_screen
        if chat is None:
            return
        rail = chat.query_one("#rail", MemoryRail)
        rail.display = not rail.display
        self.journal.log("ui.rail", visible=rail.display)

    # -- screen actions --------------------------------------------------------
    #
    # The five key-bound screens carry the ``action_`` prefix their
    # bindings resolve through: a binding to "show_memory" needs an
    # ``action_show_memory`` method, and the bare names C4.4 shipped were
    # unreachable from the keyboard (the slash commands and the palette
    # called the methods directly, so only the keys were dead). Renamed
    # here so Ctrl+M / Ctrl+L / Ctrl+K / Ctrl+O / Ctrl+B all fire.

    def action_show_memory(self) -> None:
        self._show(MemoryScreen())

    def action_show_ledger(self) -> None:
        self._show(LedgerScreen())

    def action_show_policy(self) -> None:
        self._show(PolicyScreen())

    def action_show_console(self) -> None:
        """Ctrl+B / ``/console`` — the Loop Console (C8.5): loops, driven.

        The console is a screen of this app, not a sibling product: the
        same wiring, the same ledger, the same journal. Every mutating
        action it offers rides the surfaces that already decide — the
        five-gate installer, the pipeline registry — so the console adds
        a view, never an authority.
        """
        self._show(ConsoleScreen())

    def action_show_sessions(self) -> None:
        self._show(SessionsScreen())

    def show_log_drawer(self) -> None:
        self.push_screen(LogDrawer())
        rich_log = self.screen.query_one("#log-drawer", RichLog)
        for row in self.journal.ring:
            fields = " ".join(
                f"{key}={value}"
                for key, value in row.items()
                if key not in ("ts", "event")
            )
            rich_log.write(f"{row['ts']}  {row['event']}  {fields}")

    def action_log_drawer(self) -> None:
        self.show_log_drawer()

    def action_toggle_theme(self) -> None:
        self.theme = HC_THEME_NAME if self.theme == CHAT_THEME_NAME else CHAT_THEME_NAME

    def action_escape(self) -> None:
        """Esc: stop the streaming turn, else climb back toward the chat."""
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            return
        if isinstance(self.screen, ModalScreen):
            self.pop_screen()
        else:
            self.back_to_chat()

    async def action_quit(self) -> None:
        """Quit (the base signature is async in Textual 8; ``self.exit`` is not)."""
        self.exit()
