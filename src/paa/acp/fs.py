"""The scoped text-file read behind ``fs/read_text_file`` (ACP v1).

One helper, used by **both** ACP fronts, because both answer the same
question — "may this file be read?" — and two implementations of a security
answer drift:

* the server front answers an ``fs/read_text_file`` *request* from the
  session's own working directory (ADR-0053 records the direction note: in
  ACP proper this is an agent→client method, and PAA's v1 server serves it
  from the workspace it was given rather than asking a client that did not
  offer the capability);
* the client front answers an external agent's ``fs/read_text_file`` request
  from the workspace the agent was spawned into — the agent is contained,
  and the file answer is the client-side half of that containment.

The rules are the coder fs tools' own (:mod:`paa.coder.fs_read_tools`,
ADR-0045), reused rather than restated: **jail, then protect, in that frozen
order** — :func:`paa.coder.workspace.assert_inside_workspace` resolves the
path against the workspace root (collapsing ``..`` and following symlinks),
and only then :func:`paa.coder.workspace.is_protected` judges the *canonical*
path (top-level ``.git``, any-depth ``.env*``, ``PAA_HOME``). A read is
capped at :data:`paa.coder.fs_read_tools.MAX_READ_BYTES` with truncation
*marked* in the return value (the ACP result object has no truncation field,
so the marker is the caller's to surface), and binary content is refused,
never decoded with replacement characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.acp.protocol import INVALID_PARAMS, RESOURCE_NOT_FOUND, AcpError
from paa.coder.fs_read_tools import MAX_READ_BYTES
from paa.coder.workspace import Workspace, assert_inside_workspace, is_protected

__all__ = ["ScopedFileRead", "read_scoped_text_file"]


@dataclass(frozen=True, slots=True)
class ScopedFileRead:
    """One served file: the (possibly line-sliced) text, and its bounds."""

    content: str
    truncated: bool
    """The 64 KB read cap was hit; ``content`` is a prefix, not the file."""


def read_scoped_text_file(
    root: Path,
    raw_path: Any,
    *,
    line: Any = None,
    limit: Any = None,
) -> ScopedFileRead:
    """Read one text file inside ``root``, jailed and protected.

    ``raw_path`` must be an absolute path (ACP's argument requirement);
    relative paths are refused rather than resolved against this process's
    cwd, which would silently re-scope the read. ``line`` is 1-based and
    ``limit`` bounds the number of lines served; both are validated, never
    trusted.

    :raises AcpError: ``-32602`` for a malformed path, an escape from the
        workspace, a protected path, a directory, or binary/non-UTF-8
        content; ``-32002`` when the file does not exist. Refusals name the
        reason, never the file's content.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AcpError(INVALID_PARAMS, "fs/read_text_file requires a non-empty 'path'")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise AcpError(
            INVALID_PARAMS,
            "fs/read_text_file paths must be absolute (ACP argument requirement)",
        )
    start_line = _positive_int(line, "line", minimum=1) if line is not None else None
    max_lines = _positive_int(limit, "limit", minimum=1) if limit is not None else None

    # Jail first, protection second — the ADR-0045 frozen order. An escape
    # attempt that also names a protected subtree must report the escape.
    ws = Workspace(root=root)
    try:
        resolved = assert_inside_workspace(ws, candidate)
    except Exception as exc:
        raise AcpError(
            INVALID_PARAMS,
            f"path is outside the session workspace: {exc}",
        ) from exc
    reason = is_protected(ws, resolved)
    if reason is not None:
        raise AcpError(INVALID_PARAMS, f"refused: {reason}")

    if resolved.is_dir():
        raise AcpError(INVALID_PARAMS, "path is a directory, not a file")
    if not resolved.is_file():
        raise AcpError(RESOURCE_NOT_FOUND, "file not found", path=raw_path)

    try:
        with resolved.open("rb") as handle:
            chunk = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        raise AcpError(
            INVALID_PARAMS,
            f"cannot read file: {exc.strerror or type(exc).__name__}",
        ) from exc
    truncated = len(chunk) > MAX_READ_BYTES
    raw_bytes = chunk[:MAX_READ_BYTES]
    if b"\x00" in raw_bytes:
        raise AcpError(INVALID_PARAMS, "binary content refused: file contains NUL bytes")
    try:
        content = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AcpError(INVALID_PARAMS, "binary content refused: file is not valid UTF-8") from exc

    if start_line is not None or max_lines is not None:
        content = _slice_lines(content, start_line, max_lines)
    return ScopedFileRead(content=content, truncated=truncated)


def _positive_int(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcpError(INVALID_PARAMS, f"{name} must be an integer")
    if value < minimum:
        raise AcpError(INVALID_PARAMS, f"{name} must be >= {minimum}")
    return value


def _slice_lines(content: str, start_line: int | None, max_lines: int | None) -> str:
    """Apply the 1-based ``line``/``limit`` window to decoded content.

    Line semantics follow ACP's (1-based, ``limit`` caps the count served).
    Splitting on ``\\n`` only: the read already refused non-UTF-8 and NUL
    content, and re-interpreting ``\\r\\n`` here would edit the file's bytes
    under the reader.
    """
    lines = content.split("\n")
    start = (start_line - 1) if start_line is not None else 0
    if start >= len(lines):
        return ""
    selected = lines[start:]
    if max_lines is not None:
        selected = selected[:max_lines]
    return "\n".join(selected)
