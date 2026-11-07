"""The Cognitive World Model — long-horizon belief state in the markdown vault.

RFC §12. The runtime does not treat history as a flat list of log lines; it
maintains fifteen living documents that abstract day-to-day signal into
strategic context (ADR-0038, PLAN:integration-fix:P010): the thirteen RFC §9
files plus the two live-only files (``strategic_risks.md``,
``active_constraints.md``) that predate the RFC union:

* ``constitution.md`` / ``principles.md`` — the governance-adjacent RFC files
* ``goals.md`` / ``anti_goals.md`` — what the operator wants and refuses
* ``operating_themes.md`` — architectural preferences and behavioural heuristics
* ``current_focus.md`` — active milestones and what is being worked on now
* ``projects.md`` / ``skills.md`` / ``playbooks.md`` / ``reflections.md`` —
  work state and distilled lessons
* ``risk_rules.md`` / ``agent_hierarchy.md`` / ``prompt_contracts.md`` —
  policy surfaces the runtime must honour
* ``strategic_risks.md`` — recurring failures, hardware limits, bottlenecks
* ``active_constraints.md`` — resource limits, token/VRAM budgets, schedules

Two further files, ``identity.md`` and ``strategy.md``, are governance-only
and live under ``vault/governance/`` (see :meth:`WorldModel.governance_path`);
they are deliberately not enum members.

The hard requirement (RFC §9: markdown is "the strategic human interface") is
that the system may rewrite *its own* managed section of each file without ever
touching text the human wrote. That is enforced with marker fences: the runtime
owns only the bytes between ``<!-- paa:managed:BEGIN section -->`` and the
matching ``END``; everything outside survives every update untouched. Writes are
atomic (temp file + ``os.replace``) so a crash mid-write cannot leave a half a
document — the previous version stays intact until the new one is fully on disk.

The fence is also a conflict error, never last-writer (ADR-0038): a human edit
*inside* the managed block since the last atomic write raises
:class:`VaultConflictError` naming the path and the files are not overwritten.
Edits outside the block merge-preserve as always. "Since the last atomic
write" is decided by read-compare against a derived write record
(``.paa-managed-hashes.json`` in the vault root, keyed by ``filename:section``,
holding the SHA-256 of the managed content the last write put inside the
fence): a block whose content differs from the record was edited by someone
other than the writer. A file with no record yet (one that predates tracking,
or whose record was lost) is adopted as the baseline on its first tracked
write — that is the one-time cost of turning tracking on — and conflicts are
detected from then on.
"""

from __future__ import annotations

import enum
import hashlib
import json
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

__all__ = ["BeliefDocument", "EpisodeSummary", "VaultConflictError", "WorldModel"]

log = structlog.get_logger(__name__)


class BeliefDocument(str, enum.Enum):
    """The fifteen managed belief-state files (ADR-0038, PLAN:integration-fix:P010).

    The set is the thirteen RFC §9 files plus the two live-only files
    (``strategic_risks``, ``active_constraints``); governance-only files are
    deliberately not members — see :meth:`WorldModel.governance_path`. The
    extension is additive (no member removed), so enum string coercion
    accepts strictly more filenames than before. All fifteen resolve flat
    under the vault root; ``path_for`` stays a flat join for managed docs.
    """

    CURRENT_FOCUS = "current_focus.md"
    STRATEGIC_RISKS = "strategic_risks.md"
    ACTIVE_CONSTRAINTS = "active_constraints.md"
    OPERATING_THEMES = "operating_themes.md"
    CONSTITUTION = "constitution.md"
    PRINCIPLES = "principles.md"
    GOALS = "goals.md"
    ANTI_GOALS = "anti_goals.md"
    PROJECTS = "projects.md"
    SKILLS = "skills.md"
    PLAYBOOKS = "playbooks.md"
    REFLECTIONS = "reflections.md"
    RISK_RULES = "risk_rules.md"
    AGENT_HIERARCHY = "agent_hierarchy.md"
    PROMPT_CONTRACTS = "prompt_contracts.md"

    @property
    def display_title(self) -> str:
        """The document's human-readable name, for the markdown header.

        Deliberately *not* called ``title``. The enum mixes in ``str``, so
        ``title`` is already ``str.title`` — a method — and shadowing it
        with a property replaces a callable with a plain string. Every
        consumer that treats a ``BeliefDocument`` as the string it is would
        then get ``TypeError: 'str' object is not callable`` the moment it
        called ``doc.title()``, which is exactly what the inherited method
        promises. The two concepts are not the same thing either: a
        document's title is "Current Focus", not the title-cased filename
        "Current_Focus.Md" that ``str.title()`` produces.
        """
        return self.value.removesuffix(".md").replace("_", " ").title()


#: Governance-only vault files (PLAN:integration-fix:P010, ADR-0038). Plan-
#: original placement, not RFC §9: the RFC mentions ``identity``/``strategy``
#: only as domain-table rows, so putting them in the enum would invent an
#: RFC membership they do not have. Closed set: the membership IS the
#: traversal guard in :meth:`WorldModel.governance_path`.
GOVERNANCE_FILES: frozenset[str] = frozenset({"identity.md", "strategy.md"})

#: Derived write record in the vault root (PLAN:integration-fix:P010). Not
#: a managed doc: it exists so the fence can tell whether the bytes inside
#: the fence are still the writer's. Hidden (dotfile) and JSON, never .md,
#: so vault listings of the managed set are undisturbed. It is rebuildable
#: bookkeeping — corruption or deletion degrades to adopt-on-next-write
#: (see WorldModel._load_hashes), never to blocking the writer.
_HASHES_FILENAME = ".paa-managed-hashes.json"


class VaultConflictError(ValueError):
    """A fence write was refused because a human edit is in the way.

    Subclasses :class:`ValueError` so existing callers that already handle
    the coercion/marker error paths keep working. Carries the offending
    ``path`` and ``section``: the error names the path, per ADR-0038 — the
    files are never overwritten to get past it.
    """

    def __init__(self, path: Path, section: str, detail: str) -> None:
        self.path = path
        self.section = section
        super().__init__(
            f"vault fence conflict in {path} (section {section!r}): {detail}"
        )


def _content_hash(text: str) -> str:
    """SHA-256 (hex) of a managed block body, the conflict-comparison key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    def __init__(self, vault_path: Path | str, *, sync_enabled: bool | None = None) -> None:
        """Create a vault handle, creating the directory if needed.

        ``sync_enabled`` is the sync-entry gate (PLAN:integration-fix:P010):
        ``False`` makes :meth:`update_belief_state` leave every file intact
        and return quietly, so ``vault_sync.enabled=False`` is the switch
        that keeps sync attempts from touching the vault. ``None`` (the
        default) allows writes — the existing behaviour every current caller
        relies on: the reflection engine's playbook write is an internal
        learning write governed by its own settings, not vault sync, so a
        future sync loop wires ``settings.vault_sync.enabled`` into this
        param when it lands; nothing reads global settings here.
        """
        self._vault = Path(vault_path)
        self._vault.mkdir(parents=True, exist_ok=True)
        self._sync_enabled = sync_enabled

    def path_for(self, doc: BeliefDocument) -> Path:
        return self._vault / doc.value

    def governance_path(self, filename: str) -> Path:
        """Resolve a governance-only file under ``vault/governance/``.

        The managed fifteen stay flat in the vault root (``path_for`` is a
        flat join for enum docs); the two governance files get their own
        subdir so managed-sync tooling never picks them up as managed docs.
        The closed set is the traversal guard: any name outside
        ``GOVERNANCE_FILES`` — including paths with ``..`` or separators —
        is rejected instead of joined onto the vault.
        """
        if filename not in GOVERNANCE_FILES:
            raise ValueError(f"not a governance file: {filename!r}")
        return self._vault / "governance" / filename

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

        PLAN:integration-fix:P010 (ADR-0038): a human edit inside the managed
        block since the last atomic write raises :class:`VaultConflictError`
        naming the path — never a silent overwrite. Edits outside the block
        merge-preserve. The write record is updated only after the document
        is fully on disk, so a crash between the two leaves a *false*
        conflict on the next write (recoverable by deleting the record /
        re-adopting), never a lost edit.
        """
        if self._sync_enabled is False:
            log.info("world_model.sync_disabled", document=doc.value, section=section)
            return
        begin_marker = _BEGIN.format(section=section)
        end_marker = _END.format(section=section)
        if begin_marker in content or end_marker in content:
            # SECURITY: the managed content must never carry the fence itself —
            # a caller building text from untrusted input (e.g. a ledger
            # payload) would otherwise smuggle markers into the block and
            # corrupt later block surgery. Reject at the boundary.
            raise ValueError(
                f"managed content for {doc.value} (section {section!r}) contains fence markers"
            )

        path = self.path_for(doc)
        begin = _BEGIN.format(section=section)
        end = _END.format(section=section)
        managed = f"{begin}\n{content.strip()}\n{end}"

        if not path.exists():
            new_text = self._new_document(doc, managed, source)
        else:
            existing = path.read_text(encoding="utf-8")
            self._check_marker_hygiene(path, section, existing)
            if self._has_block(existing, section):
                self._check_conflict(path, section, existing)
                new_text = self._replace_block(existing, section, managed)
            else:
                # Append a managed block; never disturb what the human wrote.
                sep = "" if existing.endswith("\n") else "\n"
                new_text = f"{existing}{sep}\n{managed}\n"

        self._atomic_write(path, new_text)
        self._record_hash(path, section, content.strip())
        log.debug(
            "world_model.updated",
            document=doc.value,
            section=section,
            source=source,
            managed_chars=len(content),
        )

    def _new_document(self, doc: BeliefDocument, managed: str, source: str) -> str:
        return (
            f"# {doc.display_title}\n\n"
            f"_This file is shared between you and the runtime._\n"
            f"_Text outside the managed block below is yours and is never "
            f"overwritten._\n"
            # PLAN:integration-fix:P010 (ADR-0038): the writer's identity is
            # part of the generated header, so "who wrote this block?" is
            # answerable when diagnosing a sync conflict.
            f"_Managed block below is maintained by the runtime (source: {source})._\n\n"
            f"{managed}\n"
        )

    # -- conflict detection --------------------------------------------------

    def _check_marker_hygiene(self, path: Path, section: str, text: str) -> None:
        """Reject injected/ambiguous fences before any surgery.

        Exactly zero or one BEGIN/END pair per section may exist in the file.
        More (pasted marker text) or a dangling marker would make
        ``count=1`` replacement ambiguous — the first match could be human
        text the writer must not treat as its own block.
        """
        begins = text.count(_BEGIN.format(section=section))
        ends = text.count(_END.format(section=section))
        if begins != ends or begins > 1:
            raise VaultConflictError(
                path,
                section,
                f"ambiguous fence markers ({begins} BEGIN / {ends} END) — refusing write",
            )

    def _check_conflict(self, path: Path, section: str, text: str) -> None:
        """Refuse the write when the block changed since the last atomic write.

        Read-compare against the write record (ADR-0038): if the on-disk
        block body no longer hashes to the record, someone else edited
        inside the fence. No record (pre-tracking file, lost record) means
        adopt — the first tracked write establishes the baseline.
        """
        recorded = self._load_hashes().get(f"{path.name}:{section}")
        if recorded is None:
            return
        current = self._extract_block(text, section)
        if current is None:
            return
        if _content_hash(current) != recorded:
            log.warning("world_model.fence_conflict", path=str(path), section=section)
            raise VaultConflictError(
                path,
                section,
                "human edit inside the managed block since the last write "
                "— refusing overwrite",
            )

    def _load_hashes(self) -> dict[str, str]:
        """Read the derived write record; degrade to empty on any problem.

        The record is bookkeeping, not truth: a missing, corrupt or
        wrong-shaped file must never block a write. Degrading to "no
        record" means adopt-on-next-write, which is recoverable and visible
        (the corruption is logged) instead of an outage.
        """
        path = self._vault / _HASHES_FILENAME
        if not path.exists():
            return {}
        try:
            # ValueError covers both JSONDecodeError and UnicodeDecodeError
            # (a record of non-UTF-8 bytes is corrupt bookkeeping, not an
            # outage); OSError covers an unreadable file.
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            log.warning("world_model.hashes_unreadable", error=str(exc))
            return {}
        if not isinstance(data, dict):
            log.warning("world_model.hashes_unreadable", error="not a JSON object")
            return {}
        out: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str):
                out[key] = value
        return out

    def _record_hash(self, path: Path, section: str, body: str) -> None:
        """Persist the hash of what the last atomic write put in the fence.

        Called only after the document write committed (data first,
        bookkeeping second). Concurrent writers share one record file via
        atomic replace — last-writer on the record. The conflict check is a
        guard, not a lock, and the vault is single-node filesystem state.
        """
        hashes = self._load_hashes()
        hashes[f"{path.name}:{section}"] = _content_hash(body)
        self._atomic_write(
            self._vault / _HASHES_FILENAME,
            json.dumps(hashes, sort_keys=True, ensure_ascii=False),
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
        the whole old file or the whole new one — never a partial write. A
        failed write removes its temp file: the old version stays intact and
        no ``.*.tmp`` residue accumulates in the vault.
        """
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            # Best-effort cleanup: if the unlink itself fails there is
            # nothing better to do — the original error is the one the
            # caller must see. The destination was never touched.
            tmp.unlink(missing_ok=True)
            raise

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
