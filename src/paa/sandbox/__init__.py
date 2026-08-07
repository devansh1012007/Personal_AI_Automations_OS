"""Containment: pluggable sandbox backends and delegation limits.

RFC §13/§14 assume gVisor everywhere. gVisor cannot run on Windows, so this
package makes the containment substrate a *choice* with an honestly declared
:class:`~paa.sandbox.base.IsolationLevel`, rather than a single assumed
implementation whose absence would be silent.

Read :mod:`paa.sandbox.subprocess_backend` before deploying on Windows. Its
isolation is materially weaker than gVisor's and the docstring says exactly
where the gaps are.

SPEC DEVIATION (docs/adr/0006, docs/adr/0009).
"""

from __future__ import annotations

import structlog

from paa.config import SandboxSettings
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSpec,
    assert_inside_workspace,
    build_child_env,
    resolve_workspace,
)
from paa.sandbox.docker_backend import DockerSandbox
from paa.sandbox.dryrun_backend import DryRunSandbox
from paa.sandbox.recursion import DelegationGraph, RecursionGuard
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.sandbox.watchdog import (
    HeartbeatTracker,
    ResourceSample,
    ResourceWatchdog,
    SamplerSource,
    WorkerLiveness,
    sample_rss_mb,
)
from paa.sandbox.wsl_backend import WslSandbox, to_wsl_path

__all__ = [
    "DelegationGraph",
    "DockerSandbox",
    "DryRunSandbox",
    "HeartbeatTracker",
    "IsolationLevel",
    "RecursionGuard",
    "ResourceSample",
    "ResourceWatchdog",
    "SamplerSource",
    "Sandbox",
    "SandboxResult",
    "SandboxSpec",
    "SubprocessSandbox",
    "WorkerLiveness",
    "WslSandbox",
    "assert_inside_workspace",
    "build_backend",
    "build_child_env",
    "get_sandbox",
    "resolve_workspace",
    "sample_rss_mb",
    "to_wsl_path",
]

log = structlog.get_logger(__name__)

#: Preference order for ``backend="auto"``, strongest containment first.
#: Docker (possibly gVisor) beats WSL2 namespaces beats a bare host process.
_AUTO_ORDER = ("docker", "wsl", "subprocess")


def build_backend(name: str, settings: SandboxSettings | None = None) -> Sandbox:
    """Construct one named backend without probing its health.

    Synchronous, so callers that already know what they want (tests, an
    explicit config) do not have to be async just to build an object.
    """
    settings = settings or SandboxSettings()
    match name:
        case "subprocess":
            return SubprocessSandbox(settings)
        case "wsl":
            return WslSandbox(settings)
        case "docker":
            return DockerSandbox(settings)
        case "dryrun":
            return DryRunSandbox()
        case _:
            raise ValueError(
                f"unknown sandbox backend {name!r}; "
                f"expected one of auto, subprocess, wsl, docker, dryrun"
            )


async def get_sandbox(settings: SandboxSettings | None = None) -> Sandbox:
    """Select a backend per :attr:`SandboxSettings.backend`.

    Async because ``"auto"`` must actually *probe* — asking whether Docker is
    running means talking to the daemon. Selecting on ``shutil.which`` alone
    would hand work to a CLI whose daemon is stopped, which on a Windows dev
    machine is the common case rather than the edge case.

    ``"auto"`` walks :data:`_AUTO_ORDER` strongest-first and takes the first
    healthy backend. ``SubprocessSandbox`` is always healthy, so the walk
    terminates — but note what that means: **auto never fails, it degrades**.
    A caller that requires real containment must check
    ``sandbox.isolation_level.is_real_containment`` rather than assume the
    factory would have refused. The factory logs a warning when it lands on
    PROCESS-level isolation precisely because that silence would otherwise be
    the dangerous part.

    An explicitly named backend is returned **without** a healthcheck: an
    operator who wrote ``backend="docker"`` wants a loud failure at run time,
    not a silent downgrade to something weaker than they asked for.
    """
    settings = settings or SandboxSettings()

    if settings.backend != "auto":
        backend = build_backend(settings.backend, settings)
        log.info(
            "sandbox.backend_selected",
            backend=backend.name,
            isolation=backend.isolation_level.name,
            mode="explicit",
        )
        return backend

    for candidate in _AUTO_ORDER:
        backend = build_backend(candidate, settings)
        try:
            healthy = await backend.healthcheck()
        except Exception as exc:  # pragma: no cover - healthchecks must not raise
            log.warning("sandbox.healthcheck_raised", backend=candidate, error=str(exc))
            continue
        if not healthy:
            continue

        if not backend.isolation_level.is_real_containment:
            log.warning(
                "sandbox.weak_isolation_selected",
                backend=backend.name,
                isolation=backend.isolation_level.name,
                detail=(
                    "no namespace-or-better backend is available; containment is "
                    "a workspace jail, resource caps and an AST pre-scan only. "
                    "See docs/adr/0006."
                ),
            )
        else:
            log.info(
                "sandbox.backend_selected",
                backend=backend.name,
                isolation=backend.isolation_level.name,
                mode="auto",
            )
        return backend

    # Unreachable: SubprocessSandbox.healthcheck() is unconditionally True.
    raise RuntimeError("no sandbox backend is available")  # pragma: no cover
