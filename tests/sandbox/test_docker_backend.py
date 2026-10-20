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
import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
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
    monkeypatch.setattr(
        sb, "_HEALTHCHECK_TIMEOUT_SECONDS", 0.01, raising=False
    )
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
    """A gVisor run is not a cgroup limit, and must not be recorded as one.

    gVisor does its own accounting, and on a cgroup v2 host the delegation is
    limited enough that the cap is not enforced as a hard ceiling. Recording
    "cgroup" would put a ceiling in the ledger that was never applied.
    """
    runsc = DockerSandbox(SandboxSettings(container_runtime="runsc"))
    runsc._runtime_available = True
    args = runsc._build_args(_spec(tmp_path), tmp_path, "paa-test")
    assert "--runtime" in args  # sanity: this is the gVisor path

    # The enforcement string is chosen by the runtime, not by the spec.
    assert runsc._memory_enforcement() == "gvisor"
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
