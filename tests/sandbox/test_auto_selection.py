"""The ``auto`` selection walk, pinned as measured (W9-C9.2).

``backend="auto"`` is the laptop default, which makes its three rules
the containment posture most runs actually get:

* the preference order is **strongest-first** — docker (possibly
  gVisor) before WSL2 namespaces before a bare host process — and it
  is a module constant, not a comment, so the walk and the ranking it
  implies cannot drift apart;
* auto **never fails, it degrades** — but it degrades *loudly*: landing
  on PROCESS-level isolation emits ``sandbox.weak_isolation_selected``
  naming the backend and pointing at ADR-0006, because a silent
  downgrade is the dangerous version of this factory;
* an **explicitly named** backend is returned *without* a healthcheck —
  an operator who wrote ``backend="docker"`` wants a loud failure at
  run time, not a quiet substitute weaker than they asked for.

The healthchecks are faked per class, not per instance, so the walk
cannot succeed by accident of which object it built; the probes record
the order they ran in, so "strongest-first" is asserted on the
*sequence*, not on the constant alone.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from paa.config import SandboxSettings
from paa.sandbox import get_sandbox
from paa.sandbox.base import IsolationLevel
from paa.sandbox.docker_backend import DockerSandbox
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.sandbox.wsl_backend import WslSandbox


def _fake_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
    cls: type[Any],
    answers: dict[str, list[bool]],
) -> None:
    """Patch one backend class's healthcheck, recording the probe order."""

    async def _probe(self: Any) -> bool:
        answer = answers[self.name].pop(0)
        answers.setdefault("__probed__", []).append(self.name)
        return answer

    monkeypatch.setattr(cls, "healthcheck", _probe)


class TestThePreferenceOrder:
    def test_the_order_constant_is_strongest_first(self) -> None:
        """docker → wsl → subprocess, and nothing weaker sits earlier.

        The order is the ranking: every candidate before subprocess
        declares real containment, and dry-run (NONE) is not on the walk
        at all — an auto that could land on dry-run would be an auto
        that executes nothing while reporting a backend.
        """
        from paa.sandbox import _AUTO_ORDER

        assert _AUTO_ORDER == ("docker", "wsl", "subprocess")
        for name in _AUTO_ORDER[:-1]:
            backend = {"docker": DockerSandbox, "wsl": WslSandbox}[name](
                SandboxSettings()
            )
            assert backend.isolation_level.is_real_containment, name

    async def test_auto_takes_the_first_healthy_strongest_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker healthy → selected, and WSL is never even probed."""
        answers = {"docker": [True], "wsl": [True]}
        _fake_healthcheck(monkeypatch, DockerSandbox, answers)
        _fake_healthcheck(monkeypatch, WslSandbox, answers)

        sb = await get_sandbox(SandboxSettings(backend="auto"))

        assert isinstance(sb, DockerSandbox)
        assert sb.isolation_level.is_real_containment
        assert answers["__probed__"] == ["docker"]

    async def test_auto_skips_an_unhealthy_stronger_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Docker unhealthy → the walk continues to WSL, in order."""
        answers = {"docker": [False], "wsl": [True]}
        _fake_healthcheck(monkeypatch, DockerSandbox, answers)
        _fake_healthcheck(monkeypatch, WslSandbox, answers)

        sb = await get_sandbox(SandboxSettings(backend="auto"))

        assert isinstance(sb, WslSandbox)
        assert answers["__probed__"] == ["docker", "wsl"]


class TestTheLoudDegradation:
    async def test_auto_degrades_to_subprocess_with_the_weak_isolation_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No container backend healthy → subprocess, PROCESS, and a warning.

        The warning is the contract: it names the backend that was
        landed on, the isolation level that means, and the ADR that
        explains the gap. A downgrade this visible can be acted on; a
        silent one can only be discovered.
        """
        answers = {"docker": [False], "wsl": [False]}
        _fake_healthcheck(monkeypatch, DockerSandbox, answers)
        _fake_healthcheck(monkeypatch, WslSandbox, answers)

        with structlog.testing.capture_logs() as logs:
            sb = await get_sandbox(SandboxSettings(backend="auto"))

        assert isinstance(sb, SubprocessSandbox)
        assert sb.isolation_level is IsolationLevel.PROCESS
        warnings = [
            e for e in logs if e["event"] == "sandbox.weak_isolation_selected"
        ]
        assert len(warnings) == 1
        assert warnings[0]["backend"] == "subprocess"
        assert warnings[0]["isolation"] == "PROCESS"
        assert "docs/adr/0006" in warnings[0]["detail"]

    async def test_a_real_containment_pick_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning fires for the degradation, not for the factory."""
        answers = {"docker": [True], "wsl": [True]}
        _fake_healthcheck(monkeypatch, DockerSandbox, answers)
        _fake_healthcheck(monkeypatch, WslSandbox, answers)

        with structlog.testing.capture_logs() as logs:
            await get_sandbox(SandboxSettings(backend="auto"))

        assert not [e for e in logs if e["event"] == "sandbox.weak_isolation_selected"]


class TestTheExplicitContract:
    async def test_an_explicit_backend_is_returned_without_a_healthcheck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``backend="docker"`` must not probe, even with a daemon absent.

        The healthcheck is patched to *record and refuse*: if the
        factory probed an explicit backend, this test fails on the probe
        itself rather than on a downstream symptom.
        """
        probed: list[str] = []

        async def _must_not_run(self: Any) -> bool:
            probed.append(self.name)
            return False

        monkeypatch.setattr(DockerSandbox, "healthcheck", _must_not_run)

        sb = await get_sandbox(SandboxSettings(backend="docker"))

        assert isinstance(sb, DockerSandbox)
        assert probed == []
