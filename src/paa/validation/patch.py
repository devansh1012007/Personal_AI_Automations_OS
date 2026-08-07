"""Unified-diff parsing and transactional application, in pure Python.

Why not shell out to ``patch`` or ``git apply``
-----------------------------------------------
Neither exists reliably on the target machine. Windows has no ``patch(1)``, and
``git`` may not be on ``PATH`` even where it is installed. A validation layer
that silently degrades to "cannot check" on the deployment platform is not a
validation layer, so hunk parsing and application are implemented here.

The secondary benefit is decisive for RFC §13: this code decides *by itself*
whether a patch is safe, rather than handing attacker-influenced text to an
external binary with its own flag surface, its own path handling and its own
fuzz behaviour. ``git apply`` will happily follow a symlink; we will not.

The security boundary
---------------------
:func:`safe_relative_path` is the one function in this module that must be
correct. A patch is agent-authored text, and the paths inside it are the most
directly attacker-controlled input the runtime accepts. Four escapes are
blocked explicitly, because each has been a real CVE class in archive and patch
tooling:

1. ``../`` traversal — including forms that only traverse after normalisation;
2. absolute paths — POSIX ``/etc/...``, Windows ``C:\\...``, UNC ``\\\\host\\``;
3. **symlink escape** — a relative path that stays relative but resolves
   through a symlink to somewhere outside the workspace. This is the one a
   string-only check misses, and it is why containment is asserted on the
   *resolved* path, not the written one;
4. NUL and control characters, which truncate paths inside C-level APIs so the
   string that gets validated and the string that gets opened differ.

Transactionality
----------------
:meth:`UnifiedDiffValidator.dry_run` computes the **complete** post-patch
content of every file before a single byte is written. Application is then a
sequence of atomic replaces of already-computed content. A hunk that does not
apply therefore fails during planning, when nothing has changed yet — the
"half-applied patch" state cannot be reached through a rejected hunk.

Multi-file application is still not atomic at the OS level (no filesystem
offers a cross-file transaction), so :meth:`PatchApplier.apply` keeps a
rollback journal of exact prior bytes and unwinds automatically if a later
write fails.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from paa.core.errors import ValidationError

__all__ = [
    "FilePatch",
    "Hunk",
    "PatchAction",
    "PatchApplier",
    "PatchJournal",
    "PatchPlan",
    "PatchPlanEntry",
    "UnifiedDiffValidator",
    "safe_relative_path",
]

log = structlog.get_logger(__name__)

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DEV_NULL = frozenset({"/dev/null", "dev/null", "nul", "NUL"})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")

#: Line offset tolerance when locating a hunk.
#:
#: Position is fuzzed; **content is never fuzzed**. The context and removed
#: lines must still match byte for byte — only the line number is allowed to
#: drift. This matters because patches here are LLM-authored and line-number
#: arithmetic is exactly what a language model gets wrong, while the code it
#: quotes is usually verbatim. Rejecting on a miscounted ``@@`` header would
#: fail correct patches; fuzzing *content* would apply wrong ones.
DEFAULT_MAX_LINE_OFFSET = 20


class PatchAction(str, Enum):
    """What a patch does to one file."""

    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``@@`` block."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]
    """Body lines *with* their leading ``' '``/``'-'``/``'+'`` marker."""

    section: str = ""
    new_no_newline: bool = False
    old_no_newline: bool = False

    @property
    def source_lines(self) -> list[str]:
        """Lines that must be present before applying (context + removed)."""
        return [line[1:] for line in self.lines if line[:1] in (" ", "-")]

    @property
    def target_lines(self) -> list[str]:
        """Lines present after applying (context + added)."""
        return [line[1:] for line in self.lines if line[:1] in (" ", "+")]

    @property
    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"

    def counts_consistent(self) -> bool:
        """Whether the body matches the counts the header declares.

        A mismatch means the diff is malformed — usually a model that wrote a
        plausible header and then emitted a different number of lines. Caught
        during planning so it can never half-apply.
        """
        return (
            len(self.source_lines) == self.old_count and len(self.target_lines) == self.new_count
        )


@dataclass(frozen=True, slots=True)
class FilePatch:
    """All hunks targeting one file."""

    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]

    @property
    def action(self) -> PatchAction:
        if self.old_path is None:
            return PatchAction.CREATE
        if self.new_path is None:
            return PatchAction.DELETE
        return PatchAction.MODIFY

    @property
    def path(self) -> str:
        """The path this patch ultimately affects."""
        result = self.new_path or self.old_path
        if result is None:  # pragma: no cover - parser never emits this
            raise ValidationError("file patch has neither an old nor a new path")
        return result


@dataclass(slots=True)
class PatchPlanEntry:
    """Planned outcome for one file."""

    relative_path: str
    action: PatchAction
    applies_cleanly: bool
    reason: str | None = None
    new_content: bytes | None = None
    """Fully computed post-patch bytes. ``None`` for a deletion or a failure."""

    prior_sha256: str | None = None
    new_sha256: str | None = None
    hunks_applied: int = 0
    line_offset: int = 0
    """How far the hunks had to shift from their declared position."""

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "action": self.action.value,
            "applies_cleanly": self.applies_cleanly,
            "reason": self.reason,
            "prior_sha256": self.prior_sha256,
            "new_sha256": self.new_sha256,
            "hunks_applied": self.hunks_applied,
            "line_offset": self.line_offset,
        }


@dataclass(slots=True)
class PatchPlan:
    """The full outcome of a dry run."""

    workspace: Path
    entries: list[PatchPlanEntry] = field(default_factory=list)
    patch_sha256: str = ""
    rejected: list[str] = field(default_factory=list)
    """Reasons the patch was refused outright (path escapes, parse failures)."""

    @property
    def ok(self) -> bool:
        """Whether every hunk applies and nothing was rejected."""
        return not self.rejected and bool(self.entries) and all(
            entry.applies_cleanly for entry in self.entries
        )

    @property
    def files_changed(self) -> list[str]:
        return [entry.relative_path for entry in self.entries]

    def failures(self) -> list[PatchPlanEntry]:
        return [entry for entry in self.entries if not entry.applies_cleanly]

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "patch_sha256": self.patch_sha256,
            "ok": self.ok,
            "rejected": list(self.rejected),
            "entries": [entry.to_payload() for entry in self.entries],
        }


@dataclass(slots=True)
class _JournalEntry:
    relative_path: str
    existed_before: bool
    prior_bytes: bytes | None


@dataclass(slots=True)
class PatchJournal:
    """Undo log capturing exact prior bytes for every touched file.

    Prior content is held in memory rather than copied to a backup directory:
    rollback must work when the failure being recovered from is a *disk* error,
    and a rollback that needs the failing disk to succeed is not a rollback.
    Patches are bounded in size by the planner, so the memory cost is bounded
    too.
    """

    workspace: Path
    patch_sha256: str
    entries: list[_JournalEntry] = field(default_factory=list)

    @property
    def touched_paths(self) -> list[str]:
        return [entry.relative_path for entry in self.entries]

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "patch_sha256": self.patch_sha256,
            "touched": self.touched_paths,
            "entry_count": len(self.entries),
        }


# ---------------------------------------------------------------------------
# Path safety — the security boundary
# ---------------------------------------------------------------------------


def safe_relative_path(raw: str, workspace: Path) -> Path:
    """Resolve a diff path inside ``workspace``, or raise.

    Raises :class:`~paa.core.errors.ValidationError` on any escape. Read the
    module docstring for the four escape classes this blocks and why the
    resolved-path check (rather than a string check) is the one that matters.
    """
    if not raw or not raw.strip():
        raise ValidationError("patch contains an empty file path")

    candidate = raw.strip()
    if _CONTROL_CHARS.search(candidate):
        raise ValidationError(
            "patch path contains control characters", path=repr(candidate)[:120]
        )

    # Strip git's a/ b/ prefixes, but only as whole leading segments — a real
    # directory named "abc/" must not lose its first character.
    for prefix in ("a/", "b/", "a\\", "b\\"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break

    candidate = candidate.replace("\\", "/")

    if candidate.startswith("//") or candidate.startswith("\\\\"):
        raise ValidationError("patch path is a UNC path", path=candidate)
    if candidate.startswith("/"):
        raise ValidationError("patch path is absolute", path=candidate)
    if _WINDOWS_ABSOLUTE.match(candidate):
        raise ValidationError("patch path is an absolute Windows path", path=candidate)

    parts = [part for part in candidate.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValidationError("patch path contains a traversal segment", path=candidate)
    if not parts:
        raise ValidationError("patch path resolves to the workspace root", path=candidate)

    relative = Path(*parts)
    workspace_real = Path(os.path.realpath(workspace))
    target_real = Path(os.path.realpath(workspace_real / relative))

    # THE check. realpath has followed every symlink in the existing ancestor
    # chain, so a path that is textually relative but physically outside the
    # workspace is caught here and nowhere earlier.
    if target_real != workspace_real and not target_real.is_relative_to(workspace_real):
        raise ValidationError(
            "patch path escapes the workspace after symlink resolution",
            path=candidate,
            resolved=str(target_real),
            workspace=str(workspace_real),
        )
    return relative


# ---------------------------------------------------------------------------
# Parsing and planning
# ---------------------------------------------------------------------------


class UnifiedDiffValidator:
    """Parses unified diffs and plans their application."""

    def __init__(self, *, max_line_offset: int = DEFAULT_MAX_LINE_OFFSET) -> None:
        self.max_line_offset = max_line_offset

    # -- parsing -----------------------------------------------------------

    def parse(self, patch_text: str) -> list[FilePatch]:
        """Parse a unified diff into per-file patches.

        Raises :class:`~paa.core.errors.ValidationError` on structurally
        invalid input. Refusing to parse is the right response to a malformed
        patch: guessing what a broken diff meant is how a "helpful" patcher
        writes something nobody asked for.
        """
        lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        patches: list[FilePatch] = []

        old_path: str | None = None
        new_path: str | None = None
        hunks: list[Hunk] = []
        current: list[str] = []
        header: tuple[int, int, int, int, str] | None = None
        new_no_newline = False
        old_no_newline = False
        index = 0

        def flush_hunk() -> None:
            nonlocal current, header, new_no_newline, old_no_newline
            if header is None:
                return
            o_start, o_count, n_start, n_count, section = header
            hunks.append(
                Hunk(
                    old_start=o_start,
                    old_count=o_count,
                    new_start=n_start,
                    new_count=n_count,
                    lines=tuple(current),
                    section=section,
                    new_no_newline=new_no_newline,
                    old_no_newline=old_no_newline,
                )
            )
            current, header = [], None
            new_no_newline = old_no_newline = False

        def flush_file() -> None:
            nonlocal old_path, new_path, hunks
            flush_hunk()
            if old_path is not None or new_path is not None:
                if not hunks:
                    raise ValidationError(
                        "patch declares a file but contains no hunks",
                        path=new_path or old_path,
                    )
                patches.append(
                    FilePatch(old_path=old_path, new_path=new_path, hunks=tuple(hunks))
                )
            old_path, new_path, hunks = None, None, []

        while index < len(lines):
            line = lines[index]

            if line.startswith("--- "):
                flush_file()
                raw = line[4:].split("\t")[0].strip()
                old_path = None if raw in _DEV_NULL else raw
                index += 1
                if index >= len(lines) or not lines[index].startswith("+++ "):
                    raise ValidationError(
                        "patch has a '---' header with no matching '+++'", line_number=index
                    )
                raw_new = lines[index][4:].split("\t")[0].strip()
                new_path = None if raw_new in _DEV_NULL else raw_new
                if old_path is None and new_path is None:
                    raise ValidationError("patch maps /dev/null to /dev/null", line_number=index)
                index += 1
                continue

            if (match := _HUNK_HEADER.match(line)) is not None:
                if old_path is None and new_path is None:
                    raise ValidationError(
                        "patch has a hunk before any file header", line_number=index
                    )
                flush_hunk()
                header = (
                    int(match.group(1)),
                    int(match.group(2)) if match.group(2) is not None else 1,
                    int(match.group(3)),
                    int(match.group(4)) if match.group(4) is not None else 1,
                    match.group(5).strip(),
                )
                index += 1
                continue

            if header is not None:
                if line.startswith("\\"):
                    # "\ No newline at end of file" — applies to whichever side
                    # the preceding body line belonged to.
                    if current and current[-1][:1] == "-":
                        old_no_newline = True
                    else:
                        new_no_newline = True
                    index += 1
                    continue
                if line[:1] in (" ", "+", "-"):
                    current.append(line)
                    index += 1
                    continue
                if line == "":
                    # Many tools strip trailing whitespace, turning a blank
                    # context line into an empty one. Only treat it as context
                    # while the hunk is still short of its declared counts —
                    # otherwise it is the blank line after the hunk.
                    provisional = Hunk(*header[:4], tuple(current))
                    if (
                        len(provisional.source_lines) < header[1]
                        or len(provisional.target_lines) < header[3]
                    ):
                        current.append(" ")
                        index += 1
                        continue
                flush_hunk()
                index += 1
                continue

            index += 1

        flush_file()
        if not patches:
            raise ValidationError("patch contains no file sections")
        return patches

    # -- planning ----------------------------------------------------------

    def dry_run(self, patch_text: str, workspace: Path | str) -> PatchPlan:
        """Plan application without writing anything.

        Every file's post-patch content is computed in full, so
        :meth:`PatchApplier.apply` becomes a sequence of writes that cannot
        fail on hunk logic.
        """
        workspace_path = Path(workspace).expanduser().resolve()
        plan = PatchPlan(
            workspace=workspace_path,
            patch_sha256=compute_patch_sha256(patch_text),
        )

        try:
            file_patches = self.parse(patch_text)
        except ValidationError as exc:
            plan.rejected.append(str(exc))
            return plan

        for file_patch in file_patches:
            try:
                relative = safe_relative_path(file_patch.path, workspace_path)
            except ValidationError as exc:
                # A path escape poisons the whole patch, not just one file:
                # applying "the safe parts" of a patch that tried to escape
                # would commit an artifact whose author was demonstrably
                # hostile or broken.
                plan.rejected.append(str(exc))
                continue

            plan.entries.append(self._plan_file(file_patch, relative, workspace_path))

        return plan

    def _plan_file(
        self, file_patch: FilePatch, relative: Path, workspace: Path
    ) -> PatchPlanEntry:
        target = workspace / relative
        posix = relative.as_posix()
        action = file_patch.action

        for hunk in file_patch.hunks:
            if not hunk.counts_consistent():
                return PatchPlanEntry(
                    relative_path=posix,
                    action=action,
                    applies_cleanly=False,
                    reason=(
                        f"malformed hunk {hunk.header}: body has "
                        f"{len(hunk.source_lines)}/{len(hunk.target_lines)} lines but the "
                        f"header declares {hunk.old_count}/{hunk.new_count}"
                    ),
                )

        if action is PatchAction.DELETE:
            if not target.is_file():
                return PatchPlanEntry(
                    relative_path=posix,
                    action=action,
                    applies_cleanly=False,
                    reason="cannot delete a file that does not exist",
                )
            return PatchPlanEntry(
                relative_path=posix,
                action=action,
                applies_cleanly=True,
                new_content=None,
                prior_sha256=_sha256_bytes(target.read_bytes()),
                hunks_applied=len(file_patch.hunks),
            )

        prior_bytes = b""
        if action is PatchAction.CREATE:
            if target.exists():
                return PatchPlanEntry(
                    relative_path=posix,
                    action=action,
                    applies_cleanly=False,
                    reason="cannot create a file that already exists",
                )
        else:
            if not target.is_file():
                return PatchPlanEntry(
                    relative_path=posix,
                    action=action,
                    applies_cleanly=False,
                    reason="target file does not exist",
                )
            prior_bytes = target.read_bytes()

        try:
            text = prior_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return PatchPlanEntry(
                relative_path=posix,
                action=action,
                applies_cleanly=False,
                reason=f"target is not valid UTF-8 and cannot be patched as text ({exc.reason})",
            )

        newline = "\r\n" if "\r\n" in text else "\n"
        normalised = text.replace("\r\n", "\n")
        had_trailing_newline = normalised.endswith("\n")
        original_lines = normalised.split("\n")
        if had_trailing_newline:
            original_lines.pop()  # drop the phantom element after the last \n

        try:
            new_lines, offset, applied = self._apply_hunks(original_lines, file_patch.hunks)
        except _HunkMismatch as exc:
            return PatchPlanEntry(
                relative_path=posix,
                action=action,
                applies_cleanly=False,
                reason=str(exc),
                prior_sha256=_sha256_bytes(prior_bytes) if prior_bytes else None,
            )

        wants_trailing = had_trailing_newline or action is PatchAction.CREATE
        if any(h.new_no_newline for h in file_patch.hunks):
            wants_trailing = False

        rebuilt = newline.join(new_lines) + (newline if wants_trailing and new_lines else "")
        new_content = rebuilt.encode("utf-8")

        return PatchPlanEntry(
            relative_path=posix,
            action=action,
            applies_cleanly=True,
            new_content=new_content,
            prior_sha256=_sha256_bytes(prior_bytes) if action is PatchAction.MODIFY else None,
            new_sha256=_sha256_bytes(new_content),
            hunks_applied=applied,
            line_offset=offset,
        )

    def _apply_hunks(
        self, original: list[str], hunks: tuple[Hunk, ...]
    ) -> tuple[list[str], int, int]:
        """Apply hunks in order, returning ``(lines, max_offset, applied)``.

        ``drift`` accumulates the net line-count change of applied hunks, so a
        later hunk's declared position is interpreted relative to the file as
        it now stands rather than as the diff author saw it.
        """
        result = list(original)
        drift = 0
        max_offset = 0
        applied = 0

        for hunk in hunks:
            source = hunk.source_lines
            target = hunk.target_lines
            expected = hunk.old_start - 1 + drift

            position = self._locate(result, source, expected)
            if position is None:
                context = source[0][:60] if source else "<empty hunk>"
                raise _HunkMismatch(
                    f"hunk {hunk.header} does not apply: expected context at line "
                    f"{expected + 1} ({context!r}) was not found within "
                    f"{self.max_line_offset} lines"
                )

            max_offset = max(max_offset, abs(position - expected))
            result[position : position + len(source)] = target
            drift += len(target) - len(source)
            applied += 1

        return result, max_offset, applied

    def _locate(self, haystack: list[str], needle: list[str], expected: int) -> int | None:
        """Find ``needle`` at or near ``expected``. Exact content match only."""
        if not needle:
            # A pure-insertion hunk has no context to match against, so the
            # declared position is all we have. Clamp it into range rather than
            # failing — an append hunk at EOF frequently declares one past the
            # end.
            return max(0, min(expected, len(haystack)))

        def matches(at: int) -> bool:
            return at >= 0 and haystack[at : at + len(needle)] == needle

        if matches(expected):
            return expected
        for delta in range(1, self.max_line_offset + 1):
            if matches(expected - delta):
                return expected - delta
            if matches(expected + delta):
                return expected + delta
        return None


class _HunkMismatch(Exception):
    """Internal: a hunk's context was not found. Converted to a plan entry."""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class PatchApplier:
    """Applies a :class:`PatchPlan` atomically per file, with rollback."""

    def apply(self, plan: PatchPlan) -> PatchJournal:
        """Write a plan to disk, unwinding completely if any write fails.

        Refuses a plan that is not fully clean. Partial application of a patch
        whose author already got something wrong is how a workspace ends up in
        a state no ledger event describes.
        """
        if not plan.ok:
            reasons = plan.rejected + [
                f"{entry.relative_path}: {entry.reason}" for entry in plan.failures()
            ]
            raise ValidationError(
                "refusing to apply a patch plan that does not apply cleanly",
                reasons=reasons[:10],
            )

        journal = PatchJournal(workspace=plan.workspace, patch_sha256=plan.patch_sha256)

        try:
            for entry in plan.entries:
                target = plan.workspace / entry.relative_path
                existed = target.is_file()
                prior = target.read_bytes() if existed else None
                journal.entries.append(
                    _JournalEntry(
                        relative_path=entry.relative_path,
                        existed_before=existed,
                        prior_bytes=prior,
                    )
                )

                if entry.action is PatchAction.DELETE:
                    target.unlink()
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, entry.new_content or b"")
        except OSError as exc:
            log.error(
                "validation.patch.apply_failed",
                error=str(exc),
                touched=journal.touched_paths,
            )
            self.rollback(journal)
            raise ValidationError(
                "patch application failed and was rolled back",
                reason=str(exc),
                rolled_back=journal.touched_paths,
            ) from exc

        log.info(
            "validation.patch.applied",
            files=len(journal.entries),
            patch_sha256=plan.patch_sha256[:12],
        )
        return journal

    def rollback(self, journal: PatchJournal) -> None:
        """Restore every touched file to its exact prior bytes.

        Unwinds in **reverse** order so a patch that created a directory and
        then a file inside it is undone innermost-first.
        """
        for entry in reversed(journal.entries):
            target = journal.workspace / entry.relative_path
            try:
                if entry.existed_before:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(target, entry.prior_bytes or b"")
                elif target.exists():
                    target.unlink()
            except OSError as exc:
                # Keep unwinding: one unrestorable file must not strand the
                # rest of the workspace in a half-patched state.
                log.error(
                    "validation.patch.rollback_failed",
                    path=entry.relative_path,
                    error=str(exc),
                )
        log.info("validation.patch.rolled_back", files=len(journal.entries))


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically.

    Temp file in the **same directory** then ``os.replace``. Same directory
    because ``os.replace`` is only atomic within one filesystem — a temp file
    in the system temp dir would silently become a non-atomic copy across a
    mount boundary, which is exactly the case where a crash mid-write leaves a
    truncated file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".paa_patch_")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            # fsync before replace: without it the rename can be durable while
            # the data is not, leaving an empty file after a power loss.
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        # Leaving a stray .paa_patch_* file behind would pollute the very
        # workspace whose manifest the recovery engine hashes, so clean up on
        # every exit path including KeyboardInterrupt.
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_patch_sha256(patch_text: str) -> str:
    """Digest of a patch for ``TaskProjection.applied_patch_sha256``.

    Line endings are normalised to ``\\n`` first, so the same logical patch
    hashes identically whether it arrived from a Windows or POSIX producer.
    Without that, the ledger's patch identity would depend on the transport
    rather than the content, and two identical patches would look like two
    different mutations during replay.
    """
    normalised = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
