"""The recorder's mediated toolset — what a harness may ask PAA to do (C7.3).

These are the tools the MCP policy proxy exposes. The set is
deliberately small and deliberately *boring*: run a command, read a
file, write a file. A harness that wants more reach runs it as a
command and meets the same policy and the same journal either way —
the point of the recorder is not to be a rich tool server, it is to
make **one deterministic policy** decide every effect, the same way,
for every harness.

Two containment stories, stated apart because they are different:

* **``bash``** executes under the sandbox's one-shot contract —
  workspace jail, resource ceilings, whole-tree kill. That is real
  containment and it is the strongest thing on offer.
* **``read_file`` / ``write_file``** are mediated *in-process*: the
  workspace jail is enforced (paths are resolved and checked before
  anything touches disk, exactly :func:`paa.sandbox.base.
  assert_inside_workspace`) and sizes are capped, but there is no
  subprocess around them. The record says which path each tool took
  (``executed: sandbox`` vs ``executed: in-process, jailed``), so a
  reviewer never has to guess what "mediated" meant for a given row.

The name ``bash`` is the policy vocabulary's name for *execute a
command* — the permission engine's mutating-tool globs
(:data:`paa.coder.permissions.DEFAULT_MUTATING_TOOLS`) speak ``bash*``,
so a tool named ``run_command`` would quietly escape the PLAN-mode
refusal that ``bash`` meets. The implementation still never passes
anything through a shell: ``argv`` is a list of strings handed to the
sandbox verbatim, exactly like every other command this substrate
spawns.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import SandboxError
from paa.sandbox.base import Sandbox, SandboxSpec, assert_inside_workspace

__all__ = [
    "INLINE_CAP_BYTES",
    "READ_CAP_BYTES",
    "TOOL_SPECS",
    "ToolExecution",
    "execute_tool",
    "validate_arguments",
]

#: How much of any stream the record carries inline. The full byte count
#: and the SHA-256 of the full stream ride beside the kept prefix, so a
#: capped row is an honest prefix of a measured whole, not a guess.
INLINE_CAP_BYTES = 16 * 1024

#: How much a ``read_file`` may return inline. Larger files are refused
#: with the byte count named — a harness that needs more should say so
#: with a command, which lands under the sandbox's own caps.
READ_CAP_BYTES = 256 * 1024

#: Per-tool-call execution budget for ``bash``.
TOOL_TIMEOUT_S = 120.0
TOOL_MEMORY_MB = 512

#: The toolset, as ``tools/list`` serves it. Input schemas are the JSON
#: Schema subset the MCP server already speaks (object, string, array,
#: required) — one schema dialect across both servers.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "bash": {
        "name": "bash",
        "description": (
            "Run one command as argv (never through a shell) under the "
            "sandbox: workspace jail, resource ceilings, whole-tree kill. "
            "Journaled in the run record and decided by the run's policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The command to run, e.g. [\"git\", \"status\"].",
                }
            },
            "required": ["argv"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": (
            "Read one file inside the workspace jail (in-process, size-capped). "
            "Journaled in the run record and decided by the run's policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative or absolute path inside the jail.",
                }
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "name": "write_file",
        "description": (
            "Write one file inside the workspace jail (in-process, parent "
            "directories created). Journaled in the run record and decided "
            "by the run's policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative or absolute path inside the jail.",
                },
                "content": {"type": "string", "description": "The full file content."},
            },
            "required": ["path", "content"],
        },
    },
}


def validate_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Shape-check one tool call; returns the normalised arguments.

    Raises ``ValueError`` with the named defect — the proxy maps that to
    the protocol's invalid-params error. Validating shape *before*
    policy is deliberate: the policy renders arguments to match rules
    against, and a rule should never be asked to weigh a value whose
    shape is already nonsense.
    """
    if tool not in TOOL_SPECS:
        raise ValueError(f"unknown tool {tool!r}")
    if tool == "bash":
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv:
            raise ValueError("bash needs a non-empty 'argv' list of strings")
        if not all(isinstance(part, str) and part for part in argv):
            raise ValueError("bash 'argv' entries must be non-empty strings")
        return {"argv": list(argv)}
    if tool == "read_file":
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("read_file needs a non-empty 'path' string")
        return {"path": path}
    path = arguments.get("path")
    content = arguments.get("content")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("write_file needs a non-empty 'path' string")
    if not isinstance(content, str):
        raise ValueError("write_file needs a 'content' string")
    return {"path": path, "content": content}


def _cap(text: str, *, cap_bytes: int) -> tuple[str, bool, int]:
    """(kept prefix, truncated?, full encoded length)."""
    encoded_len = len(text.encode("utf-8", errors="replace"))
    if encoded_len <= cap_bytes:
        return text, False, encoded_len
    kept = text.encode("utf-8", errors="replace")[:cap_bytes].decode(
        "utf-8", errors="ignore"
    )
    return kept, True, encoded_len


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _stream_fact(name: str, text: str) -> dict[str, Any]:
    kept, truncated, full_len = _cap(text, cap_bytes=INLINE_CAP_BYTES)
    return {
        "stream": name,
        "bytes": full_len,
        "sha256": _sha256_text(text),
        "truncated": truncated,
        "text": kept,
    }


def _resolve_target(raw: str, workspace: Path) -> Path:
    """A relative tool path is workspace-relative, never CWD-relative.

    A harness that asks for ``src/main.py`` means the workspace's
    ``src/main.py``; resolving against the proxy's own CWD would make
    the same call mean different things depending on where the operator
    happened to stand when they started the run — and would silently
    point the jail check at the wrong tree.
    """
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else workspace / candidate


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """One executed (or refused-by-execution) tool call's outcome.

    ``ok`` is the tool's own verdict: exit 0 for ``bash``, a read or
    write that landed for the file tools. A policy refusal never
    produces one of these — it produces no execution at all, which is
    the entire difference between *denied* and *failed*. A jail
    refusal is ``executed="refused"`` with the reason named: the call
    was decided (allowed) and then refused by the boundary itself, a
    different fact from both, journalled as its own row.
    """

    ok: bool
    executed: str
    exit_code: int | None
    duration_ms: float
    streams: tuple[dict[str, Any], ...]
    path: str | None = None
    bytes_written: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "executed": self.executed,
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 3),
            "streams": [dict(s) for s in self.streams],
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.bytes_written is not None:
            payload["bytes"] = self.bytes_written
        if self.error is not None:
            payload["error"] = self.error
        return payload


async def execute_tool(
    sandbox: Sandbox,
    workspace: Path,
    tool: str,
    arguments: dict[str, Any],
) -> ToolExecution:
    """Execute one validated tool call. The proxy's whole executor.

    Every path here produces a :class:`ToolExecution` — an execution
    that fails is a *result*, not an exception, because the harness
    must see the failure and the record must carry it. The one raise
    is the record's own failure, which is the caller's business.
    """
    started = time.monotonic()
    try:
        return await _execute(sandbox, workspace, tool, arguments, started)
    except SandboxError as exc:
        # The jail refused the target before anything touched disk:
        # a refused row, not a crash and not a silent success.
        return ToolExecution(
            ok=False,
            executed="refused",
            exit_code=None,
            duration_ms=(time.monotonic() - started) * 1000,
            streams=(),
            error=exc.message,
        )


async def _execute(
    sandbox: Sandbox,
    workspace: Path,
    tool: str,
    arguments: dict[str, Any],
    started: float,
) -> ToolExecution:
    if tool == "bash":
        result = await sandbox.run(
            SandboxSpec(
                command=tuple(arguments["argv"]),
                workspace_path=workspace,
                timeout_seconds=TOOL_TIMEOUT_S,
                memory_mb=TOOL_MEMORY_MB,
            )
        )
        streams: tuple[dict[str, Any], ...] = (
            _stream_fact("stdout", result.stdout),
            _stream_fact("stderr", result.stderr),
        )
        return ToolExecution(
            ok=result.ok,
            executed="sandbox",
            exit_code=result.exit_code,
            duration_ms=(time.monotonic() - started) * 1000,
            streams=streams,
            error=(
                "the command was killed by the sandbox: "
                f"{result.killed_reason or 'signal'}"
                if not result.ok and result.exit_code is None
                else None
            ),
        )
    if tool == "read_file":
        target = assert_inside_workspace(_resolve_target(arguments["path"], workspace), workspace)
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolExecution(
                ok=False,
                executed="in-process",
                exit_code=None,
                duration_ms=(time.monotonic() - started) * 1000,
                streams=(),
                path=str(target),
                error=f"read failed: {exc}",
            )
        encoded_len = len(text.encode("utf-8", errors="replace"))
        if encoded_len > READ_CAP_BYTES:
            return ToolExecution(
                ok=False,
                executed="in-process",
                exit_code=None,
                duration_ms=(time.monotonic() - started) * 1000,
                streams=(),
                path=str(target),
                error=(
                    f"the file is {encoded_len} bytes, over the "
                    f"{READ_CAP_BYTES}-byte read cap — use a command for "
                    "large files"
                ),
            )
        return ToolExecution(
            ok=True,
            executed="in-process",
            exit_code=0,
            duration_ms=(time.monotonic() - started) * 1000,
            streams=(_stream_fact("stdout", text),),
            path=str(target),
        )
    # write_file
    target = assert_inside_workspace(_resolve_target(arguments["path"], workspace), workspace)
    content = arguments["content"]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolExecution(
            ok=False,
            executed="in-process",
            exit_code=None,
            duration_ms=(time.monotonic() - started) * 1000,
            streams=(),
            path=str(target),
            error=f"write failed: {exc}",
        )
    return ToolExecution(
        ok=True,
        executed="in-process",
        exit_code=0,
        duration_ms=(time.monotonic() - started) * 1000,
        streams=(),
        path=str(target),
        bytes_written=len(content.encode("utf-8")),
    )
