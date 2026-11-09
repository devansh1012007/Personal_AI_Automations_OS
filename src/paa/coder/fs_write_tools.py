"""Filesystem write tools — journals, hash-verified, rollback-capable (ADR-0045).

Three tools — ``write_file``, ``edit_file`` (``old_str`` exactly once)
and ``apply_patch`` — and one rule: every write goes through
``UnifiedDiffValidator.dry_run`` then ``PatchApplier.apply``. A direct
write path is rejected on purpose (ADR-0045 D-3): a second write
surface would double the audit and recovery problem, so the applier
that already journals exact prior bytes, writes atomically and unwinds
on failure is the only way bytes reach the tree.

The guarantees the callers get:

- **Jail, then protect, then touch.** Every path is resolved by
  ``assert_inside_workspace`` and only then classified by
  ``is_protected`` (the frozen order). A refusal surfaces the frozen
  reason string (``path escapes the workspace jail`` /
  ``protected path: git`` / ``protected path: env`` /
  ``protected path: paa-home``) as a JSON error value, and the disk
  is not touched.
- **Hash-verified at apply.** ``dry_run`` records ``prior_sha256``
  per file and ``PatchApplier.apply`` re-verifies it against the file
  as it stands now (prior-hash fix, N1.2). A file changed between
  plan and apply refuses and unwinds instead of being clobbered.
- **The patch SHA is the identity.** Every success payload carries
  ``patch_sha256`` equal to ``compute_patch_sha256`` of the patch
  that was applied, and ``PatchJournal`` entries are kept per session
  in memory (``FsWriteSession.journals``) — ledger persistence is
  Wave 2.
- **Encoding is explicit.** New content must be UTF-8 text without a
  NUL; prior files that are binary (NUL) or not valid UTF-8 are
  refused, never patched as text. BOM is preserved on edit (matched
  in BOM-free space), CRLF is preserved when modifying a CRLF file
  (the planner re-joins with the file's own newline) and new files
  are created LF — a diff line cannot carry a trailing CR through
  the patch wire format, so a new file has no CRLF to preserve yet.

Errors are returned as JSON values (``{"ok": false, ...}``), matching
``runtime_tools``: the loop feeds a tool's return string back to the
model, so a refusal the model can reason about beats a traceback it
has to interpret. Tool names ``write_file`` / ``edit_file`` /
``apply_patch`` match ``DEFAULT_MUTATING_TOOLS`` globs, so PLAN
refuses them and ASK prompts for them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from paa.coder.workspace import (
    Workspace,
    WorkspaceViolation,
    assert_inside_workspace,
    is_protected,
    resolve_workspace,
)
from paa.core.errors import ValidationError
from paa.validation.patch import (
    PatchApplier,
    PatchJournal,
    PatchPlan,
    UnifiedDiffValidator,
)

__all__ = [
    "MAX_CONTENT_BYTES",
    "MAX_PATCH_BYTES",
    "ApplyPatchTool",
    "EditFileTool",
    "FsWriteSession",
    "WriteFileTool",
    "build_fs_write_tools",
]

log = structlog.get_logger(__name__)

#: Size ceiling for one file's new content (bytes after UTF-8 encoding).
#: Writes above it are refused, never truncated — truncating user
#: content would be silent corruption. 256 KiB is far above a coding
#: session's single edit and far below anything that would make the
#: whole-file hunk synthesis or the in-memory journal unbounded.
MAX_CONTENT_BYTES = 256 * 1024

#: Size ceiling for one patch's text (bytes after UTF-8 encoding).
MAX_PATCH_BYTES = 256 * 1024

_BOM = "\ufeff"
_NO_NEWLINE = "\\ No newline at end of file\n"


class _ToolError(Exception):
    """An expected refusal — bad arguments, jail/protect denial, a plan
    that does not apply, an over-limit input. Caught inside ``run`` and
    returned as a JSON error value; never raised out of a tool."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra: dict[str, Any] = extra


class FsWriteSession:
    """One session's write state: workspace, planner, applier, journals.

    The session is the journal scope the brief freezes for Wave 1:
    every applied patch appends its :class:`PatchJournal` here, in
    order, and ``rollback_all`` unwinds them in reverse (the same
    innermost-first order ``PatchApplier.rollback`` uses inside one
    patch). Ledger persistence of the ``patch_sha256`` is Wave 2;
    until then this list is the recoverable record.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        validator: UnifiedDiffValidator | None = None,
        applier: PatchApplier | None = None,
    ) -> None:
        self.workspace = workspace
        self.validator = validator or UnifiedDiffValidator()
        self.applier = applier or PatchApplier()
        self.journals: list[PatchJournal] = []
        #: The patch text of the most recent successful apply, kept so
        #: callers (and tests) can verify ``patch_sha256`` against
        #: ``compute_patch_sha256`` without re-deriving the synthesis.
        self.last_patch_text: str | None = None

    def record(self, patch_text: str, journal: PatchJournal) -> None:
        """Append one applied patch to the session journal."""
        self.journals.append(journal)
        self.last_patch_text = patch_text

    def rollback_all(self) -> list[str]:
        """Unwind every recorded journal, newest first. Returns touched
        paths across all journals (a path may appear more than once)."""
        touched: list[str] = []
        for journal in reversed(self.journals):
            self.applier.rollback(journal)
            touched.extend(journal.touched_paths)
        return touched


def build_fs_write_tools(
    workspace: Workspace | Path | str,
    *,
    session: FsWriteSession | None = None,
) -> dict[str, Any]:
    """The three write tools bound to one workspace (and session).

    ``workspace`` is the frozen value: a :class:`Workspace`, or a path
    (``Path``/``str``) wrapped as ``Workspace(root=...)`` — the
    jail re-resolves defensively, so a directly constructed root still
    jails correctly. Pass ``session`` to share one journal across the
    three tools (the default) or to inject a fake planner/applier.
    """
    ws = workspace if isinstance(workspace, Workspace) else _as_workspace(workspace)
    sess = session or FsWriteSession(ws)
    tools: list[WriteFileTool | EditFileTool | ApplyPatchTool] = [
        WriteFileTool(sess),
        EditFileTool(sess),
        ApplyPatchTool(sess),
    ]
    return {tool.name: tool for tool in tools}


def _as_workspace(workspace: Path | str) -> Workspace:
    raw = workspace if isinstance(workspace, str) else str(workspace)
    if isinstance(workspace, str):
        return Workspace(root=resolve_workspace(raw))
    # A Path root is already a directory the caller named: wrap it
    # directly (resolve_workspace would demand a str). The jail still
    # canonicalises on every check, so this is not a weaker root.
    return Workspace(root=workspace)


class WriteFileTool:
    """Create or fully overwrite one text file."""

    name = "write_file"
    description = (
        "Write text content to a file in the workspace, creating it if it "
        "does not exist or fully replacing it if it does. The content is "
        "written exactly as given (UTF-8). This is a mutating tool: it "
        "prompts in ASK mode and is refused in PLAN mode."
    )

    def __init__(self, session: FsWriteSession) -> None:
        self._session = session

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path, e.g. 'src/app.py'.",
                },
                "content": {
                    "type": "string",
                    "description": "Full new content, written exactly as given.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        try:
            raw_path = _require_path(arguments)
            content = _require_text(arguments, "content", allow_empty=True)
            resolved, relative = _jail(self._session.workspace, raw_path)
            _encode_new_content(content, relative)
            if resolved.is_file():
                old_lines, _old_trailing = _split_lines(
                    _normalise(_read_prior_text(resolved, relative))
                )
                new_lines, new_trailing = _split_lines(_normalise(content))
                patch_text = _whole_file_patch(
                    relative, "MODIFY", old_lines, new_lines, new_trailing
                )
            else:
                # Either a new file, or an existing non-file (a directory
                # the planner will refuse as "already exists"): the CREATE
                # synthesis covers both, and the refusal is the planner's.
                new_lines, new_trailing = _split_lines(_normalise(content))
                patch_text = _whole_file_patch(relative, "CREATE", [], new_lines, new_trailing)
            return _emit(_execute(self._session, patch_text, self.name, relative))
        except _ToolError as exc:
            return _emit(_error(self.name, exc))


class EditFileTool:
    """Replace the single occurrence of ``old_str`` with ``new_str``."""

    name = "edit_file"
    description = (
        "Replace exactly one occurrence of old_str with new_str in an "
        "existing text file. old_str must match exactly once: zero or "
        "multiple matches fail without writing anything. This is a "
        "mutating tool: it prompts in ASK mode and is refused in PLAN mode."
    )

    def __init__(self, session: FsWriteSession) -> None:
        self._session = session

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path of an existing file.",
                },
                "old_str": {
                    "type": "string",
                    "description": "Text to replace. Must occur exactly once.",
                },
                "new_str": {
                    "type": "string",
                    "description": "Replacement text. May be empty (a deletion).",
                },
            },
            "required": ["path", "old_str", "new_str"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        try:
            raw_path = _require_path(arguments)
            old_str = _require_text(arguments, "old_str", allow_empty=False)
            new_str = _require_text(arguments, "new_str", allow_empty=True)
            resolved, relative = _jail(self._session.workspace, raw_path)
            if not resolved.is_file():
                if resolved.is_dir():
                    raise _ToolError(f"path is not a file: {relative}")
                raise _ToolError(f"file does not exist: {relative}")
            prior_text = _read_prior_text(resolved, relative)
            has_bom = prior_text.startswith(_BOM)
            body = prior_text[len(_BOM) :] if has_bom else prior_text
            normalised_body = _normalise(body)
            old_normalised = _normalise(old_str)
            count = normalised_body.count(old_normalised)
            if count == 0:
                raise _ToolError(f"old_str not found in {relative}")
            if count > 1:
                raise _ToolError(
                    f"old_str matched {count} times in {relative}; must match exactly once"
                )
            new_body = normalised_body.replace(old_normalised, _normalise(new_str), 1)
            new_full = (_BOM + new_body) if has_bom else new_body
            # The result must clear the same gate write_file applies to new
            # content (MAJOR-2): without this check one edit could grow a
            # file far past MAX_CONTENT_BYTES, past the memory/journal
            # bound, and the bloated file would then be uneditable (the
            # read side refuses over-limit priors). The NUL check rides
            # along: edit composes text the caller supplied, and a NUL
            # reaching disk would land a file the read side refuses as
            # binary.
            _encode_new_content(new_full, relative)
            old_lines, _old_trailing = _split_lines(_normalise(prior_text))
            new_lines, new_trailing = _split_lines(_normalise(new_full))
            patch_text = _whole_file_patch(relative, "MODIFY", old_lines, new_lines, new_trailing)
            return _emit(_execute(self._session, patch_text, self.name, relative))
        except _ToolError as exc:
            return _emit(_error(self.name, exc))


class ApplyPatchTool:
    """Apply a caller-supplied unified diff."""

    name = "apply_patch"
    description = (
        "Apply a unified-diff patch to the workspace. Every file in "
        "the patch is jailed and checked against the protected set "
        "before any file is written, and the patch refuses as a whole "
        "if any file is denied or any hunk does not apply. This is a "
        "mutating tool: it prompts in ASK mode and is refused in PLAN mode."
    )

    def __init__(self, session: FsWriteSession) -> None:
        self._session = session

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "A unified diff with ---/+++ headers and @@ hunks.",
                },
            },
            "required": ["patch"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        try:
            patch = _require_text(arguments, "patch", allow_empty=False)
            _check_patch_bytes(patch)
            return _emit(_execute(self._session, patch, self.name, None))
        except _ToolError as exc:
            return _emit(_error(self.name, exc))


# ---------------------------------------------------------------------------
# Argument + jail helpers
# ---------------------------------------------------------------------------


def _require_path(arguments: Mapping[str, Any]) -> str:
    value = arguments.get("path")
    if not isinstance(value, str) or not value.strip():
        raise _ToolError("path must be a non-empty string")
    return value


def _require_text(arguments: Mapping[str, Any], field_name: str, *, allow_empty: bool) -> str:
    value = arguments.get(field_name)
    if not isinstance(value, str):
        raise _ToolError(f"{field_name} must be a string")
    if not allow_empty and value == "":
        raise _ToolError(f"{field_name} must be a non-empty string")
    return value


def _jail(workspace: Workspace, raw_path: str) -> tuple[Path, str]:
    """Jail-then-protect one path. Returns (resolved, workspace-relative
    POSIX path) — the relative form the synthesized diff header needs,
    derived from the canonical resolved path, never from the raw string
    (a raw ``a/../b`` is inside the workspace but is not a legal diff
    path)."""
    resolved = _jail_resolved(workspace, workspace.root / Path(raw_path))
    try:
        canonical_root = workspace.root.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise _ToolError("path escapes the workspace jail") from None
    try:
        relative = resolved.relative_to(canonical_root)
    except ValueError:
        raise _ToolError("path escapes the workspace jail") from None
    return resolved, relative.as_posix()


def _jail_resolved(workspace: Workspace, candidate: Path) -> Path:
    """Assert containment, then protection, on an already-joined path."""
    try:
        resolved = assert_inside_workspace(workspace, candidate)
        reason = is_protected(workspace, resolved)
    except WorkspaceViolation as exc:
        raise _ToolError(exc.reason) from exc
    if reason is not None:
        raise _ToolError(reason)
    return resolved


def _read_prior_text(resolved: Path, relative: str) -> str:
    """Read a prior file's text, refusing binary, non-UTF-8 and
    over-size content. The size is stat-checked before the read so a
    hostile multi-gigabyte file is not first loaded into memory."""
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise _ToolError(f"cannot read {relative}: {exc}") from exc
    if size > MAX_CONTENT_BYTES:
        raise _ToolError(
            f"file exceeds the read limit: {relative} is {size} bytes",
            limit=str(MAX_CONTENT_BYTES),
            size=str(size),
            limit_exceeded=True,
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise _ToolError(f"cannot read {relative}: {exc}") from exc
    if b"\x00" in data:
        raise _ToolError(f"binary files are refused: {relative} contains a NUL byte")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _ToolError(
            f"binary or non-UTF-8 file refused: {relative} is not valid UTF-8"
        ) from None


def _encode_new_content(content: str, relative: str) -> bytes:
    """Validate new content as writable text. Returns its UTF-8 bytes."""
    if "\x00" in content:
        raise _ToolError("binary content refused: content contains a NUL byte")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise _ToolError("content is not valid UTF-8 text") from None
    if len(data) > MAX_CONTENT_BYTES:
        raise _ToolError(
            f"content exceeds the write limit: {len(data)} bytes",
            limit=str(MAX_CONTENT_BYTES),
            size=str(len(data)),
            limit_exceeded=True,
        )
    return data


def _check_patch_bytes(patch: str) -> None:
    try:
        data = patch.encode("utf-8")
    except UnicodeEncodeError:
        raise _ToolError("patch is not valid UTF-8 text") from None
    if len(data) > MAX_PATCH_BYTES:
        raise _ToolError(
            f"patch exceeds the size limit: {len(data)} bytes",
            limit=str(MAX_PATCH_BYTES),
            size=str(len(data)),
            limit_exceeded=True,
        )


# ---------------------------------------------------------------------------
# Diff synthesis + the shared dry-run/apply path
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Collapse line endings the way the patch wire format does, so the
    split/count/replace in this module agrees with the planner's model."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_lines(text: str) -> tuple[list[str], bool]:
    """Split normalised text into (lines, had_trailing_newline) — the
    planner's own model, including the empty-file case (TASK-2: an empty
    prior has zero lines, not one phantom empty line)."""
    if text == "":
        return [], False
    had_trailing = text.endswith("\n")
    lines = text.split("\n")
    if had_trailing:
        lines.pop()
    return lines, had_trailing


def _whole_file_patch(
    relative: str,
    action: str,
    old_lines: list[str],
    new_lines: list[str],
    new_has_trailing: bool,
) -> str:
    """Synthesize a single-hunk whole-file diff. The removed lines are
    the complete prior content and the added lines the complete new
    content, so the planner's exact-content location is a plain check
    at the top of the file. The no-newline marker preserves content
    that intentionally has no trailing newline."""
    no_newline = _NO_NEWLINE if new_lines and not new_has_trailing else ""
    new_start = 1 if new_lines else 0
    if action == "CREATE":
        header = (
            "--- /dev/null\n"
            f"+++ b/{relative}\n"
            f"@@ -0,0 +{new_start},{len(new_lines)} @@\n"
        )
        body = "".join(f"+{line}\n" for line in new_lines)
        return header + body + no_newline
    old_start = 1 if old_lines else 0
    header = (
        f"--- a/{relative}\n"
        f"+++ b/{relative}\n"
        f"@@ -{old_start},{len(old_lines)} +{new_start},{len(new_lines)} @@\n"
    )
    body = "".join(f"-{line}\n" for line in old_lines)
    body += "".join(f"+{line}\n" for line in new_lines)
    return header + body + no_newline


def _execute(
    session: FsWriteSession,
    patch_text: str,
    tool_name: str,
    single_path: str | None,
) -> dict[str, Any]:
    """dry_run → jail every entry → apply → record. Returns the success
    payload; every expected failure raises :class:`_ToolError`."""
    try:
        plan = session.validator.dry_run(patch_text, session.workspace.root)
    except OSError as exc:
        raise _ToolError(f"cannot read from workspace: {exc}") from exc
    if plan.rejected:
        raise _ToolError(f"patch rejected: {plan.rejected[0]}", rejected=list(plan.rejected))
    if not plan.ok or not plan.entries:
        reasons = [f"{entry.relative_path}: {entry.reason}" for entry in plan.failures()]
        raise _ToolError("patch does not apply cleanly", reasons=reasons)
    # Jail-then-protect every entry *before* apply touches the disk.
    # (write_file/edit_file already jailed their single path before the
    # prior read; this is the check that protects multi-file patches.)
    for entry in plan.entries:
        _jail_resolved(session.workspace, session.workspace.root / Path(entry.relative_path))
    journal = _apply(session, plan)
    session.record(patch_text, journal)
    payload: dict[str, Any] = {
        "ok": True,
        "tool": tool_name,
        "patch_sha256": plan.patch_sha256,
        "files_changed": plan.files_changed,
        "touched_paths": journal.touched_paths,
    }
    if single_path is not None and len(plan.entries) == 1:
        entry = plan.entries[0]
        payload.update(
            {
                "path": single_path,
                "action": entry.action.value,
                "prior_sha256": entry.prior_sha256,
                "new_sha256": entry.new_sha256,
            }
        )
    log.info(
        "coder.fs_write.applied",
        tool=tool_name,
        files=len(journal.entries),
        patch_sha256=plan.patch_sha256[:12],
    )
    return payload


def _apply(session: FsWriteSession, plan: PatchPlan) -> PatchJournal:
    try:
        return session.applier.apply(plan)
    except ValidationError as exc:
        raise _ToolError(str(exc)) from exc


def _error(tool_name: str, exc: _ToolError) -> dict[str, Any]:
    return {"ok": False, "tool": tool_name, "error": exc.message, **exc.extra}


def _emit(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
