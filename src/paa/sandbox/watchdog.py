"""Resource sampling and liveness tracking for running sandboxes.

Two independent mechanisms live here because they answer two different
questions about a worker:

:class:`ResourceWatchdog`
    "Is it using more than it was given?" — samples RSS/CPU and kills on
    breach. Enforces the memory ceiling on backends that have no kernel-level
    cap.

:class:`HeartbeatTracker`
    "Is it still alive?" — RFC §1.4. A worker that has stopped emitting is
    declared dead after ``heartbeat_miss_tolerance`` missed intervals. This
    catches the case the watchdog structurally cannot: a process that is
    *healthy* by every resource metric but wedged on a deadlock or a blocking
    read, consuming nothing and finishing never.

SPEC DEVIATION (docs/adr/0009) — the memory bound
--------------------------------------------------
RFC §13 terminates a worker when ``∫MemoryUsage(t)dt > Ceiling``. That integral
has units of MB·seconds, not MB, so it is not a memory bound at all, and the
dimensional error is not academic — it inverts the intended behaviour in both
directions:

* A process that spikes to 8 GB for 200 ms integrates to ~1.6 GB·s and stays
  under a "1024" ceiling. The actual OOM sails straight through.
* A process holding a benign 50 MB for 30 s integrates to 1500 MB·s and is
  killed for exceeding the same ceiling. The false positive is guaranteed by
  the clock, not by the workload.

So the rule fires exactly when it should not and fails to fire exactly when it
should. We enforce **peak RSS** instead, which is what cgroups, Job Objects and
every OOM killer in production actually measure.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import enum
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

__all__ = [
    "HeartbeatTracker",
    "ResourceSample",
    "ResourceWatchdog",
    "SamplerSource",
    "WorkerLiveness",
    "sample_rss_mb",
]

log = structlog.get_logger(__name__)

_BYTES_PER_MB = 1024.0 * 1024.0


class SamplerSource(str, enum.Enum):
    """Which mechanism produced a memory sample, and therefore how much it is
    worth. Recorded on the result so a caller can tell a measured zero from an
    unmeasured one.

    ``(str, enum.Enum)`` rather than ``enum.StrEnum`` to match every other
    enum in this codebase (:mod:`paa.core.types`). Converting one enum in
    isolation would make the serialisation behaviour inconsistent across the
    ledger payloads these values end up in.
    """

    PSUTIL = "psutil"
    """Best: whole process tree, cross-platform, includes children."""

    WINDOWS_NATIVE = "windows_native"
    """``GetProcessMemoryInfo`` via ctypes. Real RSS, but *parent only* — a
    child that forks a memory hog is invisible to this tier."""

    PROC_FS = "proc_fs"
    """``/proc/<pid>/status`` VmRSS. Same parent-only caveat."""

    UNAVAILABLE = "unavailable"
    """No measurement at all. Memory ceilings are NOT enforced. The run still
    proceeds — the wall-clock timeout remains — but nothing may report a
    memory figure, because a fabricated zero is worse than an absent one."""


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One observation of a running process."""

    monotonic_ts: float
    rss_mb: float
    cpu_percent: float
    source: SamplerSource


# ---------------------------------------------------------------------------
# Tiered RSS sampling
#
# psutil is NOT a declared dependency of this project (see pyproject.toml), so
# the fallbacks are not decoration — on the target machine they are the only
# path that ever runs. Each tier is imported lazily so an absent module costs
# nothing at import time.
# ---------------------------------------------------------------------------


def _psutil_rss_mb(pid: int) -> tuple[float, float] | None:
    """RSS and CPU% for the whole process tree via psutil, or ``None``.

    Children are summed deliberately: the ceiling applies to the *tree*, and a
    workload that shells out to a subprocess to do its allocating would
    otherwise slip the cap entirely.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            rss = proc.memory_info().rss
            cpu = proc.cpu_percent(interval=None)
        for child in proc.children(recursive=True):
            with contextlib.suppress(Exception):
                rss += child.memory_info().rss
                cpu += child.cpu_percent(interval=None)
        return rss / _BYTES_PER_MB, cpu
    except Exception:
        # Process exited between the liveness check and the read — a completely
        # normal race at the end of a run, not an error worth surfacing.
        return None


class _ProcessMemoryCounters(ctypes.Structure):
    """``PROCESS_MEMORY_COUNTERS`` from psapi.h."""

    _fields_ = (
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010


def _windows_rss_mb(pid: int) -> float | None:
    """Working-set size via ``GetProcessMemoryInfo``, or ``None``.

    ``PROCESS_QUERY_LIMITED_INFORMATION`` is requested rather than the broader
    ``PROCESS_QUERY_INFORMATION``: it is the least privilege that returns
    memory counters, and asking for more than needed is how a monitoring path
    turns into a privilege-escalation path.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid
        )
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            ok = psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), ctypes.sizeof(counters)
            )
            if not ok:
                return None
            # PeakWorkingSetSize, not WorkingSetSize: sampling at a fixed
            # interval will always miss a spike between two samples, but the
            # kernel's own peak counter does not. This is what makes the
            # polling fallback usable rather than merely present.
            return float(counters.PeakWorkingSetSize) / _BYTES_PER_MB
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _procfs_rss_mb(pid: int) -> float | None:
    """VmHWM (peak RSS) from ``/proc/<pid>/status``, or ``None``.

    VmHWM rather than VmRSS for the same reason Windows uses the peak counter:
    the kernel remembers the high-water mark, the sampler cannot.
    """
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def sample_rss_mb(pid: int) -> tuple[float | None, float, SamplerSource]:
    """Best available ``(rss_mb, cpu_percent, source)`` for ``pid``.

    ``rss_mb`` is ``None`` when nothing could measure it. Callers must treat
    that as "unknown", never as zero.
    """
    if (result := _psutil_rss_mb(pid)) is not None:
        return result[0], result[1], SamplerSource.PSUTIL
    if (rss := _windows_rss_mb(pid)) is not None:
        return rss, 0.0, SamplerSource.WINDOWS_NATIVE
    if (rss := _procfs_rss_mb(pid)) is not None:
        return rss, 0.0, SamplerSource.PROC_FS
    return None, 0.0, SamplerSource.UNAVAILABLE


class ResourceWatchdog:
    """Samples a running process and kills it when it breaches its envelope.

    The kill callback is invoked **once** — a watchdog that re-fires while the
    process is already being torn down produces duplicate ledger events and
    races the reaper.

    Honest limits, in the order they will bite you:

    1. Sampling is periodic, so a process can allocate and free between two
       samples. Mitigated by reading the kernel's *peak* counter where one
       exists (see :func:`_windows_rss_mb`), which is why the fallback tiers
       are more useful than a naive poller.
    2. Without psutil, only the parent process is measured. A child's memory
       is invisible.
    3. Killing is asynchronous. The process may allocate more between the
       breach and the kill landing.

    On Windows the subprocess backend prefers a Job Object memory cap, which
    has none of these gaps because the kernel refuses the allocation itself.
    This class is the fallback for when that cannot be established.
    """

    def __init__(
        self,
        pid: int,
        *,
        memory_mb: int,
        interval_seconds: float = 0.25,
        on_breach: Callable[[str, float], Any] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        self._pid = pid
        self._memory_mb = memory_mb
        self._interval = interval_seconds
        self._on_breach = on_breach

        self.peak_rss_mb: float | None = None
        self.peak_cpu_percent: float = 0.0
        self.samples_taken: int = 0
        self.source: SamplerSource = SamplerSource.UNAVAILABLE
        self.breached: bool = False
        self.breach_reason: str | None = None

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._breach_fired = False
        #: Held so the fire-and-forget kill task is not garbage collected
        #: mid-flight — an unreferenced task can be reaped by the event loop
        #: before it runs, which would silently skip the kill.
        self._pending_kill: asyncio.Task[None] | None = None

    @property
    def sampling_available(self) -> bool:
        """Whether any sample was actually measured.

        ``False`` means the memory ceiling was **not enforced** for this run.
        """
        return self.source is not SamplerSource.UNAVAILABLE and self.peak_rss_mb is not None

    def start(self) -> asyncio.Task[None]:
        """Begin sampling in the background."""
        if self._task is not None:
            raise RuntimeError("watchdog already started")
        self._task = asyncio.create_task(self._loop(), name=f"watchdog-{self._pid}")
        return self._task

    async def stop(self) -> None:
        """Stop sampling and wait for the loop to unwind. Idempotent."""
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            # wait_for on the stop event rather than a bare sleep: shutdown is
            # then immediate instead of taking up to one full interval, which
            # at MAX modality would be a visible stall on every run.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    def _sample_once(self) -> None:
        rss_mb, cpu_percent, source = sample_rss_mb(self._pid)
        if source is not SamplerSource.UNAVAILABLE:
            self.source = source
        if rss_mb is None:
            return

        self.samples_taken += 1
        if self.peak_rss_mb is None or rss_mb > self.peak_rss_mb:
            self.peak_rss_mb = rss_mb
        self.peak_cpu_percent = max(self.peak_cpu_percent, cpu_percent)

        if rss_mb > self._memory_mb and not self._breach_fired:
            self._breach_fired = True
            self.breached = True
            self.breach_reason = "memory"
            log.warning(
                "sandbox.watchdog.memory_breach",
                pid=self._pid,
                rss_mb=round(rss_mb, 2),
                ceiling_mb=self._memory_mb,
                source=source.value,
            )
            if self._on_breach is not None:
                result = self._on_breach("memory", rss_mb)
                if asyncio.iscoroutine(result):
                    # Fire-and-forget: the sampling loop must not block on the
                    # kill, or a slow taskkill would stop us sampling entirely.
                    task = asyncio.create_task(result)
                    self._pending_kill = task

    def snapshot(self) -> dict[str, Any]:
        """Watchdog state for the ledger payload."""
        return {
            "peak_rss_mb": self.peak_rss_mb,
            "peak_cpu_percent": round(self.peak_cpu_percent, 2),
            "samples_taken": self.samples_taken,
            "sampler_source": self.source.value,
            "sampling_available": self.sampling_available,
            "breached": self.breached,
            "breach_reason": self.breach_reason,
        }


# ---------------------------------------------------------------------------
# Heartbeat liveness (RFC §1.4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerLiveness:
    """Per-worker heartbeat bookkeeping."""

    worker_id: str
    registered_at: float
    last_beat_at: float
    beat_count: int = 0
    declared_dead: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class HeartbeatTracker:
    """Declares a worker dead after ``miss_tolerance`` missed heartbeats.

    RFC §1.4. Workers emit every ``interval_seconds``; the host counts silence
    in whole intervals and gives up after the tolerance is exceeded.

    The clock is injectable and defaults to :func:`time.monotonic`. Two reasons,
    both load-bearing: monotonic time cannot go backwards when the system clock
    is adjusted (an NTP step would otherwise resurrect a dead worker or kill a
    live one), and an injected clock lets the tests verify expiry arithmetic
    without sleeping through real intervals.
    """

    def __init__(
        self,
        *,
        interval_seconds: float = 5.0,
        miss_tolerance: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        if miss_tolerance < 1:
            raise ValueError(f"miss_tolerance must be at least 1, got {miss_tolerance}")
        self._interval = interval_seconds
        self._tolerance = miss_tolerance
        self._clock = clock
        self._workers: dict[str, WorkerLiveness] = {}

    @property
    def deadline_seconds(self) -> float:
        """Silence after which a worker is dead."""
        return self._interval * self._tolerance

    def register(self, worker_id: str, **metadata: Any) -> WorkerLiveness:
        """Start tracking a worker. Registration counts as the first beat.

        Otherwise a worker would be born already-missing, and a slow start-up
        would be indistinguishable from a crash.
        """
        now = self._clock()
        liveness = WorkerLiveness(
            worker_id=worker_id, registered_at=now, last_beat_at=now, metadata=dict(metadata)
        )
        self._workers[worker_id] = liveness
        return liveness

    def beat(self, worker_id: str) -> WorkerLiveness:
        """Record a heartbeat. Auto-registers an unknown worker.

        A beat from a worker already declared dead *revives* it: the worker is
        demonstrably alive, and refusing the evidence would strand a task that
        merely paused (a long GC, a swapped-out page) rather than died.
        """
        if (liveness := self._workers.get(worker_id)) is None:
            liveness = self.register(worker_id)
        liveness.last_beat_at = self._clock()
        liveness.beat_count += 1
        if liveness.declared_dead:
            log.info("sandbox.heartbeat.revived", worker_id=worker_id)
            liveness.declared_dead = False
        return liveness

    def silence_seconds(self, worker_id: str) -> float:
        if (liveness := self._workers.get(worker_id)) is None:
            raise KeyError(f"unknown worker {worker_id!r}")
        return self._clock() - liveness.last_beat_at

    def missed_beats(self, worker_id: str) -> int:
        """Whole intervals of silence."""
        return int(self.silence_seconds(worker_id) // self._interval)

    def is_alive(self, worker_id: str) -> bool:
        """Whether the worker has beaten recently enough.

        An unknown worker is *not* alive — a caller asking about an id we never
        saw has lost track of it, which is exactly the condition this exists to
        surface.
        """
        if worker_id not in self._workers:
            return False
        return self.silence_seconds(worker_id) <= self.deadline_seconds

    def sweep(self) -> list[WorkerLiveness]:
        """Declare newly-dead workers and return them.

        Each worker is returned from ``sweep`` only once; repeated sweeps do
        not re-report the same corpse, so the caller can emit one
        ``EXECUTION_FAILED`` per death instead of one per sweep tick.
        """
        newly_dead: list[WorkerLiveness] = []
        for liveness in self._workers.values():
            if liveness.declared_dead:
                continue
            if self._clock() - liveness.last_beat_at > self.deadline_seconds:
                liveness.declared_dead = True
                newly_dead.append(liveness)
                log.warning(
                    "sandbox.heartbeat.worker_dead",
                    worker_id=liveness.worker_id,
                    silence_seconds=round(self._clock() - liveness.last_beat_at, 2),
                    tolerance=self._tolerance,
                )
        return newly_dead

    def dead_workers(self) -> list[str]:
        """Ids currently considered dead. Runs a sweep first."""
        self.sweep()
        return [w.worker_id for w in self._workers.values() if w.declared_dead]

    def forget(self, worker_id: str) -> None:
        """Stop tracking a worker that finished cleanly."""
        self._workers.pop(worker_id, None)

    def __contains__(self, worker_id: object) -> bool:
        return worker_id in self._workers

    def __len__(self) -> int:
        return len(self._workers)
