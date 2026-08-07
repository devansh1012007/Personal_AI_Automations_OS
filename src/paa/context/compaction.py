"""Deterministic text reduction — the RFC §11.1 context-flooding mitigation.

The obvious way to shrink an over-budget context is to ask a model to summarise
it. This module deliberately does not do that, for three reasons:

1. **Determinism.** RFC §1.5 requires that replaying a correlation reproduces
   its state exactly. An LLM summariser is non-deterministic in general and
   version-dependent in particular, so a packet compacted today could not be
   reproduced from the ledger after a model upgrade.
2. **Circularity.** Summarising context to fit a context window spends context
   to save context, and does so on the exact path that is already under
   pressure.
3. **Fidelity.** A summariser can hallucinate. A whitespace collapser cannot.

So everything here is mechanical: collapse whitespace, strip markdown
decoration, drop repeated lines, truncate over-long ones. Lossy, but lossy in
ways a reader can predict and a test can pin down.

Two properties are guaranteed and both are exhaustively tested:

**Idempotence** — ``compact(compact(x)) == compact(x)``. Achieved structurally
rather than by careful regex authorship: :meth:`ContextCompactor.compact` runs
the reduction to a *fixed point*, so its output is by construction a value the
reduction no longer changes.

**Non-expansion** — ``len(compact(x)) <= len(x)``. Every operation only removes
characters or replaces a separator with a shorter-or-equal one, and truncation
accounts for its own marker. A "compactor" that can grow its input would be a
denial-of-service vector on the one path that exists to prevent one.
"""

from __future__ import annotations

import re
from typing import Final

import structlog

from paa.config import ContextSettings
from paa.context.budget import CharEstimator, TokenEstimator

__all__ = ["ContextCompactor"]

log = structlog.get_logger(__name__)

#: Default per-line cap before truncation. Roughly 60 tokens at the standard
#: 4-chars-per-token heuristic — long enough for a real sentence or a file path
#: with arguments, short enough that a pasted blob cannot monopolise the packet.
DEFAULT_MAX_LINE_CHARS: Final[int] = 240

#: Marker appended to a truncated line. ASCII rather than U+2026 so the output
#: survives any encoding the ledger, a terminal or a log shipper might impose.
DEFAULT_ELLIPSIS: Final[str] = "..."

# -- markdown decoration ----------------------------------------------------
# Anchored and conservative. Each pattern removes *decoration* while preserving
# the text it decorates; the fixed-point loop handles nesting, so no pattern
# needs to be clever about it.

_FENCE = re.compile(r"^[ \t]{0,3}(?:`{3,}|~{3,})\s*\S*\s*$")
_HRULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*")
_BLOCKQUOTE = re.compile(r"^[ \t]{0,3}(?:>[ \t]?)+")
_LIST_BULLET = re.compile(r"^[ \t]{0,3}[-*+][ \t]+")
_IMAGE_OR_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"\*(?=\S)([^*\n]+?)(?<=\S)\*")
_INLINE_CODE = re.compile(r"`+([^`\n]+?)`+")
_HORIZONTAL_WS = re.compile(r"[^\S\n]+")


class ContextCompactor:
    """Mechanical, non-LLM text reduction.

    Stateless after construction; one instance is safe to share.
    """

    __slots__ = ("ellipsis", "estimator", "max_line_chars")

    def __init__(
        self,
        *,
        settings: ContextSettings | None = None,
        estimator: TokenEstimator | None = None,
        max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
        ellipsis: str = DEFAULT_ELLIPSIS,
    ) -> None:
        """
        :param settings: context tuning, for the default estimator's
            ``chars_per_token``.
        :param estimator: token measurement for :meth:`compact_to_budget`.
            Must satisfy ``estimate("") == 0`` and be monotonic over prefixes;
            :meth:`compact_to_budget` binary-searches against it.
        :param max_line_chars: lines longer than this are truncated. Must leave
            room for the marker, otherwise truncation could not shorten a line.
        :param ellipsis: truncation marker.
        """
        if isinstance(max_line_chars, bool) or not isinstance(max_line_chars, int):
            raise ValueError(f"max_line_chars must be an int, got {type(max_line_chars).__name__}")
        if max_line_chars <= len(ellipsis):
            raise ValueError(
                f"max_line_chars ({max_line_chars}) must exceed len(ellipsis) ({len(ellipsis)}); "
                "otherwise truncation cannot shorten a line"
            )
        self.max_line_chars = max_line_chars
        self.ellipsis = ellipsis
        self.estimator = (
            estimator if estimator is not None else CharEstimator.from_settings(settings)
        )

    # -- public API --------------------------------------------------------

    def compact(self, text: str) -> str:
        """Reduce ``text`` to a fixed point of the reduction rules.

        Idempotence follows directly: the returned value ``y`` satisfies
        ``_reduce_once(y) == y``, so ``compact(y)`` terminates on its first pass
        and returns ``y`` unchanged.

        The pass bound is ``len(text) + 2``, which is *provably* sufficient
        rather than merely generous. After the first pass no line separators
        remain to be normalised, so from then on every pass either removes at
        least one character or is already at the fixed point. A strictly
        decreasing non-negative integer therefore reaches the fixed point within
        ``len(text)`` further passes. Real inputs converge in two or three; the
        bound exists so that convergence is a guarantee rather than an
        expectation.
        """
        current = text
        for _ in range(len(text) + 2):
            nxt = self._reduce_once(current)
            if nxt == current:
                return current
            current = nxt
        return current  # pragma: no cover — unreachable given the bound above

    def compact_to_budget(self, text: str, max_tokens: int) -> str:
        """Reduce ``text`` until the estimator says it fits in ``max_tokens``.

        Three escalating stages, cheapest and least destructive first:

        1. :meth:`compact` — pure decoration removal. Often enough on its own.
        2. drop whole trailing lines. Lines carry meaning as units, so losing
           the last few is far kinder than cutting mid-sentence, and the caller
           has already ranked the content so the tail is the least important
           part.
        3. character-level truncation of what remains, with a marker.

        The result is guaranteed to satisfy ``estimate(result) <= max_tokens``
        and ``len(result) <= len(text)``.

        Only stages 1 and 2 return a :meth:`compact` fixed point. Stage 3 can
        cut inside a construct that the reducer would then rewrite, so the
        output of a hard truncation is not guaranteed to be idempotent under a
        further :meth:`compact`. That is an acceptable trade at a stage which
        only runs when the alternative is breaching the ceiling.
        """
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError(f"max_tokens must be an int, got {type(max_tokens).__name__}")
        if max_tokens < 0:
            raise ValueError(f"max_tokens must be non-negative, got {max_tokens}")

        compacted = self.compact(text)
        if self.estimator.estimate(compacted) <= max_tokens:
            return compacted

        lines = compacted.split("\n")
        while len(lines) > 1:
            lines.pop()
            candidate = "\n".join(lines).strip("\n")
            if self.estimator.estimate(candidate) <= max_tokens:
                log.debug(
                    "compacted_to_budget_by_line_drop",
                    max_tokens=max_tokens,
                    kept_lines=len(lines),
                )
                return candidate

        return self._truncate_to_tokens("\n".join(lines).strip("\n"), max_tokens)

    def fits(self, text: str, max_tokens: int) -> bool:
        """Whether ``text`` already fits the budget under this estimator."""
        return self.estimator.estimate(text) <= max_tokens

    # -- reduction ---------------------------------------------------------

    def _reduce_once(self, text: str) -> str:
        """One reduction pass. Never lengthens its input.

        ``str.splitlines`` recognises every Unicode line break and each becomes
        a single ``\\n``, so a two-character ``\\r\\n`` shortens and every other
        separator stays the same width. Joining *n* lines emits at most the
        ``n - 1`` separators the input already contained, so the join cannot
        grow the text either.
        """
        raw_lines = text.splitlines()

        reduced: list[str] = []
        for raw in raw_lines:
            line = self._reduce_line(raw)
            reduced.append(line)

        out: list[str] = []
        seen: set[str] = set()
        for line in reduced:
            if not line:
                # Collapse runs of blank lines to a single separator. Blank
                # lines are excluded from deduplication on purpose: treating
                # them as duplicates would keep only the very first one in the
                # whole document and weld every paragraph into one block.
                if out and out[-1] == "":
                    continue
                out.append("")
                continue
            if line in seen:
                continue
            seen.add(line)
            out.append(line)

        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()

        return "\n".join(out)

    def _reduce_line(self, line: str) -> str:
        """Strip decoration, collapse whitespace and truncate a single line."""
        if _FENCE.match(line) or _HRULE.match(line):
            # A fence or rule is pure decoration: it carries no content of its
            # own, so the whole line goes and the blank-run collapse absorbs it.
            return ""

        line = _HEADING.sub("", line)
        line = _BLOCKQUOTE.sub("", line)
        line = _LIST_BULLET.sub("", line)
        line = _IMAGE_OR_LINK.sub(r"\1", line)
        line = _INLINE_CODE.sub(r"\1", line)
        line = _BOLD.sub(r"\2", line)
        line = _ITALIC.sub(r"\1", line)

        line = _HORIZONTAL_WS.sub(" ", line).strip()

        if len(line) > self.max_line_chars:
            line = line[: self.max_line_chars - len(self.ellipsis)] + self.ellipsis
        return line

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Largest character prefix of ``text`` that fits ``max_tokens``.

        Binary search rather than a shrink loop: with an exact BPE tokenizer a
        character-at-a-time walk costs a full re-encode per step, which on a
        multi-kilobyte blob is the slowest thing in the whole subsystem. The
        search is valid because :class:`TokenEstimator` requires monotonicity
        over prefixes.

        The marker is appended only when the input is long enough to absorb it,
        so the non-expansion guarantee holds even for inputs shorter than the
        marker itself.
        """
        if self.estimator.estimate(text) <= max_tokens:
            return text

        marker = self.ellipsis
        if len(text) >= len(marker):
            limit = len(text) - len(marker)
            best = self._search_prefix(text, max_tokens, suffix=marker, hi=limit)
            if best is not None:
                return text[:best] + marker

        # Even the bare marker exceeds the budget — drop it and return the
        # longest prefix that fits, possibly empty.
        best = self._search_prefix(text, max_tokens, suffix="", hi=len(text))
        return text[: best if best is not None else 0]

    def _search_prefix(self, text: str, max_tokens: int, *, suffix: str, hi: int) -> int | None:
        """Largest ``k <= hi`` with ``estimate(text[:k] + suffix) <= max_tokens``."""
        lo = 0
        best: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.estimator.estimate(text[:mid] + suffix) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def __repr__(self) -> str:
        return (
            f"ContextCompactor(max_line_chars={self.max_line_chars}, "
            f"ellipsis={self.ellipsis!r})"
        )
