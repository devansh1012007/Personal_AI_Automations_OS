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
Duplex sessions              Yes — :meth:`SubprocessSandbox.open` holds the
                             same facts open with pipes, a deadline and a
                             whole-tree kill for the session's lifetime
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
import signal
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import structlog

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.sandbox._win32 import IS_WINDOWS, last_error, win_dll
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
    assert_inside_workspace,
    build_child_env,
    resolve_workspace,
)
from paa.sandbox.watchdog import ResourceWatchdog

__all__ = ["SubprocessSandbox", "SubprocessSandboxSession"]

log = structlog.get_logger(__name__)

#: How long to wait for a killed process to actually die before giving up and
#: reporting the result anyway. A process wedged in an uninterruptible kernel
#: wait can outlive SIGKILL; blocking the runtime forever on it would turn one
#: stuck workload into a stuck host.
_REAP_TIMEOUT_SECONDS = 10.0

#: How long to wait for the output readers to drain after the process exits.
_DRAIN_TIMEOUT_SECONDS = 5.0

#: How long ``close()`` lets a SIGTERMed tree finish flushing before the
#: SIGKILL lands. A duplex child — an MCP server mid-conversation is the
#: concrete case — may have bytes in flight when the caller closes; the grace
#: is what turns "terminate" into "terminate cleanly when possible". Bounded
#: well under the 5-second cancellation contract this package's callers test
#: against, skipped where a grace cannot be honoured (Windows has no
#: process-group SIGTERM) and skipped where the budget is already spent (the
#: timeout path — old ``run()`` semantics — and orphan reaping).
_CLOSE_GRACE_SECONDS = 1.0

#: The size of one ``read()`` — the same 64 KiB the one-shot capture path
#: reads, so a session and a ``run()`` drain a chatty child at the same
#: cadence and neither invents its own coalescing behaviour.
_READ_CHUNK_BYTES = 65536

#: How often ``_watch_exit`` polls the transport's returncode. The poll —
#: not ``await Process.wait()`` — is what observes a mid-conversation death
#: (see that method for the pipe-disconnect circularity it breaks); 50 ms
#: is far inside every caller's deadline while costing nothing measurable
#: on an otherwise idle loop.
_EXIT_POLL_SECONDS = 0.05


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
        if not IS_WINDOWS:
            return False
        try:
            kernel32 = win_dll("kernel32")
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                log.debug("sandbox.job_object.create_failed", error=last_error())
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
                log.debug("sandbox.job_object.set_info_failed", error=last_error())
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
                        error=last_error(),
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
# The duplex session
# ---------------------------------------------------------------------------


class SubprocessSandboxSession(SandboxSession):
    """A duplex child of the host backend: one process group, pipes, a deadline.

    Containment facts — each one is the one-shot ``run()`` path's fact,
    held open rather than re-explained (see the module docstring and
    :meth:`SubprocessSandbox.open` for the gaps this backend does NOT close):

    * the env is the :func:`~paa.sandbox.base.build_child_env` allowlist;
    * the cwd is the resolved workspace jail;
    * memory is the Job Object where the platform has one and the sampled
      watchdog where it does not — the same two mechanisms the one-shot path
      uses. There is deliberately no third, quieter ``setrlimit`` deal here:
      it would have to go through ``preexec_fn``, which is unsafe in a
      threaded parent (the comment at the spawn site says why), and it would
      make the session's containment a *different story* from ``run()``'s,
      which is exactly what this task forbids drifting;
    * the root is a **session leader** (``start_new_session``), so the whole
      tree is killable as one process group — grandchildren included, which
      an MCP server spawning helpers makes the concrete case.

    Four background tasks hold those facts while the pipes are open:

    ``_watchdog``
        The memory ceiling's sampler — the *same* :class:`ResourceWatchdog`
        the one-shot path starts, running for the session's lifetime, not
        just the length of a call. It is the enforcement on hosts with no
        Job Object (POSIX: it kills the whole tree on a breach) and the
        observer where one is in force, and its peak reading is what the
        final ``SandboxResult`` reports as ``peak_rss_mb``. Started in
        :meth:`_start` right after the pipes open — the audit that found this
        line missing also found the result reporting ``memory_enforcement=``
        ``"watchdog"`` with the sampler never running, so the start is a
        fact the whole class leans on, not a detail.
    ``_watch_task``
        The single canonical waiter on the root's exit. Everything else
        awaits ``_exit_future`` so the process is reaped exactly once no
        matter how many coroutines care. When the root dies *outside* one
        of our own kills, this task reaps the group it led — the session
        owns that group, and an orphaned grandchild is precisely the leak
        the tree-kill exists to prevent (Gate G-H1: ``kill -9`` on an MCP
        child mid-conversation leaves no process behind).
    ``_stderr_task``
        A bounded capture of stderr, exactly the one-shot path's reader. The
        session contract has no ``read_stderr`` — but leaving stderr
        undrained would let a chatty child wedge itself on a full pipe, the
        deadlock the capture cap's design note exists to avoid.
    ``_deadline_task``
        The wall clock: ``spec.timeout_seconds`` is a **lifetime** budget
        (from open, not from the last read — silence is not idleness), and
        overrun kills the whole tree with ``killed_reason="timeout"``.
        ``None`` arms nothing: wait forever, as ``run()`` would.
    """

    def __init__(
        self,
        sandbox: SubprocessSandbox,
        spec: SandboxSpec,
        proc: asyncio.subprocess.Process,
        *,
        workspace: Path,
        job: _WindowsJobObject,
        job_active: bool,
    ) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._proc = proc
        self._workspace = workspace
        self._job = job
        self._job_active = job_active
        self._memory_enforcement = "job_object" if job_active else "watchdog"
        #: ``proc.pid`` *is* the process-group id: the spawn used
        #: ``start_new_session``, making the root the leader of a fresh
        #: session and group. Held in its own attribute because
        #: ``os.getpgid`` stops answering once the leader is reaped, and the
        #: group must stay killable after that — orphan reaping.
        self._pgid = proc.pid
        self._started = time.perf_counter()
        self._watchdog: ResourceWatchdog | None = None
        self._stderr_task: asyncio.Task[tuple[bytes, bool]] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._deadline_task: asyncio.Task[None] | None = None
        #: Children spawned by :meth:`run_within`, each a process-group
        #: leader of its own (see that method for the honest reason the
        #: group cannot be shared). Completed children stay registered:
        #: killing a dead group is a suppressed no-op, and a removal
        #: protocol racing ``close()`` would buy nothing but a window.
        self._children: list[asyncio.subprocess.Process] = []
        self._exit_future: asyncio.Future[int | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._terminated = False
        self._timed_out = False
        self._killed_reason: str | None = None
        self._final_result: SandboxResult | None = None
        self._stdout_transcript = bytearray()
        self._stdout_truncated = False

    # -- lifecycle ---------------------------------------------------------

    def _attach_watchdog(self, watchdog: ResourceWatchdog) -> None:
        """Take the watchdog ``open()`` built (its breach callback closes
        over this session, so it could only be wired after construction)."""
        self._watchdog = watchdog

    def _start(self) -> None:
        """Start the background machinery. Called exactly once, by ``open()``."""
        # The watchdog first: every interval it does not run is a window in
        # which the memory ceiling is claimed (``memory_enforcement``) but
        # not sampled, and the peak the result reports cannot be measured.
        if self._watchdog is not None:
            self._watchdog.start()
        cap = self._sandbox._settings.max_capture_bytes
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._sandbox._read_capped(self._proc.stderr, cap)
            )
        self._watch_task = asyncio.create_task(self._watch_exit())
        if self._spec.timeout_seconds is not None:
            self._deadline_task = asyncio.create_task(
                self._deadline(self._spec.timeout_seconds)
            )

    @property
    def pid(self) -> int | None:
        """The root child's PID — on POSIX also its process-group id."""
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    # -- the duplex contract -------------------------------------------------

    async def read(self, *, timeout: float | None = None) -> bytes:
        """One chunk of stdout, as soon as available. See the ABC docstring.

        The chunk is also appended to the capped transcript that the final
        ``SandboxResult`` reports as ``stdout`` — for a duplex caller that
        transcript is *what the caller consumed*, which is the honest thing
        for the ledger to record, and it is capped exactly like the one-shot
        capture so a long conversation cannot grow it without bound.
        """
        if self._closed:
            raise SandboxError("session is closed; its pipes are gone", pid=self._proc.pid)
        stream = self._proc.stdout
        if stream is None:  # pragma: no cover - open() always pipes stdout
            raise SandboxError("session has no stdout pipe", pid=self._proc.pid)
        if timeout is None:
            chunk = await stream.read(_READ_CHUNK_BYTES)
        else:
            chunk = await asyncio.wait_for(stream.read(_READ_CHUNK_BYTES), timeout=timeout)
        if not chunk:
            return b""
        self._record_stdout(chunk)
        return chunk

    async def write(self, data: bytes) -> None:
        """Write ``data`` to the child's stdin. See the ABC docstring."""
        if self._closed:
            raise SandboxError("session is closed; its pipes are gone", pid=self._proc.pid)
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing() or self._proc.returncode is not None:
            raise SandboxError(
                "session stdin is gone: the child has exited or closed its stdin",
                pid=self._proc.pid,
            )
        try:
            stdin.write(data)
            await stdin.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            raise SandboxError(
                "failed to write to the session's stdin",
                pid=self._proc.pid,
                reason=str(exc),
            ) from exc

    async def close(self) -> SandboxResult:
        """Terminate the session's whole tree, idempotently. See the ABC docstring.

        The lock, not the ``_final_result`` check alone, is what makes
        concurrent closes honest: two callers closing at once would
        otherwise both pass the "already built?" check and both run the
        teardown — the SIGTERM grace racing itself for no gain.
        """
        async with self._close_lock:
            if self._final_result is not None:
                return self._final_result
            self._closing = True
            if self._proc.returncode is None and not self._terminated:
                # The caller is ending a live session: that is a kill, and
                # the result must say so. A root that already exited is
                # reported by its own exit code instead — "closed" would
                # claim a kill that never happened.
                if self._killed_reason is None:
                    self._killed_reason = "closed"
                log.info("sandbox.subprocess.session_closed", pid=self._proc.pid)
            await self._cancel_deadline()
            await self._terminate(grace=_CLOSE_GRACE_SECONDS)
            return await self._collect_and_build()

    # -- background tasks ----------------------------------------------------

    async def _watch_exit(self) -> None:
        """The single canonical observer of the root's death.

        Everything else awaits ``_exit_future``, so the exit code is
        collected exactly once no matter how many coroutines care —
        ``Process.wait()`` allows concurrent waiters, but one observer
        plus one future keeps the ordering of "record exit, then act on
        it" in one place.

        The death is observed by **polling the transport's returncode**
        rather than by awaiting ``Process.wait()`` directly, and the reason
        is a load-bearing asyncio fact: a ``wait()`` started *before* the
        exit only completes when every pipe to the child is disconnected
        (``BaseSubprocessTransport._try_finish`` — the transport refuses to
        report an exit while a write to stdin might still be in flight). A
        session's stdin is deliberately still open — a duplex caller is
        mid-conversation — so a plain ``await wait()`` would hang forever
        on a child that died mid-conversation, returncode already set,
        tree never reaped. The child watcher sets the transport's
        returncode the moment the process dies, pipes or no pipes, so
        polling it breaks the circularity: on observation, stdin is sealed
        (a dead child cannot read it, and the open write pipe is the one
        thing holding asyncio's exit bookkeeping open), the exit code is
        collected, and — when the death was not one of our own kills — the
        process group it led is reaped with it. The session owns that
        group for its whole lifetime; a grandchild that outlives a *dead*
        root is an orphan wearing the session's pgid, which is exactly
        the leak the acceptance tests exist to catch (Gate G-H1: ``kill
        -9`` on an MCP child mid-conversation leaves no process behind).
        """
        # ASYNC110 would rather an Event; there is none to await. The
        # returncode is a plain attribute the child-watcher thread sets, and
        # the apparent event source — ``await proc.wait()`` — is the very
        # thing that hangs while the session's stdin pipe is open. The poll
        # is the mechanism, not a style accident.
        while self._proc.returncode is None:  # noqa: ASYNC110
            await asyncio.sleep(_EXIT_POLL_SECONDS)
        self._seal_stdin()
        exit_code = await self._proc.wait()
        if not self._exit_future.done():
            self._exit_future.set_result(exit_code)
        if self._terminated:
            # Our own kill already did (or is doing) the reap. This observer
            # only ever reports the exit it observed.
            return
        log.warning(
            "sandbox.subprocess.session_root_died",
            pid=self._proc.pid,
            exit_code=exit_code,
            detail="reaping the process group the dead root leaves behind",
        )
        await self._terminate(grace=0.0)

    async def _deadline(self, budget: float) -> None:
        """The session's wall clock: kill the tree when the budget is spent.

        This is the *lifetime* budget described on the class — from open,
        not from the last read, because silence from a duplex child is not
        idleness. It is also the backstop for a wedged exchange: a caller
        whose ``write`` blocks on a child that stopped reading is unwedged
        by this kill rather than by silently dropped bytes.
        """
        await asyncio.sleep(budget)
        if self._closing or self._terminated or self._final_result is not None:
            return
        self._note_timeout(budget)
        await self._terminate(grace=0.0)
        # Sampling a dead pid is pure noise from here on; the kill has done
        # the work and the result-building close() reads the peak it found.
        if self._watchdog is not None:
            await self._watchdog.stop()

    def _note_timeout(self, budget: float) -> None:
        """Record (once) that the wall-clock budget was exceeded.

        The timeout wins over every other label by design: the budget
        really was exceeded, and reporting it as a clean close would be the
        same lie as a timeout that reports itself as a success. Both the
        session's deadline task and the one-shot wrapper's wait call this;
        whichever notices first records the fact.
        """
        if self._timed_out:
            return
        self._timed_out = True
        self._killed_reason = "timeout"
        log.warning(
            "sandbox.subprocess.timeout",
            pid=self._proc.pid,
            timeout_seconds=budget,
            command=list(self._spec.command)[:3],
        )

    def _note_memory(self) -> Coroutine[Any, Any, None] | None:
        """Kill the tree on a watchdog breach (the POSIX enforcement path).

        Returns the kill coroutine rather than awaiting it because the
        watchdog calls this from its sampling loop and schedules the kill
        fire-and-forget — a slow kill must never stop the sampling.
        """
        if self._terminated:
            return None
        return self._terminate(grace=0.0)

    def _seal_stdin(self) -> None:
        """Close the child's stdin pipe — the one-shot's ``DEVNULL`` equivalent.

        The one-shot ``run()`` feeds no input, so its child historically got
        ``DEVNULL`` and a stdin *reader* saw EOF immediately. A session
        always opens a pipe (a duplex caller needs one), so the wrapper seals
        it right after spawn to hand a stdin-reading child the same EOF —
        otherwise a child that blocks on ``read(stdin)`` would hang until
        the wall-clock killed it, which is not the outcome its spec asked
        for.
        """
        stdin = self._proc.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()

    async def _drain_stdout(self) -> None:
        """Read the session's stdout to EOF, feeding the capped transcript.

        This is the one-shot wrapper's reader: it keeps the pipe moving so
        a chatty child never blocks on a full pipe — the deadlock the
        capture-cap design note warns about. The duplex caller does not run
        this task: their ``read()`` calls *are* the drain, which is also
        their flow control.
        """
        while await self.read():
            pass

    # -- teardown ------------------------------------------------------------

    async def _terminate(self, grace: float) -> None:
        """Kill the session's whole tree — root, grandchildren, and every
        ``run_within`` child — with a SIGTERM grace when one is wanted.

        Stdin is sealed **first**: nothing more can be written to a tree
        being torn down, and the still-open write pipe is the one thing
        that keeps asyncio's exit bookkeeping open (see ``_watch_exit`` for
        the load-bearing fact; from the teardown side the same seal also
        hands a stdin-blocked child the EOF it needs to notice it should
        leave during the grace window).

        ``grace == 0`` skips the SIGTERM entirely: either the budget is
        already spent (timeout, the one-shot wrapper's historical kill
        shape) or the root is dead and the survivors are orphans being
        reaped. Windows cannot honour a process-group SIGTERM at all, so it
        takes the immediate path everywhere.
        """
        if self._terminated:
            return
        self._terminated = True
        self._seal_stdin()
        if grace > 0.0 and not IS_WINDOWS:
            # The remembered pgid for the root (``os.getpgid`` stops answering
            # once the leader is reaped, and the group must stay killable
            # after that); each registered child leads a group of its own.
            targets = [self._pgid, *(child.pid for child in self._children)]
            for pgid in targets:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(pgid, signal.SIGTERM)
            # Give the tree until the grace (or the root's own exit, which
            # is usually much sooner) to flush and exit on the SIGTERM.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._exit_future), timeout=grace
                )
        if IS_WINDOWS:
            if self._job_active:
                self._job.terminate()
            for proc in [self._proc, *self._children]:
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
            targets = [self._pgid, *(child.pid for child in self._children)]
            for pgid in targets:
                # ``pgid`` is each tree's leader pid: every process this
                # session spawned was made a session leader at spawn.
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(pgid, signal.SIGKILL)
            # Belt and braces, exactly like the one-shot path: if the group
            # kill did not land, kill the parent directly.
            with contextlib.suppress(ProcessLookupError, OSError):
                if self._proc.returncode is None:
                    self._proc.kill()
        await self._reap_all()

    async def _reap_all(self) -> None:
        """Reap the root and every registered child, bounded.

        The bound is the one-shot path's reap bound: a process wedged in an
        uninterruptible kernel wait can outlive SIGKILL, and blocking the
        session's teardown forever on it would turn one stuck workload into
        a stuck host.
        """
        for proc in [self._proc, *self._children]:
            await self._sandbox._reap(proc)

    async def _cancel_deadline(self) -> None:
        """Cancel the wall-clock deadline, if one is still armed.

        From here on ``close()`` owns how the tree ends; a deadline left
        running would race it with the same kill and could mislabel a clean
        caller close as a timeout. A deadline that already fired has
        already torn the tree down and stands itself down.
        """
        task = self._deadline_task
        self._deadline_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _collect_and_build(self) -> SandboxResult:
        """Build the final result — the only writer of ``_final_result``."""
        exit_code: int | None
        try:
            exit_code = await asyncio.wait_for(
                asyncio.shield(self._exit_future), timeout=_REAP_TIMEOUT_SECONDS
            )
        except TimeoutError:  # pragma: no cover - defensive
            log.error("sandbox.subprocess.unreapable", pid=self._proc.pid)
            exit_code = None

        stderr_bytes, stderr_truncated = b"", False
        if self._stderr_task is not None:
            stderr_bytes, stderr_truncated = await self._sandbox._collect(self._stderr_task)

        # The canonical waiter may still be unwinding its own post-exit
        # group reap; bounding it keeps close() from hanging on a teardown
        # that lost its way, and keeps no task dangling after close().
        if self._watch_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._watch_task), timeout=_REAP_TIMEOUT_SECONDS
                )
            except TimeoutError:  # pragma: no cover - defensive
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._watch_task

        if self._watchdog is not None:
            await self._watchdog.stop()
        if self._job_active:
            # Closing with KILL_ON_JOB_CLOSE guarantees no survivor holds the
            # workspace open, which on Windows would make the directory
            # undeletable and strand the next run.
            self._job.terminate()
        self._job.close()

        if self._watchdog is not None and self._watchdog.breached and (
            self._killed_reason is None
        ):
            self._killed_reason = "memory"

        self._closed = True
        self._final_result = SandboxResult(
            exit_code=exit_code,
            stdout=bytes(self._stdout_transcript).decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=(time.perf_counter() - self._started) * 1000.0,
            peak_rss_mb=self._watchdog.peak_rss_mb if self._watchdog is not None else None,
            timed_out=self._timed_out,
            killed_reason=self._killed_reason,
            truncated_output=self._stdout_truncated or stderr_truncated,
            backend=self._sandbox.name,
            isolation_level=self._sandbox.isolation_level,
            memory_enforcement=self._memory_enforcement,
        )
        return self._final_result

    def _record_stdout(self, chunk: bytes) -> None:
        """Append one read chunk to the capped result transcript.

        The transcript is what the final ``SandboxResult`` reports as
        ``stdout`` — for the one-shot wrapper it is the whole capture, for a
        duplex session it is what the caller consumed through ``read()``.
        Capped at ``max_capture_bytes`` exactly like the one-shot capture:
        the pipe keeps moving either way (the caller's reads are the
        drain), but the sandbox never holds more than the cap.
        """
        cap = self._sandbox._settings.max_capture_bytes
        if len(self._stdout_transcript) < cap:
            room = cap - len(self._stdout_transcript)
            self._stdout_transcript += chunk[:room]
            if len(chunk) > room:
                self._stdout_truncated = True
        else:
            self._stdout_truncated = True

    def _note_drain_overrun(self) -> None:
        """The one-shot wrapper's bounded drain gave up on the pipe.

        A grandchild that inherited the write end holds it open past the
        root's death; after the bounded wait, the wrapper reports the loss
        the same way the capture cap does — the flag, not silent bytes.
        """
        self._stdout_truncated = True

    # -- one-shots inside the envelope ---------------------------------------

    async def run_within(self, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec`` as a one-shot inside this session's envelope.

        The envelope is: this session's workspace jail (the spec's workspace
        must resolve inside it or the child would escape containment the
        one-shot path never allowed), the same env-allowlist construction,
        and this session's kill list. The one deliberate asymmetry, stated
        here rather than hidden: the child becomes the leader of its **own**
        process group, because POSIX refuses ``setpgid`` across sessions and
        this session's root is (by design) a session leader. ``close()`` and
        the session's deadline kill every registered group, so the envelope
        contains exactly the same trees — it is *not* one OS process group,
        and this paragraph is where that is said.

        The one-shot keeps the one-shot contract: its own spec-derived
        resource envelope (its own watchdog, its own Job Object on Windows),
        its own wall clock, its own ``SandboxResult``. A session kill that
        lands mid-run surfaces in that result as the session's
        ``killed_reason`` — the envelope died, and the result says which
        death it was.
        """
        if self._closed:
            raise SandboxError(
                "the session is closed; its envelope is gone",
                pid=self._proc.pid,
            )
        # The envelope's jail is the session's workspace; the spec's own
        # workspace must resolve to somewhere inside it.
        workspace = assert_inside_workspace(spec.workspace_path, self._workspace)

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
        if not IS_WINDOWS:
            # Own session => own process group, for exactly the tree-kill
            # reasons above — see the method docstring for why it cannot be
            # the session root's group.
            popen_kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=str(workspace),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            job.close()
            raise SandboxError(
                "failed to start a one-shot inside the session envelope",
                command=list(spec.command),
                workspace_path=str(workspace),
                reason=str(exc),
            ) from exc
        self._children.append(proc)
        if job_active:
            job_active = job.assign(proc.pid)

        memory_enforcement = "job_object" if job_active else "watchdog"
        killed_reason: str | None = None
        timed_out = False

        watchdog = ResourceWatchdog(
            proc.pid,
            memory_mb=spec.memory_mb,
            interval_seconds=self._sandbox._settings.watchdog_interval_seconds,
            on_breach=None
            if job_active
            else (lambda _reason, _rss: self._sandbox._kill_tree(proc, job)),
        )
        watchdog.start()

        cap = self._sandbox._settings.max_capture_bytes
        stdout_reader = asyncio.create_task(self._sandbox._read_capped(proc.stdout, cap))
        stderr_reader = asyncio.create_task(self._sandbox._read_capped(proc.stderr, cap))

        exit_code: int | None
        try:
            if spec.timeout_seconds is None:
                exit_code = await proc.wait()
            else:
                try:
                    exit_code = await asyncio.wait_for(
                        proc.wait(), timeout=spec.timeout_seconds
                    )
                except TimeoutError:
                    timed_out = True
                    killed_reason = "timeout"
                    log.warning(
                        "sandbox.subprocess.timeout",
                        pid=proc.pid,
                        timeout_seconds=spec.timeout_seconds,
                        command=list(spec.command)[:3],
                    )
                    await self._sandbox._kill_tree(proc, job)
                    exit_code = await self._sandbox._reap(proc)
        except asyncio.CancelledError:
            # Same decision as the one-shot path: a cancelled run reaps its
            # tree, tears down its own machinery, and re-raises — returning a
            # result here would read as normal completion and hide the
            # cancel from the journal.
            log.warning(
                "sandbox.subprocess.cancelled",
                pid=proc.pid,
                command=list(spec.command)[:3],
            )
            await self._sandbox._kill_tree(proc, job)
            await self._sandbox._reap(proc)
            stdout_reader.cancel()
            stderr_reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stdout_reader
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_reader
            job.terminate()
            job.close()
            raise
        finally:
            await watchdog.stop()

        if watchdog.breached and killed_reason is None:
            killed_reason = "memory"
        # A kill that came from the session's own teardown — close, or its
        # deadline — is the envelope's death, not this child's budget, and
        # the result reports it as such (a signal exit code with no reason
        # of its own would otherwise look like an unexplained crash).
        if (
            killed_reason is None
            and exit_code is not None
            and exit_code < 0
            and self._killed_reason is not None
        ):
            killed_reason = self._killed_reason

        stdout_bytes, stdout_truncated = await self._sandbox._collect(stdout_reader)
        stderr_bytes, stderr_truncated = await self._sandbox._collect(stderr_reader)

        if job_active:
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
            backend=self._sandbox.name,
            isolation_level=self._sandbox.isolation_level,
            memory_enforcement=memory_enforcement,
        )


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

    async def open(self, spec: SandboxSpec) -> SubprocessSandboxSession:
        """Open a long-lived duplex child under ``spec``'s containment.

        The same facts as :meth:`run`, held open (see
        :class:`SubprocessSandboxSession` for the machinery that holds
        them): the env is the explicit allowlist, the cwd is the resolved
        workspace jail, memory is the Job Object where the platform has one
        and the sampled watchdog where it does not, the child is a fresh
        session leader so the whole tree dies as one process group, and
        ``timeout_seconds`` arms a **lifetime** deadline that kills the
        tree with ``killed_reason="timeout"``. ``None`` arms nothing: the
        session waits forever, exactly as ``run()`` would.

        NETWORK DENIAL IS NOT ENFORCED HERE — the honest paragraph in
        :meth:`run`'s docstring applies verbatim: a session child shares
        the host network stack, and the two layers that really enforce
        denial (policy, AST pre-scan) are the same two.
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

        job = _WindowsJobObject(spec.memory_mb)
        job_active = job.create()

        popen_kwargs: dict[str, Any] = {}
        if not IS_WINDOWS:
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
                # The one difference from run(): stdin is a pipe the caller
                # writes to over the session's lifetime, not DEVNULL. The
                # one-shot wrapper below seals it immediately, which gives a
                # stdin-reading child the same EOF DEVNULL would have.
                stdin=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            job.close()
            raise SandboxError(
                "failed to start sandboxed session process",
                command=list(spec.command),
                workspace_path=str(workspace),
                reason=str(exc),
            ) from exc

        if job_active:
            job_active = job.assign(proc.pid)

        session = SubprocessSandboxSession(
            self, spec, proc, workspace=workspace, job=job, job_active=job_active
        )
        watchdog = ResourceWatchdog(
            proc.pid,
            memory_mb=spec.memory_mb,
            interval_seconds=self._settings.watchdog_interval_seconds,
            # With a Job Object in force the kernel already refuses the
            # allocation, so the watchdog only observes; without one it is the
            # sole enforcement path and must kill — the session's whole tree.
            on_breach=None
            if job_active
            else (lambda _reason, _rss: session._note_memory()),
        )
        session._attach_watchdog(watchdog)
        session._start()
        return session

    async def run_within(self, session: SandboxSession, spec: SandboxSpec) -> SandboxResult:
        """Run ``spec`` as a one-shot inside an open session's envelope.

        The session must be one this backend opened: the envelope being
        claimed — workspace jail, env allowlist rules, kill list — is this
        backend's, and accepting a foreign session here would be claiming
        containment facts we never established.
        """
        if not isinstance(session, SubprocessSandboxSession):
            raise SandboxError(
                "run_within requires a session opened by the subprocess backend",
                backend=self.name,
                session_type=type(session).__name__,
            )
        return await session.run_within(spec)

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` as a capped child process.

        A thin wrapper over :meth:`open` since the session refactor: open
        the child under the same containment, seal its stdin (a one-shot
        feeds no input — the seal is the EOF ``DEVNULL`` used to hand it),
        drain stdout to completion, close. The ``SandboxResult`` shape and
        every observable behaviour are the pre-refactor contract's — 4000+
        tests depend on that, and ``tests/sandbox`` is the witness.

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
        session = await self.open(spec)
        session._seal_stdin()
        drain = asyncio.create_task(session._drain_stdout())
        try:
            await self._await_exit_under_budget(session, spec)
            try:
                await asyncio.wait_for(
                    asyncio.shield(drain), timeout=_DRAIN_TIMEOUT_SECONDS
                )
            except TimeoutError:
                # A grandchild that inherited the write end can hold stdout
                # open past the root's death. Bound the wait, then report
                # the loss the same way the capture cap does — the flag,
                # not silent bytes. (The transcript already holds every
                # byte that did arrive, which is more than the one-shot
                # path's reader could salvage from the same situation.)
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain
                session._note_drain_overrun()
            return await session.close()
        except asyncio.CancelledError:
            # DECISION: an externally-cancelled run reaps its tree exactly like
            # a timed-out one, then re-raises — the cancel is the caller's
            # signal, and returning a ``SandboxResult`` here would read as
            # normal completion and hide the cancel (the loop journals FAILED
            # only because this re-raises). The drain reader and the session's
            # own machinery (watchdog, deadline, capture) are torn down by
            # ``close()``, which is the whole reason run() routes through a
            # session now instead of its own ad-hoc cleanup.
            log.warning(
                "sandbox.subprocess.cancelled",
                pid=session.pid,
                command=list(spec.command)[:3],
            )
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
            await session.close()
            raise

    async def _await_exit_under_budget(
        self, session: SubprocessSandboxSession, spec: SandboxSpec
    ) -> None:
        """Await the session root's exit under the spec's wall-clock budget.

        This wait is the one-shot run's *work budget*, handed to
        ``asyncio.wait_for`` exactly as before the session refactor — the
        recorded budget is part of this backend's contract
        (``tests/sandbox/test_subprocess_backend.py`` pins the number, and
        the container-start grace must appear in neither direction). The
        session's own deadline task races this wait with the same number;
        whichever notices first kills the tree, and both record the same
        fact, so the result does not depend on which of the two won.
        """
        if spec.timeout_seconds is None:
            # ``None`` is "wait forever", not "wait zero" — the same semantics
            # as the one-shot path, held by the session's un-armed deadline.
            await asyncio.shield(session._exit_future)
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(session._exit_future), timeout=spec.timeout_seconds
            )
        except TimeoutError:
            # The budget is spent: kill now with no grace — the historical
            # one-shot kill shape — and record the timeout. The session's
            # deadline may have fired a moment earlier and done it already;
            # every step here is idempotent, so either winner is correct.
            session._note_timeout(spec.timeout_seconds)
            await session._terminate(grace=0.0)

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

        if IS_WINDOWS:
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

    def describe(self) -> dict[str, Any]:
        """Backend identity for the ledger and the ``paa doctor`` command.

        It also says, in as many words, that this backend **ignores**
        ``container_start_grace_seconds``. The setting is the *container*
        backend's start budget: a ``fork``/``exec`` is milliseconds, there is
        no container to create and no daemon to ask, and the work budget is
        already the whole of what this backend spends. A setting that only
        one backend honours must not be silently applied to the others
        (ADR-0031) — and it must not be added to the work budget either,
        which is what "charged over and above the timeout" used to mean.
        """
        described = super().describe()
        described.update(
            {
                "container_start_grace_seconds": 0,
                "container_start_budget": "not applicable — this backend has no start phase",
            }
        )
        return described

    def preflight(self, spec: SandboxSpec) -> dict[str, Any]:
        """What containment this spec would actually get. For ``paa doctor``.

        Exists so an operator can see the gap *before* running work, rather
        than inferring it from a docstring.

        ``container_start_grace_seconds`` is reported as **zero**, and the
        reason is given next to it: this backend has no start phase, so it
        neither spends the setting nor folds it into the work budget
        (ADR-0031). The work budget is ``spec.timeout_seconds`` and nothing
        else.
        """
        return {
            "backend": self.name,
            "isolation_level": self.isolation_level.name,
            "memory_enforcement": "job_object" if IS_WINDOWS else "watchdog",
            "network_severed": False,
            "filesystem_confined": False,
            "syscall_filtered": False,
            "timeout_enforced": spec.timeout_seconds is not None,
            "container_start_grace_seconds": 0,
            "container_start_budget": "not applicable — this backend has no start phase",
            "workspace": str(Path(spec.workspace_path).resolve()),
        }
