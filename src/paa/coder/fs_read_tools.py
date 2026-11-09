"""Filesystem read tools for the coder agent (N1.1, ADR-0045).

Three read-only tools — ``read_file``, ``glob`` and ``grep`` — implemented
against the frozen :mod:`paa.coder.workspace` jail. They are what an agent
needs to *see* a working tree before any write/shell/git tool exists, and
they follow the two conventions every tool in this package follows
(see :mod:`paa.coder.runtime_tools`):

* **Errors are values, not exceptions.** The loop feeds a tool's return
  string back to the model, and a raised exception becomes ``"error: ..."``
  with no structure, so a jail refusal, a protected path, a missing file or
  a binary file is reported as a JSON payload the model can reason about.
  Unexpected faults (an ``OSError`` from an unreadable disk) are also
  reported as values; nothing here swallows a fault silently — every
  refusal names its reason in the payload.
* **Jail, then protect, in that frozen order** (ADR-0045, reviewer F3).
  Every path that touches the disk is canonicalised by
  :func:`assert_inside_workspace` first and only then consulted by
  :func:`is_protected` on the *returned canonical path*. The order is
  load-bearing: an escape attempt that also names a protected subtree
  (``../.env``) must report the escape, never the protected name, and a
  symlink pointing outside the workspace — or into a protected subtree —
  is resolved by the jail before any content is read. Tools convert the
  model's string argument to :class:`~pathlib.Path` themselves; passing a
  ``str`` into the jail crashes with ``AttributeError`` (reviewer MINOR-1).

The size and encoding shapes are the ones ADR-0045 freezes for all work
tools: a 64 KB read cap with truncation *marked* (never silent), UTF-8
round-trip with CRLF preserved byte-for-byte on the read passthrough
(bytes are read and decoded, never opened in text mode, so no newline
translation ever happens), BOM handled via the ``utf-8-sig`` codec (it
strips the BOM when present and is plain UTF-8 otherwise), and binary
content refused with a clear message rather than decoded with replacement
characters. Glob and grep results carry the same truncation marking, and
their walk never follows symlinked directories, so a symlink loop or a
symlink pointing outside the workspace cannot pull the tool outside the
jail or hang the enumeration.

The check is best-effort against a concurrent symlink swap between the
jail check and the subsequent open — the same posture the ADR records for
the jail itself. Every operation below uses the canonical path the jail
returned, never the raw argument string, so the raw string is never
handed to the filesystem.
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from paa.coder.workspace import (
    Workspace,
    WorkspaceViolation,
    assert_inside_workspace,
    is_protected,
)

__all__ = ["FS_READ_TOOLS", "MAX_READ_BYTES", "build_fs_read_tools"]

#: The read-only catalogue this module adds, in the same posture as
#: ``READ_ONLY_TOOLS`` in :mod:`paa.coder.runtime_tools`. None of these names
#: matches a ``DEFAULT_MUTATING_TOOLS`` glob (``read_file``/``glob``/``grep``
#: contain none of ``write*``/``edit*``/``bash*``/``shell*``/``apply_patch*``/
#: ``*delete*``/``*write*``/``send_*``/``*_commit*``), so they are allowed in
#: PLAN mode and, once N1.5 standing-allows this set, allowed without a
#: prompt in ASK mode. A test pins both properties.
FS_READ_TOOLS: frozenset[str] = frozenset({"read_file", "glob", "grep"})

#: The per-read byte cap frozen by ADR-0045 (64 KB). A file larger than
#: this is read up to the cap and reported with ``truncated: true``; the
#: tool never loads the whole file into memory to discover that.
MAX_READ_BYTES = 64 * 1024

#: Per-file byte cap for grep: a larger file is searched in its first
#: ``MAX_READ_BYTES`` only and counted as ``files_truncated``.
_MAX_GLOB_RESULTS = 200
_MAX_GLOB_RESULTS_LIMIT = 1000
_MAX_GREP_MATCHES = 50
_MAX_GREP_MATCHES_LIMIT = 200
_MAX_WALK_ENTRIES = 10_000
_MAX_LINE_CHARS = 500

_ESCAPES_REASON = "path escapes the workspace jail"
_BINARY_REFUSAL = "binary content refused: file contains NUL bytes"
_NON_UTF8_REFUSAL = "binary content refused: file is not valid UTF-8"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")
_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_UNC_PREFIX = re.compile(r"^(?:\\\\|//)")

_GLOB_META = "*?["


def build_fs_read_tools(ws: Workspace) -> dict[str, Any]:
    """The read catalogue, bound to one jailed workspace (N1.1).

    Bound rather than global for the same reason ``build_runtime_tools``
    is: every tool reaches through this workspace and no other, and the
    workspace arrives as a value the caller resolved (``Workspace(root=
    resolve_workspace(raw))``), so the tools cannot disagree about which
    tree they are allowed to see.
    """
    tools: list[_FsReadTool] = [_ReadFile(ws), _Glob(ws), _Grep(ws)]
    return {t.name: t for t in tools}


def _emit(payload: dict[str, Any]) -> str:
    """JSON tool output: sorted keys, pydantic-free (same shape as the
    runtime tools' ``_emit``; duplicated deliberately rather than imported
    so this module has no dependency on a sibling's private helper)."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


class _Denied(Exception):
    """A refusal that becomes an error value. Carries the frozen reason
    (when one exists) so the payload states it verbatim."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class _FsReadTool:
    """Shared plumbing: the ``Tool`` protocol's members plus the jail."""

    name: str
    description: str

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    def schema(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def run(self, arguments: Mapping[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _jail_then_protect(self, raw: Any) -> Path:
        """Resolve a model-supplied path to a canonical, unprotected path.

        Jail first, protection second — the ADR-0045 frozen order. A
        relative argument is resolved against the workspace root (not the
        process CWD, which ``Path.resolve()`` would use); an absolute
        argument is checked as-is, so an absolute path *inside* the
        workspace is legitimate and one outside is an escape.
        """
        if not isinstance(raw, str) or not raw.strip():
            raise _Denied("path must be a non-empty string")
        candidate = Path(raw)
        target = candidate if candidate.is_absolute() else self._ws.root / candidate
        try:
            resolved = assert_inside_workspace(self._ws, target)
        except WorkspaceViolation as exc:
            raise _Denied(exc.message, reason=exc.reason) from exc
        reason = is_protected(self._ws, resolved)
        if reason is not None:
            raise _Denied(reason, reason=reason)
        return resolved

    @staticmethod
    def _error(raw: Any, denied: _Denied) -> str:
        rel = str(raw) if isinstance(raw, str) else f"<{type(raw).__name__}>"
        return _emit({"error": str(denied), "path": rel, "reason": denied.reason})


def _clamp_limit(value: Any, *, default: int, low: int, high: int) -> int:
    """A caller-supplied limit is clamped, never trusted (same rule as the
    runtime tools' ``_clamp_int`` — a limit of 0 must not mean unbounded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _decode_utf8(raw: bytes, *, truncated: bool) -> str:
    """Decode a capped read slice as UTF-8, or refuse binary content.

    The UTF-8 BOM is stripped when present (``utf-8-sig``), CRLF and LF are
    preserved byte-for-byte (no text-mode translation anywhere on this
    path), and anything that is not UTF-8 is refused with the frozen
    message rather than decoded with replacement characters.

    The incremental decoder is the point: an incomplete multi-byte
    sequence at the very end of a *truncated* slice is buffered during the
    feed and only rejected at the final flush, while a genuinely invalid
    byte — even one sitting two bytes from the cap boundary — raises
    during the feed. This distinguishes "the cap split a character" from
    "the file is not UTF-8" structurally, with no positional heuristic.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        content = decoder.decode(raw, False)
    except UnicodeDecodeError as exc:
        raise _Denied(_NON_UTF8_REFUSAL) from exc
    try:
        decoder.decode(b"", True)
    except UnicodeDecodeError as exc:
        if not truncated:
            raise _Denied(_NON_UTF8_REFUSAL) from exc
        # Incomplete tail char at the cap boundary: the content decoded so
        # far is the complete, valid prefix (the partial character sits
        # past the cap and is part of the truncated-away remainder).
    return content


class _ReadFile(_FsReadTool):
    name = "read_file"
    description = (
        "Read one UTF-8 text file inside the workspace and return its "
        "contents. Capped at 64 KB (truncation is flagged, never silent); "
        "CRLF line endings are preserved; a UTF-8 BOM is stripped; binary "
        "files are refused with an error. Paths outside the workspace and "
        "protected paths (.git, .env*, PAA_HOME) are denied with a reason."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Path to the file, relative to the workspace "
                    "root (or an absolute path inside the workspace).",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        raw = arguments.get("path")
        try:
            resolved = self._jail_then_protect(raw)
        except _Denied as denied:
            return self._error(raw, denied)
        if resolved.is_dir():
            return _emit({"error": "path is a directory, not a file", "path": str(raw)})
        if not resolved.is_file():
            return _emit({"error": "file not found", "path": str(raw)})
        try:
            size = resolved.stat().st_size
            with resolved.open("rb") as handle:
                chunk = handle.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            return _emit(
                {"error": f"cannot read file: {exc.strerror or type(exc).__name__}",
                 "path": str(raw)}
            )
        truncated = len(chunk) > MAX_READ_BYTES
        raw_bytes = chunk[:MAX_READ_BYTES]
        if b"\x00" in raw_bytes:
            return _emit({"error": _BINARY_REFUSAL, "path": str(raw)})
        had_bom = raw_bytes.startswith(codecs.BOM_UTF8)
        try:
            content = _decode_utf8(raw_bytes, truncated=truncated)
        except _Denied as denied:
            return self._error(raw, denied)
        return _emit(
            {
                "path": str(raw),
                "content": content,
                "encoding": "utf-8",
                "had_bom": had_bom,
                "truncated": truncated,
                "size_bytes": size,
                "note": (
                    "file is larger than 64 KB; showing the first 64 KB only"
                    if truncated
                    else "full file shown"
                ),
            }
        )


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern to an anchored regex with glob semantics.

    ``*`` and ``?`` match within one path segment, ``**`` crosses segments,
    ``**/`` additionally matches zero directories. Implemented here rather
    than via :meth:`pathlib.Path.glob` because the stdlib recursion follows
    symlinked directories — a symlink loop inside the workspace would hang
    the enumeration, and a symlink pointing outside would leak outside
    entries — while this translation is matched against names produced by
    the module's own walk, which never descends a symlinked directory.
    """
    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
                i += 1
            else:
                stuff = pattern[i + 1 : j]
                if stuff.startswith("!"):
                    stuff = "^" + stuff[1:]
                out.append("[" + stuff.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _check_glob_pattern(pattern: str, ws: Workspace) -> None:
    """Validate a glob pattern's own pathhood before any walking.

    A pattern is workspace-relative by definition, so an absolute, drive,
    UNC or control-character-bearing pattern is an escape attempt, not a
    pattern to reinterpret. The literal base (the leading segments with no
    glob metacharacters) is jailed and protected as a directory: ``..``
    segments that escape and a ``.git/**`` base are denied with the frozen
    reason, exactly as a literal path would be.
    """
    if _CONTROL_CHARS.search(pattern):
        raise _Denied(_ESCAPES_REASON, reason=_ESCAPES_REASON)
    if _UNC_PREFIX.match(pattern) or _DRIVE_RELATIVE.match(pattern):
        raise _Denied(_ESCAPES_REASON, reason=_ESCAPES_REASON)
    if _DRIVE_ABSOLUTE.match(pattern):  # ``C:/...`` — absolute on Windows
        raise _Denied(_ESCAPES_REASON, reason=_ESCAPES_REASON)
    if pattern.startswith("/") or pattern.startswith("\\"):
        raise _Denied(_ESCAPES_REASON, reason=_ESCAPES_REASON)
    segments = [s for s in re.split(r"[/\\]", pattern) if s not in ("", ".")]
    base_parts: list[str] = []
    for segment in segments:
        if any(meta in segment for meta in _GLOB_META):
            break
        base_parts.append(segment)
    # ``..`` may appear only as a literal base (a wildcard cannot spell it),
    # so the base check is the whole escape check for traversal segments.
    base = ws.root
    if base_parts:
        base = ws.root.joinpath(*base_parts)
        try:
            resolved_base = assert_inside_workspace(ws, base)
        except WorkspaceViolation as exc:
            raise _Denied(exc.message, reason=exc.reason) from exc
        reason = is_protected(ws, resolved_base)
        if reason is not None:
            raise _Denied(reason, reason=reason)


class _Glob(_FsReadTool):
    name = "glob"
    description = (
        "List files and directories in the workspace whose workspace-relative "
        "POSIX path matches a glob pattern (e.g. '**/*.py', 'docs/*.md'). "
        "Results are relative paths, sorted. Protected entries (.git, .env*, "
        "PAA_HOME) are never returned; entries that escape the workspace "
        "through a symlink are omitted, not followed. At most 1000 results "
        "(default 200); truncation is flagged."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Glob pattern, workspace-relative, '/'-separated.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_GLOB_RESULTS_LIMIT,
                    "description": f"Maximum results. Default {_MAX_GLOB_RESULTS}.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return _emit({"error": "pattern must be a non-empty string"})
        limit = _clamp_limit(
            arguments.get("limit"),
            default=_MAX_GLOB_RESULTS,
            low=1,
            high=_MAX_GLOB_RESULTS_LIMIT,
        )
        try:
            _check_glob_pattern(pattern, self._ws)
        except _Denied as denied:
            return self._error(pattern, denied)
        regex = _glob_to_regex(pattern.replace("\\", "/"))
        paths: list[str] = []
        skipped_protected = 0
        skipped_escaped = 0
        truncated = False
        for rel, entry in _walk(self._ws.root):
            if regex.match(rel) is None:
                continue
            try:
                resolved = assert_inside_workspace(self._ws, entry)
            except WorkspaceViolation:
                skipped_escaped += 1
                continue
            if is_protected(self._ws, resolved) is not None:
                skipped_protected += 1
                continue
            if len(paths) >= limit:
                truncated = True
                break
            paths.append(rel)
        return _emit(
            {
                "pattern": pattern,
                "paths": paths,
                "count": len(paths),
                "truncated": truncated,
                "skipped_protected": skipped_protected,
                "skipped_escaped": skipped_escaped,
                "note": (
                    "result list truncated at the limit" if truncated else ""
                ),
            }
        )


class _Grep(_FsReadTool):
    name = "grep"
    description = (
        "Search UTF-8 text files under a workspace path for a Python regex, "
        "returning matching lines (path, 1-based line number, line text). "
        "Binary and non-UTF-8 files are skipped, as are protected entries "
        "(never read) and symlinks escaping the workspace (never followed). "
        "At most 200 matches (default 50); truncation is flagged."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Python regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search, workspace-relative. "
                    "Default: the workspace root.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename filter, e.g. '*.py'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_GREP_MATCHES_LIMIT,
                    "description": f"Maximum matches. Default {_MAX_GREP_MATCHES}.",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _emit({"error": "pattern must be a non-empty string"})
        if _CONTROL_CHARS.search(pattern):
            return _emit({"error": "pattern must not contain control characters"})
        glob_filter = arguments.get("glob")
        if glob_filter is not None and not isinstance(glob_filter, str):
            return _emit({"error": "glob must be a string"})
        limit = _clamp_limit(
            arguments.get("limit"),
            default=_MAX_GREP_MATCHES,
            low=1,
            high=_MAX_GREP_MATCHES_LIMIT,
        )
        try:
            expr = re.compile(pattern)
        except re.error as exc:
            return _emit({"error": f"invalid regular expression: {exc}"})
        raw_path = arguments.get("path", ".")
        try:
            root = self._jail_then_protect(raw_path)
        except _Denied as denied:
            return self._error(raw_path, denied)
        if not root.exists():
            return _emit({"error": "path not found", "path": str(raw_path)})
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        files_truncated = 0
        files_skipped = 0
        truncated = False
        for rel, entry in _walk(root, root_is_workspace=self._ws.root):
            if not entry.is_file():
                continue
            if glob_filter is not None and not fnmatch.fnmatchcase(
                entry.name, glob_filter
            ):
                continue
            try:
                resolved = assert_inside_workspace(self._ws, entry)
            except WorkspaceViolation:
                files_skipped += 1
                continue
            if is_protected(self._ws, resolved) is not None:
                files_skipped += 1
                continue
            try:
                with resolved.open("rb") as handle:
                    chunk = handle.read(MAX_READ_BYTES + 1)
            except OSError:
                files_skipped += 1
                continue
            file_truncated = len(chunk) > MAX_READ_BYTES
            raw_bytes = chunk[:MAX_READ_BYTES]
            if b"\x00" in raw_bytes:
                continue  # binary: skipped, never searched
            try:
                text = _decode_utf8(raw_bytes, truncated=file_truncated)
            except _Denied:
                continue  # not UTF-8: skipped, never searched
            files_scanned += 1
            files_truncated += int(file_truncated)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if expr.search(line):
                    matches.append(
                        {
                            "path": rel,
                            "line": lineno,
                            "text": line if len(line) <= _MAX_LINE_CHARS else (
                                line[:_MAX_LINE_CHARS] + "…"
                            ),
                        }
                    )
                    if len(matches) >= limit:
                        truncated = True
                        break
                if len(matches) >= limit:
                    break
            if truncated:
                break
        return _emit(
            {
                "pattern": pattern,
                "path": str(raw_path),
                "matches": matches,
                "count": len(matches),
                "files_scanned": files_scanned,
                "files_truncated": files_truncated,
                "files_skipped": files_skipped,
                "truncated": truncated,
                "note": (
                    "match list truncated at the limit" if truncated else ""
                ),
            }
        )


def _walk(top: Path, *, root_is_workspace: Path | None = None) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix_relative_to_workspace, absolute_path)`` — never follow.

    ``os.walk`` with ``followlinks=False`` never descends a symlinked
    directory, so a symlink loop cannot hang the enumeration and a
    symlink pointing outside the workspace is never traversed. The
    top-level ``.git`` directory is pruned (it is protected; pruning it is
    a bound on work, not a security decision — entry-level
    ``is_protected`` still guards every individual entry). Relative paths
    are computed against the workspace root for display; when ``top`` is a
    subdirectory of it (grep's ``path``), the walk still yields
    workspace-relative spellings.
    """
    ws_root = root_is_workspace if root_is_workspace is not None else top
    seen = 0
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        current = Path(dirpath)
        try:
            rel_current = current.relative_to(ws_root)
        except ValueError:
            continue  # pragma: no cover - walk stays under ws_root by construction
        if not rel_current.parts:  # at the workspace root: prune .git
            dirnames[:] = [d for d in dirnames if d.casefold() != ".git"]
        for name in sorted(dirnames):
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                return
            entry = current / name
            yield f"{rel_current / name}".replace("\\", "/"), entry
        for name in sorted(filenames):
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                return
            entry = current / name
            yield f"{rel_current / name}".replace("\\", "/"), entry
