"""Docker backend argv construction, gVisor (``runsc``) wiring, and budgets.

Docker is not installed on the Windows dev box, so nothing here starts a real
container. The ``docker create`` argv is a pure function of the spec and the
detected runtime, so it is tested directly; runtime *detection* is tested by
mocking ``asyncio.create_subprocess_exec`` so no daemon is needed. This is the
guard that the RFC §13 ``--runtime=runsc`` containment is actually requested
when configured — and, just as important, that it is *not* claimed when the
daemon cannot provide it.

The **budgets** are the other half of the file. The daemon is faked behind the
``_exec`` seam — the single place every ``docker`` call goes through, and
therefore the single place a test has to intercept — and the budget each call
was given is read off the ``asyncio.wait_for`` that carries it. No fake clock
is used anywhere in this repository and none is introduced here: the tests
assert on the *number* the backend passed, and let it expire against a real
(very short) sleep where the behaviour has to be shown as well.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from pydantic import ValidationError

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.core.types import MODALITY_PROFILES, ComplexityModality
from paa.sandbox import build_backend, docker_backend, get_sandbox
from paa.sandbox.base import IsolationLevel, SandboxSpec
from paa.sandbox.docker_backend import DockerSandbox


def _spec(workspace: Path) -> SandboxSpec:
    return SandboxSpec(
        command=("python", "-c", "print(1)"),
        workspace_path=workspace,
        memory_mb=256,
        cpu_cores=0.5,
    )


def _spec_with(workspace: Path, **overrides: Any) -> SandboxSpec:
    """A spec with the fields a budget test needs to control."""
    return SandboxSpec(
        command=("python", "-c", "print(1)"),
        workspace_path=workspace,
        memory_mb=256,
        cpu_cores=0.5,
        **overrides,
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
    """Every containment flag is on the ``create`` argv, and ``--rm`` is not.

    ``--rm`` used to be one of them, and it is gone on purpose: the
    container's logs have to still be there after it exits, and ``--rm``
    destroys them with the container. The removal is an explicit
    ``docker rm -f`` in a ``finally`` instead — which is also the only
    version of it that cannot leave an orphan behind.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = True
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    for flag in (
        "--read-only",
        "--network",
        "--cap-drop",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--cpus",
        "--user",
        "--tmpfs",
        "--security-opt",
        "--volume",
        "--env",
    ):
        assert flag in args
    assert "--rm" not in args
    # And it is a ``create``, not a ``run``: the start boundary has to be
    # observable, and one opaque ``docker run`` is what hid it.
    assert args[0] == "docker"
    assert args[1] == "create"


# ---------------------------------------------------------------------------
# runtime detection (mocked subprocess)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    async def wait(self) -> int:
        return self.returncode


class _SlowProc:
    """A child that never answers, and remembers whether it was killed."""

    def __init__(self) -> None:
        self.killed = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return -9


async def test_detect_runtime_finds_runsc(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(b'{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}')

    monkeypatch.setattr(sb, "_exec", fake_exec)
    assert await sb._detect_runtime() is True
    assert sb._runtime_available is True


async def test_detect_runtime_missing_runsc(monkeypatch: pytest.MonkeyPatch) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        return _FakeProc(b'{"runc":{"path":"runc"}}')

    monkeypatch.setattr(sb, "_exec", fake_exec)
    assert await sb._detect_runtime() is False
    assert sb.isolation_level == IsolationLevel.NAMESPACE


async def test_detect_runc_needs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """runc is always present, so detection must not shell out at all."""
    sb = DockerSandbox(SandboxSettings(container_runtime="runc"))

    async def fail_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        raise AssertionError("runc detection must not spawn a subprocess")

    monkeypatch.setattr(sb, "_exec", fail_exec)
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


# ---------------------------------------------------------------------------
# self-report (paa doctor)
# ---------------------------------------------------------------------------


def test_describe_reports_the_container_facts() -> None:
    """The facts that decide containment must be visible, not inferred.

    The isolation level alone cannot distinguish "runsc, and the daemon has
    it" from "runsc was asked for and the daemon does not have it" — both
    report NAMESPACE until the runtime is probed. The doctor has to be able
    to say which.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    described = sb.describe()
    assert described["backend"] == "docker"
    assert described["container_runtime"] == "runsc"
    assert described["image"] == DockerSandbox(SandboxSettings()).image
    # Not probed yet is a third state, distinct from "absent".
    assert described["runtime_available"] is None
    assert sb.runtime_available is None


def test_describe_reports_runtime_absent_after_a_probe() -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = False
    assert sb.describe()["runtime_available"] is False


async def test_diagnose_says_so_when_the_runtime_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one fact an operator needs: is the runtime I asked for the one I get?

    A daemon that answers but does not have ``runsc`` is the case that
    matters, because the sandbox will run under runc and the isolation level
    is NAMESPACE rather than VM. Reporting "reachable" alone would hide it.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        # `docker version` answers, `docker info` does not list runsc.
        if "info" in _args:
            return _FakeProc(b'{"runc":{"path":"runc"}}')
        return _FakeProc(b"29.9.0")

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    facts = await sb.diagnose()
    assert facts["container_runtime"] == "runsc"
    assert facts["runtime_available"] is False
    assert "not registered" in facts["daemon"]
    # And the isolation level still does not overstate.
    assert sb.isolation_level == IsolationLevel.NAMESPACE


async def test_diagnose_reports_the_runtime_when_it_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        if "info" in _args:
            return _FakeProc(b'{"runc":{"path":"runc"},"runsc":{"path":"/usr/bin/runsc"}}')
        return _FakeProc(b"29.9.0")

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    facts = await sb.diagnose()
    assert facts["runtime_available"] is True
    assert facts["daemon"] == "reachable"
    assert sb.isolation_level == IsolationLevel.VM


async def test_diagnose_reports_a_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI is a different failure from "the daemon is down"."""
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    facts = await sb.diagnose()
    assert "no docker CLI" in facts["daemon"]


async def test_a_spawn_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that cannot spawn anything is unhealthy, not broken.

    The health check's contract is that it never raises — the factory calls
    it to choose a backend, and a health check that throws takes down
    backend selection itself. And backend selection swallows its failures,
    so an exception here would quietly downgrade the sandbox from NAMESPACE
    to PROCESS: a bug in the containment path would look like a weaker
    configuration choice.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))

    async def no_spawn(*_args: object, **_kwargs: object) -> object:
        raise OSError("no docker here")

    monkeypatch.setattr(sb, "_exec", no_spawn)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    assert await sb.healthcheck() is False
    # And the tri-state is preserved: a failed probe is not "not registered".
    assert sb.runtime_available is None


async def test_a_timed_out_probe_kills_its_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that overruns its timeout must not leave a process behind.

    The child would otherwise outlive the probe and hold the event loop's
    transport table open — and the TUI re-probes on every dashboard render,
    so the orphans accumulate.
    """
    sb = DockerSandbox(SandboxSettings())
    killed: list[bool] = []

    class _SlowProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(3600)
            return b"", b""

        def kill(self) -> None:
            killed.append(True)
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    async def fake_exec(*_args: object, **_kwargs: object) -> _SlowProc:
        return _SlowProc()

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(docker_backend, "_HEALTHCHECK_TIMEOUT_SECONDS", 0.01)
    result = await sb._capture("docker", "version")
    assert result is None
    assert killed == [True]


async def test_a_cancelled_probe_kills_its_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled wait is the one path that arrives with no timeout of its own.

    The boot sequence gives up on a slow probe and cancels the wait. The
    child is still running at that point, so it has to be killed explicitly —
    otherwise every boot against a wedged daemon leaks a ``docker version``
    process.
    """
    sb = DockerSandbox(SandboxSettings())
    proc = _SlowProc()

    async def fake_exec(*_args: object, **_kwargs: object) -> _SlowProc:
        return proc

    monkeypatch.setattr(sb, "_exec", fake_exec)
    task = asyncio.create_task(sb._capture("docker", "version"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed is True


async def test_the_image_presence_is_not_claimed_when_the_daemon_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable daemon must not produce "NOT present on the daemon".

    ``docker image inspect`` exits non-zero when the daemon cannot be
    reached, exactly as it does when the image is absent — so the exit code
    cannot tell the two apart, and reporting "absent" would send the
    operator to rebuild an image that exists. The doctor would also
    contradict itself: "unreachable" on one row, "NOT present" on the next.
    """
    sb = DockerSandbox(SandboxSettings())

    async def fake_exec(*_args: object, **_kwargs: object) -> _FakeProc:
        # The daemon is not reachable: every command fails.
        return _FakeProc(b"permission denied", returncode=1)

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    facts = await sb.diagnose()
    assert facts["daemon"] == "unreachable"
    # "could not tell" — not "absent".
    assert facts.get("image_present") is None


async def test_a_missing_image_is_reported_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same question: a reachable daemon that says no.

    ``image_present`` has to be ``False`` here — and *not* ``None``, because
    ``None`` means "could not tell", and the operator would have no way to
    distinguish "the image is absent" from "I could not look".
    """
    sb = DockerSandbox(SandboxSettings())

    async def fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        if "image" in args and "inspect" in args:
            # The daemon answered: the image is not there.
            return _FakeProc(b"", returncode=1)
        return _FakeProc(b"29.9.0")

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")

    facts = await sb.diagnose()
    assert facts["daemon"] == "reachable"
    assert facts["image_present"] is False


async def test_base_sandbox_diagnose_reports_its_identity() -> None:
    """A backend with nothing extra to say still says what it is.

    Returning an empty dict here would make the doctor answer "none
    (dry-run)" for a subprocess backend — which is not merely unhelpful, it
    is false: the workload *does* run, on the host, at PROCESS isolation.
    """
    sb = build_backend("dryrun")
    facts = await sb.diagnose()
    assert facts["backend"] == "dryrun"
    assert facts["isolation_level"] == "NONE"


async def test_the_image_presence_check_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard re-renders on every command; the daemon is not re-asked.

    One ``docker image inspect`` per keystroke is a cost with no benefit, and
    the answer cannot change within the cache window.
    """
    sb = DockerSandbox(SandboxSettings())
    calls: list[str] = []

    async def fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        calls.append(str(args))
        return _FakeProc(b"")

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(sb, "healthcheck", _always_healthy)
    assert await sb._image_present() is True
    assert await sb._image_present() is True
    assert len(calls) == 1


async def test_the_image_user_probe_runs_and_reaches_the_user_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image's declared user is read from the daemon and used.

    It is the fallback identity when the workspace's owner cannot be used, so
    the probe has to be wired into the health check — and the value it
    returns has to reach the ``--user`` argument.
    """
    sb = DockerSandbox(SandboxSettings())

    async def fake_exec(*args: object, **_kwargs: object) -> _FakeProc:
        if "image" in args and "inspect" in args and "--format" in args:
            return _FakeProc(b"paa\n")
        return _FakeProc(b"29.9.0")

    monkeypatch.setattr(sb, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    assert await sb.healthcheck() is True
    assert sb._image_user == "paa"

    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    assert args[args.index("--user") + 1] == "paa"


# ---------------------------------------------------------------------------
# containment arguments
# ---------------------------------------------------------------------------


def test_user_is_the_workspace_owner_not_root(tmp_path: Path) -> None:
    """The container runs as whoever owns the workspace it was given.

    A container-root process can write the workspace too — and can leave
    behind root-owned files the operator then has to remove with sudo.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(_spec(workspace), workspace, "paa-test")
    assert "--user" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_user_is_never_root_even_for_a_root_owned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant is structural: the container never runs as uid 0.

    A root-owned workspace would otherwise select ``--user 0:0`` and put the
    workload back in container-root — which is precisely the harm ``--user``
    exists to prevent, and precisely the harm the flag was added to avoid
    (root-owned files in a directory the operator owns).

    The workspace owner is faked because the test cannot chown to root
    without privilege.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    sb = DockerSandbox(SandboxSettings())
    monkeypatch.setattr(
        DockerSandbox, "_workspace_owner", staticmethod(lambda _ws: (0, 0))
    )
    args = sb._build_args(_spec(workspace), workspace, "paa-test")
    assert "--user" in args
    uid = args[args.index("--user") + 1].split(":")[0]
    assert uid not in {"0", "root"}


def test_user_is_always_present(tmp_path: Path) -> None:
    """``--user`` is never omitted.

    Omitting the flag means "whatever the image says", and an image that says
    nothing runs as root. The flag is the invariant, so it is always there.
    """
    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    assert "--user" in args


def test_user_falls_back_to_the_images_own_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace we cannot stat falls back to the image's declared user.

    Docker resolves the value against the *image's* /etc/passwd, so a name
    like ``paa`` is passed through unchanged.
    """
    sb = DockerSandbox(SandboxSettings())
    sb._image_user = "paa"
    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    assert args[args.index("--user") + 1] == "paa"


def test_an_image_that_declares_root_is_not_used(tmp_path: Path) -> None:
    """An image that says it runs as root must not be the fallback."""
    sb = DockerSandbox(SandboxSettings())
    sb._image_user = "root"
    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    uid = args[args.index("--user") + 1].split(":")[0]
    assert uid not in {"0", "root"}


@pytest.mark.parametrize("declared", ["0", "00", "000", "0:1000", "root", "root:root", ""])
def test_a_root_declaring_image_user_is_refused_in_every_spelling(
    declared: str, tmp_path: Path
) -> None:
    """Docker parses a numeric uid **decimally**, so ``00`` is uid 0.

    A literal string comparison against ``"0"`` would wave container-root
    through — and the invariant is stated as absolute, so it has to hold for
    every spelling Docker accepts.
    """
    sb = DockerSandbox(SandboxSettings())
    sb._image_user = declared
    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    value = args[args.index("--user") + 1]
    assert value != declared
    uid = value.split(":")[0]
    assert uid.isdigit()
    assert int(uid) != 0


def test_an_unexpected_image_user_is_refused(tmp_path: Path) -> None:
    """A value the daemon should not produce is not handed to ``docker run``.

    The value is not attacker-controlled, but it is unvalidated, and an
    unexpected string becomes an unexpected argument. The fallback is
    always safe, so an unusable value costs nothing.
    """
    sb = DockerSandbox(SandboxSettings())
    sb._image_user = "not a user; rm -rf /"
    args = sb._build_args(_spec(tmp_path / "missing"), tmp_path / "missing", "paa-test")
    value = args[args.index("--user") + 1]
    assert ";" not in value
    assert value == "10001:10001"


def test_exec_tmpfs_is_exec_capable_and_separate_from_tmp(tmp_path: Path) -> None:
    """npm's cache holds executable shims; /tmp stays noexec.

    A server started through ``npx`` fails with ``Permission denied`` (exit
    126) when its cache is on a noexec mount, so the tool layer asks for an
    exec-capable scratch path. /tmp keeps the noexec property.
    """
    spec = SandboxSpec(
        command=("node", "server.js"),
        workspace_path=tmp_path,
        exec_tmpfs=("/run",),
    )
    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(spec, tmp_path, "paa-test")
    tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert "/tmp:rw,noexec,nosuid,size=64m" in tmpfs
    assert any(t.startswith("/run:") and "exec" in t and "noexec" not in t for t in tmpfs)
    # mode=1777: the tmpfs is created by the daemon and owned by root, so
    # without it the non-root container user cannot make its cache directory.
    assert any("mode=1777" in t for t in tmpfs)


@pytest.mark.parametrize("path", ["/", "/usr", "/etc", "/bin", "/proc", "/workspace"])
def test_exec_tmpfs_may_not_shadow_a_path_the_container_needs(path: str) -> None:
    """A tmpfs mount replaces the path — it does not add a directory to it.

    Mounting over ``/usr`` would not give the workload a scratch directory;
    it would give it an empty filesystem where the image's own binaries were,
    and the failure that follows names the wrong cause entirely.
    """
    with pytest.raises(ValidationError):
        SandboxSpec(
            command=("sh", "-c", "true"),
            workspace_path=Path("/tmp"),
            exec_tmpfs=(path,),
        )


@pytest.mark.parametrize(
    "path",
    ["/run/../etc", "//etc", "/usr/../etc", "/workspace/../etc", "/etc/./"],
)
def test_exec_tmpfs_may_not_shadow_a_path_by_another_spelling(path: str) -> None:
    """The comparison is against the path the container will *see*.

    ``/run/../etc`` resolves to ``/etc``. A validator that compared the
    literal string would wave it through, and the tmpfs would then replace
    the container's ``/etc`` — the exact failure the check exists to
    prevent, reached by a different route.
    """
    with pytest.raises(ValidationError):
        SandboxSpec(
            command=("sh", "-c", "true"),
            workspace_path=Path("/tmp"),
            exec_tmpfs=(path,),
        )


@pytest.mark.parametrize("path", ["/usr/bin", "/usr/lib", "/usr/local/bin", "/bin", "/sbin"])
def test_exec_tmpfs_may_not_shadow_a_subdirectory_of_a_protected_path(path: str) -> None:
    """``/usr/bin`` is just as fatal as ``/usr``.

    It holds the shell. An exact-match denylist would wave it through, and
    the container would fail to start with "executable file not found in
    $PATH" — an error that names the wrong cause entirely.
    """
    with pytest.raises(ValidationError):
        SandboxSpec(
            command=("sh", "-c", "true"),
            workspace_path=Path("/tmp"),
            exec_tmpfs=(path,),
        )


@pytest.mark.parametrize("path", ["/run", "/var/cache", "/opt/scratch", "/tmp2", "/dev/shm"])
def test_exec_tmpfs_allows_an_ordinary_scratch_path(path: str) -> None:
    """The rule is about the container's own files, not about being fussy."""
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=Path("/tmp"),
        exec_tmpfs=(path,),
    )
    assert spec.exec_tmpfs == (path,)


@pytest.mark.parametrize("path", ["/tmp", "/tmp/x", "/tmp/nested/deep"])
def test_exec_tmpfs_may_not_make_tmp_executable(path: str) -> None:
    """``/tmp`` is mounted ``noexec`` on purpose, and that has to hold.

    The backend always emits ``--tmpfs /tmp:rw,noexec,...``. A second
    ``--tmpfs /tmp:rw,exec,...`` would *win* — Docker takes the last one —
    and the ``noexec`` property would silently disappear. The invariant is
    stated in ADR-0024 and in the deployment guide, so it is enforced here
    rather than assumed.
    """
    with pytest.raises(ValidationError):
        SandboxSpec(
            command=("sh", "-c", "true"),
            workspace_path=Path("/tmp"),
            exec_tmpfs=(path,),
        )


def test_exec_tmpfs_is_normalised_to_the_path_the_container_sees() -> None:
    """A harmless path is normalised, so the mount lands where it says."""
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=Path("/tmp"),
        exec_tmpfs=("/run//cache/../cache",),
    )
    assert spec.exec_tmpfs == ("/run/cache",)


def test_exec_tmpfs_must_be_an_absolute_container_path() -> None:
    """A relative path would be resolved against the container's workdir."""
    with pytest.raises(ValidationError):
        SandboxSpec(
            command=("sh", "-c", "true"),
            workspace_path=Path("/tmp"),
            exec_tmpfs=("relative/path",),
        )


def test_network_is_explicit_when_the_spec_names_one(tmp_path: Path) -> None:
    """A networked container must not silently land on the default bridge."""
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=True,
        network="paa_tools_egress",
    )
    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(spec, tmp_path, "paa-test")
    assert args[args.index("--network") + 1] == "paa_tools_egress"


def test_network_none_when_the_network_is_denied(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=False,
        network="paa_tools_egress",
    )
    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(spec, tmp_path, "paa-test")
    assert args[args.index("--network") + 1] == "none"


def test_default_container_network_is_used_when_the_spec_names_none(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=True,
    )
    sb = DockerSandbox(SandboxSettings(default_container_network="paa_tools"))
    args = sb._build_args(spec, tmp_path, "paa-test")
    assert args[args.index("--network") + 1] == "paa_tools"


async def test_a_denied_network_is_never_checked_for_existence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existence check must look at the network the container joins.

    The container was going to run with ``--network none``; a network it
    never joins cannot be missing in a way that matters, and refusing to
    start over it is a false failure that hides a real one.
    """
    sb = DockerSandbox(SandboxSettings())
    checked: list[str] = []

    async def fake_exists(name: str) -> bool:
        checked.append(name)
        return False

    monkeypatch.setattr(sb, "_network_exists", fake_exists)
    monkeypatch.setattr(sb, "healthcheck", _always_healthy)
    monkeypatch.setattr(sb, "_exec", _no_docker)

    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=False,
        network="a_network_that_does_not_exist",
    )
    # The container is about to start; the sentinel below is what stops it.
    with pytest.raises(_DockerNotLaunched):
        await sb.run(spec)
    assert "does not exist" not in str(checked)
    assert checked == []


async def test_a_missing_network_is_refused_before_the_container_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker's own "network not found" arrives too late to be useful.

    It comes back after the container has been created, and reads like a
    workload failure rather than a configuration mistake.
    """
    sb = DockerSandbox(SandboxSettings())
    monkeypatch.setattr(sb, "_network_exists", _network_missing)
    monkeypatch.setattr(sb, "healthcheck", _always_healthy)

    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=True,
        network="paa_tools_egress",
    )
    with pytest.raises(SandboxError) as excinfo:
        await sb.run(spec)
    assert "does not exist" in str(excinfo.value)


class _DockerNotLaunched(Exception):
    """Sentinel: the test must not reach the real Docker daemon."""


async def _no_docker(*_args: object, **_kwargs: object) -> object:
    raise _DockerNotLaunched("the test reached the real docker daemon")


async def _always_healthy() -> bool:
    return True


async def _network_missing(_name: str) -> bool:
    return False


async def test_the_default_bridge_is_never_checked_for_existence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network allowed but not named lands on the Docker default bridge.

    The bridge always exists, so asking the daemon whether it is there is
    both pointless and — because the question has to be phrased with a name
    — the thing that turns "no network configured" into "network '' does not
    exist". The container must be allowed to start.
    """
    sb = DockerSandbox(SandboxSettings())
    checked: list[str] = []

    async def fake_exists(name: str) -> bool:
        checked.append(name)
        return False

    monkeypatch.setattr(sb, "_network_exists", fake_exists)
    monkeypatch.setattr(sb, "healthcheck", _always_healthy)
    monkeypatch.setattr(sb, "_exec", _no_docker)

    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=True,
    )
    with pytest.raises(_DockerNotLaunched):
        await sb.run(spec)
    assert checked == []


def test_the_default_bridge_is_announced_rather_than_named(tmp_path: Path) -> None:
    """No network flag at all means the daemon's default bridge.

    That is the difference between "no egress" and "all egress", so it is
    logged rather than left to be discovered.
    """
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path=tmp_path,
        allow_network=True,
    )
    sb = DockerSandbox(SandboxSettings())
    args = sb._build_args(spec, tmp_path, "paa-test")
    assert "--network" not in args
    assert sb._effective_network(spec) is None


# ---------------------------------------------------------------------------
# host path translation
# ---------------------------------------------------------------------------


def test_host_path_translates_the_containers_view(tmp_path: Path) -> None:
    """The Docker daemon resolves bind mounts against the HOST filesystem.

    When the runtime is itself containerised, the path it knows is not the
    path the daemon must be given — and passing the wrong one does not fail
    loudly: Docker creates an empty, root-owned directory in its place.
    """
    sb = DockerSandbox(SandboxSettings(host_path_map="/data=/srv/paa/data"))
    assert str(sb.host_path("/data/workspaces/t1")) == "/srv/paa/data/workspaces/t1"
    assert str(sb.host_path("/data")) == "/srv/paa/data"


def test_host_path_leaves_unmapped_paths_alone(tmp_path: Path) -> None:
    """No map means the paths are the same — the laptop's answer."""
    sb = DockerSandbox(SandboxSettings())
    assert str(sb.host_path("/data/workspaces/t1")) == "/data/workspaces/t1"


def test_host_path_prefers_the_longest_matching_prefix(tmp_path: Path) -> None:
    sb = DockerSandbox(
        SandboxSettings(host_path_map="/data=/srv/paa,/data/workspaces=/srv/ws")
    )
    assert str(sb.host_path("/data/workspaces/t1")) == "/srv/ws/t1"
    assert str(sb.host_path("/data/other")) == "/srv/paa/other"


def test_host_path_does_not_match_a_prefix_that_is_not_a_path_boundary(
    tmp_path: Path,
) -> None:
    """``/data`` must not translate ``/database``.

    A prefix match that ignores the path boundary would rewrite an unrelated
    directory into the mapped one — the classic way a "safe" translation
    silently mounts the wrong thing.
    """
    sb = DockerSandbox(SandboxSettings(host_path_map="/data=/srv/paa/data"))
    assert str(sb.host_path("/database/x")) == "/database/x"
    assert str(sb.host_path("/datax")) == "/datax"


def test_host_path_normalises_before_matching(tmp_path: Path) -> None:
    """``..`` is collapsed first, so the map cannot be walked out of."""
    sb = DockerSandbox(SandboxSettings(host_path_map="/data=/srv/paa/data"))
    assert str(sb.host_path("/data/../etc/passwd")) == "/etc/passwd"


def test_a_malformed_host_path_map_is_refused_not_ignored(tmp_path: Path) -> None:
    """A broken map must fail loudly.

    Ignoring the bad entry is the *worse* outcome: the daemon is then handed
    the runtime's own path, and answers by creating an empty, root-owned
    directory in its place. A wrong mount that fails loudly beats one that
    quietly mounts the wrong thing.
    """
    sb = DockerSandbox(SandboxSettings(host_path_map="/data:/srv/paa/data"))
    with pytest.raises(SandboxError) as excinfo:
        sb.host_path("/data/workspaces/t1")
    assert "host_path_map" in str(excinfo.value)


def test_a_malformed_host_path_map_is_reported_not_raised() -> None:
    """The diagnostic has to be able to *report* a broken setting.

    A setting that raises during import stops the runtime from booting, and a
    runtime that will not boot cannot run the doctor that explains the
    problem.
    """
    settings = SandboxSettings(host_path_map="/data:/srv/paa/data")
    assert settings.host_path_map_error is not None
    assert "inside=host" in settings.host_path_map_error


def test_a_root_mapping_is_expressible(tmp_path: Path) -> None:
    """A mapping of "/" is a legitimate answer: "everything under this root".

    Stripping the trailing slash off "/" would turn it into the empty
    string, and the mapping would be refused as non-absolute.
    """
    sb = DockerSandbox(SandboxSettings(host_path_map="/=/srv/paa"))
    assert str(sb.host_path("/data/workspaces/t1")) == "/srv/paa/data/workspaces/t1"


def test_volume_argument_uses_the_host_path(tmp_path: Path) -> None:
    """The volume the daemon is given must name the host's directory."""
    sb = DockerSandbox(SandboxSettings(host_path_map="/data=/srv/paa/data"))
    spec = SandboxSpec(
        command=("sh", "-c", "true"),
        workspace_path="/data/workspaces/t1",
    )
    args = sb._build_args(spec, Path("/data/workspaces/t1"), "paa-test")
    volume = args[args.index("--volume") + 1]
    assert volume.startswith("/srv/paa/data/workspaces/t1:")


def test_home_and_tmpdir_name_the_containers_workspace(tmp_path: Path) -> None:
    """HOME must point at a directory that exists *inside* the container.

    The runtime knows the workspace by a host path; the container sees it at
    /workspace. A HOME that names the host path points at nothing.
    """
    sb = DockerSandbox(SandboxSettings())
    spec = SandboxSpec(command=("sh", "-c", "true"), workspace_path=tmp_path)
    args = sb._build_args(spec, tmp_path, "paa-test")
    envs = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert "HOME=/workspace" in envs
    assert "TMPDIR=/workspace" in envs


# ---------------------------------------------------------------------------
# the memory ceiling, reported honestly
# ---------------------------------------------------------------------------


def test_memory_enforcement_names_the_mechanism_not_the_aspiration(
    tmp_path: Path,
) -> None:
    """The ceiling is the kernel's cgroup, under either runtime.

    ``--memory`` is a *daemon*-level limit: the daemon caps the container's
    cgroup and refuses to start a container whose limit it cannot apply.
    The OCI runtime in charge does not change that, and gVisor does not get
    an exception to it.

    That was measured rather than assumed. With ``--memory 256m`` and a
    process touching 512 MiB, both runtimes produced exit 137,
    ``OOMKilled=true``, ``memory.peak == memory.max`` and the kernel's own
    ``oom_kill 1`` — so recording anything weaker than "cgroup" for a
    gVisor run would put a claim in the ledger that the host's own
    accounting contradicts.

    Which runtime ran the container is a *different* fact, and it is
    recorded separately: ``isolation_level`` says ``VM`` under gVisor.
    """
    runsc = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    runsc._runtime_available = True
    args = runsc._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert "--runtime" in args  # sanity: this is the gVisor path

    # The enforcement string is chosen by the mechanism, not by the spec.
    assert runsc._memory_enforcement() == "cgroup"
    runc = DockerSandbox(SandboxSettings(container_runtime="runc"))
    assert runc._memory_enforcement() == "cgroup"


def test_memory_enforcement_follows_the_effective_runtime(tmp_path: Path) -> None:
    """A runsc that fell back to runc was capped by a cgroup, and says so.

    The mechanism follows the runtime that actually ran the container, not
    the one that was requested. Keying it on the request would put a ceiling
    in the ledger on every host where gVisor is not installed — which is
    every host, by default.
    """
    sb = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    sb._runtime_available = False
    args = sb._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert "--runtime" not in args  # sanity: this is the runc path
    assert sb._memory_enforcement() == "cgroup"


# ---------------------------------------------------------------------------
# the two budgets: the container's start, and the work
# ---------------------------------------------------------------------------


class _FakeProc:
    """A stand-in for ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
        hang: bool = False,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = returncode
        self._delay = delay
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._hang:
            # A child that never answers. The client's own wait is what
            # gives up on it, and the kill below is what ends it.
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    async def wait(self) -> int:
        if self._hang and not self.killed:
            await asyncio.sleep(3600)
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True


def _iso(epoch: float) -> str:
    """A Docker-shaped RFC 3339 timestamp (nanosecond precision, ``Z``)."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _FakeDaemon:
    """A fake Docker daemon, behind the backend's ``_exec`` seam.

    The seam exists for exactly this: "the single seam for every ``docker``
    call … exactly one place a test has to intercept". Every command the
    backend issues is recorded, so a test can say *which* call it is talking
    about, and the budget each call was given is read off the
    ``asyncio.wait_for`` that carried it.

    The daemon answers the way the real one does: ``create`` prints the
    container id, ``start`` returns once the process is running, ``inspect``
    reports the container's state in the **daemon's** clock, ``wait`` prints
    the exit code, and ``logs`` demultiplexes the container's two streams.
    """

    def __init__(
        self,
        *,
        create_delay: float = 0.0,
        start_delay: float = 0.0,
        work_delay: float = 0.0,
        inspect_delay: float = 0.0,
        exit_code: int = 0,
        started_at_offset: float = 0.0,
        report_started_at: bool = True,
        rm_hang: bool = False,
        wait_hang: bool = False,
    ) -> None:
        #: How long ``docker create`` takes. A create that overruns its
        #: grace is a *host* problem, and this is how one is simulated.
        self.create_delay = create_delay
        #: How long ``docker start`` takes. A start that overruns its grace
        #: is a *host* problem, and this is how one is simulated.
        self.start_delay = start_delay
        #: How long the **first** ``docker wait`` takes. The later waits are
        #: the backend asking the daemon for an exit code it already knows
        #: the answer to, and they answer at once.
        self.work_delay = work_delay
        #: How long ``docker inspect`` takes. An inspect that overruns its
        #: budget is a *report* that cannot be made, and it must not
        #: destroy the work it was reporting on.
        self.inspect_delay = inspect_delay
        #: The container's exit code, as the daemon reports it.
        self.exit_code = exit_code
        #: How long before *now* the daemon says the container started. It
        #: is what makes the deadline's anchor observable: the work budget
        #: is what is left of ``timeout_seconds`` after that moment.
        self.started_at_offset = started_at_offset
        #: Whether the daemon reports a start time at all. ``False`` is the
        #: daemon that does not say, which is what the fallback exists for.
        self.report_started_at = report_started_at
        #: Whether the ``docker rm`` **client** never exits. The removal's
        #: client is the backend's own process, and it has to be reaped.
        self.rm_hang = rm_hang
        #: Whether the wait *after* a kill never answers — the double fault
        #: where the kill did not land and the container is still running.
        self.wait_hang = wait_hang
        self.commands: list[list[str]] = []
        #: Every client the daemon handed out, paired with the command it
        #: answered. A test that needs to look at a *client* — rather than
        #: at the daemon's answer — finds it here.
        self.procs: list[tuple[list[str], _FakeProc]] = []
        #: The command currently being answered. It is what pairs a recorded
        #: budget with the call it was given to.
        self.current: list[str] = []
        self._started_at: float | None = None
        self._inspects = 0
        self._waits = 0

    async def __call__(self, *args: str, **_kwargs: Any) -> _FakeProc:
        command = [str(a) for a in args]
        self.commands.append(command)
        self.current = command
        proc = self._respond(command)
        self.procs.append((command, proc))
        return proc

    def proc_for(self, subcommand: str) -> _FakeProc:
        """The client the daemon handed out for ``docker <subcommand>``."""
        for command, proc in self.procs:
            if len(command) > 1 and command[1] == subcommand:
                return proc
        raise AssertionError(
            f"no `docker {subcommand}` client was issued; commands={self.commands}"
        )

    def command_for(self, subcommand: str) -> list[str]:
        """The argv of the first ``docker <subcommand>`` that was issued."""
        for command in self.commands:
            if len(command) > 1 and command[1] == subcommand:
                return command
        raise AssertionError(
            f"no `docker {subcommand}` was issued; commands={self.commands}"
        )

    def _respond(self, command: list[str]) -> _FakeProc:
        subcommand = command[1] if len(command) > 1 else ""
        if subcommand == "create":
            return _FakeProc(b"a-container-id\n", delay=self.create_delay)
        if subcommand == "start":
            return _FakeProc(f"{command[-1]}\n".encode(), delay=self.start_delay)
        if subcommand == "wait":
            delay = self.work_delay if self._waits == 0 else 0.0
            self._waits += 1
            hang = self.wait_hang and self._waits == 2
            return _FakeProc(f"{self.exit_code}\n".encode(), delay=delay, hang=hang)
        if subcommand == "inspect":
            return _FakeProc(self._state().encode(), delay=self.inspect_delay)
        if subcommand == "logs":
            return _FakeProc(b"MARKER_OUT\n", b"MARKER_ERR\n")
        if subcommand == "rm":
            return _FakeProc(b"", hang=self.rm_hang)
        return _FakeProc(b"")

    def _state(self) -> str:
        if self._started_at is None:
            self._started_at = time.time() - self.started_at_offset
        started_at = (
            _iso(self._started_at)
            if self.report_started_at
            # The zero time is what the daemon reports for "this never
            # happened", and it is not a moment in history.
            else "0001-01-01T00:00:00Z"
        )
        if self._inspects == 0:
            self._inspects += 1
            return json.dumps(
                {
                    "Running": True,
                    "ExitCode": 0,
                    "StartedAt": started_at,
                    "FinishedAt": "0001-01-01T00:00:00Z",
                }
            )
        return json.dumps(
            {
                "Running": False,
                "ExitCode": self.exit_code,
                "StartedAt": started_at,
                "FinishedAt": _iso(time.time()),
                "OOMKilled": False,
            }
        )


def _sandbox(
    monkeypatch: pytest.MonkeyPatch,
    daemon: _FakeDaemon,
    settings: SandboxSettings | None = None,
) -> DockerSandbox:
    """A backend whose daemon is the fake and whose health check passes."""
    sb = DockerSandbox(settings or SandboxSettings())
    monkeypatch.setattr(sb, "_exec", daemon)
    monkeypatch.setattr(sb, "healthcheck", _always_healthy)
    return sb


def _budget_for(recorder: Any, subcommand: str) -> float | None:
    """The budget the backend gave the call to ``docker <subcommand>``.

    The first call of that kind, because the backend makes a *second*
    ``docker wait`` after a kill — to ask the daemon for the exit code it
    already knows the answer to — and that one is not the work's budget.
    """
    for label, budget in recorder.calls:
        parts = label.split()
        if len(parts) > 1 and parts[1] == subcommand:
            return budget
    raise AssertionError(f"no `docker {subcommand}` call was recorded: {recorder.calls}")


async def test_the_work_budget_is_the_work_budget_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The wait is bounded by ``timeout_seconds``, not by the sum of both.

    The defect this pins: the backend built **one** budget —
    ``timeout_seconds + container_start_grace_seconds`` — and gave it to a
    single timer around ``docker run``, so the *workload* could run for the
    sum and be reported as a success. Measured on this host: a step with a
    0.25-second budget and a three-second workload ran for 3.6 seconds and
    was told ``timed_out=False``. A quarter-second budget became five and a
    quarter seconds of work.
    """
    daemon = _FakeDaemon()
    recorder = record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=5.0)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=0.25))

    work_budget = _budget_for(recorder, "wait")
    assert work_budget is not None
    assert work_budget <= 0.25
    # The sum is the defect, and it must not appear anywhere at all.
    assert 5.25 not in recorder.timeouts
    assert result.timed_out is False


async def test_the_work_deadline_is_anchored_at_the_daemons_own_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The budget is what is left of ``timeout_seconds`` after ``StartedAt``.

    The deadline is ``StartedAt + timeout_seconds`` in the **daemon's**
    clock, so the time the container has already been alive is spent out of
    the caller's budget — not added to it, and not ignored either. A
    deadline computed from the client's own clock would drift by however
    late the client was, and a deadline that drifts is a deadline the
    caller cannot rely on.
    """
    daemon = _FakeDaemon(started_at_offset=0.2)
    recorder = record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=5.0)
    )

    await sb.run(_spec_with(tmp_path, timeout_seconds=1.0))

    work_budget = _budget_for(recorder, "wait")
    assert work_budget is not None
    # 1.0 s of budget, of which 0.2 s is already spent.
    assert work_budget == pytest.approx(0.8, abs=0.05)


async def test_the_start_calls_are_bounded_by_the_start_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """``create`` and ``start`` each get the start grace, and nothing else.

    The grace is the operator's statement about how slow the daemon may be
    before the *start* is a failure. It is not the work's budget and it is
    not the work's to pay.
    """
    daemon = _FakeDaemon()
    recorder = record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=2.5)
    )

    await sb.run(_spec_with(tmp_path, timeout_seconds=30.0))

    assert _budget_for(recorder, "create") == 2.5
    assert _budget_for(recorder, "start") == 2.5


@pytest.mark.parametrize("phase", ["create", "start"])
async def test_a_start_breach_is_not_a_work_timeout(
    phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """A start that overruns the grace is a *start* failure.

    The two failures demand opposite responses: a work timeout is a
    workload problem, and a start timeout is a **host** problem — a daemon
    that is too slow, a missing image, a runtime that is not registered. A
    caller that cannot tell them apart retries the workload on a host that
    cannot start it, and fails identically.

    **Both** phases are pinned here, because they are two branches with two
    budgets: the ``docker create`` and the ``docker start`` each get the
    whole grace, and each has its own breach. Only the ``start`` branch was
    covered before, which left the ``create`` branch free to report a
    work timeout — a *workload* failure for a container that never ran.
    """
    daemon = _FakeDaemon(
        create_delay=2.0 if phase == "create" else 0.0,
        start_delay=2.0 if phase == "start" else 0.0,
    )
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=0.05)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=30.0))

    assert result.killed_reason == "start_timeout"
    assert result.timed_out is False
    assert not result.ok
    # The start's cost is reported even when the start is what failed —
    # otherwise a breached start looks like a run that never began.
    assert result.start_duration_ms > 0.0


@pytest.mark.parametrize("phase", ["create", "start"])
async def test_a_container_killed_by_the_start_grace_is_not_reported_as_memory(
    phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """Exit 137 from a *start* kill is not an out-of-memory kill.

    137 is 128 + SIGKILL, and it is what the cgroup OOM killer produces — and
    it is also what ``docker kill`` produces. The start timeout is therefore
    checked **first**: reporting this as "memory" would send the
    orchestrator to retry at a higher modality when the real problem is a
    host that cannot start a container in time.
    """
    daemon = _FakeDaemon(
        create_delay=2.0 if phase == "create" else 0.0,
        start_delay=2.0 if phase == "start" else 0.0,
        exit_code=137,
    )
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=0.05)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=30.0))

    assert result.exit_code == 137
    assert result.killed_reason == "start_timeout"
    assert result.killed_reason != "memory"
    assert result.timed_out is False


async def test_the_containment_flags_survive_the_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The ``create`` argv still carries every containment flag.

    The rewrite replaced ``docker run`` with ``docker create`` and dropped
    ``--rm``; nothing else about the container's containment may have moved.
    The uid is never 0 and ``--user`` is always passed, and the isolation
    level follows the **effective** runtime — so the runtime flag is here
    too, and only when the daemon really has it.
    """
    daemon = _FakeDaemon()
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_runtime="runsc")
    )
    sb._runtime_available = True

    await sb.run(_spec(tmp_path))

    create = daemon.command_for("create")
    for flag in (
        "--user",
        "--cap-drop",
        "--read-only",
        "--security-opt",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--network",
        "--tmpfs",
        "--runtime",
    ):
        assert flag in create
    assert create[create.index("--cap-drop") + 1] == "ALL"
    assert create[create.index("--security-opt") + 1] == "no-new-privileges"
    assert create[create.index("--runtime") + 1] == "runsc"
    # The uid is never 0, whatever the workspace looks like.
    uid = create[create.index("--user") + 1].split(":")[0]
    assert uid not in {"0", "root"}


async def test_the_container_is_removed_on_the_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The container is removed, forcibly, on the way out.

    ``--rm`` is gone — the logs have to outlive the container — so the
    removal is explicit. A container that is left behind holds the
    workspace bind mount, which is precisely the orphan the timeout exists
    to prevent.
    """
    daemon = _FakeDaemon()
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    await sb.run(_spec(tmp_path))

    removal = daemon.command_for("rm")
    assert removal[1:3] == ["rm", "-f"]


async def test_the_container_is_removed_on_the_timeout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The removal happens on the timeout path too, not only on the happy one.

    The timeout path is the one where the container is most likely to still
    be running, and that is exactly the case the removal has to cover.
    """
    daemon = _FakeDaemon(work_delay=2.0)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=0.2))

    assert result.timed_out is True
    removal = daemon.command_for("rm")
    assert removal[1:3] == ["rm", "-f"]


async def test_a_work_overrun_with_a_generous_grace_still_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The two budgets are enforced **independently**.

    A generous start grace must not buy the work any extra time: the grace
    is the daemon's budget, and the work's budget is the work's.
    """
    daemon = _FakeDaemon(work_delay=2.0)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=30.0)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=0.2))

    assert result.timed_out is True
    assert result.killed_reason == "timeout"


async def test_a_start_overrun_with_a_generous_work_budget_still_fails_as_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """And the other way round: a generous work budget buys no extra start.

    A slow start is a host failure whatever the workload's budget is, and
    reporting it as a work timeout would send the operator looking at the
    wrong thing entirely.
    """
    daemon = _FakeDaemon(start_delay=2.0)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=0.05)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=300.0))

    assert result.killed_reason == "start_timeout"
    assert result.timed_out is False


async def test_a_state_query_that_does_not_answer_does_not_destroy_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """A report that cannot be made must not destroy the work it reports on.

    ``_docker_call`` raises :class:`TimeoutError` whenever the daemon does
    not answer inside the call's budget, and the pre-work inspect's budget
    **is the start grace** — so an operator who tightens the grace to fail
    fast on a slow host *raises* the probability of hitting this.

    When it did escape, the container was created, started, killed and
    removed because the *state query* did not answer, and no result was
    produced at all: a bare :class:`TimeoutError`, which is not a
    :class:`~paa.core.errors.SandboxError` and so is not on the failure
    surface the callers are documented to handle. A run that was killed
    still produces a result, because the ledger needs to record *how* it
    died.
    """
    daemon = _FakeDaemon(inspect_delay=0.5)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=0.05)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=5.0))

    # A result, not an exception.
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.ok
    # The daemon did not say when the container started, so the client's own
    # start-of-work clock is the anchor — and the run still finished.
    assert result.start_duration_ms > 0.0
    # And the container was removed anyway: a query that fails is not a
    # reason to leave an orphan holding the workspace mount.
    assert daemon.command_for("rm")[1:3] == ["rm", "-f"]


async def test_the_start_duration_is_reported_and_is_inside_the_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The start's cost is a number the caller can see.

    ``start_duration_ms`` is the field that says "the start was not charged
    to the work", and it is the field the ADR's budget table is written
    against. A field nobody asserts is a field nobody has to keep right.
    """
    daemon = _FakeDaemon()
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=5.0))

    assert 0.0 < result.start_duration_ms <= result.duration_ms
    # And it survives into the ledger-safe summary.
    assert result.to_payload()["start_duration_ms"] == round(result.start_duration_ms, 3)


async def test_the_duration_is_the_total_wall_clock_of_both_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """``duration_ms`` is the **total**, and it covers the start as well.

    It keeps that meaning on purpose: this field is the sandbox's **own**
    measurement of the two phases together, and a total that silently stops
    counting the start is not a total. The skill-ranking signal is a
    **different** field of the same name — the improvement loop reads
    ``duration_ms`` from the run record (``hot_serving_execution_runs``),
    which ``observability/runs.py`` computes from the run's own
    ``started_at``/``ended_at`` and never from this field — so redefining
    *this* one would not change a ranking signal. The start is reported
    separately, in ``start_duration_ms`` — separately, not *instead*.
    """
    daemon = _FakeDaemon(start_delay=0.25, work_delay=0.25)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=5.0))

    # The start phase, on its own.
    assert result.start_duration_ms >= 250.0
    # The total covers the start **and** the work — a duration that excluded
    # the start would be under 250 ms here.
    assert result.duration_ms >= 500.0
    assert result.duration_ms >= result.start_duration_ms


async def test_the_fallback_budget_is_anchored_on_the_clients_own_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """When the daemon does not say, the client's own clock is the anchor.

    The fallback fires when ``docker inspect`` fails or the daemon reports
    the zero time. It used to grant the caller's **full** ``timeout_seconds``
    measured from the moment of the *inspect* — so the container's total
    lifetime was ``timeout + (create + start + inspect)``, more than the
    budget the ADR says the wait bounds.

    The anchor is now the client's own start-of-work clock: the moment
    ``docker start`` returned. The inspect has already eaten its round trip
    out of the caller's budget, which is the honest accounting — the
    container really had been alive for that long.
    """
    daemon = _FakeDaemon(inspect_delay=0.5, report_started_at=False)
    recorder = record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=5.0)
    )

    await sb.run(_spec_with(tmp_path, timeout_seconds=10.0))

    budget = _budget_for(recorder, "wait")
    assert budget is not None
    # 10 s of budget, of which the 0.5 s inspect has already been spent.
    assert budget == pytest.approx(9.5, abs=0.25)
    # And the fallback does not hand back the caller's *full* ask.
    assert budget < 10.0


async def test_a_budget_spent_by_a_clock_is_named_as_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """A clock that disagrees with the host must not read as a workload fault.

    The deadline is ``StartedAt + timeout`` in the **daemon's** clock, and
    the budget is measured against the **client's** ``now``. A daemon whose
    clock runs behind the host's by more than the budget makes the budget
    zero, and the container is killed the instant the wait starts. That errs
    the safe way — but reported as an ordinary work timeout, it sends the
    operator to look at a workload that did nothing wrong.
    """
    daemon = _FakeDaemon(started_at_offset=10.0)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    with structlog.testing.capture_logs() as captured:
        result = await sb.run(_spec_with(tmp_path, timeout_seconds=1.0))

    exhausted = [
        entry
        for entry in captured
        if entry.get("event") == "sandbox.docker.work_budget_exhausted"
    ]
    assert exhausted, captured
    entry = exhausted[0]
    assert entry["timeout_seconds"] == 1.0
    # The container was running, and the daemon's clock was behind.
    assert entry["container_running"] is True
    assert entry["daemon_clock_lag_seconds"] is not None
    assert entry["daemon_clock_lag_seconds"] >= 9.0
    # And the clock is *named*, so the log says which clock is at fault.
    assert "daemon's clock" in entry["detail"]
    # The run still errs the safe way.
    assert result.timed_out is True
    assert result.killed_reason == "timeout"


async def test_a_removal_that_overruns_leaves_no_client_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The removal's *client* is reaped when the removal overruns.

    The container is the daemon's business; the ``docker rm`` **process** is
    the backend's own, and a client left running holds the event loop's
    transport table open — the same leak the health check's own reap exists
    to prevent.
    """
    daemon = _FakeDaemon(rm_hang=True)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)
    monkeypatch.setattr(docker_backend, "_STOP_TIMEOUT_SECONDS", 0.05)

    result = await sb.run(_spec(tmp_path))

    # The run itself is unaffected: the removal's failure is swallowed on
    # purpose, so a finished run does not become an exception.
    assert result.exit_code == 0
    assert result.ok
    # And the client was killed rather than left running.
    assert daemon.proc_for("rm").killed is True


async def test_a_kill_that_did_not_land_does_not_stall_the_timeout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The double fault is bounded by the *short* budget, not the long one.

    If the ``docker kill`` itself failed, the container is still running and
    ``docker wait`` blocks — and the client's patience with a process that
    will not die (15 s) is the wrong number for a path that has already
    failed. The ``docker rm -f`` in the ``finally`` is what cleans the
    container up; the wait only has to give up quickly.
    """
    daemon = _FakeDaemon(work_delay=2.0, wait_hang=True)
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)
    monkeypatch.setattr(docker_backend, "_KILL_SETTLE_TIMEOUT_SECONDS", 0.05)

    started = time.perf_counter()
    result = await sb.run(_spec_with(tmp_path, timeout_seconds=0.2))
    elapsed = time.perf_counter() - started

    assert result.timed_out is True
    assert result.killed_reason == "timeout"
    # The stall is bounded by the settle budget, not by the client's
    # patience with a process that will not die.
    assert elapsed < 2.0
    assert docker_backend._KILL_SETTLE_TIMEOUT_SECONDS < docker_backend._STOP_TIMEOUT_SECONDS
    # And the waiting client was reaped rather than left running.
    assert daemon.proc_for("wait").killed is True


async def test_two_runs_never_share_a_container_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """The container's name is a handle, and a taken name is a failed run.

    The name used to be ``paa-{task}-{milliseconds}``: a millisecond clock,
    so two runs that began in the same millisecond collided and the second
    one failed to start for a reason that had nothing to do with the
    workload. Dropping ``--rm`` made the window wider, because the name is
    held until the ``docker rm -f`` in the ``finally``.

    The clock is frozen here, so the collision is deterministic rather than
    a matter of how fast the machine is.
    """
    daemon = _FakeDaemon()
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)
    frozen = 1_700_000_000.0
    monkeypatch.setattr(docker_backend.time, "time", lambda: frozen)

    await sb.run(_spec(tmp_path))
    await sb.run(_spec(tmp_path))

    names = [
        command[command.index("--name") + 1]
        for command in daemon.commands
        if command[1:2] == ["create"]
    ]
    assert len(names) == 2
    assert names[0] != names[1]
    # And the name is still short enough for the daemon to accept.
    assert all(len(name) <= 63 for name in names)


async def test_the_output_arrives_with_the_two_streams_kept_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """``docker logs`` demultiplexes, so stdout and stderr stay apart.

    Measured on this host: a container that writes to both streams comes
    back with ``MARKER_OUT`` on stdout and ``MARKER_ERR`` on stderr, exactly
    as ``docker run`` did. The rewrite does not lose the separation.
    """
    daemon = _FakeDaemon()
    record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(monkeypatch, daemon)

    result = await sb.run(_spec(tmp_path))

    assert "MARKER_OUT" in result.stdout
    assert "MARKER_ERR" in result.stderr
    assert result.exit_code == 0
    assert result.ok


async def test_a_spec_with_no_timeout_waits_for_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_wait_for: Any
) -> None:
    """``timeout_seconds=None`` means "wait forever", not "wait for nothing".

    It is the MAX modality's budget — a human decides, not a clock — and
    the wait must not be bounded by the start grace either.
    """
    daemon = _FakeDaemon()
    recorder = record_wait_for(docker_backend, lambda: " ".join(daemon.current))
    sb = _sandbox(
        monkeypatch, daemon, SandboxSettings(container_start_grace_seconds=2.5)
    )

    result = await sb.run(_spec_with(tmp_path, timeout_seconds=None))

    assert _budget_for(recorder, "wait") is None
    assert result.exit_code == 0


def test_the_docker_backend_reports_the_start_budget_it_spends() -> None:
    """The backend that *has* a start phase says what it does with the grace.

    The other backends report zero and say why. A setting only one backend
    honours must not be silently applied to the others, and it must not be
    invisible either.
    """
    sb = DockerSandbox(SandboxSettings(container_start_grace_seconds=3.0))
    described = sb.describe()
    assert described["container_start_grace_seconds"] == 3.0
    assert "separate" in described["container_start_budget"]


def test_the_simple_profile_accommodates_the_container_fixed_cost() -> None:
    """SIMPLE's budget has to cover what the container backend cannot avoid.

    The container's fixed cost — the runtime's setup and teardown, inside
    the lifetime the work budget bounds — is **~200 ms** (measured: a
    ``sh -c true`` container's lifetime is 186-225 ms; 114-130 ms on the
    host this was re-measured on). A budget below that cannot accommodate
    even a trivial container step, so every SIMPLE step would fail on the
    container backend — a deployment that looks healthy and fails every
    trivial task.

    The trade-off is deliberate and it is recorded in ADR-0031: a SIMPLE
    step's blast radius grows from 0.25 s of container time to 1.0 s, while
    the *wall clock* barely changes, because the container's start dominates
    it and the start is no longer charged to the work.
    """
    measured_fixed_cost_seconds = 0.2
    simple = MODALITY_PROFILES[ComplexityModality.SIMPLE]
    assert simple.timeout_seconds is not None
    assert simple.timeout_seconds >= measured_fixed_cost_seconds * 5
    # And the other profiles are untouched.
    assert MODALITY_PROFILES[ComplexityModality.STANDARD].timeout_seconds == 10.0
    assert MODALITY_PROFILES[ComplexityModality.COMPLEX].timeout_seconds == 30.0
    assert MODALITY_PROFILES[ComplexityModality.MAX].timeout_seconds is None
