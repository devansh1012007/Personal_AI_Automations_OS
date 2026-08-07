"""Container containment, with gVisor when the runtime is present.

This is the backend the RFC actually describes. Docker is **not installed on
the target machine**, so nothing here runs today — but it is implemented in
full rather than stubbed, because the alternative is discovering on the day
Docker appears that the "supported" path was never written.

:meth:`DockerSandbox.healthcheck` returns ``False`` when the CLI is absent, so
the ``auto`` factory skips it cleanly and the runtime degrades to WSL or
subprocess without the caller doing anything.

Isolation levels this backend can report:

``VM``
    ``--runtime=runsc`` is available and selected. gVisor interposes a
    user-space kernel; the container's syscalls are serviced by Sentry, not by
    the host kernel. This is the RFC §13 requirement, actually met.
``NAMESPACE``
    Stock runc. Namespaces plus cgroups plus seccomp-default — strong, and
    still not a syscall boundary.

The level is resolved per-run from what Docker reports, never assumed. A
backend that *claims* VM while silently running runc would be worse than one
that admits NAMESPACE, because the policy layer routes high-risk work on the
strength of that claim.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from typing import Any

import structlog

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSpec,
    build_child_env,
    resolve_workspace,
)

__all__ = ["DockerSandbox"]

log = structlog.get_logger(__name__)

_HEALTHCHECK_TIMEOUT_SECONDS = 20.0
_STOP_TIMEOUT_SECONDS = 15.0

#: Matches the ledger's ``allocated_worker_image`` default so the recorded
#: image and the executed image cannot drift apart.
DEFAULT_IMAGE = "paa/base_worker:v4.1"


class DockerSandbox(Sandbox):
    """Run a command in a throwaway container.

    Every container is ``--rm``: the filesystem layer is destroyed on exit, so
    a workload cannot persist state between runs except through the explicitly
    mounted workspace. That property is what makes retries clean.
    """

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        image: str = DEFAULT_IMAGE,
        runtime: str | None = None,
        prefer_gvisor: bool | None = None,
    ) -> None:
        self._settings = settings or SandboxSettings()
        self._image = image
        # The OCI runtime to request via ``--runtime``. Comes from
        # ``SandboxSettings.container_runtime`` (default ``runc``) unless an
        # explicit override is passed. ``runsc`` selects gVisor (RFC §13).
        self._runtime = runtime if runtime is not None else self._settings.container_runtime
        # ``prefer_gvisor`` is retained for backward compatibility; when unset it
        # is derived from the configured runtime, so selecting ``runsc`` is the
        # single source of truth rather than two flags that can disagree.
        self._prefer_gvisor = (
            self._runtime == "runsc" if prefer_gvisor is None else prefer_gvisor
        )
        self._healthy: bool | None = None
        #: Whether the daemon reports the *configured* runtime is registered.
        self._runtime_available: bool | None = None

    @property
    def name(self) -> str:
        return "docker"

    @property
    def isolation_level(self) -> IsolationLevel:
        """VM only when gVisor (``runsc``) is confirmed present, else NAMESPACE.

        Reported optimistically *before* a probe has run (``None`` state) as
        NAMESPACE — understating containment is safe, overstating it is not. A
        non-gVisor custom runtime is not assumed to interpose on syscalls, so it
        stays at NAMESPACE regardless.
        """
        if self._runtime == "runsc" and self._runtime_available:
            return IsolationLevel.VM
        return IsolationLevel.NAMESPACE

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def image(self) -> str:
        return self._image

    async def healthcheck(self) -> bool:
        """Whether the Docker CLI exists and a daemon answers. Never raises.

        Checks the *daemon*, not just the CLI: ``docker`` on PATH with Docker
        Desktop stopped is the single most common state on a Windows dev
        machine, and a CLI-only check would route work to a backend that
        cannot start a container.
        """
        if self._healthy is not None:
            return self._healthy
        self._healthy = await self._probe()
        return self._healthy

    async def _probe(self) -> bool:
        if shutil.which("docker") is None:
            log.debug("sandbox.docker.unavailable", reason="docker CLI not on PATH")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_HEALTHCHECK_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            log.debug("sandbox.docker.unavailable", reason=str(exc))
            return False

        if proc.returncode != 0:
            log.debug(
                "sandbox.docker.daemon_unreachable",
                stderr=stderr.decode("utf-8", errors="replace").strip()[:200],
            )
            return False

        log.info(
            "sandbox.docker.available",
            server_version=stdout.decode("utf-8", errors="replace").strip(),
        )
        await self._detect_runtime()
        return True

    async def _detect_runtime(self) -> bool:
        """Ask the daemon whether the *configured* runtime is registered.

        Stock ``runc`` is always present, so no probe is needed for it. For any
        other runtime (``runsc``, or a bespoke one) the daemon's registered
        runtimes are queried; the result gates both ``--runtime`` selection and
        the honest :attr:`isolation_level`. A configured runtime the daemon does
        not know about is a loud warning, never a silent overstatement of
        containment.
        """
        if self._runtime_available is not None:
            return self._runtime_available
        if self._runtime == "runc":
            self._runtime_available = True
            return True

        self._runtime_available = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                "--format",
                "{{json .Runtimes}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_HEALTHCHECK_TIMEOUT_SECONDS
            )
            if proc.returncode == 0:
                runtimes = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
                self._runtime_available = self._runtime in runtimes
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            log.debug("sandbox.docker.runtime_probe_failed", reason=str(exc))

        if not self._runtime_available:
            log.warning(
                "sandbox.docker.runtime_unavailable",
                requested=self._runtime,
                detail="daemon does not report this runtime; falling back to runc "
                "with NAMESPACE isolation. See docs/deployment/docker.md.",
            )
        log.info(
            "sandbox.docker.runtime", requested=self._runtime, available=self._runtime_available
        )
        return bool(self._runtime_available)

    def _build_args(self, spec: SandboxSpec, workspace: Any, container_name: str) -> list[str]:
        """Assemble the full ``docker run`` argv.

        Each flag maps to a specific RFC §13 requirement:

        ``--rm``
            No layer survives the run.
        ``--network=none``
            Real egress severance — no interfaces but loopback exist in the
            container's netns. This is the guarantee the subprocess backend
            documents that it *cannot* make.
        ``--read-only``
            Root filesystem is immutable; only the workspace bind mount and
            the tmpfs are writable, so a workload cannot tamper with its own
            interpreter or libraries mid-run.
        ``--tmpfs /tmp``
            A writable scratch area that is RAM-backed and size-capped, so
            ``--read-only`` does not break every well-behaved program while
            still bounding scratch growth.
        ``--memory`` / ``--memory-swap``
            cgroup memory cap. ``--memory-swap`` is pinned equal to
            ``--memory`` deliberately: left unset Docker grants swap equal to
            memory, silently doubling the effective ceiling and making the
            limit a suggestion.
        ``--cpus``
            CFS quota.
        ``--pids-limit``
            Fork-bomb containment. Without it a workload can exhaust host PIDs
            while staying comfortably inside its memory budget.
        ``--cap-drop=ALL`` / ``--security-opt=no-new-privileges``
            No capabilities, and no way to regain any via setuid binaries.
        """
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--workdir",
            "/workspace",
            "--read-only",
            "--tmpfs",
            # A container-internal path, not a host one: this string is
            # interpreted by the container runtime inside the mount
            # namespace, so the host /tmp is never touched.
            "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108
            "--memory",
            f"{spec.memory_mb}m",
            "--memory-swap",
            f"{spec.memory_mb}m",
            "--cpus",
            f"{spec.cpu_cores}",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]

        # Request a non-default runtime only when the daemon actually has it.
        # ``--runtime=runsc`` is the RFC §13 containment; a bespoke runtime name
        # is honoured identically. Stock runc needs no flag.
        if self._runtime != "runc" and self._runtime_available:
            args += ["--runtime", self._runtime]

        if not spec.allow_network:
            args += ["--network", "none"]

        args += ["--volume", f"{workspace}:/workspace:rw"]
        for index, mount in enumerate(spec.read_only_mounts):
            args += ["--volume", f"{mount.resolve()}:/mnt/ro{index}:ro"]

        # Env is passed as explicit -e pairs built from the allowlist. Docker
        # does not inherit the host environment, so this is the only channel —
        # which makes the allowlist airtight on this backend.
        for key, value in sorted(build_child_env(spec, workspace=workspace).items()):
            if key in {"SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP"}:
                continue  # Windows-only bootstrap keys are meaningless in Linux
            args += ["--env", f"{key}={value}"]

        args.append(self._image)
        args.extend(spec.command)
        return args

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` in a container."""
        if not await self.healthcheck():
            raise SandboxError(
                "docker backend is not available",
                hint="install Docker Desktop or select another backend",
            )

        workspace = resolve_workspace(spec.workspace_path)
        container_name = f"paa-{spec.parent_task_id or 'task'}-{int(time.time() * 1000)}"[:63]
        args = self._build_args(spec, workspace, container_name)

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SandboxError("failed to launch docker", reason=str(exc)) from exc

        timed_out = False
        killed_reason: str | None = None
        cap = self._settings.max_capture_bytes

        try:
            if spec.timeout_seconds is None:
                stdout_raw, stderr_raw = await proc.communicate()
            else:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=spec.timeout_seconds
                )
        except TimeoutError:
            timed_out = True
            killed_reason = "timeout"
            stdout_raw, stderr_raw = b"", b""
            # `docker kill` the container rather than killing the CLI: killing
            # the client leaves the container running and holding the workspace
            # bind mount, which is precisely the orphan the timeout exists to
            # prevent.
            await self._force_kill(container_name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            log.warning("sandbox.docker.timeout", container=container_name)

        exit_code = proc.returncode
        # 137 == 128 + SIGKILL: the cgroup OOM killer fired. Distinguishing it
        # from an ordinary failure is what lets the orchestrator retry at a
        # higher modality instead of retrying identically and failing again.
        if exit_code == 137 and not timed_out:
            killed_reason = "memory"

        truncated = len(stdout_raw) > cap or len(stderr_raw) > cap
        duration_ms = (time.perf_counter() - started) * 1000.0

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_raw[:cap].decode("utf-8", errors="replace"),
            stderr=stderr_raw[:cap].decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            peak_rss_mb=None,
            timed_out=timed_out,
            killed_reason=killed_reason,
            truncated_output=truncated,
            backend=self.name,
            isolation_level=self.isolation_level,
            memory_enforcement="cgroup",
        )

    @staticmethod
    async def _force_kill(container_name: str) -> None:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=_STOP_TIMEOUT_SECONDS)
