"""The background daemon — what makes the runtime *ambient* rather than one-shot.

A single :class:`Runtime` is driven by three cooperating loops plus a one-time
boot recovery:

1. **Boot recovery** runs first, before any new work is accepted, so a task that
   was mid-flight when the process died is reconstructed from the ledger and
   re-queued (RFC §7). This is why recovery is a precondition of serving, not a
   background chore.
2. **The dispatch loop** claims queued tasks and runs them through the
   orchestrator. Claiming (not just reading) means a crash mid-task leaves the
   message reclaimable, so no task is silently dropped.
3. **The ingestion loop** drains raw signals into hot serving via the memory
   creator, isolating one poison signal from the batch.
4. **The maintenance loop** runs memory curation on its interval.

Each loop is independent and supervised: one loop raising does not stop the
others, because a stalled maintenance pass must not take ingestion down with it.
Shutdown is cooperative — an ``asyncio.Event`` the loops check between cycles —
so a stop request drains cleanly rather than tearing work off mid-flight.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

from paa.models.base import redact

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from paa.memory.creator import MemoryCreator
    from paa.memory.curator import CurationPhase, CurationReport
    from paa.runtime import Runtime

__all__ = ["Daemon"]

log = structlog.get_logger(__name__)


class Daemon:
    """Runs the runtime's background loops until stopped."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        dispatch_interval: float = 0.5,
        ingestion_interval: float = 1.0,
        maintenance_interval: float = 6 * 3600.0,
        claim_batch: int = 4,
    ) -> None:
        self._rt = runtime
        self._dispatch_interval = dispatch_interval
        self._ingestion_interval = ingestion_interval
        self._maintenance_interval = maintenance_interval
        self._claim_batch = claim_batch
        self._curate_next_phase: CurationPhase | None = None
        # First-tick preview flag (REV R-W2-002): the first curator tick is a
        # dry-run preview; later ticks delete. In-memory only — a daemon
        # restart previews again (documented limit, R-W2-005).
        self._curate_preview_done: bool = False
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Run boot recovery, then launch the loops. Returns once they're running."""
        await self._rt.boot_recovery()
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._supervise("dispatch", self._dispatch_loop)),
            asyncio.create_task(self._supervise("ingestion", self._ingestion_loop)),
            asyncio.create_task(self._supervise("maintenance", self._maintenance_loop)),
        ]
        log.info("daemon.started", loops=len(self._tasks))

    async def stop(self) -> None:
        """Signal shutdown and wait for the loops to drain."""
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        log.info("daemon.stopped")

    async def run_forever(self) -> None:
        """Start and block until stopped (the ``paa serve`` daemon path)."""
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    # -- supervision -------------------------------------------------------

    async def _supervise(self, name: str, loop: Callable[[], Awaitable[None]]) -> None:
        """Run a loop, restarting it on unexpected error so one failing loop
        cannot silently take the daemon down."""
        while not self._stop.is_set():
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("daemon.loop_crashed", loop=name, error=redact(str(exc)))
                await self._sleep(1.0)

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes early on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # -- loops -------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            claimed = await self._claim_and_run()
            # If nothing was waiting, back off; if we drained a batch, loop hot.
            await self._sleep(0 if claimed else self._dispatch_interval)

    async def _claim_and_run(self) -> int:
        """Claim queued tasks and run each through the orchestrator."""
        if self._rt.queue is None:
            # No queue backend: nothing to drain. Sleep out the interval.
            await self._sleep(self._dispatch_interval)
            return 0

        from paa.storage.queue.base import StreamName

        messages = await self._rt.queue.claim(
            StreamName.ORCHESTRATOR_CORE, "daemon", limit=self._claim_batch
        )
        for msg in messages:
            cid = msg.payload.get("correlation_id")
            try:
                if cid:
                    await self._rt.run(cid)
                await self._rt.queue.ack(msg.id)
            except Exception as exc:
                log.error("daemon.task_failed", correlation_id=cid, error=redact(str(exc)))
                await self._rt.queue.nack(msg.id, redact(str(exc)))
        return len(messages)

    async def _ingestion_loop(self) -> None:
        creator = self._memory_creator()
        while not self._stop.is_set():
            processed = 0
            if creator is not None:
                try:
                    report = await creator.run_batch(limit=25)
                    processed = report.claimed
                except Exception as exc:
                    log.error("daemon.ingestion_failed", error=redact(str(exc)))
                # C02: drain the pending-embedding backlog after each batch.
                # drain_pending_embeddings never flips facts to terminal
                # ``failed`` on sink errors, but isolate the call so a
                # substrate hiccup cannot stall ingestion.
                try:
                    await creator.drain_pending_embeddings()
                except Exception as exc:
                    log.error("daemon.embedding_drain_failed", error=redact(str(exc)))
            await self._sleep(0 if processed else self._ingestion_interval)

    async def _maintenance_loop(self) -> None:
        # Wait one interval before the first pass — a fresh boot has nothing to
        # curate, and running heavy maintenance during startup would compete
        # with recovery. The cadence is this loop's own maintenance_interval
        # (6h default); MemorySettings.decay_sweep_interval_hours and
        # curation_window_start_hour are not wired here (see C06 scope).
        await self._sleep(self._maintenance_interval)
        while not self._stop.is_set():
            try:
                await self._run_curation_tick()
            except Exception as exc:
                log.error("daemon.maintenance_failed", error=redact(str(exc)))
            await self._sleep(self._maintenance_interval)

    # -- collaborators -----------------------------------------------------

    def _memory_creator(self) -> MemoryCreator | None:
        """Build the memory creator, or ``None`` when it cannot be built.

        ``None`` is not a daemon failure: the ingestion loop then has nothing
        to drain, and the dispatch and maintenance loops carry on regardless.
        """
        try:
            from paa.memory.creator import MemoryCreator

            return self._rt.memory_creator or MemoryCreator(self._rt.db)
        except Exception as exc:
            log.warning("daemon.memory_creator_unavailable", error=redact(str(exc)))
            return None

    async def _run_curation_tick(self) -> CurationReport | None:
        """Run one curation pass, degrading to a bare decay sweep if needed.

        C04: the daemon maintenance loop schedules ``MemoryCurator`` so decay,
        prune-edges, consolidate, orphans, reconcile-vectors, and analyze
        actually execute in production. The curator's own ``run_maintenance``
        budget/resume contract is preserved: the budget is the existing
        ``MemorySettings.curation_max_runtime_hours`` (2.0h default, no new
        config), and a budget-stopped pass records ``stopped_early_at`` in
        ``self._curate_next_phase`` so the next tick resumes there instead of
        restarting at DECAY and starving later phases.

        Budget wording matters (REV R-W2-005): the budget is checked
        *between* phases, never inside one, so the worst-case wall time is
        the budget plus the longest single phase duration — it is not a hard
        7200s cap. Resume is in-memory only: a daemon restart drops the
        cursor back to DECAY and previews again; a zero or tiny budget will
        keep stopping at the same phase (no progress is made until a tick
        has budget left for that phase). An invalid ``start_phase`` is reset
        to DECAY inside ``run_maintenance`` with a warning, so a stale
        cursor cannot wedge every later tick.

        Wiring is read from the real runtime: settings from
        ``runtime.settings.memory`` and the vector store from
        ``runtime.vector_store`` (``None``-able by the degrade-never-raise
        factory — reconcile then no-ops). Runtime exposes no archive writer,
        so ``archive_writer`` is resolved as ``None`` (eviction deletes without
        the cold-lake summary; restore when a writer is wired). The decay phase
        still runs inside the curator.

        R3 safety: the delete phases run for the first time in production, so
        the first tick is a dry-run preview — counts are logged, nothing is
        deleted — and later ticks delete. Every tick is observable —
        ``daemon.curation_started`` and ``daemon.curation_completed`` carry
        the budget and the full ``CurationReport.summary()`` counts (preview
        counts are would-delete counts). If curator construction fails,
        the tick falls back to a bare ``DecaySweeper.sweep()`` with a warning.
        A ``run_maintenance`` exception is never masked by a fallback sweep
        (that would double-run decay); it propagates to the loop, which logs
        ``daemon.maintenance_failed`` and keeps the daemon alive. This tick
        runs in the maintenance task only — never inline in ingestion.
        """
        settings = self._rt.settings.memory
        budget = settings.curation_max_runtime_hours * 3600.0
        dry_run = not self._curate_preview_done
        try:
            from paa.memory.curator import MemoryCurator

            curator = MemoryCurator(
                self._rt.db,
                settings=settings,
                vector_store=self._rt.vector_store,
                archive_writer=getattr(self._rt, "archive_writer", None),
            )
        except Exception as exc:
            log.warning("daemon.curator_unavailable", error=redact(str(exc)))
            from paa.memory.decay import DecaySweeper

            await DecaySweeper(self._rt.db).sweep(dry_run=dry_run)
            return None
        phase = self._curate_next_phase
        log.info(
            "daemon.curation_started",
            budget_seconds=budget,
            start_phase=phase.value if phase is not None else None,
            dry_run=dry_run,
        )
        report = await curator.run_maintenance(
            budget_seconds=budget, start_phase=phase, dry_run=dry_run
        )
        self._curate_next_phase = report.stopped_early_at
        if report.completed_fully and report.errors == 0:
            self._curate_preview_done = True
        log.info("daemon.curation_completed", dry_run=dry_run, **report.summary())
        return report
