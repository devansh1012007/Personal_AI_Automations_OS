"""A sandbox that executes nothing and records what would have run.

Two uses, both real:

**SUPERVISED planning-only mode.** ``PermissionMode.SUPERVISED`` grants
``PERM_SANDBOX_RUN`` but gates every mutation on a human. Planning a task end
to end and showing the operator the exact argv, limits and workspace *before*
anything executes is the point of that mode; this backend is what makes the
plan inspectable without a side effect.

**Deterministic tests.** Every other backend depends on a real process, real
timing and a real filesystem. Tests of the layers *above* the sandbox — policy,
orchestration, ledger — need none of that, and would otherwise be slow and
flaky in proportion to how much of the stack they touch.

Isolation level is :attr:`~paa.sandbox.base.IsolationLevel.NONE`: nothing runs,
so there is nothing to contain. That is the honest reading, and it also means a
policy layer that requires real containment will correctly refuse to accept
this backend for live work.
"""

from __future__ import annotations

from typing import Any

import structlog

from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSpec,
    build_child_env,
)

__all__ = ["DryRunSandbox"]

log = structlog.get_logger(__name__)


class DryRunSandbox(Sandbox):
    """Records invocations and returns a canned success.

    The recorded environment is the *real* one
    :func:`~paa.sandbox.base.build_child_env` would construct, so a test can
    assert on env-isolation behaviour without spawning anything — the property
    under test is the env construction, not the process.
    """

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._duration_ms = duration_ms
        self.invocations: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "dryrun"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def healthcheck(self) -> bool:
        """Always healthy — there is nothing that could be unhealthy."""
        return True

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Record the invocation and return the canned result.

        The workspace is deliberately **not** resolved or required to exist:
        planning happens before a workspace is provisioned, and refusing to
        plan against a not-yet-created directory would defeat the purpose.
        """
        record = {
            "command": list(spec.command),
            "workspace_path": str(spec.workspace_path),
            "read_only_mounts": [str(p) for p in spec.read_only_mounts],
            "env": build_child_env(spec),
            "env_keys": sorted(build_child_env(spec)),
            "memory_mb": spec.memory_mb,
            "cpu_cores": spec.cpu_cores,
            "timeout_seconds": spec.timeout_seconds,
            "allow_network": spec.allow_network,
            "recursion_depth": spec.recursion_depth,
            "parent_task_id": spec.parent_task_id,
        }
        self.invocations.append(record)
        log.info(
            "sandbox.dryrun.recorded",
            command=record["command"][:3],
            workspace=record["workspace_path"],
            depth=spec.recursion_depth,
        )
        return SandboxResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
            duration_ms=self._duration_ms,
            peak_rss_mb=None,
            timed_out=False,
            killed_reason=None,
            truncated_output=False,
            backend=self.name,
            isolation_level=self.isolation_level,
            memory_enforcement="none",
        )

    @property
    def last_invocation(self) -> dict[str, Any] | None:
        return self.invocations[-1] if self.invocations else None

    def reset(self) -> None:
        self.invocations.clear()
