"""The five chat screens and their modals.

Layout grammar (spec §3.2): one header bar, body, two-row footer — no boxes
in boxes, 1 px rules only, whitespace separates. Every screen is reachable
from the keyboard (bindings on the app and on the screen), shows a visible
focus target, and closes with Escape back to the chat.

Honest scoping, per the task: **pending clearances are not shown** on the
Policy screen — the chat service exposes grants (the facade's minted
capability set) and the ledger's denial rows, but no clearance queue. The
screen says so in place of the missing section rather than inventing one.

Data-derived strings reach Statics as :class:`rich.text.Text` objects, never
markup strings: a fact statement containing ``[something]`` must render as
literal text, not raise ``MarkupError`` mid-session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, ListView, RichLog, Static
from textual.widgets.data_table import CellDoesNotExist

from paa.chat.loop import CHAT_CAPABILITIES
from paa.tui.chat.widgets import (
    ChatHeader,
    MemoryRail,
    MessageStream,
    SessionCard,
)

if TYPE_CHECKING:
    from paa.chat.session import ChatSessionRecord
    from paa.ledger.events import LedgerEvent
    from paa.memory.facts import Fact
    from paa.tui.chat.app import ChatApp

__all__ = [
    "ChatScreen",
    "ContextModal",
    "DoctorModal",
    "FactDetailModal",
    "LedgerScreen",
    "LogDrawer",
    "MemoryScreen",
    "PolicyScreen",
    "SessionsScreen",
]

#: Keybindings shown in the footer of every non-chat screen.
_BACK_BINDING: Final[Binding] = Binding(
    "escape", "app.back_to_chat", "back to chat", priority=True
)


def _chat_app(screen: Screen[None]) -> ChatApp:
    """The screen's app, narrowed to :class:`ChatApp` (the only host here)."""
    return cast("ChatApp", screen.app)


class ChatScreen(Screen[None]):
    """The default screen: message stream + memory rail + input row."""

    SCREEN_NAME: Final[str] = "chat"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+t", "app.toggle_rail", "memory rail"),
        Binding("escape", "app.escape", "stop turn", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield ChatHeader(id="chat-header")
        with Horizontal(id="chat-body"):
            yield MessageStream(id="msg-stream")
            yield MemoryRail(id="rail")
        yield Input(id="prompt", placeholder="message — or / for commands")
        yield Footer(show_command_palette=True)

    @property
    def stream(self) -> MessageStream:
        return self.query_one("#msg-stream", MessageStream)

    @property
    def rail(self) -> MemoryRail:
        return self.query_one("#rail", MemoryRail)

    @property
    def header(self) -> ChatHeader:
        return self.query_one("#chat-header", ChatHeader)

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)

    def on_mount(self) -> None:
        """Kick the session setup (create/resume + transcript) as a worker.

        The turn path refuses to send until the app has a session id, so this
        hook is the difference between a chat surface and an empty window —
        :meth:`ChatApp.setup_session` documents it as its only trigger.
        """
        self.run_worker(_chat_app(self).setup_session(), exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter on the prompt: slash command or one chat turn."""
        event.stop()
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.run_worker(self._submit(text), exclusive=False)

    async def _submit(self, text: str) -> None:
        await _chat_app(self).handle_input(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Debounced draft journaling (spec §3.2 crash-safety, 250 ms)."""
        _chat_app(self).note_draft(event.value)


class _PaaModal(ModalScreen[None]):
    """Shared modal chrome: Escape closes, one scrollable body, no boxes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", priority=True),
        Binding("ctrl+q", "close", "close", show=False),
    ]

    def action_close(self) -> None:
        self.dismiss()


class ContextModal(_PaaModal):
    """``/context`` — the assembled packet, verbatim (trust through observability)."""

    SCREEN_NAME: Final[str] = "context"

    def __init__(self, *, label: str, packet_json: str, provenance: str) -> None:
        super().__init__()
        self._label = label
        self._packet_json = packet_json
        self._provenance = provenance

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(Text("CONTEXT PACKET — verbatim", style="bold"), id="context-title"),
            Static(Text(self._label), id="context-label"),
            Static(Text(self._packet_json), id="context-json"),
            Static(Text(self._provenance), id="context-provenance"),
            id="context-body",
        )


class FactDetailModal(_PaaModal):
    """One fact with everything the substrate exposes about its lineage."""

    SCREEN_NAME: Final[str] = "fact-detail"

    def __init__(self, *, title: str, body: str, lineage: str) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._lineage = lineage

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(Text(self._title, style="bold"), id="fact-title"),
            Static(Text(self._body), id="fact-body"),
            Static(Text(self._lineage), id="fact-lineage"),
            id="fact-detail-body",
        )


class LogDrawer(_PaaModal):
    """F12 — the structured event drawer (the journal's ring, oldest first)."""

    SCREEN_NAME: Final[str] = "logs"

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-drawer", wrap=True, markup=False, highlight=False)
        yield Footer()


class DoctorModal(_PaaModal):
    """``/doctor`` — the checks this surface can actually run, honestly labelled."""

    SCREEN_NAME: Final[str] = "doctor"

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        lines = ["DOCTOR — chat surface checks", ""]
        for name, glyph, detail in self._rows:
            lines.append(f" {glyph} {name:<16} {detail}")
        yield VerticalScroll(
            Static(Text("\n".join(lines)), id="doctor-body"),
            id="doctor-scroll",
        )


class SessionsScreen(Screen[None]):
    """``/sessions`` — session cards: fence badge, turns, tokens, started."""

    SCREEN_NAME: Final[str] = "sessions"

    BINDINGS: ClassVar[list[BindingType]] = [
        _BACK_BINDING,
        Binding("enter", "switch", "switch / resume"),
        Binding("a", "archive", "archive"),
        Binding("f", "fork", "fork"),
        Binding("r", "refresh", "refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            Text("SESSIONS — enter: switch · a: archive · f: fork"), id="sessions-title"
        )
        yield ListView(id="session-list")
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_sessions()
        self.query_one("#session-list", ListView).focus()

    async def refresh_sessions(self) -> None:
        """Fold every session from the ledger and render one card each."""
        listing = self.query_one("#session-list", ListView)
        await listing.clear()
        records = await _chat_app(self).service.list_sessions()
        for record in records:
            await listing.append(SessionCard(record))

    async def action_refresh(self) -> None:
        await self.refresh_sessions()

    async def action_switch(self) -> None:
        """Switch (and resume — resume is a re-read) to the highlighted session."""
        card = self.query_one("#session-list", ListView).highlighted_child
        if isinstance(card, SessionCard):
            await _chat_app(self).switch_session(card.record.session_id)
            _chat_app(self).back_to_chat()

    async def action_archive(self) -> None:
        card = self.query_one("#session-list", ListView).highlighted_child
        if isinstance(card, SessionCard):
            await _chat_app(self).archive_session(card.record)

    async def action_fork(self) -> None:
        _chat_app(self).system_line(
            "fork — not yet in this build: the chat service has no fork API "
            "(sessions are ledger lineages; forking needs a lineage-copy "
            "primitive). Next: /new starts a fresh session."
        )


class MemoryScreen(Screen[None]):
    """``/memory`` — the memory dashboard: KPIs, sortable facts, funnel."""

    SCREEN_NAME: Final[str] = "memory"

    #: Sort cycle (the `s` key), mapped onto the table's column keys.
    SORT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
        ("confidence", "conf"),
        ("last-served", "served"),
        ("cited", "cited"),
    )

    BINDINGS: ClassVar[list[BindingType]] = [
        _BACK_BINDING,
        Binding("s", "cycle_sort", "sort"),
        Binding("enter", "detail", "fact detail"),
        Binding("r", "refresh", "refresh", show=False),
    ]

    def __init__(self) -> None:
        # Explicit args (no **kwargs pass-through): Screen's __init__ takes
        # (name, id, classes) and this screen is always constructed bare,
        # so the constructor states exactly what it accepts.
        super().__init__()
        self._sort_index: int = 0
        self._facts: dict[str, Fact] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="memory-kpis")
        yield Static("", id="memory-funnel")
        yield Static("", id="memory-curator")
        yield DataTable(id="fact-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#fact-table", DataTable)
        for label, key in (
            ("fact", "fact"),
            ("statement", "statement"),
            ("conf", "conf"),
            ("cited", "cited"),
            ("last-served", "served"),
            ("domain", "domain"),
        ):
            table.add_column(label, key=key)
        await self.refresh_memory()

    async def action_refresh(self) -> None:
        await self.refresh_memory()

    async def refresh_memory(self) -> None:
        """Pull the real numbers from the substrate — never invented ones."""
        table = self.query_one("#fact-table", DataTable)
        kpis, funnel, curator, facts = await _chat_app(self).memory_snapshot()
        self.query_one("#memory-kpis", Static).update(kpis)
        self.query_one("#memory-funnel", Static).update(funnel)
        self.query_one("#memory-curator", Static).update(curator)
        table.clear()
        self._facts.clear()
        for fact in facts:
            served = (
                fact.last_queried_at.strftime("%m-%d %H:%M")
                if fact.last_queried_at
                else "never"
            )
            table.add_row(
                fact.id[:12],
                Text(fact.statement),
                f"{fact.confidence:.2f}",
                str(fact.use_count),
                served,
                fact.domain,
                key=fact.id,
            )
            self._facts[fact.id] = fact
        self._apply_sort()

    def _apply_sort(self) -> None:
        _, key = self.SORT_COLUMNS[self._sort_index % len(self.SORT_COLUMNS)]
        self.query_one("#fact-table", DataTable).sort(key, reverse=True)

    async def action_cycle_sort(self) -> None:
        self._sort_index = (self._sort_index + 1) % len(self.SORT_COLUMNS)
        self._apply_sort()
        label, _ = self.SORT_COLUMNS[self._sort_index % len(self.SORT_COLUMNS)]
        _chat_app(self).system_line(f"memory table sorted by {label}")

    async def action_detail(self) -> None:
        """Open the highlighted fact; Enter on an empty table is a no-op."""
        table = self.query_one("#fact-table", DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except CellDoesNotExist:
            # No row under the cursor (empty table, header row) — nothing to
            # open, and a keyboard dead-end must not surface as an error.
            return
        fact = self._facts.get(str(row_key))
        if fact is not None:
            await _chat_app(self).open_fact_detail(fact)


class LedgerScreen(Screen[None]):
    """Ctrl+L — this session's event chain, filterable, with a verify badge."""

    SCREEN_NAME: Final[str] = "ledger"

    BINDINGS: ClassVar[list[BindingType]] = [
        _BACK_BINDING,
        Binding("r", "refresh", "refresh", show=False),
        Binding("f", "focus_filter", "filter", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="chain-badge")
        yield Input(id="ledger-filter", placeholder="filter by event kind (e.g. CHAT_TURN)")
        yield DataTable(id="ledger-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#ledger-table", DataTable)
        for label, key in (
            ("v", "v"),
            ("event", "event"),
            ("discriminator", "discriminator"),
            ("when (UTC)", "when"),
            ("digest", "digest"),
        ):
            table.add_column(label, key=key)
        await self.refresh_ledger()
        self.query_one("#ledger-filter", Input).focus()

    async def refresh_ledger(self) -> None:
        """Read the lineage, verify the hash chain, fill the table."""
        app = _chat_app(self)
        events: list[LedgerEvent] = await app.session_events()
        ok, problems = await app.verify_chain()
        badge = self.query_one("#chain-badge", Static)
        if ok:
            badge.update(Text(f"✓ chain verified — {len(events)} events", style="bold"))
        else:
            first = problems[0] if problems else "?"
            badge.update(
                Text(f"✗ chain BROKEN — {len(problems)} problem(s); first: {first}", style="bold")
            )
        needle = self.query_one("#ledger-filter", Input).value.strip().upper()
        table = self.query_one("#ledger-table", DataTable)
        table.clear()
        for event in events:
            kind = event.event_type.value
            if needle and needle not in kind:
                continue
            table.add_row(
                str(event.state_version),
                Text(kind),
                Text((event.discriminator or "")[:24]),
                event.recorded_at.strftime("%H:%M:%S"),
                (event.event_hash or "")[:8],
            )

    async def action_refresh(self) -> None:
        await self.refresh_ledger()

    async def action_focus_filter(self) -> None:
        self.query_one("#ledger-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live filter: re-run the (cheap, local) fill on each keystroke."""
        if event.input.id == "ledger-filter":
            self.run_worker(self.refresh_ledger(), exclusive=True)


class PolicyScreen(Screen[None]):
    """Ctrl+K — grants in effect for this session, and the denial log."""

    SCREEN_NAME: Final[str] = "policy"

    BINDINGS: ClassVar[list[BindingType]] = [
        _BACK_BINDING,
        Binding("r", "refresh", "refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="policy-grants")
        yield Static("", id="policy-denials-title")
        yield DataTable(id="denial-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="policy-clearance-note")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#denial-table", DataTable)
        for label, key in (("when (UTC)", "when"), ("rule / reason", "rule")):
            table.add_column(label, key=key)
        await self.refresh_policy()

    async def refresh_policy(self) -> None:
        app = _chat_app(self)
        record: ChatSessionRecord | None = await app.service.session_record(
            app.session_id
        )
        grants = ", ".join(sorted(capability.value for capability in CHAT_CAPABILITIES))
        fence = "isolated" if app.isolated else "cross-session episodic ON"
        mandate = (
            record.mandate_ref
            if record and record.mandate_ref
            else "implicit (none recorded)"
        )
        scope = record.scope if record else "personal"
        self.query_one("#policy-grants", Static).update(
            Text(
                "GRANTS IN EFFECT\n"
                f" capabilities: {grants}  (minted per turn by the chat host, ADR-0051)\n"
                f" permission mode: {app.policy_mode} · session fence: {fence}\n"
                f" scope: {scope} · mandate: {mandate}",
            )
        )
        self.query_one("#policy-denials-title", Static).update(
            Text("DENIAL LOG — the rule that fired, from the ledger", style="bold")
        )
        table = self.query_one("#denial-table", DataTable)
        table.clear()
        denials: list[LedgerEvent] = await app.denial_log()
        if not denials:
            table.add_row("—", Text("no denials recorded for this session"))
        for event in denials:
            table.add_row(
                event.recorded_at.strftime("%m-%d %H:%M:%S"),
                Text(app.denial_reason(event)),
            )
        self.query_one("#policy-clearance-note", Static).update(
            Text(
                "pending clearances: not exposed by the chat service in this build — "
                "refusals are ledger rows (above); clearance actions land with the "
                "service work (W4-D)."
            )
        )

    async def action_refresh(self) -> None:
        await self.refresh_policy()
