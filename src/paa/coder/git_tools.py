"""Git tools for the coder agent (ADR-0045 N1.4, D-7).

Four :class:`~paa.coder.loop.Tool` implementations — ``git_status`` and
``git_diff`` (read-only) plus ``git_stage`` and ``git_commit`` (mutating) —
over a fixed git argv surface. The argv is built **in code only**: every call
is one of the literal tuples below, so the model can never smuggle
``push``/``fetch``/``config``/``--force``/free-form argv into a git
invocation. ``git_stage`` was renamed from ``git_add`` by owner fix: the
``git_add`` spelling matched zero of the ``DEFAULT_MUTATING_TOOLS`` globs and
would have been PLAN-exempt by accident (plan §D N1.4).
SPEC DEVIATION (docs/adr/0045): none — this module implements the frozen
surface from ``"Agent-facing behaviour frozen for downstream tools"``.

Each tool carries its jail inside ``run()`` (below every permission
decision, like an explicit deny below ``bypass``):

1. **Repo root check.** ``git rev-parse --show-toplevel`` must resolve to
   exactly ``ws.root``. A workspace that is not inside a git repository, or
   is merely a subdirectory of one, is refused — otherwise git would walk
   up and commit into a repo the jail never approved.
2. **Path jail, then protection** (for ``git_stage``/``git_diff`` paths,
   in that frozen order): every path is validated as a non-empty string,
   canonicalised by :func:`paa.coder.workspace.assert_inside_workspace`,
   then refused if :func:`paa.coder.workspace.is_protected` names a reason
   (``.git`` subtree, any-depth ``.env*``, ``PAA_HOME``). Validation is
   all-or-nothing: one protected or escaping path aborts the whole call
   before git runs, so a batch cannot stage "the safe half".

Subprocess discipline, borrowed from the sandbox backend's patterns
(``sandbox/subprocess_backend.py``): capped output reads that drain past the
cap (a chatty child deadlocks on a full pipe otherwise), a hard timeout that
kills the child, and an allowlisted child environment. The allowlist is what
kills the ``GIT_*`` injection family (``GIT_CONFIG_COUNT``/``GIT_SSH_COMMAND``/
``GIT_EXTERNAL_DIFF``/``GIT_PAGER``/…): the child inherits no ``GIT_*`` from
the ambient environment, only the small passthrough set plus the forced
``GIT_TERMINAL_PROMPT=0``.

Fixed argv hardening, all code-side so the model cannot reach it:

- ``--no-pager`` on every invocation: no pager *command* from repo config
  is ever executed, even when stdout is a tty.
- ``git_diff`` adds ``--no-ext-diff --no-textconv``: external diff/textconv
  *drivers* configured in repo config never execute.
- ``git_commit`` runs ``-c core.hooksPath=`` (empty) so no commit hooks —
  pre-commit, commit-msg, post-commit — execute from ``.git/hooks``. This
  is the D-7 "no hooks" mechanism: an internally fixed ``-c`` that
  *disables*, never a config the model can write, and there is no ``git
  config`` tool to set one. ``--no-gpg-sign`` prevents a repo-configured
  signing program from executing on a commit the agent made. The message
  always travels as the value of ``-m`` (never ``--amend``/editor), as a
  fixed pair, so ``{"message": "--amend"}`` is a subject, not a flag.

Known residual, stated plainly: ``.gitattributes`` filter drivers
(``filter.<name>.clean``) and ``core.fsmonitor`` are repo-configured
executables that run during ``git add``/``git status`` whichever tool drives
git. The mitigations are the same ones the workspace gives: ``.git`` is
protected (the agent cannot write ``.git/config`` or ``.gitattributes``
through the fs/write tools), the env is allowlisted (no ``GIT_*`` override),
and the workspace root must be the repository root it approved. A workspace
that already contains hostile git configuration is a hostile input to any
git client, not an escape of this boundary.

Errors are returned as JSON values, not raised (the ``runtime_tools.py``
convention): the loop feeds the return string back to the model, and a
model-readable refusal (``{"error": "..."}``) teaches it what it cannot do.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.coder.workspace import (
    Workspace,
    WorkspaceViolation,
    assert_inside_workspace,
    is_protected,
)

__all__ = [
    "GIT_MUTATING_TOOLS",
    "GIT_READ_ONLY_TOOLS",
    "build_git_tools",
]

#: The tools that change repository state. ``git_stage`` needs the
#: ``git_stage*`` glob in :data:`paa.coder.permissions.DEFAULT_MUTATING_TOOLS`
#: (owner fix, N1.5) — without it, PLAN mode exempts staging by accident.
#: ``git_commit`` already matches the existing ``*_commit*`` glob.
GIT_MUTATING_TOOLS: tuple[str, ...] = ("git_stage", "git_commit")

#: The read-only tools, for the agent to standing-allow in ASK mode
#: (``CoderAgent.build`` ``allow=[...]``) exactly like ``READ_ONLY_TOOLS``.
GIT_READ_ONLY_TOOLS: frozenset[str] = frozenset({"git_status", "git_diff"})

#: Agent-facing caps (ADR-0045: "64 KB read and output caps, truncated
#: and marked"). Stored bytes above the cap are drained and discarded, so
#: a huge diff reports ``truncated: true`` instead of exhausting RAM.
_OUTPUT_CAP_BYTES = 64 * 1024

#: Hard per-invocation bound. Git work here is local and fast; this is a
#: defensive ceiling, never a retry. On expiry the child is killed and the
#: timeout is reported as a value.
_GIT_TIMEOUT_SECONDS = 120.0

#: How long to wait for a capped reader after the child is gone (mirrors
#: ``sandbox/subprocess_backend.py``'s drain bound: a grandchild holding
#: the pipe must not hold the runtime hostage).
_DRAIN_TIMEOUT_SECONDS = 5.0

#: How many paths a single stage/diff call may name. Bounds argv length and
#: the per-path jail cost (one canonicalisation each).
_MAX_PATHS = 100

#: Commit message bound: a real subject plus a body, never a dump.
_MAX_MESSAGE_CHARS = 2048

#: The complete child-environment passthrough. Everything else — and every
#: ``GIT_*`` variable in particular — is dropped before the child starts.
_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
    }
)

#: Forced child variables: no credential prompt (there are no network
#: operations to authenticate, and a prompt would hang on a closed stdin).
_ENV_FORCED: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}


@dataclass(frozen=True, slots=True)
class _GitResult:
    """One finished git invocation, already capped and decoded."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


def _child_env() -> dict[str, str]:
    """The exact environment a git child starts with. Ambient secrets and
    every ``GIT_*`` override are dropped; the passthrough set covers PATH
    (finding ``git`` and its helpers) and the home variables git needs to
    locate the global config and trust database."""
    env = {key: value for key, value in os.environ.items() if key in _ENV_PASSTHROUGH}
    env.update(_ENV_FORCED)
    return env


async def _read_capped(
    stream: asyncio.StreamReader | None, max_bytes: int
) -> tuple[bytes, bool]:
    """Read a pipe keeping at most ``max_bytes`` (store-capped, drain-full).

    The asymmetry that matters (see
    ``sandbox/subprocess_backend.py::_read_capped``): past the cap we keep
    draining but stop storing, so a verbose child fills neither RAM nor the
    OS pipe buffer — which would deadlock it mid-write and turn a chatty
    but correct call into a timeout.
    """
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        try:
            chunk = await stream.read(65536)
        except (ValueError, asyncio.LimitOverrunError):  # pragma: no cover - defensive
            truncated = True
            break
        if not chunk:
            break
        if total < max_bytes:
            room = max_bytes - total
            chunks.append(chunk[:room])
            if len(chunk) > room:
                truncated = True
            total += len(chunk)
        else:
            truncated = True
    return b"".join(chunks), truncated


async def _collect(
    reader: asyncio.Task[tuple[bytes, bool]],
) -> tuple[bytes, bool]:
    """Await a reader, bounded; a surviving pipe-holder must not hang us."""
    try:
        return await asyncio.wait_for(asyncio.shield(reader), timeout=_DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reader
        return b"", True


async def _run_git(argv: tuple[str, ...], *, cwd: Path) -> _GitResult:
    """Run one fixed git argv under the caps. Raises only via OSError from
    spawning (the caller maps it to an error payload)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=_child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    out_reader: asyncio.Task[tuple[bytes, bool]] = asyncio.create_task(
        _read_capped(proc.stdout, _OUTPUT_CAP_BYTES)
    )
    err_reader: asyncio.Task[tuple[bytes, bool]] = asyncio.create_task(
        _read_capped(proc.stderr, _OUTPUT_CAP_BYTES)
    )
    timed_out = False
    try:
        exit_code = await asyncio.wait_for(proc.wait(), timeout=_GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        timed_out = True
        proc.kill()
        exit_code = await proc.wait()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    stdout_bytes, stdout_truncated = await _collect(out_reader)
    stderr_bytes, stderr_truncated = await _collect(err_reader)
    return _GitResult(
        exit_code=exit_code,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        truncated=stdout_truncated or stderr_truncated,
        timed_out=timed_out,
    )


async def _preflight(ws: Workspace, verb: str) -> dict[str, Any] | None:
    """Refuse unless ``ws.root`` is exactly the git repository root.

    Runs ``git rev-parse --show-toplevel`` (fixed argv) and canonical-
    compares both sides. Returns an error payload, or ``None`` when the
    workspace is allowed onward. Checks run per call: a repository can be
    de-initialised or re-rooted between calls in a long session.
    """
    try:
        result = await _run_git(
            ("git", "--no-pager", "rev-parse", "--show-toplevel"), cwd=ws.root
        )
    except OSError as exc:
        return {"error": f"git executable could not be started: {exc}"}
    if result.timed_out:
        return {"error": "git timed out", "timed_out": True}
    if result.exit_code != 0:
        return {
            "error": "workspace is not inside a git repository",
            "detail": result.stderr.strip()[:500],
            "exit_code": result.exit_code,
        }
    toplevel = result.stdout.strip()
    try:
        resolved_top = Path(toplevel).expanduser().resolve()
        resolved_root = ws.root.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return {"error": "git repository root could not be canonicalised"}
    if resolved_top != resolved_root:
        return {
            "error": f"{verb}: workspace root is not the git repository root",
            "toplevel": str(resolved_top),
            "workspace": str(resolved_root),
        }
    return None


def _emit(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _parse_paths(arguments: Mapping[str, Any]) -> list[str] | str:
    """Validate the optional/required ``paths`` array. Returns the list, or
    an error payload string. Rejects non-strings, empty/blank entries and
    over-long lists before any ``Path`` conversion (``Path("")`` is ``"."``
    — the N1.0a caller contract)."""
    raw = arguments.get("paths")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return _emit({"error": "paths must be an array of strings"})
    if len(raw) > _MAX_PATHS:
        return _emit({"error": f"paths exceeds the {_MAX_PATHS}-path limit"})
    paths: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return _emit({"error": "each path must be a non-empty string"})
        paths.append(item)
    return paths


def _jail_paths(ws: Workspace, paths: list[str]) -> list[Path] | dict[str, Any]:
    """Jail-then-protect every path; first refusal wins (all-or-nothing).

    ``ws.root / Path(raw)`` with an absolute ``raw`` keeps the absolute path
    (pathlib discards the left side), so an absolute path *inside* the
    workspace is allowed on the same terms as a relative one — both go
    through the canonical-compare jail either way. A bare ``Path(raw)``
    would resolve against the process cwd, never the workspace.
    """
    jailed: list[Path] = []
    for raw in paths:
        try:
            resolved = assert_inside_workspace(ws, ws.root / Path(raw))
        except WorkspaceViolation as exc:
            return {"error": f"path escapes the workspace jail: {raw}", "reason": exc.reason}
        reason = is_protected(ws, resolved)
        if reason is not None:
            return {"error": f"{reason}: {raw}", "reason": reason}
        jailed.append(resolved)
    return jailed


def _relative_paths(ws: Workspace, resolved: list[Path]) -> tuple[str, ...]:
    """Repository-relative, forward-slash pathspecs for the argv tail."""
    root = ws.root.expanduser().resolve()
    return tuple(p.relative_to(root).as_posix() for p in resolved)


class _GitTool:
    """Shared plumbing: the ``Tool`` protocol plus JSON output.

    Subclasses declare their fixed argv builder; ``run()`` always starts
    with the repository-root preflight, so no tool can act on a workspace
    that is not the git root.
    """

    name: str
    description: str

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws

    def schema(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def run(self, arguments: Mapping[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError


class _GitStatus(_GitTool):
    name = "git_status"
    description = (
        "Report working-tree status (porcelain v1): staged, modified and "
        "untracked entries. Read-only; safe in PLAN mode."
    )

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, arguments: Mapping[str, Any]) -> str:
        if error := await _preflight(self._ws, "git_status"):
            return _emit(error)
        try:
            result = await _run_git(
                (
                    "git",
                    "--no-pager",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=normal",
                ),
                cwd=self._ws.root,
            )
        except OSError as exc:
            return _emit({"error": f"git executable could not be started: {exc}"})
        if result.timed_out:
            return _emit({"error": "git_status timed out", "timed_out": True})
        if result.exit_code != 0:
            return _emit(
                {
                    "error": f"git status failed (exit {result.exit_code})",
                    "detail": result.stderr.strip()[:500],
                    "exit_code": result.exit_code,
                }
            )
        return _emit(
            {
                "status": result.stdout,
                "exit_code": 0,
                "truncated": result.truncated,
            }
        )


class _GitDiff(_GitTool):
    name = "git_diff"
    description = (
        "Show the working-tree diff (or, with staged=true, the staged "
        "diff). Read-only. Output is capped at 64 KB and marked when "
        "truncated."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show the staged (index) diff instead of the working-tree diff.",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_PATHS,
                    "description": "Limit the diff to these workspace-relative paths.",
                },
            },
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        if error := await _preflight(self._ws, "git_diff"):
            return _emit(error)
        parsed = _parse_paths(arguments)
        if isinstance(parsed, str):
            return parsed
        resolved: list[Path] = []
        if parsed:
            jailed = _jail_paths(self._ws, parsed)
            if isinstance(jailed, dict):
                return _emit(jailed)
            resolved = jailed
        staged = arguments.get("staged") is True
        argv: tuple[str, ...] = (
            "git",
            "--no-pager",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            *(("--cached",) if staged else ()),
            "--",
            *_relative_paths(self._ws, resolved),
        )
        try:
            result = await _run_git(argv, cwd=self._ws.root)
        except OSError as exc:
            return _emit({"error": f"git executable could not be started: {exc}"})
        if result.timed_out:
            return _emit({"error": "git_diff timed out", "timed_out": True})
        if result.exit_code != 0:
            return _emit(
                {
                    "error": f"git diff failed (exit {result.exit_code})",
                    "detail": result.stderr.strip()[:500],
                    "exit_code": result.exit_code,
                }
            )
        return _emit(
            {
                "diff": result.stdout,
                "staged": staged,
                "exit_code": 0,
                "truncated": result.truncated,
            }
        )


class _GitStage(_GitTool):
    name = "git_stage"
    description = (
        "Stage the named workspace-relative paths (fixed argv: git add -- "
        "<paths>). Mutating: prompts in ASK, refused in PLAN. Every path "
        "is jail- and protection-checked before git runs."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": _MAX_PATHS,
                    "description": "Workspace-relative paths to stage.",
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        if error := await _preflight(self._ws, "git_stage"):
            return _emit(error)
        parsed = _parse_paths(arguments)
        if isinstance(parsed, str):
            return parsed
        if not parsed:
            return _emit({"error": "git_stage requires at least one path"})
        jailed = _jail_paths(self._ws, parsed)
        if isinstance(jailed, dict):
            return _emit(jailed)
        argv = ("git", "--no-pager", "add", "--", *_relative_paths(self._ws, jailed))
        try:
            result = await _run_git(argv, cwd=self._ws.root)
        except OSError as exc:
            return _emit({"error": f"git executable could not be started: {exc}"})
        if result.timed_out:
            return _emit({"error": "git_stage timed out", "timed_out": True})
        if result.exit_code != 0:
            return _emit(
                {
                    "error": f"git add failed (exit {result.exit_code})",
                    "detail": result.stderr.strip()[:500],
                    "exit_code": result.exit_code,
                }
            )
        return _emit(
            {
                "staged": list(_relative_paths(self._ws, jailed)),
                "output": result.stderr,
                "exit_code": 0,
            }
        )


class _GitCommit(_GitTool):
    name = "git_commit"
    description = (
        "Commit the staged changes with the given message. Mutating: "
        "prompts in ASK, refused in PLAN. Never pushes, amends, or runs "
        "hooks; committing with nothing staged fails cleanly."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_MESSAGE_CHARS,
                    "description": "Commit message (subject and optional body).",
                }
            },
            "required": ["message"],
            "additionalProperties": False,
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        if error := await _preflight(self._ws, "git_commit"):
            return _emit(error)
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            return _emit({"error": "git_commit requires a non-empty 'message'"})
        if len(message) > _MAX_MESSAGE_CHARS:
            return _emit({"error": f"message exceeds the {_MAX_MESSAGE_CHARS} character limit"})
        argv = (
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=",
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        )
        try:
            result = await _run_git(argv, cwd=self._ws.root)
        except OSError as exc:
            return _emit({"error": f"git executable could not be started: {exc}"})
        if result.timed_out:
            return _emit({"error": "git_commit timed out", "timed_out": True})
        if result.exit_code != 0:
            # The acceptance case: nothing staged. Git's nonzero exit is
            # the signal; the payload carries it as a value, so the model
            # sees "stage something first" instead of a traceback.
            return _emit(
                {
                    "error": f"git commit failed (exit {result.exit_code})",
                    "detail": (result.stdout or result.stderr).strip()[:500],
                    "exit_code": result.exit_code,
                }
            )
        # The commit already succeeded; the sha lookup must never re-raise
        # into the row above. OSError here is near-impossible (the same spawn
        # just succeeded), and the commit output survives in the payload.
        try:
            head = await _run_git(("git", "--no-pager", "rev-parse", "HEAD"), cwd=self._ws.root)
        except OSError:
            return _emit(
                {
                    "commit": result.stdout.strip(),
                    "message": message,
                    "output": result.stdout,
                    "exit_code": 0,
                }
            )
        return _emit(
            {
                "commit": head.stdout.strip() or result.stdout.strip(),
                "message": message,
                "output": result.stdout,
                "exit_code": 0,
            }
        )


def build_git_tools(ws: Workspace) -> dict[str, Any]:
    """The four git tools bound to one jailed workspace (``ws	root`` must be
    the git repository root; every call re-verifies)."""
    return {t.name: t for t in (_GitStatus(ws), _GitDiff(ws), _GitStage(ws), _GitCommit(ws))}
