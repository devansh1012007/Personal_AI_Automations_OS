"""The chat widgets: message stream, memory rail, budget gauge, header, cards.

The performance-critical piece is :class:`MessageStream` — the whole
conversation lives in **one** widget, never one widget per message (spec
§3.2: "virtualized list; do not mount every message forever"). Line *counts*
are computed once per block; rendered strips are computed on demand and
cached, so scrolling renders only the visible rows and a 10k-turn session
scrolls at the same speed as a 10-turn one. The prefix-sum mapping from
scroll line to block line is only correct because counts and strips come
from the same wrap — the two never disagree.

Input robustness (spec §3.2 "input fuzz: unicode, CJK, ANSI injection on the
renderer"): every text that enters a block passes through :func:`sanitize`,
which strips ANSI escape sequences and control characters. Textual renders
Segments rather than passthrough bytes, but user text is hostile input and is
neutralised at the door anyway. Wrapping goes through Rich's ``Text.wrap``,
which measures display cells — CJK text wraps correctly instead of
overflowing.

Colour rule: widgets never hardcode a semantic hex. Colours resolve from the
app's theme variables at render time (:func:`_var_style`) or from TCSS
classes on the widgets themselves; text and glyphs carry the meaning.
"""

from __future__ import annotations

import io
import uuid
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from rich.console import Console, ConsoleDimensions, ConsoleOptions
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.style import Style as TextualStyle
from textual.widget import Widget
from textual.widgets import ListItem, Static

from paa.tui.chat.render import MAX_TEXT_CHARS, fmt_tokens, sanitize

#: Textual's render filters (``textual.filter.monochrome_style`` and friends)
#: dereference ``Segment.style`` unguarded, so a Segment may never carry
#: ``style=None`` — even a deliberately blank filler one carries this bare
#: rich Style instead.
_NO_STYLE: Final = Style()

if TYPE_CHECKING:
    from paa.chat.session import ChatSessionRecord

__all__ = [
    "BudgetGauge",
    "ChatHeader",
    "MemoryRail",
    "RailFact",
    "SessionCard",
    "sanitize",
]

ROLE_USER: Final = "user"
ROLE_ASSISTANT: Final = "assistant"
ROLE_SYSTEM: Final = "system"

#: Column width of the role label ("assistant: " is the widest). The label is
#: the glyph-first role signifier; colour is not involved.
LABEL_WIDTH: Final = 11

#: Upper bound on any block's rendered line count, used for the Markdown
#: render's ConsoleOptions. ``sanitize`` bounds a block's text at
#: MAX_TEXT_CHARS, and a render can add at most one line per character
#: (plus the final line), so this bound never crops a real block — the
#: render's natural line count is what the prefix-sum table records.
_MAX_RENDER_LINES: Final = MAX_TEXT_CHARS + 1

#: The budget gauge's cell count (▱/▰ cells).
_GAUGE_CELLS: Final = 10

#: Shared console for display-cell measurement (CJK-correct wrapping). One
#: instance; ``Text.wrap`` only reads its cell-width helpers.
#:
#: The shared text shaping lives in :mod:`paa.tui.chat.render` — the one
#: textual-free module, so the plain loop neutralises hostile input at the
#: same door with the same rule. ``sanitize`` is re-exported below (it is in
#: this module's ``__all__``) and ``MAX_TEXT_CHARS`` bounds every block.
_WRAP_CONSOLE: Final = Console(width=80, legacy_windows=False, highlight=False)


def _var_style(widget: Widget, name: str) -> Style:
    """A :class:`rich.style.Style` from one theme variable, via the running app.

    Segments carry rich Styles, not Textual's, so the resolved variable is
    converted once here (Textual's ``Style.rich_style`` — the sanctioned
    bridge between the two) and every Segment call site stays type-honest.

    Falls back to a bare style when no app is attached (a widget rendered
    outside :class:`~paa.tui.chat.app.ChatApp`, possible only in isolation) —
    the text still reads, it just loses its accent, which is exactly the
    degrade the design language asks for ("colour never carries meaning
    alone"). ``is_attached`` is checked *first* because ``Widget.app`` raises
    :class:`~textual.app.NoActiveAppError` on a detached widget rather than
    returning ``None`` — asking for the app before asking if there is one
    would make the documented fallback unreachable.

    The ``dim`` *attribute* is deliberately never set here: Textual's dim
    filter (:func:`textual.filter.dim_style`) unpacks the colour triplet of
    every dimmed Segment, and a default/terminal-inherited colour has none —
    a dimmed label on ``ansi_default`` (this theme's foreground) crashes the
    renderer. De-emphasis is carried by the ``muted`` colour variable instead,
    which keeps the "colour never carries meaning alone" rule intact: the
    ``┄`` glyph and the ``system`` role word still name the line kind.
    """
    if widget.is_attached:
        value = widget.app.get_css_variables().get(name)
        if value:
            return TextualStyle.parse(value).rich_style
    return Style()


# ---------------------------------------------------------------------------
# Header — the one-row bar
# ---------------------------------------------------------------------------


class ChatHeader(Static):
    """The single header row: ``paa chat ─ session <id> · isolated · model …``."""

    DEFAULT_CSS = """
    ChatHeader { height: 1; padding: 0 1; color: $foreground; }
    """

    def set_session(
        self,
        *,
        session_id: uuid.UUID,
        isolated: bool,
        model: str,
        policy_mode: str,
        scope: str = "personal",
    ) -> None:
        """Re-render the bar. Text first: the id, the fence state, the model."""
        short = str(session_id)[:8]
        fence = "isolated" if isolated else f"cross-session · scope {scope}"
        self.update(
            f"paa chat ─ session {short} · {fence} · model:{model} · policy:{policy_mode}"
        )


# ---------------------------------------------------------------------------
# The message stream — one widget, virtualized by line
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    """One message: role, text, and its lazily-rendered lines.

    ``markdown`` blocks (fresh assistant replies) render through Rich's
    Markdown eagerly — their line *count* is the render's line count. Plain
    blocks (users, systems, resumed history) keep only the count until the
    scroll actually touches them, which is what makes a resumed 10k-turn
    transcript cheap to load and to scroll.
    """

    role: str
    text: str
    width: int = 0
    streaming: bool = False
    markdown: bool = False
    strips: list[Strip] | None = None

    @property
    def is_plain(self) -> bool:
        return not self.markdown


class MessageStream(ScrollView):
    """The virtualized conversation: one widget, strips rendered on demand.

    Blocks are appended one per message. The scroll maps an absolute line to
    (block, offset) through a prefix-sum table rebuilt only when blocks are
    appended, the width changes, or density changes — never per frame.
    """

    DEFAULT_CSS = """
    MessageStream { padding: 0 1; }
    """

    def __init__(self, *, compact: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._blocks: list[_Block] = []
        # ``_starts[i]`` is the absolute first line of block ``i``; the last
        # entry is the total. Rebuilt whenever counts change in bulk.
        self._starts: list[int] = [0]
        self._total: int = 0
        self._widest: int = 0
        self._compact: bool = compact
        self._following: bool = True

    # -- public surface ------------------------------------------------------

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        """/compact density: no inter-message gap."""
        if compact == self._compact:
            return
        self._compact = compact
        self._invalidate_all()

    def append_message(self, role: str, text: str, *, streaming: bool = False) -> None:
        """Append one message block. ``streaming`` marks the live assistant block."""
        block = _Block(
            role=role,
            text=sanitize(text),
            streaming=streaming,
            markdown=role == ROLE_ASSISTANT,
        )
        self._append_block(block)

    def load_history(self, messages: list[tuple[str, str]]) -> None:
        """Bulk-load a resumed transcript (plain rendering, one rebuild).

        One pass, one prefix-sum rebuild — this is the method that keeps a
        10k-turn resume linear instead of quadratic.
        """
        for role, text in messages:
            self._blocks.append(_Block(role=role, text=sanitize(text)))
        self._rebuild()

    def update_streaming(self, text: str) -> None:
        """Replace the live block's text with the accumulated stream so far."""
        block = self._active_block()
        if block is None:
            return
        block.text = sanitize(text)
        self._refresh_block(block)

    def finalize_streaming(self, text: str) -> None:
        """Complete the live block: full text, markdown render, caret off."""
        block = self._active_block()
        if block is None:
            return
        block.text = sanitize(text)
        block.streaming = False
        self._refresh_block(block)

    def mark_cancelled(self) -> None:
        """Stop the live block without replacing its text (Esc mid-stream)."""
        block = self._active_block()
        if block is None:
            return
        block.streaming = False
        self._refresh_block(block)

    def system_line(self, text: str) -> None:
        """One dim, one-line system note (recalls, denials, journal notes)."""
        block = _Block(role=ROLE_SYSTEM, text=sanitize(text).replace("\n", " "))
        self._append_block(block)

    def messages(self) -> list[tuple[str, str]]:
        """The conversation so far, as (role, text) pairs — ``/export``'s source."""
        return [(block.role, block.text) for block in self._blocks]

    def clear(self) -> None:
        """Session switch: drop every block."""
        self._blocks.clear()
        self._rebuild()

    def scroll_to_end(self) -> None:
        """Follow the stream (used after appends and on resume)."""
        self._following = True
        self.scroll_end(animate=False, duration=0.0)

    @property
    def total_lines(self) -> int:
        """Total rendered line count (the virtual height). For tests."""
        return self._total

    # -- internals -------------------------------------------------------------

    def _active_block(self) -> _Block | None:
        for block in self._blocks:
            if block.streaming:
                return block
        return None

    def _content_width(self) -> int:
        region = self.scrollable_content_region
        width = region.width if region.width > 0 else self.size.width
        return max(8, width - 2)  # padding 0 1

    def _gap(self) -> int:
        """Blank lines after each message (comfortable density default)."""
        return 0 if self._compact else 1

    def _wrap_width(self, width: int) -> int:
        return max(8, width - LABEL_WIDTH)

    def _wrap_lines(self, text: str, width: int) -> list[str]:
        """Display-cell wrap (CJK-correct); the shared count/render source."""
        if not text.strip():
            return [""]
        return [line.plain for line in Text(text).wrap(_WRAP_CONSOLE, width=width)]

    def _plain_count(self, block: _Block, width: int) -> int:
        if block.role == ROLE_SYSTEM:
            return 1  # system lines are dense by design: dim, one line, no gap
        lines = self._wrap_lines(block.text, self._wrap_width(width))
        return len(lines) + self._gap()

    def _measure(self, block: _Block, width: int) -> int:
        """Sync the block to ``width``; return its line count.

        Markdown renders eagerly (fresh, interactive turns only); plain
        blocks re-wrap only when the width actually changed, so a resize is
        O(conversation) once, not per frame.
        """
        if block.strips is not None and block.width == width:
            return len(block.strips)
        if block.markdown:
            block.strips = self._render_markdown(block, width)
            block.width = width
            return len(block.strips)
        if block.width != width:
            block.strips = None  # lazily rebuilt at the new width
            block.width = width
        return self._plain_count(block, width)

    def _append_block(self, block: _Block) -> None:
        width = self._content_width()
        count = self._measure(block, width)
        self._blocks.append(block)
        self._starts.append(self._starts[-1] + count)
        self._total = self._starts[-1]
        self._touch_layout(max(self._widest, self._block_width(block)))
        if self._following:
            self.scroll_to_end()

    def _refresh_block(self, block: _Block) -> None:
        """Re-render one block in place (streaming flush, finalize, density)."""
        width = self._content_width()
        index = self._blocks.index(block)
        old = len(block.strips) if block.strips is not None else 0
        count = self._measure(block, width)
        delta = count - old
        if delta:
            for i in range(index + 1, len(self._starts)):
                self._starts[i] += delta
        self._total += delta
        self._touch_layout(max(self._widest, self._block_width(block)))
        if self._following:
            self.scroll_to_end()

    def _invalidate_all(self) -> None:
        """Density or width changed: drop caches, recount, rebuild sums."""
        for block in self._blocks:
            block.strips = None
        self._rebuild()

    def _rebuild(self) -> None:
        """Recompute counts and the prefix-sum table from scratch."""
        width = self._content_width()
        starts = [0]
        widest = 0
        for block in self._blocks:
            count = self._measure(block, width)
            starts.append(starts[-1] + count)
            widest = max(widest, self._block_width(block))
        self._starts = starts
        self._total = starts[-1]
        self._touch_layout(widest)

    def _block_width(self, block: _Block) -> int:
        if block.strips:
            return max(strip.cell_length for strip in block.strips)
        return max((len(line) for line in block.text.splitlines() or [""]), default=0)

    def _touch_layout(self, widest: int) -> None:
        self._widest = widest
        self.virtual_size = Size(self._widest + 1, self._total)

    # -- rendering ---------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        """One strip for one viewport row. This is the virtualization hot path."""
        scroll_x, scroll_y = self.scroll_offset
        line_y = scroll_y + y
        width = self.scrollable_content_region.width or self.size.width
        index = bisect_right(self._starts, line_y) - 1
        if index < 0 or index >= len(self._blocks):
            return Strip.blank(width, self.rich_style or _NO_STYLE)
        block = self._blocks[index]
        strips = self._strips(block)
        offset = line_y - self._starts[index]
        if offset >= len(strips):
            return Strip.blank(width, self.rich_style or _NO_STYLE)
        return strips[offset].crop_extend(
            scroll_x, scroll_x + width, self.rich_style or _NO_STYLE
        )

    def _strips(self, block: _Block) -> list[Strip]:
        if block.strips is None or block.width != self._content_width():
            self._measure(block, self._content_width())
            if block.strips is None:
                block.strips = self._render_plain(block, self._content_width())
        assert block.strips is not None  # _measure/_render_plain guarantee it
        return block.strips

    def _label_style(self) -> Style:
        return _var_style(self, "muted")

    def _caret_style(self) -> Style:
        return _var_style(self, "memory")

    def _render_plain(self, block: _Block, width: int) -> list[Strip]:
        """Role-labelled, display-cell-wrapped strips for plain blocks.

        The same wrap that decided the block's count decides these strips —
        the prefix-sum mapping's correctness rests on that equality.
        """
        label_style = self._label_style()
        body_style = _var_style(self, "muted") if block.role == ROLE_SYSTEM else Style()
        wrap_width = self._wrap_width(width)

        if block.role == ROLE_SYSTEM:
            # Dense by design: one line, ellipsised to the body column. The
            # count (1, from _plain_count) and this strip agree by construction.
            # Text.truncate mutates in place and returns None, so the call is
            # statement-shaped, never an assignment.
            line = Text(block.text)
            line.truncate(wrap_width, overflow="ellipsis")
            head = "┄ system:".ljust(LABEL_WIDTH)
            return [Strip([Segment(head, label_style), Segment(line.plain, body_style)])]

        if not block.text.strip():
            # An empty block still occupies one line (the caret while live),
            # matching _wrap_lines' own [""] for empty text.
            caret = Segment("▌", self._caret_style()) if block.streaming else Segment("", _NO_STYLE)
            strips = [Strip([caret])]
            for _ in range(self._gap()):
                strips.append(Strip.blank(width, _NO_STYLE))
            return strips

        # THE wrap: the identical call _plain_count makes, with the identical
        # width — so len(strips) here equals the count the prefix-sum table
        # already recorded for this block, cell for cell.
        wrapped = self._wrap_lines(block.text, wrap_width)
        label = f"{block.role}:".ljust(LABEL_WIDTH)
        strips = []
        for i, body in enumerate(wrapped):
            head = label if i == 0 else " " * LABEL_WIDTH
            segments = [Segment(head, label_style), Segment(body, body_style)]
            if block.streaming and i == len(wrapped) - 1:
                segments.append(Segment("▌", self._caret_style()))
            strips.append(Strip(segments))
        for _ in range(self._gap()):
            strips.append(Strip.blank(width, _NO_STYLE))
        return strips

    def _render_markdown(self, block: _Block, width: int) -> list[Strip]:
        """Rich Markdown lines for an assistant reply (fresh turns only)."""
        if not block.text.strip():
            caret = Segment("▌", self._caret_style()) if block.streaming else Segment("", _NO_STYLE)
            return [Strip([caret])]
        console = Console(
            width=width,
            file=io.StringIO(),
            force_terminal=False,
            legacy_windows=False,
            highlight=False,
        )
        options = ConsoleOptions(
            size=ConsoleDimensions(width, _MAX_RENDER_LINES),
            legacy_windows=False,
            min_width=1,
            max_width=width,
            is_terminal=False,
            encoding="utf-8",
            max_height=_MAX_RENDER_LINES,
        )
        lines = console.render_lines(Markdown(block.text), options, pad=False)
        strips = [Strip(list(row)) for row in lines]
        if block.streaming:
            strips[-1] = Strip([*strips[-1], Segment("▌", self._caret_style())])
        for _ in range(self._gap()):
            strips.append(Strip.blank(width, _NO_STYLE))
        return strips

    def on_resize(self) -> None:
        """Width changed: recount everything once (never per frame)."""
        self._rebuild()
        if self._following:
            self.scroll_to_end()

    def on_focus(self) -> None:
        """Keyboard focus entering the stream resumes following."""
        self.scroll_to_end()

    def on_blur(self) -> None:
        pass


# ---------------------------------------------------------------------------
# The memory rail — memory made visible
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RailFact:
    """One consulted fact as the rail renders it."""

    fact_id: str
    statement: str
    confidence: float
    cited: int
    origin_session: str | None = None


class MemoryRail(VerticalScroll):
    """The rail: consulted facts, captured signals, the token budget gauge.

    Sections are separate widgets so the accents come from TCSS classes
    ($memory for cognition), never inline markup — and so each section is
    independently addressable in tests by id.
    """

    DEFAULT_CSS = """
    MemoryRail { width: 36; padding: 0 1; background: $surface; }
    MemoryRail .rail-title { color: $memory; text-style: bold; }
    MemoryRail .rail-section { color: $memory; margin-top: 1; }
    MemoryRail .rail-line { color: $foreground; }
    MemoryRail .rail-gauge { color: $memory; margin-top: 1; }
    MemoryRail .rail-hint { color: $foreground-muted; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._title = Static("MEMORY  ·  Ctrl+T hides", classes="rail-title")
        self._consulted = Static("consulted  —  none yet", classes="rail-section")
        self._captured = Static("captured this turn  —  none", classes="rail-section")
        self._gauge = BudgetGauge(classes="rail-gauge")
        self._hint = Static(
            "facts consulted are shown with confidence and times cited",
            classes="rail-hint",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._consulted
        yield self._captured
        yield self._gauge
        yield self._hint

    def set_consulted(self, facts: list[RailFact]) -> None:
        """The facts the last turn served, confidence and times cited shown."""
        if not facts:
            self._consulted.update("consulted  —  none served")
            return
        lines = ["consulted"]
        for fact in facts:
            statement = fact.statement.replace("\n", " ")
            if len(statement) > 34:
                statement = statement[:33] + "…"
            line = f" ▸ {statement}  {fact.confidence:.2f}"
            if fact.cited:
                line += f" · cited {fact.cited}\u00d7"
            lines.append(line)
        self._consulted.update("\n".join(lines))

    def set_captured(self, signals: list[str]) -> None:
        """The signals the last turn wrote (ids attested by the ledger)."""
        if not signals:
            self._captured.update("captured this turn  —  none")
            return
        lines = ["captured this turn"]
        for signal in signals:
            lines.append(f" ▸ signal {signal[:12]}… → extracted")
        self._captured.update("\n".join(lines))

    def set_budget(self, used: int, ceiling: int) -> None:
        """The token budget gauge (used / ceiling)."""
        self._gauge.set_budget(used, ceiling)


class BudgetGauge(Static):
    """The token budget gauge: ``▰▰▰▱▱▱▱▱▱▱ 2.3k/8k tokens``."""

    DEFAULT_CSS = """
    BudgetGauge { height: auto; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("budget  —  no turn yet", **kwargs)

    def set_budget(self, used: int, ceiling: int) -> None:
        """Re-render the gauge. Text first (the numbers), bar second."""
        if ceiling <= 0:
            self.update("budget  —  ceiling not set")
            return
        ratio = min(1.0, used / ceiling)
        filled = round(ratio * _GAUGE_CELLS)
        bar = "▰" * filled + "▱" * (_GAUGE_CELLS - filled)
        self.update(f"{bar}  {fmt_tokens(used)}/{fmt_tokens(ceiling)} tokens")


# ---------------------------------------------------------------------------
# Session cards
# ---------------------------------------------------------------------------


class SessionCard(ListItem):
    """One session in the list: id, fence, model, turns, tokens, started."""

    DEFAULT_CSS = """
    SessionCard { padding: 0 1; height: auto; }
    SessionCard > Static { color: $foreground; }
    """

    def __init__(self, record: ChatSessionRecord, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.record = record
        fence = "isolated" if not record.cross_session_episodic else "cross-session"
        cancelled = f", {record.turns_cancelled} cancelled" if record.turns_cancelled else ""
        failed = f", {record.turns_failed} failed" if record.turns_failed else ""
        started = (
            record.created_at.strftime("%Y-%m-%d %H:%M UTC")
            if record.created_at
            else "unknown"
        )
        body = (
            f"▸ session {str(record.session_id)[:12]}…  [{record.status}]  {fence}\n"
            f"  model:{record.model or 'unknown'} · turns {record.turns_proposed} "
            f"({record.turns_completed} ok{failed}{cancelled}) · "
            f"{fmt_tokens(record.tokens_consumed)} tok\n"
            f"  started {started}"
        )
        self._body = Static(body, classes="card-body")
        if record.cross_session_episodic:
            self.add_class("-cross")

    def compose(self) -> ComposeResult:
        yield self._body
