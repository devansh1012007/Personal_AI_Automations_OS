"""Host-process containment for Windows and POSIX.

READ THIS BEFORE TRUSTING IT
============================
This backend is **materially weaker than gVisor** and is not a substitute for
it. gVisor interposes on the syscall boundary: the workload talks to a
user-space kernel and never reaches the host kernel's syscall table. Nothing
here does that. A process started by this backend runs:

* on the host kernel, with the **entire** syscall surface reachable;
* as the **same OS user** as the runtime, so every file that user can read, the
  workload can read — including ``~/.ssh``, the ledger database, and this
  source tree;
* on the **host network stack**, reachable in and out.

What it does enforce, honestly:

===========================  =========================================
Wall-clock timeout           Yes — kills the whole process **tree**
Peak memory ceiling          Yes on Windows via Job Object (kernel-enforced);
                             elsewhere sampled best-effort by the watchdog
Environment isolation        Yes — the child env is built from empty
Output volume                Yes — capture is capped
Workspace cwd jail           Yes — cwd is a resolved path inside the workspace
Filesystem confinement       **No** — cwd is not a chroot
Network severance            **No** — see :meth:`SubprocessSandbox.run`
Syscall restriction          **No** — none whatsoever
===========================  =========================================

It is a guard against a *confused* agent — one that loops forever, allocates
without bound, or writes to the wrong directory. It is **not** a guard against
a *hostile* one. Where the threat model includes hostile code, the policy layer
must require :class:`~paa.sandbox.base.IsolationLevel.NAMESPACE` or better and
route to :class:`~paa.sandbox.wsl_backend.WslSandbox` or
:class:`~paa.sandbox.docker_backend.DockerSandbox`.

SPEC DEVIATION (docs/adr/0006, docs/adr/0009).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import sys
import time
from pathlib import Path
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
from paa.sandbox.watchdog import ResourceWatchdog

__all__ = ["SubprocessSandbox"]

log = structlog.get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"

#: How long to wait for a killed process to actually die before giving up and
#: reporting the result anyway. A process wedged in an uninterruptible kernel
#: wait can outlive SIGKILL; blocking the runtime forever on it would turn one
#: stuck workload into a stuck host.
_REAP_TIMEOUT_SECONDS = 10.0

#: How long to wait for the output readers to drain after the process exits.
_DRAIN_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Windows Job Object — genuinely kernel-enforced memory containment
# ---------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        # ULONG_PTR — pointer-sized integer, NOT a pointer. Declaring this as
        # a POINTER type silently mis-sizes the struct on 64-bit and every
        # field after it reads garbage.
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _WindowsJobObject:
    """A kernel-enforced resource cap around a process tree.

    This is the one place where the Windows backend is genuinely *stronger*
    than a polling watchdog rather than merely equivalent: the kernel refuses
    the allocation at ``VirtualAlloc`` time. The workload sees ``MemoryError``
    (or a failed ``malloc``); no sampling interval exists for a spike to hide
    in, and the limit covers every process in the job, not just the parent.

    Three flags are set:

    ``JOB_OBJECT_LIMIT_PROCESS_MEMORY``
        Per-process committed-memory cap.
    ``JOB_OBJECT_LIMIT_JOB_MEMORY``
        Cap on the *sum* across the tree, so spawning ten children each just
        under the per-process limit does not multiply the budget.
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
        Every process in the job dies when the last handle closes. This makes
        cleanup total even if the runtime itself is killed — the OS becomes the
        reaper, which is the only reaper that cannot itself be crashed.

    KNOWN RACE: the process is assigned to the job *after* ``CreateProcess``
    returns, because ``asyncio.create_subprocess_exec`` gives no pre-resume
    hook (the ``CREATE_SUSPENDED`` route needs the thread handle, which
    ``Popen`` does not expose). The window is sub-millisecond and a process
    cannot meaningfully allocate in it, but it is a real window and is
    documented rather than papered over.
    """

    def __init__(self, memory_mb: int, *, max_processes: int = 64) -> None:
        self._handle: int | None = None
        self._kernel32: Any = None
        self.active = False
        self._memory_mb = memory_mb
        self._max_processes = max_processes

    def create(self) -> bool:
        """Create the job and apply limits. ``False`` on any failure."""
        if not _IS_WINDOWS:
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                log.debug("sandbox.job_object.create_failed", error=ctypes.get_last_error())
                return False

            info = _JobObjectExtendedLimitInformation()
            limit_bytes = self._memory_mb * 1024 * 1024
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _JOB_OBJECT_LIMIT_JOB_MEMORY
                | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            info.BasicLimitInformation.ActiveProcessLimit = self._max_processes
            info.ProcessMemoryLimit = limit_bytes
            info.JobMemoryLimit = limit_bytes

            ok = kernel32.SetInformationJobObject(
                ctypes.c_void_p(handle),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                log.debug("sandbox.job_object.set_info_failed", error=ctypes.get_last_error())
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return False

            self._handle = handle
            self._kernel32 = kernel32
            self.active = True
            return True
        except Exception as exc:
            # Any ctypes/ABI problem degrades to the watchdog rather than
            # taking down the run. Containment getting weaker is survivable;
            # the sandbox refusing to start is not.
            log.debug("sandbox.job_object.unavailable", error=str(exc))
            return False

    def assign(self, pid: int) -> bool:
        """Put ``pid`` (and everything it spawns) under the job's limits."""
        if not self.active or self._handle is None:
            return False
        try:
            self._kernel32.OpenProcess.restype = ctypes.c_void_p
            proc_handle = self._kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
            )
            if not proc_handle:
                return False
            try:
                ok = self._kernel32.AssignProcessToJobObject(
                    ctypes.c_void_p(self._handle), ctypes.c_void_p(proc_handle)
                )
                if not ok:
                    log.debug(
                        "sandbox.job_object.assign_failed",
                        pid=pid,
                        error=ctypes.get_last_error(),
                    )
                return bool(ok)
            finally:
                self._kernel32.CloseHandle(ctypes.c_void_p(proc_handle))
        except Exception as exc:
            log.debug("sandbox.job_object.assign_error", pid=pid, error=str(exc))
            return False

    def terminate(self) -> None:
        """Kill every process in the job immediately."""
        if not self.active or self._handle is None:
            return
        with contextlib.suppress(Exception):
            self._kernel32.TerminateJobObject(ctypes.c_void_p(self._handle), 1)

    def close(self) -> None:
        """Release the job. With KILL_ON_JOB_CLOSE this also reaps the tree."""
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._kernel32.CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None
        self.active = False


# ---------------------------------------------------------------------------


class SubprocessSandbox(Sandbox):
    """Run a command as a resource-capped child process. See module docstring.

    Isolation level is :attr:`~paa.sandbox.base.IsolationLevel.PROCESS` and
    that is not modesty — there is no filesystem, network or syscall boundary.
    """

    def __init__(self, settings: SandboxSettings | None = None) -> None:
        self._settings = settings or SandboxSettings()

    @property
    def name(self) -> str:
        return "subprocess"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    async def healthcheck(self) -> bool:
        """Always available — spawning a child is the one thing every host does."""
        return True

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` as a capped child process.

        NETWORK DENIAL IS NOT ENFORCED HERE, and ``spec.allow_network=False``
        must not be read as a guarantee. A plain child process shares the host
        network stack; severing it would need a per-process WFP firewall rule
        (Windows) or a network namespace (Linux), neither of which a subprocess
        can arrange for itself without Administrator/root. Refusing to run
        would be worse — it would push callers onto a backend with *no* limits
        at all — so we run and log the gap.

        Network denial for this backend is really enforced by two other layers:

        1. **Policy** refuses to dispatch a skill that declares
           ``PERM_NET_EGRESS`` when the mode does not grant it, so
           network-requiring work never reaches a sandbox that cannot contain
           it (:class:`paa.core.types.PermissionMode`).
        2. **The AST scanner** rejects ``socket``, ``urllib``, ``requests``,
           ``httpx`` and friends before execution
           (:class:`paa.validation.ast_scanner.AstSecurityScanner`).

        Both are pre-execution gates on *inspectable* code. Neither stops a
        compiled binary or a novel egress path. Say it plainly: on this
        backend, network denial is a code-review property, not a containment
        property.
        """
        workspace = resolve_workspace(spec.workspace_path)

        # Workspace jail. The cwd we hand the child must resolve to somewhere
        # inside the workspace — checked after resolution so a symlinked or
        # ``..``-laden workspace cannot slip past a string comparison.
        cwd = workspace
        if not cwd.is_relative_to(workspace.resolve()):  # pragma: no cover - defensive
            raise SandboxError(
                "resolved cwd escapes the workspace jail",
                cwd=str(cwd),
                workspace_path=str(workspace),
            )

        if spec.allow_network is False:
            log.debug(
                "sandbox.subprocess.network_not_severed",
                detail="allow_network=False cannot be enforced by a host subprocess",
                workspace=str(workspace),
            )
        if spec.read_only_mounts:
            log.warning(
                "sandbox.subprocess.read_only_mounts_ignored",
                detail="a host subprocess cannot enforce read-only mounts",
                count=len(spec.read_only_mounts),
            )

        env = build_child_env(spec, workspace=workspace)
        started = time.perf_counter()

        job = _WindowsJobObject(spec.memory_mb)
        job_active = job.create()

        popen_kwargs: dict[str, Any] = {}
        if not _IS_WINDOWS:
            # New session => new process group => killpg reaches grandchildren.
            # start_new_session is preferred over preexec_fn=os.setsid: it is
            # implemented in the child after fork by CPython itself and is safe
            # in a threaded parent, which preexec_fn is not.
            popen_kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            job.close()
            raise SandboxError(
                "failed to start sandboxed process",
                command=list(spec.command),
                workspace_path=str(workspace),
                reason=str(exc),
            ) from exc

        if job_active:
            job_active = job.assign(proc.pid)

        memory_enforcement = "job_object" if job_active else "watchdog"
        killed_reason: str | None = None
        timed_out = False

        watchdog = ResourceWatchdog(
            proc.pid,
            memory_mb=spec.memory_mb,
            interval_seconds=self._settings.watchdog_interval_seconds,
            # With a Job Object in force the kernel already refuses the
            # allocation, so the watchdog only observes; without one it is the
            # sole enforcement path and must kill.
            on_breach=None if job_active else (lambda reason, _rss: self._kill_tree(proc, job)),
        )
        watchdog.start()

        cap = self._settings.max_capture_bytes
        stdout_reader = asyncio.create_task(self._read_capped(proc.stdout, cap))
        stderr_reader = asyncio.create_task(self._read_capped(proc.stderr, cap))

        try:
            if spec.timeout_seconds is None:
                exit_code = await proc.wait()
            else:
                try:
                    exit_code = await asyncio.wait_for(proc.wait(), timeout=spec.timeout_seconds)
                except TimeoutError:
                    timed_out = True
                    killed_reason = "timeout"
                    log.warning(
                        "sandbox.subprocess.timeout",
                        pid=proc.pid,
                        timeout_seconds=spec.timeout_seconds,
                        command=list(spec.command)[:3],
                    )
                    await self._kill_tree(proc, job)
                    exit_code = await self._reap(proc)
        finally:
            await watchdog.stop()

        if watchdog.breached and killed_reason is None:
            killed_reason = "memory"

        stdout_bytes, stdout_truncated = await self._collect(stdout_reader)
        stderr_bytes, stderr_truncated = await self._collect(stderr_reader)

        if job_active:
            # Closing with KILL_ON_JOB_CLOSE guarantees no survivor holds the
            # workspace open, which on Windows would make the directory
            # undeletable and strand the next run.
            job.terminate()
        job.close()

        duration_ms = (time.perf_counter() - started) * 1000.0

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            peak_rss_mb=watchdog.peak_rss_mb,
            timed_out=timed_out,
            killed_reason=killed_reason,
            truncated_output=stdout_truncated or stderr_truncated,
            backend=self.name,
            isolation_level=self.isolation_level,
            memory_enforcement=memory_enforcement,
        )

    # -- output capture ----------------------------------------------------

    @staticmethod
    async def _read_capped(
        stream: asyncio.StreamReader | None, max_bytes: int
    ) -> tuple[bytes, bool]:
        """Read a pipe, keeping at most ``max_bytes``.

        Past the cap we keep *draining* but stop *storing*. That asymmetry is
        the whole design:

        * Storing without bound lets a workload that prints in a loop exhaust
          host RAM through the pipe buffer — the failure this cap exists for.
        * Draining without storing costs nothing and keeps the pipe moving.
        * **Not** draining would fill the OS pipe buffer (~64 KB) and block the
          child on ``write()``. It would look like containment, but it is a
          deadlock: the workload freezes mid-run and only the wall-clock
          timeout unwedges it, turning a chatty-but-correct job into a failed
          one.

        So the runaway process is allowed to keep running and keep printing
        into the void, and we report ``truncated=True``.
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

    @staticmethod
    async def _collect(reader: asyncio.Task[tuple[bytes, bool]]) -> tuple[bytes, bool]:
        """Await a reader, bounded.

        A reader can outlive the process when a grandchild inherited the pipe
        write end. We wait briefly, then cancel and report what we have — a
        surviving grandchild must not hold the runtime hostage.
        """
        try:
            return await asyncio.wait_for(asyncio.shield(reader), timeout=_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            return b"", True
        except asyncio.CancelledError:  # pragma: no cover - defensive
            return b"", True

    # -- teardown ----------------------------------------------------------

    async def _kill_tree(self, proc: asyncio.subprocess.Process, job: _WindowsJobObject) -> None:
        """Kill the process **and every descendant**.

        Killing only the parent is the bug this method exists to avoid: an
        orphaned child keeps running, keeps its file handles open, and on
        Windows keeps the workspace directory locked so the next run cannot
        clean it. The task looks terminated while the machine says otherwise.

        Three mechanisms, strongest first:

        1. Job Object ``TerminateJobObject`` — atomic across the whole tree.
        2. ``taskkill /F /T /PID`` — walks the child list; the documented
           Windows way to kill a tree without a job.
        3. POSIX ``killpg`` on the session we created with
           ``start_new_session``.
        """
        if proc.returncode is not None:
            return

        if job.active:
            job.terminate()

        if _IS_WINDOWS:
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=_REAP_TIMEOUT_SECONDS)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                import signal

                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

        # Belt and braces: if the tree kill did not land, kill the parent.
        with contextlib.suppress(ProcessLookupError, OSError):
            if proc.returncode is None:
                proc.kill()

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> int | None:
        """Wait for an already-killed process, bounded."""
        try:
            return await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
        except TimeoutError:  # pragma: no cover - defensive
            log.error("sandbox.subprocess.unreapable", pid=proc.pid)
            return None

    def preflight(self, spec: SandboxSpec) -> dict[str, Any]:
        """What containment this spec would actually get. For ``paa doctor``.

        Exists so an operator can see the gap *before* running work, rather
        than inferring it from a docstring.
        """
        return {
            "backend": self.name,
            "isolation_level": self.isolation_level.name,
            "memory_enforcement": "job_object" if _IS_WINDOWS else "watchdog",
            "network_severed": False,
            "filesystem_confined": False,
            "syscall_filtered": False,
            "timeout_enforced": spec.timeout_seconds is not None,
            "workspace": str(Path(spec.workspace_path).resolve()),
        }
