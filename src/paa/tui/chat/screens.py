"""The chat screens, their modals, and the Loop Console (C8.5).

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
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    ListItem,
    ListView,
    RichLog,
    Static,
)
from textual.widgets.data_table import CellDoesNotExist

from paa.chat.loop import CHAT_CAPABILITIES
from paa.core.errors import PaaError
from paa.pipelines.conformance import ConformanceReport, render_reports
from paa.pipelines.registry import RegisteredPipeline
from paa.tui.chat.console import PERSONAL_SOURCE_WORD, ConsoleService
from paa.tui.chat.widgets import (
    ChatHeader,
    MemoryRail,
    MessageStream,
    SessionCard,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from paa.chat.extensions import DiscoveredSkill
    from paa.chat.session import ChatSessionRecord
    from paa.ledger.events import LedgerEvent
    from paa.memory.facts import Fact
    from paa.tui.chat.app import ChatApp

__all__ = [
    "ChatScreen",
    "ConformanceModal",
    "ConsoleInspectModal",
    "ConsoleScreen",
    "ContextModal",
    "DoctorModal",
    "FactDetailModal",
    "ForkNameModal",
    "InstallSourceModal",
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
        fact = self._facts.get(row_key.value) if row_key.value else None
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


# ---------------------------------------------------------------------------
# The Loop Console (C8.5, SPEC 2 Wave 8) — a view, not an authority
# ---------------------------------------------------------------------------


def _declaration_text(row: RegisteredPipeline) -> str:
    """One pipeline's declaration as text — the facts ``paa pipeline inspect`` prints."""
    d = row.declaration
    lines = [
        f"{d.name} {d.version}",
        f" trust: {row.trust} · source: {row.source}",
        f" runtime {d.requires_runtime} · runnable: "
        f"{'yes' if row.runnable else 'no (P3 — listed, inspected, not executed)'}"
        f" · resume: {d.resume}",
        f" evidence tier: {d.evidence_tier}",
    ]
    if d.description:
        lines.append(f" {d.description}")
    if d.capabilities:
        lines.append("capabilities (the ask):")
        lines.extend(f"  {key} = {value!r}" for key, value in d.capabilities.items())
    if d.params:
        lines.append("params:")
        for key, spec in d.params.items():
            low = "" if spec.minimum is None else str(spec.minimum)
            high = "" if spec.maximum is None else str(spec.maximum)
            bounds = f"{low}..{high}" if (low or high) else "any"
            lines.append(f"  {key}: {spec.type} = {spec.default!r} ({bounds})")
    if d.slots:
        lines.append("slots:")
        lines.extend(
            f"  {slot.name}: {slot.interface} = {slot.default}"
            for slot in d.slots.values()
        )
    lines.append(
        "limits: "
        + ", ".join(
            f"{key}={getattr(d.limits, key)}"
            for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs")
        )
    )
    return "\n".join(lines)


class ConsoleInspectModal(_PaaModal):
    """One pipeline's declaration — the registry's own row, read out."""

    SCREEN_NAME: Final[str] = "console-inspect"

    def __init__(self, *, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(Text("PIPELINE DECLARATION", style="bold"), id="inspect-title"),
            Static(Text(self._body), id="inspect-body"),
            id="inspect-scroll",
        )


class ConformanceModal(_PaaModal):
    """``c`` — the six-check standard's own one-page report, verbatim.

    The page is :func:`~paa.pipelines.conformance.render_reports` over the
    report the console's run produced — the same renderer the conformance
    suite asserts on, so the console cannot show a verdict the standard
    would phrase differently.
    """

    SCREEN_NAME: Final[str] = "conformance"

    def __init__(self, *, pipeline: str, report: ConformanceReport) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._report = report

    def compose(self) -> ComposeResult:
        verdict = "GREEN" if self._report.passed else "RED"
        yield VerticalScroll(
            Static(
                Text(f"CONFORMANCE — {self._pipeline}: {verdict}", style="bold"),
                id="conf-title",
            ),
            Static(Text(render_reports([self._report])), id="conf-page"),
            id="conf-scroll",
        )


class ForkNameModal(_PaaModal):
    """One Input: the new name for a pipeline's personal fork.

    The callback receives the modal itself, so the screen can keep the
    modal open and show a named refusal in it when the fork name is
    taken, empty or shadowing — the operator fixes the name in place
    rather than starting the flow over.
    """

    SCREEN_NAME: Final[str] = "fork-name"

    def __init__(
        self,
        *,
        pipeline: str,
        on_submit: Callable[[ForkNameModal, str], None],
    ) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._on_submit = on_submit

    @property
    def pipeline(self) -> str:
        return self._pipeline

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(Text(f"FORK {self._pipeline}", style="bold"), id="fork-title"),
            Static(
                Text(
                    "a fork is a personal overlay seeded from this pipeline's own "
                    "declaration — every value restated, any widening edit refused "
                    "at load. Run it with: paa pipeline run "
                    f"{self._pipeline} --overlay <path>"
                ),
                id="fork-help",
            ),
            Input(id="fork-name", placeholder="fork name — a new name for your copy"),
            id="fork-scroll",
        )

    def on_mount(self) -> None:
        self.query_one("#fork-name", Input).focus()

    def note(self, text: str) -> None:
        """Show a refusal (or a hint) without closing the modal."""
        self.query_one("#fork-help", Static).update(Text(text))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "fork-name":
            event.stop()
            self._on_submit(self, event.value)


class InstallSourceModal(_PaaModal):
    """The install flow: name a source, pick a skill, Enter installs it.

    Two stages in one modal, because the flow has two operator decisions:
    the source (a Claw-Hub/OpenClaw-shaped directory, or the word
    ``personal`` for the personal skills dir), then the skill. The second
    stage's help line carries the opt-in wording — local skills are
    unsigned, and Enter is the operator's word the signature gate needs
    (the same opt-in ``paa skills install --allow-unverified`` asks for).
    """

    SCREEN_NAME: Final[str] = "install-source"

    def __init__(
        self,
        *,
        on_source: Callable[[InstallSourceModal, str], None],
        on_install: Callable[[InstallSourceModal, int], None],
    ) -> None:
        super().__init__()
        self._on_source = on_source
        self._on_install = on_install

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(Text("INSTALL A SKILL", style="bold"), id="install-title"),
            Static(
                Text(
                    "type a source directory (the layout 'claw pull' produces), "
                    f"or '{PERSONAL_SOURCE_WORD}' for the personal skills dir"
                ),
                id="install-help",
            ),
            Input(id="install-source", placeholder="source directory — or personal"),
            ListView(id="install-found"),
            id="install-scroll",
        )

    def on_mount(self) -> None:
        self.query_one("#install-source", Input).focus()

    def note(self, text: str) -> None:
        """Show a refusal (or a hint) without closing the modal."""
        self.query_one("#install-help", Static).update(Text(text))

    async def show_results(self, labels: list[str]) -> None:
        """Fill the found-skills list and hand it the focus."""
        listing = self.query_one("#install-found", ListView)
        await listing.clear()
        for label in labels:
            await listing.append(_InstallCandidate(label))
        if labels:
            # Highlight the first candidate: a ListView whose index is
            # ``None`` swallows Enter (nothing highlighted, nothing
            # selected), and the flow's promise is Enter-installs.
            listing.index = 0
        listing.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "install-source":
            event.stop()
            self._on_source(self, event.value.strip())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self._on_install(self, event.index)


class _InstallCandidate(ListItem):
    """One discovered skill in the install modal's list.

    A ``ListItem`` subclass so the modal can style its rows without
    reaching into the item's children, and so a hostile skill
    description renders as literal text (the label is a rich ``Text``).
    """

    def __init__(self, label: str) -> None:
        super().__init__(Static(Text(label)))


class ConsoleScreen(Screen[None]):
    """Ctrl+B — the Loop Console: loops as the registry sees them, driven.

    A view plus a thin dispatcher, per the row's own rule: the panel reads
    the pipeline registry and the skills/marketplace rows (never a
    parallel list), and every mutating action calls
    :class:`~paa.tui.chat.console.ConsoleService`, which drives the C8.4
    extension surface, the five-gate installer and the conformance
    runners — the same doors the CLI verbs open. Refusals render as
    refusals; nothing here retries around a gate.
    """

    SCREEN_NAME: Final[str] = "console"

    BINDINGS: ClassVar[list[BindingType]] = [
        _BACK_BINDING,
        Binding("r", "refresh", "refresh", show=False),
        Binding("i", "inspect", "inspect"),
        Binding("c", "conformance", "conformance"),
        Binding("f", "fork", "fork"),
        Binding("d", "discover", "install"),
        Binding("a", "activate", "activate"),
    ]

    def __init__(self) -> None:
        # Explicit constructor (no **kwargs pass-through), the MemoryScreen
        # rule: Screen's __init__ takes (name, id, classes) and this screen
        # is always constructed bare.
        super().__init__()
        self._pipeline_rows: dict[str, RegisteredPipeline] = {}
        self._discovered: list[DiscoveredSkill] = []

    def compose(self) -> ComposeResult:
        yield Static(
            Text(
                "LOOPS — i inspect · c conformance · f fork · d install · "
                "a activate (extensions)",
                style="bold",
            ),
            id="console-title",
        )
        yield DataTable(id="console-pipelines", cursor_type="row", zebra_stripes=True)
        yield Static(Text("INSTALLED EXTENSIONS"), id="console-skills-title")
        yield DataTable(id="console-skills", cursor_type="row", zebra_stripes=True)
        yield Static(Text(""), id="console-forks")
        yield Static(Text(""), id="console-status")
        yield Footer()

    async def on_mount(self) -> None:
        pipelines = self.query_one("#console-pipelines", DataTable)
        for label, key in (
            ("pipeline", "pipeline"),
            ("version", "version"),
            ("trust", "trust"),
            ("evidence", "evidence"),
            ("runnable", "runnable"),
            ("description", "description"),
        ):
            pipelines.add_column(label, key=key)
        skills = self.query_one("#console-skills", DataTable)
        for label, key in (
            ("skill", "skill"),
            ("version", "version"),
            ("provider", "provider"),
            ("signature", "signature"),
            ("state", "state"),
        ):
            skills.add_column(label, key=key)
        await self.refresh_console()
        pipelines.focus()

    @property
    def service(self) -> ConsoleService:
        """The app's console service (built once, from the wiring)."""
        return _chat_app(self).console_service

    async def refresh_console(self) -> None:
        """Pull the real rows from the real registries — never invented ones."""
        service = self.service
        pipelines = self.query_one("#console-pipelines", DataTable)
        pipelines.clear()
        self._pipeline_rows = {}
        for row in service.pipeline_rows():
            pipelines.add_row(
                Text(row.name),
                row.version,
                row.trust,
                f"[{row.declaration.evidence_tier}]",
                "yes" if row.runnable else "no (P3)",
                Text(row.declaration.description),
                key=row.name,
            )
            self._pipeline_rows[row.name] = row
        skills = self.query_one("#console-skills", DataTable)
        skills.clear()
        for skill in await service.skill_rows():
            skills.add_row(
                Text(skill.name),
                skill.version,
                skill.provider,
                skill.signature,
                "live" if skill.active else "staged",
                key=skill.name,
            )
        forks = service.fork_rows()
        lines = ["PIPELINE FORKS — personal overlays (tighten-only, load-checked)"]
        if forks:
            lines.extend(
                f" {fork.name} ← {fork.source} ({fork.note}) — {fork.path}"
                for fork in forks
            )
        else:
            lines.append(
                " none — f on a pipeline seeds one under "
                "PAA_HOME/pipelines/overlays/"
            )
        for name, reason in service.local_manifest_errors().items():
            lines.append(f" ✗ off-contract local manifest: {name} — {reason}")
        self.query_one("#console-forks", Static).update(Text("\n".join(lines)))

    def _status(self, text: str, *, ok: bool = True) -> None:
        """One outcome line — the glyph names the outcome, colour never does."""
        glyph = "✓" if ok else "✗"
        self.query_one("#console-status", Static).update(Text(f"{glyph} {text}"))

    def _skills_table_focused(self) -> bool:
        return self.focused is self.query_one("#console-skills", DataTable)

    def _highlighted_pipeline(self) -> RegisteredPipeline | None:
        table = self.query_one("#console-pipelines", DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except CellDoesNotExist:
            return None
        # ``RowKey`` hashes equal to its string but does not ``str()`` to
        # it (textual 8.x), so the value is read, never stringified.
        return self._pipeline_rows.get(row_key.value) if row_key.value else None

    def _highlighted_skill(self) -> str | None:
        table = self.query_one("#console-skills", DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except CellDoesNotExist:
            return None
        return row_key.value

    async def action_refresh(self) -> None:
        await self.refresh_console()

    # -- inspect ---------------------------------------------------------------

    async def action_inspect(self) -> None:
        row = self._highlighted_pipeline()
        if row is None:
            self._status(
                "select a pipeline first — the table is empty or the cursor is "
                "off its rows",
                ok=False,
            )
            return
        _chat_app(self).journal.log("console.inspect", pipeline=row.name)
        self.app.push_screen(ConsoleInspectModal(body=_declaration_text(row)))

    # -- conformance -------------------------------------------------------------

    async def action_conformance(self) -> None:
        row = self._highlighted_pipeline()
        if row is None:
            self._status("select a pipeline to run conformance for", ok=False)
            return
        self._status(
            f"conformance running for {row.name} — the six checks run on a "
            "scratch ledger; this session's ledger is not written"
        )
        self.run_worker(self._conformance(row.name), exclusive=True)

    async def _conformance(self, pipeline: str) -> None:
        try:
            report = await self.service.run_conformance(pipeline)
        except PaaError as exc:
            self._status(exc.message, ok=False)
            return
        _chat_app(self).journal.log(
            "console.conformance", pipeline=pipeline, green=report.passed
        )
        self.app.push_screen(ConformanceModal(pipeline=pipeline, report=report))

    # -- fork --------------------------------------------------------------------

    async def action_fork(self) -> None:
        if self._skills_table_focused():
            await self._fork_highlighted_skill()
            return
        row = self._highlighted_pipeline()
        if row is None:
            self._status(
                "select a pipeline to fork — or focus the extensions table to "
                "fork a skill",
                ok=False,
            )
            return
        self.app.push_screen(
            ForkNameModal(pipeline=row.name, on_submit=self._fork_requested)
        )

    async def _fork_highlighted_skill(self) -> None:
        name = self._highlighted_skill()
        if name is None:
            self._status("select a skill to fork", ok=False)
            return
        try:
            target = self.service.fork_skill(name)
        except PaaError as exc:
            self._status(exc.message, ok=False)
            return
        _chat_app(self).journal.log("console.fork_skill", skill=name)
        self._status(
            f"forked {name} → {target} — the personal skills dir; install it "
            f"with d → '{PERSONAL_SOURCE_WORD}' (the five gates run again)"
        )

    def _fork_requested(self, modal: ForkNameModal, name: str) -> None:
        self.run_worker(self._fork_pipeline(modal, name), exclusive=True)

    async def _fork_pipeline(self, modal: ForkNameModal, name: str) -> None:
        try:
            path = self.service.fork_pipeline(modal.pipeline, name)
        except PaaError as exc:
            modal.note(exc.message)
            return
        modal.dismiss()
        _chat_app(self).journal.log(
            "console.fork_pipeline", pipeline=modal.pipeline, fork=name
        )
        await self.refresh_console()
        self._status(
            f"forked → {path} — a personal overlay seeded from "
            f"{modal.pipeline}'s declaration; edit it, then run with "
            f"paa pipeline run {modal.pipeline} --overlay {path}"
        )

    # -- install / activate ---------------------------------------------------------

    def action_discover(self) -> None:
        self.app.push_screen(
            InstallSourceModal(
                on_source=self._source_requested, on_install=self._install_requested
            )
        )

    def _source_requested(self, modal: InstallSourceModal, text: str) -> None:
        self.run_worker(self._discover(modal, text), exclusive=True)

    async def _discover(self, modal: InstallSourceModal, text: str) -> None:
        if not text:
            modal.note(
                "a source is needed — a directory path, or "
                f"'{PERSONAL_SOURCE_WORD}'"
            )
            return
        personal = text == PERSONAL_SOURCE_WORD
        try:
            discovered = await self.service.discover(
                None if personal else text, personal=personal
            )
        except PaaError as exc:
            modal.note(exc.message)
            return
        if not discovered:
            where = (
                "the personal skills dir" if personal else f"{text!r}"
            )
            modal.note(f"no skills discovered in {where}")
            return
        self._discovered = list(discovered)
        labels = []
        for skill in discovered:
            description = skill.contract.description.strip()
            first_line = description.splitlines()[0] if description else ""
            labels.append(
                f"{skill.contract.skill_name} v{skill.contract.version} — {first_line}"
            )
        await modal.show_results(labels)
        modal.note(
            "enter installs the highlighted skill through all five gates — "
            "local skills are unsigned; your Enter is the opt-in the "
            "signature gate needs"
        )

    def _install_requested(self, modal: InstallSourceModal, index: int) -> None:
        self.run_worker(self._install(modal, index), exclusive=True)

    async def _install(self, modal: InstallSourceModal, index: int) -> None:
        if not self._discovered:
            return
        skill = self._discovered[index]
        try:
            report = await self.service.install(skill, allow_unverified=True)
        except PaaError as exc:
            # The gates' own refusals arrive as reports (below); this is
            # the flow's own named failure (e.g. PAA_HOME vanished) and
            # renders in place too, never as a crash out of the console.
            modal.note(exc.message)
            return
        _chat_app(self).journal.log(
            "console.install",
            skill=skill.contract.skill_name,
            installed=report.installed,
        )
        modal.dismiss()
        await self.refresh_console()
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
            self._status(
                f"installed {report.skill_name} through the gates ({gates})"
                f"{pipeline_note} — EXTENSION_INSTALLED recorded"
            )
        else:
            gate = (
                report.install.refused_at.value
                if report.install.refused_at is not None
                else "unknown"
            )
            reason = report.install.reason or "no reason given"
            self._status(
                f"refused {report.skill_name} at the {gate} gate: "
                f"{reason} — nothing landed: no staged bytes, "
                "no registry row",
                ok=False,
            )

    async def action_activate(self) -> None:
        if not self._skills_table_focused():
            self._status(
                "activate acts on the extensions table — focus it (or click a "
                "skill row) first",
                ok=False,
            )
            return
        name = self._highlighted_skill()
        if name is None:
            self._status("select a skill to activate", ok=False)
            return
        self.run_worker(self._activate(name), exclusive=True)

    async def _activate(self, name: str) -> None:
        try:
            activation = await self.service.activate(name)
        except PaaError as exc:
            self._status(exc.message, ok=False)
            return
        _chat_app(self).journal.log("console.activate", skill=name)
        await self.refresh_console()
        pipeline_note = (
            f" — coding loop: {activation.pipeline}" if activation.pipeline else ""
        )
        self._status(
            f"activated {activation.skill_name} v{activation.version} "
            f"({activation.provider}){pipeline_note} — EXTENSION_ACTIVATED recorded"
        )
