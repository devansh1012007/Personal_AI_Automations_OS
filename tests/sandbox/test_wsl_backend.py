"""The WSL backend's self-report.

The WSL backend cannot run on a POSIX host — ``wsl.exe`` does not exist
there — so nothing here starts a distribution. What *can* be checked on any
host is the backend's **report**, and the report has one job in this file:
to say, out loud, that the container backend's start budget is not this
backend's money.
"""

from __future__ import annotations

from paa.config import SandboxSettings
from paa.sandbox.base import IsolationLevel
from paa.sandbox.wsl_backend import WslSandbox


def test_the_wsl_backend_says_it_has_no_start_phase() -> None:
    """The grace is the *container* backend's start budget.

    The WSL2 VM's own start happens in the (cached) health check, not per
    run, and a command inside the distro starts in milliseconds. There is no
    start phase here to budget, so the setting is neither spent nor folded
    into the work's timeout — and the report says so, because a setting that
    only one backend honours must not be applied to the others silently
    (ADR-0031).
    """
    sandbox = WslSandbox(SandboxSettings(container_start_grace_seconds=30.0))
    described = sandbox.describe()
    assert described["container_start_grace_seconds"] == 0
    assert "no start phase" in described["container_start_budget"]
    # And the backend still says what it is.
    assert described["backend"] == "wsl"
    assert described["isolation_level"] == IsolationLevel.NAMESPACE.name
