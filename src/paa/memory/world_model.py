"""The Cognitive World Model — long-horizon belief state in the markdown vault.

RFC §12. The runtime does not treat history as a flat list of log lines; it
maintains four living documents that abstract day-to-day signal into strategic
context:

* ``current_focus.md`` — active milestones and what is being worked on now
* ``strategic_risks.md`` — recurring failures, hardware limits, bottlenecks
* ``active_constraints.md`` — resource limits, token/VRAM budgets, schedules
* ``operating_themes.md`` — architectural preferences and behavioural heuristics

The hard requirement (RFC §9: markdown is "the strategic human interface") is
that the system may rewrite *its own* managed section of each file without ever
touching text the human wrote. That is enforced with marker fences: the runtime
owns only the bytes between ``<!-- paa:managed:BEGIN section -->`` and the
matching ``END``; everything outside survives every update untouched. Writes are
atomic (temp file + ``os.replace``) so a crash mid-write cannot leave a half a
document — the previous version stays intact until the new one is fully on disk.
"""

from __future__ import annotations

import enum
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["BeliefDocument", "EpisodeSummary", "WorldModel"]

log = structlog.get_logger(__name__)


class BeliefDocument(str, enum.Enum):
    """The four belief-state files. Values are the vault filenames."""

    CURRENT_FOCUS = "current_focus.md"
    STRATEGIC_RISKS = "strategic_risks.md"
    ACTIVE_CONSTRAINTS = "active_constraints.md"
    OPERATING_THEMES = "operating_themes.md"

    @property
    def title(self) -> str:
        return self.value.removesuffix(".md").replace("_", " ").title()


_BEGIN = "<!-- paa:managed:BEGIN {section} -->"
_END = "<!-- paa:managed:END {section} -->"
# Non-greedy, DOTALL: capture exactly one managed block, newline-tolerant.
_BLOCK_RE_TMPL = r"{begin}\n?(.*?)\n?{end}"


@dataclass(slots=True)
class EpisodeSummary:
    """One rung of the §12 abstraction ladder."""

    level: str  # observation | pattern | principle | playbook
    text: str
    evidence_count: int = 1


class WorldModel:
    """Reads and updates the belief-state documents, and compresses episodes."""

    def __init__(self, vault_path: Path | str) -> None:
        self._vault = Path(vault_path)
        self._vault.mkdir(parents=True, exist_ok=True)

    def path_for(self, doc: BeliefDocument) -> Path:
        return self._vault / doc.value

    # -- reading -----------------------------------------------------------

    def read_managed(self, doc: BeliefDocument, section: str = "state") -> str:
        """Return the current managed-section text, or '' if none yet."""
        path = self.path_for(doc)
        if not path.exists():
            return ""
        return self._extract_block(path.read_text(encoding="utf-8"), section) or ""

    def read_full(self, doc: BeliefDocument) -> str:
        path = self.path_for(doc)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # -- writing -----------------------------------------------------------

    def update_belief_state(
        self,
        doc: BeliefDocument,
        content: str,
        *,
        section: str = "state",
        source: str = "memory_curator",
    ) -> None:
        """Replace only the managed section, preserving all human-authored text.

        If the file does not exist it is created with a human-editable header
        above the managed block, so the user immediately sees where they may
        write and where the system will. If the file exists but has no managed
        block yet, one is appended — again leaving prior content untouched.
        """
        path = self.path_for(doc)
        begin = _BEGIN.format(section=section)
        end = _END.format(section=section)
        managed = f"{begin}\n{content.strip()}\n{end}"

        if not path.exists():
            new_text = self._new_document(doc, managed, source)
        else:
            existing = path.read_text(encoding="utf-8")
            if self._has_block(existing, section):
                new_text = self._replace_block(existing, section, managed)
            else:
                # Append a managed block; never disturb what the human wrote.
                sep = "" if existing.endswith("\n") else "\n"
                new_text = f"{existing}{sep}\n{managed}\n"

        self._atomic_write(path, new_text)
        log.debug(
            "world_model.updated",
            document=doc.value,
            section=section,
            source=source,
            managed_chars=len(content),
        )

    def _new_document(self, doc: BeliefDocument, managed: str, source: str) -> str:
        return (
            f"# {doc.title}\n\n"
            f"_This file is shared between you and the runtime._\n"
            f"_Text outside the managed block below is yours and is never "
            f"overwritten._\n\n"
            f"{managed}\n"
        )

    # -- block surgery -----------------------------------------------------

    def _block_re(self, section: str) -> re.Pattern[str]:
        return re.compile(
            _BLOCK_RE_TMPL.format(
                begin=re.escape(_BEGIN.format(section=section)),
                end=re.escape(_END.format(section=section)),
            ),
            re.DOTALL,
        )

    def _has_block(self, text: str, section: str) -> bool:
        return self._block_re(section).search(text) is not None

    def _extract_block(self, text: str, section: str) -> str | None:
        match = self._block_re(section).search(text)
        return match.group(1).strip() if match else None

    def _replace_block(self, text: str, section: str, managed: str) -> str:
        return self._block_re(section).sub(lambda _: managed, text, count=1)

    def _atomic_write(self, path: Path, text: str) -> None:
        """Write via a temp file + os.replace so a crash cannot truncate.

        os.replace is atomic on both POSIX and Windows, so a reader either sees
        the whole old file or the whole new one — never a partial write.
        """
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    # -- episodic compression (§12 abstraction ladder) ---------------------

    def compress_episodes(
        self,
        observations: list[str],
        *,
        summarizer: object | None = None,
        min_pattern_support: int = 2,
    ) -> list[EpisodeSummary]:
        """Map raw observations up the ladder: observation → pattern → principle.

        Deterministic by default (frequency of recurring phrases), with an
        optional injected ``summarizer`` callable for LLM-assisted compression.
        The deterministic path keeps token overhead at zero and is what runs
        when no model is available — the compression must not depend on one.

        Returns the surviving rungs, highest abstraction last. A phrase that
        recurs at least ``min_pattern_support`` times becomes a *pattern*; the
        most frequent patterns become *principles*.
        """
        if not observations:
            return []

        if summarizer is not None:
            try:
                text = summarizer(observations)  # type: ignore[operator]
                return [
                    EpisodeSummary(
                        level="principle", text=str(text), evidence_count=len(observations)
                    )
                ]
            except Exception as exc:
                log.warning("world_model.summarizer_failed", error=str(exc))
                # Fall through to the deterministic path.

        # Deterministic: count recurring normalised lines.
        counts: Counter[str] = Counter(_normalise(o) for o in observations if o.strip())
        summaries: list[EpisodeSummary] = [
            EpisodeSummary(level="observation", text=o.strip(), evidence_count=1)
            for o in observations
            if o.strip()
        ]
        patterns = [
            EpisodeSummary(level="pattern", text=phrase, evidence_count=n)
            for phrase, n in counts.most_common()
            if n >= min_pattern_support
        ]
        summaries.extend(patterns)
        if patterns:
            top = patterns[0]
            summaries.append(
                EpisodeSummary(
                    level="principle",
                    text=f"Recurring theme: {top.text}",
                    evidence_count=top.evidence_count,
                )
            )
        return summaries

    def render_summaries(self, summaries: list[EpisodeSummary]) -> str:
        """Render compressed episodes as markdown for a managed block."""
        by_level: dict[str, list[EpisodeSummary]] = {}
        for s in summaries:
            by_level.setdefault(s.level, []).append(s)
        lines: list[str] = []
        for level in ("principle", "pattern", "observation"):
            items = by_level.get(level, [])
            if not items:
                continue
            lines.append(f"### {level.title()}s")
            for item in items:
                suffix = f" _(x{item.evidence_count})_" if item.evidence_count > 1 else ""
                lines.append(f"- {item.text}{suffix}")
            lines.append("")
        return "\n".join(lines).strip()


@dataclass(slots=True)
class _Themes:
    """Internal: accumulates observations toward operating themes over a run."""

    observations: list[str] = field(default_factory=list)
    updated_at: datetime | None = None


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for grouping."""
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", text.lower())).strip()
