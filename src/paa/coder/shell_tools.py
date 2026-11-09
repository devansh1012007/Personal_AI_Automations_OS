"""The ``bash`` work tool: a shell through the sandbox (ADR-0045, D-10).

SPEC DEVIATION (docs/adr/0045): the shell is ``pwsh`` on Windows and
``bash`` on POSIX, per owner decision. ``python -c`` is rejected because it
hides shell bugs; WSL-only bash is rejected because it blocks Windows
development. The argv is a frozen tuple, never a string split:
``("pwsh", "-NoProfile", "-Command", ...)`` or ``("bash", "-c", ...)``.
The command string itself is the shell script — that is the owner-chosen
surface — while ambient injection is bounded by the sandbox argv (no
``shell=True`` anywhere in this module), an explicit environment allowlist,
and a workspace jail on the child's cwd.

What this tool inherits from :class:`~paa.sandbox.base.Sandbox` rather than
re-implementing: the wall-clock kill switch (the whole process tree), the
environment built from empty, the output cap while draining, and the
per-run :attr:`~paa.sandbox.base.SandboxResult.isolation_level`. What it
adds is the tool-level contract the plan names for N1.3: timeout default
120 s and cap 600 s (invalid values are refused, never silently clamped),
a combined 64 KB output cap with an explicit truncation mark, an env dict
validated key-by-key before it reaches ``SandboxSpec.env``, and a result
that states ``exit_code``, ``timed_out`` and ``isolation_level`` on every
call — success or refusal.

The network limit is stated honestly, not papered over. The subprocess
backend shares the host network stack and cannot enforce
``allow_network=False``; its own ``run`` logs ``network_not_severed`` and
runs anyway (:meth:`SubprocessSandbox.run`). This tool therefore never
claims severance on a ``PROCESS`` backend: every result carries
``network_severed=False`` there, ``preflight()`` says the same before
anything executes, and a caller that sets
``require_network_severance=True`` is refused unless the backend's
isolation is ``NAMESPACE`` or better. Filesystem confinement is the same
shape: the child's cwd is the jailed workspace root, and at ``PROCESS``
that is a jail on where relative paths land, not a chroot — a shell script
can still name absolute paths. The tool does not pretend otherwise, and
the result's ``isolation_level`` is what the Wave-2 ledger must attest.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from paa.coder.workspace import (
    Workspace,
    WorkspaceViolation,
    assert_inside_workspace,
    is_protected,
)
from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.sandbox._win32 import IS_WINDOWS
from paa.sandbox.base import IsolationLevel, Sandbox, SandboxResult, SandboxSpec
from paa.sandbox.subprocess_backend import SubprocessSandbox

__all__ = [
    "BASH_TOOL_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "OUTPUT_CAP_BYTES",
    "BashTool",
    "build_bash_tool",
    "build_shell_tools",
    "default_sandbox",
    "preflight_shell",
    "shell_argv",
    "shell_executable",
]

log = structlog.get_logger(__name__)

#: The tool name. ``bash*`` already matches ``DEFAULT_MUTATING_TOOLS`` in
#: :mod:`paa.coder.permissions`, so PLAN refuses it and ASK prompts for it
#: without this module being listed anywhere else.
BASH_TOOL_NAME: str = "bash"

#: Wall-clock default and cap (N1.3). The cap is an *effective-timeout*
#: ceiling enforced by validation: a request above it is refused rather
#: than run with a longer budget the model did not acknowledge.
DEFAULT_TIMEOUT_SECONDS: float = 120.0
MAX_TIMEOUT_SECONDS: float = 600.0

#: Combined stdout+stderr cap (N1.3). The backend drains each stream while
#: storing at most ``max_capture_bytes``; this tool then enforces the
#: combined bound so no backend combination can return more than this many
#: characters in the transcript payload.
OUTPUT_CAP_BYTES: int = 64 * 1024

#: Defence-in-depth bounds on model-supplied strings. A shell script larger
#: than this has no legitimate use behind this tool, and an env block of
#: dozens of entries is either a splat attempt or a caller mistake.
_MAX_COMMAND_CHARS: int = 100_000
_MAX_ENV_ENTRIES: int = 64
_MAX_ENV_VALUE_CHARS: int = 8192

#: Portable env-var identifier. Anything else (spaces, ``=``, NUL, leading
#: digit) is refused rather than smuggled into ``SandboxSpec.env`` where a
#: backend would interpret it in an OS-specific way.
_ENV_NAME: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Schedule of accepted argument names. Anything else is refused so a typo
#: (``timeout`` for ``timeout_seconds``) fails loudly instead of being
#: silently ignored while the call runs with defaults.
_ALLOWED_ARGS: frozenset[str] = frozenset(
    {
        "command",
        "timeout_seconds",
        "env",
        "allow_network",
        "require_network_severance",
    }
)


def shell_executable() -> str:
    """The shell binary for this host: ``pwsh`` on Windows, ``bash`` else."""
    return "pwsh" if IS_WINDOWS else "bash"


def shell_argv(command: str) -> tuple[str, ...]:
    """The frozen argv for one shell script (ADR-0045, D-10).

    The script is a single argv element handed to ``-Command`` / ``-c``;
    no backend passes it through a shell of its own, so metacharacters
    inside the script are shell syntax by *design* (that is what a shell
    tool is) while the argv boundary itself cannot be broken by content.
    """
    if IS_WINDOWS:
        return ("pwsh", "-NoProfile", "-Command", command)
    return ("bash", "-c", command)


def default_sandbox() -> Sandbox:
    """The default backend: subprocess, with capture bounded at the tool cap."""
    return SubprocessSandbox(SandboxSettings(max_capture_bytes=OUTPUT_CAP_BYTES))


def _emit(payload: dict[str, Any]) -> str:
    """Render a payload as the JSON string the loop feeds back to the model."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _truncate_combined(
    stdout: str, stderr: str, backend_truncated: bool
) -> tuple[str, str, bool]:
    """Enforce the combined 64 KB cap, preserving stdout priority.

    Returns the (possibly shortened) streams and whether anything was cut,
    including cuts the backend already reported. Length is measured in
    characters; for the ASCII output a shell produces this coincides with
    bytes, and for UTF-8 text it stays within the byte cap's character
    count, so the bound holds a fortiori for the payload that carries it.
    """
    if len(stdout) + len(stderr) <= OUTPUT_CAP_BYTES:
        return stdout, stderr, backend_truncated
    kept_stdout = stdout[:OUTPUT_CAP_BYTES]
    room = OUTPUT_CAP_BYTES - len(kept_stdout)
    kept_stderr = stderr[:room] if room > 0 else ""
    return kept_stdout, kept_stderr, True


def _parse_timeout(value: Any) -> tuple[float | None, str | None]:
    """Validate ``timeout_seconds``: finite, positive, at or under the cap."""
    if isinstance(value, bool):
        return None, "timeout_seconds must be a number, not a boolean"
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None, "timeout_seconds must be a number"
    else:
        return None, "timeout_seconds must be a number"
    if not math.isfinite(number):
        return None, "timeout_seconds must be finite"
    if number <= 0:
        return None, "timeout_seconds must be positive"
    if number > MAX_TIMEOUT_SECONDS:
        return None, (
            f"timeout_seconds must not exceed {MAX_TIMEOUT_SECONDS:g}s "
            f"(got {number:g}s)"
        )
    return number, None


def _parse_env(value: Any) -> tuple[dict[str, str] | None, str | None]:
    """Validate the env allowlist without touching the host environment.

    The returned mapping is exactly what the model asked for — the child
    build-up (bootstrap keys plus this allowlist) stays in
    :func:`~paa.sandbox.base.build_child_env`. A host variable the caller
    never names here is absent by construction, not by deny-list
    vigilance. Keys must be portable identifiers; values must be strings
    without NUL. Anything else is refused rather than dropped silently.
    """
    if value is None:
        return {}, None
    if not isinstance(value, Mapping):
        return None, "env must be a mapping of string names to string values"
    if len(value) > _MAX_ENV_ENTRIES:
        return None, f"env must not contain more than {_MAX_ENV_ENTRIES} entries"
    parsed: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _ENV_NAME.match(key) is None:
            return None, (
                f"env key {key!r} is not a valid environment variable name"
            )
        if not isinstance(item, str):
            return None, f"env[{key!r}] must be a string"
        if "\x00" in item:
            return None, f"env[{key!r}] must not contain a NUL byte"
        if len(item) > _MAX_ENV_VALUE_CHARS:
            return None, (
                f"env[{key!r}] exceeds {_MAX_ENV_VALUE_CHARS} characters"
            )
        parsed[key] = item
    return parsed, None


def _parse_bool(value: Any, field: str) -> tuple[bool | None, str | None]:
    """Validate a boolean argument strictly (no truthy strings)."""
    if isinstance(value, bool):
        return value, None
    return None, f"{field} must be a boolean"


class BashTool:
    """Runs one shell script in the workspace through the sandbox.

    ``workspace`` is the frozen :class:`~paa.coder.workspace.Workspace`;
    the tool never resolves paths itself. Every call jail-then-protects the
    cwd (the frozen order in ADR-0045) and reports the isolation the
    backend actually delivered. ``sandbox`` defaults to the subprocess
    backend bounded at the output cap; N1.5 may hand in the runtime's
    backend instead, which is how a ``NAMESPACE`` backend makes
    ``allow_network=False`` mean something.
    """

    name: str = BASH_TOOL_NAME
    description: str = (
        "Run a shell command in the workspace through the sandbox "
        "(pwsh on Windows, bash on POSIX). Returns JSON with exit_code, "
        "timed_out, isolation_level, backend and network_severed "
        "(False on a subprocess backend, which cannot sever the host "
        "network), plus stdout/stderr capped at 64KB combined. "
        "Mutating: prompts in ASK mode and is refused in PLAN mode."
    )

    def __init__(
        self, workspace: Workspace, *, sandbox: Sandbox | None = None
    ) -> None:
        self._ws = workspace
        self._sandbox: Sandbox = (
            sandbox if sandbox is not None else default_sandbox()
        )

    def schema(self) -> dict[str, Any]:
        """JSON schema for the model: one required ``command`` string."""
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The shell script to run (pwsh -Command on Windows, "
                        "bash -c on POSIX). Runs with the workspace as cwd."
                    ),
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": (
                        "Wall-clock budget in seconds. Default 120, maximum 600. "
                        "Invalid values are refused, not clamped."
                    ),
                },
                "env": {
                    "type": "object",
                    "description": (
                        "Explicit allowlist of extra environment variables. "
                        "The host environment is never inherited."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "allow_network": {
                    "type": "boolean",
                    "description": (
                        "Whether the workload may use the network. Default False. "
                        "On a subprocess backend False cannot be enforced; the "
                        "result reports network_severed=False honestly."
                    ),
                },
                "require_network_severance": {
                    "type": "boolean",
                    "description": (
                        "Refuse unless allow_network=False can actually be "
                        "enforced (NAMESPACE or better). Default False: run "
                        "and report network_severed=False instead."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    def preflight(
        self,
        *,
        allow_network: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """What a call this tool would make actually gets, before it runs.

        Mirrors the subprocess backend's own ``preflight()`` vocabulary so
        an operator (or the ledger writer) reads one shape: the effective
        shell, the timeout and output caps, whether the filesystem is
        confined and whether the network is severed. At ``PROCESS`` the
        honest answers are all ``False`` — this method exists so nobody
        has to infer that from a docstring.
        """
        isolation = self._sandbox.isolation_level
        severed = (not allow_network) and (isolation >= IsolationLevel.NAMESPACE)
        return {
            "backend": self._sandbox.name,
            "isolation_level": isolation.name,
            "isolation_rank": int(isolation),
            "real_containment": isolation.is_real_containment,
            "network_severed": severed,
            "allow_network": allow_network,
            "filesystem_confined": isolation >= IsolationLevel.NAMESPACE,
            "syscall_filtered": isolation >= IsolationLevel.VM,
            "shell": shell_executable(),
            "argv_prefix": ["pwsh", "-NoProfile", "-Command"]
            if IS_WINDOWS
            else ["bash", "-c"],
            "timeout_seconds": timeout_seconds,
            "default_timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
            "output_cap_bytes": OUTPUT_CAP_BYTES,
            "workspace_cwd": str(self._ws.root),
        }

    async def run(self, arguments: Mapping[str, Any]) -> str:
        """Execute one shell script and return the JSON result for the model.

        Expected failures — bad arguments, a jail or protection refusal, a
        network-severance demand the backend cannot meet, a sandbox that
        cannot start — are returned as JSON with ``ok=False`` and an
        ``error`` string, following the runtime-tools convention. Success
        and ordinary command failure (non-zero exit, timeout) are normal
        results with ``ok=True``; the loop must not mistake a timed-out
        command for a tool fault.
        """
        backend = self._sandbox.name
        isolation = self._sandbox.isolation_level

        def located_error(
            error: str,
            resolved: Path | None,
            *,
            reason: str | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS,
            allow_network: bool = False,
        ) -> str:
            # ``reason`` carries the frozen protection/jail string when
            # there is one; it duplicates ``error`` only because the jail
            # contract exposes it as a separate ledger field.
            severed = (not allow_network) and (
                isolation >= IsolationLevel.NAMESPACE
            )
            payload: dict[str, Any] = {
                "ok": False,
                "error": error,
                "reason": reason if reason is not None else error,
                "exit_code": None,
                "timed_out": False,
                "killed_reason": None,
                "isolation_level": isolation.name,
                "backend": backend,
                "network_severed": severed,
                "allow_network": allow_network,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                "truncated_output": False,
                "duration_ms": 0.0,
                "memory_enforcement": "none",
                "workspace": str(resolved) if resolved is not None else str(self._ws.root),
                "shell": shell_executable(),
                "timeout_seconds": timeout,
            }
            log.warning(
                "coder.bash.refused",
                backend=backend,
                isolation_level=isolation.name,
                error=error[:300],
            )
            return _emit(payload)

        unknown = sorted(set(str(k) for k in arguments) - _ALLOWED_ARGS)
        if unknown:
            return located_error(
                "unknown argument(s): " + ", ".join(unknown), None
            )

        raw_command = arguments.get("command")
        if not isinstance(raw_command, str):
            return located_error("command must be a string", None)
        if not raw_command.strip():
            return located_error("command must be non-empty", None)
        if "\x00" in raw_command:
            return located_error("command must not contain a NUL byte", None)
        if len(raw_command) > _MAX_COMMAND_CHARS:
            return located_error(
                f"command exceeds {_MAX_COMMAND_CHARS} characters", None
            )

        raw_timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        timeout, timeout_error = _parse_timeout(raw_timeout)
        if timeout_error is not None or timeout is None:
            assert timeout_error is not None
            return located_error(timeout_error, None)

        env, env_error = _parse_env(arguments.get("env"))
        if env_error is not None or env is None:
            assert env_error is not None
            return located_error(env_error, None, timeout=timeout)

        raw_allow = arguments.get("allow_network", False)
        allow_network, allow_error = _parse_bool(raw_allow, "allow_network")
        if allow_error is not None or allow_network is None:
            assert allow_error is not None
            return located_error(allow_error, None, timeout=timeout)

        raw_require = arguments.get("require_network_severance", False)
        require_severance, require_error = _parse_bool(
            raw_require, "require_network_severance"
        )
        if require_error is not None or require_severance is None:
            assert require_error is not None
            return located_error(require_error, None, timeout=timeout)

        # Jail-then-protect the cwd, in the frozen order. The child runs
        # with the workspace root as its cwd; this is the workspace-root
        # check, not a check of any path inside the script, which the shell
        # states in its own way and a PROCESS backend cannot confine.
        try:
            resolved = assert_inside_workspace(self._ws, self._ws.root)
        except WorkspaceViolation as exc:
            return located_error(
                exc.message,
                None,
                reason=exc.reason,
                timeout=timeout,
                allow_network=allow_network,
            )
        try:
            protected = is_protected(self._ws, resolved)
        except WorkspaceViolation as exc:
            return located_error(
                exc.message,
                resolved,
                reason=exc.reason,
                timeout=timeout,
                allow_network=allow_network,
            )
        if protected is not None:
            return located_error(
                protected,
                resolved,
                reason=protected,
                timeout=timeout,
                allow_network=allow_network,
            )

        network_severed = (not allow_network) and (
            isolation >= IsolationLevel.NAMESPACE
        )
        if require_severance and not allow_network and not network_severed:
            return located_error(
                f"network severance required but backend {backend!r} "
                f"({isolation.name}) cannot enforce allow_network=False; "
                "use a NAMESPACE or better backend, or set allow_network=True",
                resolved,
                timeout=timeout,
                allow_network=allow_network,
            )

        argv = shell_argv(raw_command)
        try:
            spec = SandboxSpec(
                command=argv,
                workspace_path=resolved,
                env=env,
                timeout_seconds=timeout,
                allow_network=allow_network,
            )
        except ValueError as exc:
            return located_error(
                str(exc)[:2000],
                resolved,
                timeout=timeout,
                allow_network=allow_network,
            )
        try:
            result = await self._sandbox.run(spec)
        except SandboxError as exc:
            return located_error(
                exc.message,
                resolved,
                timeout=timeout,
                allow_network=allow_network,
            )

        return _emit(
            _result_payload(
                result,
                resolved=resolved,
                timeout=timeout,
                allow_network=allow_network,
            )
        )


def _result_payload(
    result: SandboxResult,
    *,
    resolved: Path,
    timeout: float,
    allow_network: bool,
) -> dict[str, Any]:
    """Turn a sandbox result into the tool's model-facing payload."""
    stdout, stderr, truncated = _truncate_combined(
        result.stdout, result.stderr, result.truncated_output
    )
    log.info(
        "coder.bash.completed",
        backend=result.backend,
        isolation_level=result.isolation_level.name,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        truncated=truncated,
        duration_ms=round(result.duration_ms, 3),
    )
    return {
        "ok": True,
        "error": None,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "killed_reason": result.killed_reason,
        "isolation_level": result.isolation_level.name,
        "backend": result.backend,
        "network_severed": bool(
            (not allow_network)
            and (result.isolation_level >= IsolationLevel.NAMESPACE)
        ),
        "allow_network": allow_network,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "truncated_output": truncated,
        "duration_ms": round(result.duration_ms, 3),
        "memory_enforcement": result.memory_enforcement,
        "workspace": str(resolved),
        "shell": shell_executable(),
        "timeout_seconds": timeout,
    }


def build_bash_tool(
    workspace: Workspace, *, sandbox: Sandbox | None = None
) -> BashTool:
    """Build the single ``bash`` tool bound to one workspace."""
    return BashTool(workspace, sandbox=sandbox)


def build_shell_tools(
    workspace: Workspace, *, sandbox: Sandbox | None = None
) -> dict[str, BashTool]:
    """The catalogue dict N1.5 merges into the agent's tools.

    One entry under the frozen name ``bash``. Kept as a factory (rather
    than a module-level singleton) because the workspace binds the tool to
    one tree, and a tool bound to the wrong tree must not be reusable.
    """
    return {"bash": BashTool(workspace, sandbox=sandbox)}


def preflight_shell(
    workspace: Workspace,
    *,
    sandbox: Sandbox | None = None,
    allow_network: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One-shot preflight for operators: what would a call here get?"""
    return BashTool(workspace, sandbox=sandbox).preflight(
        allow_network=allow_network,
        timeout_seconds=timeout_seconds,
    )
