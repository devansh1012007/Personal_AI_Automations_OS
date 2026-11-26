"""The patch proposal — the guarded loop's deterministic middle artifact.

C8.1's second stage. A proposal is *described change*: which files, with
what full content, for what intent, with what rationale. It is produced
deterministically from a :class:`~paa.improvement.reflection.ReflectionReport`
(the reflection engine's own numbers and its own rule template — see
:meth:`proposal_from_report`), it is never applied by the stage that
produced it, and every later stage (validation, human gate, commit)
consumes it by hash (:attr:`PatchProposal.sha256`) so the artifact a
reviewer reads is the artifact the pipeline acted on.

Two honesty rules shape the type:

* **The content is the whole file, not a diff.** A unified diff implies a
  patch algebra (hunks, offsets, fuzz) that this loop has no business
  reimplementing; a full-content change is idempotent to apply, trivially
  reviewable, and cannot silently merge into a file that changed since the
  proposal was made — :meth:`PatchProposal.apply` refuses when the target
  on disk no longer hashes to the content the proposal was built against.
* **No model is involved.** The proposal text is the reflection engine's
  deterministic template. The honesty line for the whole loop (ADR-0017):
  prototypical few-shot classification ≠ gradient meta-learning — and this
  artifact is not even that; it is a deterministic function of the ledger's
  friction numbers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.improvement.reflection import DomainFriction, ReflectionEngine
from paa.ledger.events import canonical_json

__all__ = [
    "DEFAULT_TARGET_FILE",
    "PROPOSAL_SCHEMA",
    "FileChange",
    "PatchProposal",
    "proposal_from_report",
    "read_proposal",
    "write_proposal",
]

#: The proposal artifact's schema tag. A JSON file that does not carry it
#: is refused at load — an artifact the loop cannot interpret is not a
#: proposal, whatever its filename says.
PROPOSAL_SCHEMA = "paa-improve-proposal/1"

#: Where the deterministic proposal lands inside a repository, relative to
#: its root. The playbook is the surface the reflection engine already
#: owns (the vault's ``playbooks.md``; the same idea, stated for a
#: repository the loop has been pointed at), so the guarded loop's first
#: writable surface is the same file the analysis-only engine writes in
#: its own home.
DEFAULT_TARGET_FILE = "playbooks.md"


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file's new content in full, with the content it replaces.

    ``prior_sha256`` is the digest of the file as it existed when the
    proposal was synthesised; :meth:`PatchProposal.apply` refuses a target
    that no longer hashes to it, so a proposal cannot silently overwrite a
    file someone else changed after the loop looked at it.
    """

    path: str
    content: str
    prior_sha256: str

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "prior_sha256": self.prior_sha256,
        }


@dataclass(frozen=True, slots=True)
class PatchProposal:
    """A described change: files, intent, rationale — nothing applied.

    ``sha256`` (over the canonical JSON of the whole proposal) is the
    identity every later stage consumes: the validation call's params, the
    human gate's question, the commit's ledger row and the attested
    bundle's mandate all name the digest, so a reviewer can check that the
    thing committed is the thing validated is the thing the human saw.
    """

    intent: str
    rationale: str
    changes: tuple[FileChange, ...] = ()
    domain: str = ""
    reflection_window: dict[str, str] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_json()).encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": PROPOSAL_SCHEMA,
            "intent": self.intent,
            "rationale": self.rationale,
            "domain": self.domain,
            "reflection_window": dict(self.reflection_window),
            "changes": [change.to_json() for change in self.changes],
        }

    def apply(self, root: Path) -> list[Path]:
        """Write the proposal's files under ``root``; return what landed.

        All-or-nothing *validation* before any write: every target must be
        a named file inside ``root`` (nothing escaping it, nothing under
        ``.git``), and every existing target must still hash to its
        ``prior_sha256``. Only then are the files written, each atomically
        (temp file + ``os.replace``), so a crash mid-apply leaves complete
        files, never a truncated one. A refusal raises
        :class:`~paa.core.errors.PaaError` naming the offending path.
        """
        root = Path(root)
        written: list[Path] = []
        resolved: list[tuple[Path, str]] = []
        for change in self.changes:
            target = _resolve_target(root, change.path)
            if target.is_dir():
                raise PaaError(
                    f"proposal target {change.path!r} is a directory; the "
                    "guarded loop writes named files only"
                )
            if target.exists():
                current = hashlib.sha256(target.read_bytes()).hexdigest()
                if current != change.prior_sha256:
                    raise PaaError(
                        f"proposal target {change.path!r} changed since the "
                        "proposal was synthesised (content hash differs from "
                        "the recorded prior); refusing to overwrite — "
                        "re-run reflection to propose against the new state"
                    )
            resolved.append((target, change.content))
        for target, content in resolved:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.improve-tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
            written.append(target)
        return written


def _resolve_target(root: Path, path: str) -> Path:
    """One proposal target, jailed to a named file under ``root``."""
    if not path or Path(path).is_absolute():
        raise PaaError(f"proposal target {path!r} must be a relative file path")
    target = (root / path).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise PaaError(f"proposal target {path!r} escapes the repository root")
    if ".git" in target.relative_to(root_resolved).parts:
        raise PaaError(f"proposal target {path!r} is inside the git directory")
    return target


def proposal_from_report(
    engine: ReflectionEngine,
    friction: DomainFriction,
    *,
    repo_root: Path,
    target_file: str = DEFAULT_TARGET_FILE,
) -> PatchProposal:
    """Synthesise the deterministic proposal for one high-friction domain.

    The change is the reflection engine's own anti-pattern rule
    (:meth:`ReflectionEngine.synthesize_rule` — the same deterministic
    template the weekly engine writes into the vault's playbook) appended
    under a managed marker in the target repository's playbook file. The
    intent and rationale restate the friction numbers that caused the
    domain to be selected, so a reviewer reading only the proposal can
    audit the selection. Nothing is applied here; the marker fence keeps
    human-written playbook text untouched, the same property the vault's
    fence gives the weekly engine (ADR-0038's fence precedent).
    """
    if not friction.is_high_friction:
        raise PaaError(
            f"domain {friction.domain!r} is not high-friction "
            f"(score {friction.score} below the reflection threshold); the "
            "guarded loop proposes only for domains reflection flagged"
        )
    target = Path(repo_root) / target_file
    prior = target.read_text(encoding="utf-8") if target.exists() else ""
    rule = engine.synthesize_rule(friction).rstrip("\n")
    content = _append_managed_rule(prior, rule)
    rationale = (
        f"reflection counted {friction.corrections} correction(s), "
        f"{friction.rollbacks} rollback(s) and {friction.successes} clean "
        f"commit(s) for domain {friction.domain!r} in the window "
        f"(operational friction {friction.score}); the anti-pattern entry "
        "routes future tasks in this domain through a human gate and a "
        "dry-run before mutating."
    )
    intent = (
        f"record the {friction.domain!r} anti-pattern in {target_file} so "
        "the next similar task starts from the lesson, not from the friction"
    )
    return PatchProposal(
        intent=intent,
        rationale=rationale,
        changes=(
            FileChange(
                path=target_file,
                content=content,
                prior_sha256=hashlib.sha256(prior.encode("utf-8")).hexdigest(),
            ),
        ),
        domain=friction.domain,
        reflection_window={},
    )


#: The managed-marker fence, matching the vault playbook's shape: the
#: guarded loop only ever writes between these two lines, and everything
#: outside them is human text it must preserve.
_MARKER_BEGIN = "<!-- managed:improve-reflection-loop begin -->"
_MARKER_END = "<!-- managed:improve-reflection-loop end -->"


def _append_managed_rule(prior: str, rule: str) -> str:
    """Append one rule inside the managed fence, preserving all other text.

    Accumulating, not idempotent: a second proposal for the same domain
    appends a second entry inside the fence (the reflection engine's own
    weekly behaviour), and a fence a human edited is preserved verbatim —
    the loop only ever appends between the markers, creating the fence at
    the end of the file when no fence exists.
    """
    if _MARKER_BEGIN in prior and _MARKER_END in prior:
        head, _, rest = prior.partition(_MARKER_BEGIN)
        existing, _, tail = rest.partition(_MARKER_END)
        fenced = existing.strip("\n")
        fenced = f"{fenced}\n\n{rule}" if fenced else rule
        return f"{head}{_MARKER_BEGIN}\n{fenced}\n{_MARKER_END}{tail}"
    separator = "\n\n" if prior.strip() else ""
    return f"{prior}{separator}{_MARKER_BEGIN}\n{rule}\n{_MARKER_END}\n"


def write_proposal(proposal: PatchProposal, path: Path) -> Path:
    """Write the proposal artifact as JSON, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(proposal.to_json(), indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def read_proposal(path: Path) -> PatchProposal:
    """Load a proposal artifact; a foreign schema is refused by name."""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PaaError(f"{path} is not a readable proposal artifact: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != PROPOSAL_SCHEMA:
        raise PaaError(
            f"{path} is not a {PROPOSAL_SCHEMA} artifact; the guarded loop "
            "refuses to act on a proposal it cannot interpret"
        )
    changes_raw = raw.get("changes")
    if not isinstance(changes_raw, list) or not changes_raw:
        raise PaaError(f"{path} carries no changes; an empty proposal is not one")
    changes: list[FileChange] = []
    for item in changes_raw:
        if not isinstance(item, dict):
            raise PaaError(f"{path} has a change entry that is not an object")
        try:
            changes.append(
                FileChange(
                    path=str(item["path"]),
                    content=str(item["content"]),
                    prior_sha256=str(item["prior_sha256"]),
                )
            )
        except KeyError as exc:
            raise PaaError(f"{path} has a change entry missing {exc.args[0]!r}") from exc
    window = raw.get("reflection_window")
    return PatchProposal(
        intent=str(raw.get("intent") or ""),
        rationale=str(raw.get("rationale") or ""),
        changes=tuple(changes),
        domain=str(raw.get("domain") or ""),
        reflection_window=dict(window) if isinstance(window, dict) else {},
    )
