"""Docker backend argv construction and gVisor (``runsc``) wiring.

Docker is not installed on the Windows dev box, so nothing here starts a real
container. The ``docker run`` argv is a pure function of the spec and the
detected runtime, so it is tested directly; runtime *detection* is tested by
mocking ``asyncio.create_subprocess_exec`` so no daemon is needed. This is the
guard that the RFC §13 ``--runtime=runsc`` containment is actually requested
when configured — and, just as important, that it is *not* claimed when the
daemon cannot provide it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from paa.config import SandboxSettings
from paa.sandbox import build_backend, get_sandbox
from paa.sandbox.base import IsolationLevel, SandboxSpec
from paa.sandbox.docker_backend import DockerSandbox


def _spec(workspace: Path) -> SandboxSpec:
    return SandboxSpec(
        command=("python", "-c", "print(1)"),
        workspace_path=workspace,
        memory_mb=256,
        cpu_cores=0.5,
    )


def _runtime_flag(args: list[str]) -> str | None:
    if "--runtime" not in args:
        return None
    return args[args.index("--runtime") + 1]


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_runsc_appears_in_args_when_available(tmp_path: Path) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = True  # simulate a daemon that registered runsc
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert _runtime_flag(args) == "runsc"
    assert sb.isolation_level == IsolationLevel.VM


def test_runc_adds_no_runtime_flag(tmp_path: Path) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runc"))
    sb._runtime_available = True
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert _runtime_flag(args) is None
    assert sb.isolation_level == IsolationLevel.NAMESPACE


def test_runsc_requested_but_unavailable_does_not_overstate(tmp_path: Path) -> None:
    """gVisor asked for, daemon lacks it: run runc and report NAMESPACE, not VM.

    Overstating containment is the dangerous failure — the policy layer routes
    high-risk work on the strength of the reported level.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = False
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert _runtime_flag(args) is None
    assert sb.isolation_level == IsolationLevel.NAMESPACE


def test_custom_runtime_is_honoured(tmp_path: Path) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="kata-runtime"))
    sb._runtime_available = True
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert _runtime_flag(args) == "kata-runtime"
    # A non-gVisor runtime is not assumed to intercept syscalls.
    assert sb.isolation_level == IsolationLevel.NAMESPACE


def test_core_containment_flags_always_present(tmp_path: Path) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = True
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    for flag in ("--rm", "--read-only", "--network", "--cap-drop", "--pids-limit"):
        assert flag in args


# ---------------------------------------------------------------------------
# runtime detection (mocked subprocess)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


async def test_detect_runtime_finds_runsc(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(b'{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await sb._detect_runtime() is True
    assert sb._runtime_available is True


async def test_detect_runtime_missing_runsc(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(b'{"runc":{"path":"runc"}}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await sb._detect_runtime() is False
    assert sb.isolation_level == IsolationLevel.NAMESPACE


async def test_detect_runc_needs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """runc is always present, so detection must not shell out at all."""
    sb = DockerSandbox(SandboxSettings(container_runtime="runc"))

    async def fail_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        raise AssertionError("runc detection must not spawn a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)
    assert await sb._detect_runtime() is True


# ---------------------------------------------------------------------------
# factory selection
# ---------------------------------------------------------------------------


def test_build_backend_reads_container_runtime() -> None:
    sb = build_backend("docker", SandboxSettings(container_runtime="runsc"))
    assert isinstance(sb, DockerSandbox)
    assert sb.runtime == "runsc"


async def test_get_sandbox_selects_docker_with_configured_runtime() -> None:
    """An explicit ``backend="docker"`` is returned without a healthcheck."""
    sb = await get_sandbox(SandboxSettings(backend="docker", container_runtime="runsc"))
    assert sb.name == "docker"
    assert sb.runtime == "runsc"  # type: ignore[attr-defined]
