"""The chat theme: semantic colours as Textual theme variables.

SPEC 2 §3.2's palette, in one place. The rule the widgets follow: **text +
glyph first, colour second** — a denial always names the rule, a verified
chain always says so, and colour never carries meaning alone (colour-blind
safe, and it degrades cleanly on a high-contrast or colourless terminal).

The variables every widget may reference:

=================  ==============================================
variable           meaning
=================  ==============================================
``$memory``        memory / cognition (cyan)
``$trust``         trust / ledger (amber)
``$denial``        policy denial + failure only (red)
``$session``       session / isolation (violet)
``$success``       success (green)
``$surface``       rails and tables (surface, never a box)
``$muted``         dim system lines
=================  ==============================================

``$primary`` is the memory colour, so Textual's own chrome (cursor, footer
keys, focus) inherits the cognition accent rather than introducing a seventh
colour. ``$accent`` is the session colour.

Backgrounds: ``ansi_default`` — the *terminal's* default, never a forced
black (the design language's "bg = terminal default"). Two variants ship:
``paa-chat`` and ``paa-chat-hc`` (high contrast — brighter foreground, the
same semantic hues pushed lighter so they hold contrast against either a
light or a dark terminal palette).
"""

from __future__ import annotations

from textual.theme import Theme

__all__ = ["CHAT_THEME_NAME", "HC_THEME_NAME", "chat_theme", "chat_theme_hc"]

#: The default theme name the chat app activates on mount.
CHAT_THEME_NAME: str = "paa-chat"

#: The high-contrast variant, toggled with F2 (see the app's bindings).
HC_THEME_NAME: str = "paa-chat-hc"

#: Cyan — memory/cognition. The dark variant is readable on both light and
#: dark terminal palettes because the background is never forced.
_MEMORY_DARK: str = "#31d0e0"
_MEMORY_HC: str = "#5ce4f0"

#: Amber — trust/ledger.
_TRUST_DARK: str = "#e0a458"
_TRUST_HC: str = "#f2c078"

#: Red — denials and failures only.
_DENIAL_DARK: str = "#e05252"
_DENIAL_HC: str = "#ff7b7b"

#: Violet — session/isolation.
_SESSION_DARK: str = "#b08ce8"
_SESSION_HC: str = "#c9a8f5"

#: Green — success.
_SUCCESS_DARK: str = "#5ec46a"
_SUCCESS_HC: str = "#7fe08a"

#: Dim system lines. De-emphasis is a *colour*, not rich's ``dim`` attribute:
#: Textual's dim filter (``textual.filter.dim_style``) unpacks the colour
#: triplet of every dimmed Segment, and a default/terminal-inherited colour
#: has none — dimmed text on ``ansi_default`` (this theme's foreground)
#: crashes the renderer. A mid grey reads on both light and dark terminals.
_MUTED_DARK: str = "#8b8b94"
_MUTED_HC: str = "#a8a8b2"

_SURFACE_LIGHT: str = "rgba(127, 127, 127, 0.08)"
_SURFACE_DARK: str = "rgba(200, 200, 200, 0.06)"


def _theme(*, name: str, hc: bool) -> Theme:
    """Build one chat theme. The high-contrast flag lightens the accents."""
    memory = _MEMORY_HC if hc else _MEMORY_DARK
    trust = _TRUST_HC if hc else _TRUST_DARK
    denial = _DENIAL_HC if hc else _DENIAL_DARK
    session = _SESSION_HC if hc else _SESSION_DARK
    success = _SUCCESS_HC if hc else _SUCCESS_DARK
    return Theme(
        name=name,
        primary=memory,
        secondary=trust,
        accent=session,
        success=success,
        warning=trust,
        error=denial,
        # The terminal's own default colours: the app never paints black over
        # a light terminal (spec §3.2 "bg = terminal default"). ``ansi_default``
        # is the special string Textual's ansi_* themes use for exactly this.
        background="ansi_default",
        foreground="ansi_default",
        dark=not hc,
        variables={
            "memory": memory,
            "trust": trust,
            "denial": denial,
            "session": session,
            "surface": _SURFACE_DARK if not hc else _SURFACE_LIGHT,
            "muted": _MUTED_HC if hc else _MUTED_DARK,
            "footer-key-foreground": memory,
        },
    )


def chat_theme() -> Theme:
    """The default chat theme (comfortable contrast, terminal-default bg)."""
    return _theme(name=CHAT_THEME_NAME, hc=False)


def chat_theme_hc() -> Theme:
    """The high-contrast variant (F2 toggles; spec §3.2 accessibility)."""
    return _theme(name=HC_THEME_NAME, hc=True)
