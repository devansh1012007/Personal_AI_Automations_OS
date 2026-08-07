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
4. **The maintenance loop** runs the decay sweep on its interval.

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

if TYPE_CHECKING:
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

    async def _supervise(self, name: str, loop) -> None:
        """Run a loop, restarting it on unexpected error so one failing loop
        cannot silently take the daemon down."""
        while not self._stop.is_set():
            try:
                await loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daemon.loop_crashed", loop=name)
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
                log.error("daemon.task_failed", correlation_id=cid, error=str(exc))
                await self._rt.queue.nack(msg.id, str(exc))
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
                    log.error("daemon.ingestion_failed", error=str(exc))
            await self._sleep(0 if processed else self._ingestion_interval)

    async def _maintenance_loop(self) -> None:
        # Wait one interval before the first pass — a fresh boot has nothing to
        # curate, and running heavy maintenance during startup would compete
        # with recovery.
        await self._sleep(self._maintenance_interval)
        while not self._stop.is_set():
            try:
                from paa.memory.decay import DecaySweeper

                await DecaySweeper(self._rt.db).sweep()
            except Exception as exc:
                log.error("daemon.maintenance_failed", error=str(exc))
            await self._sleep(self._maintenance_interval)

    # -- collaborators -----------------------------------------------------

    def _memory_creator(self):
        try:
            from paa.memory.creator import MemoryCreator

            return MemoryCreator(self._rt.db)
        except Exception as exc:
            log.warning("daemon.memory_creator_unavailable", error=str(exc))
            return None
