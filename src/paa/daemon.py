"""The background daemon — what makes the runtime *ambient* rather than one-shot.

A single :class:`Runtime` is driven by four cooperating loops plus a one-time
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
5. **The reflection loop** runs the weekly reflection pass when due, serialized
   after the maintenance prune by a shared lock (never parallel to it).
6. **The allocation loop** (PLAN:integration-fix:P007, ADR-0036) runs one
   attention-allocation pass per interval when ``allocator.enabled``, also
   serialized after the maintenance prune by the same lock; disabled (the
   default) the tick returns without touching the ``workers:alloc`` queue.

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
    from datetime import datetime

    from paa.context.allocator import AllocationResult
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
        reflection_poll_interval: float = 3600.0,
        allocation_interval: float = 60.0,
        claim_batch: int = 4,
    ) -> None:
        self._rt = runtime
        self._dispatch_interval = dispatch_interval
        self._ingestion_interval = ingestion_interval
        self._maintenance_interval = maintenance_interval
        self._reflection_poll_interval = reflection_poll_interval
        # PLAN:integration-fix:P007 (ADR-0036) — the allocation tick cadence.
        # DECISION: 60s, between the ingestion cadence (1s, claim-based) and
        # the reflection poll (1h). It is advisory only — the gatherer ranks
        # without it — so a minute-scale cadence costs nothing and a hot
        # 1 Hz claim over an operator-fed queue would wake the loop needlessly.
        self._allocation_interval = allocation_interval
        self._claim_batch = claim_batch
        # PLAN:integration-fix:P013: serialises the curator prune tick
        # (maintenance loop) against the weekly reflection pass (reflection
        # loop), so the playbook write never runs parallel to a prune.
        self._curation_lock = asyncio.Lock()
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
            asyncio.create_task(self._supervise("reflection", self._reflection_loop)),
            asyncio.create_task(self._supervise("allocation", self._allocation_loop)),
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
                # PLAN:integration-fix:P013: the curation lock serialises
                # the prune tick against the weekly reflection pass (which
                # acquires the same lock) — never parallel; reflection runs
                # after the prune completes.
                async with self._curation_lock:
                    await self._run_curation_tick()
            except Exception as exc:
                log.error("daemon.maintenance_failed", error=redact(str(exc)))
            await self._sleep(self._maintenance_interval)

    async def _reflection_loop(self) -> None:
        # PLAN:integration-fix:P013 — the weekly reflection loop, supervised
        # like the others. Unlike maintenance it does not sleep first: with
        # no last-run marker the pass is due immediately, so the first
        # iteration checks (and on first boot, runs) before any sleep. The
        # delay after each check is then the time until the next due point
        # (or the poll interval while disabled / after a failure), so the
        # loop fires once per weekly interval instead of tight-looping.
        while not self._stop.is_set():
            try:
                await self._run_reflection_tick()
            except Exception as exc:
                log.error("daemon.reflection_failed", error=redact(str(exc)))
            await self._sleep(self._reflection_delay_seconds())

    async def _run_reflection_tick(self, *, now: datetime | None = None) -> bool:
        """Run one weekly reflection pass if due; return whether it ran.

        The pass is gated by ``ReflectionSettings.enabled`` and the
        file-based last-run marker (see :mod:`paa.improvement.reflection`).
        ``run_weekly`` itself guards each domain's playbook write against
        ``VaultConflictError`` (skipped + logged per rule), so a fenced
        human edit never aborts the tick; any other failure propagates to
        the loop, which logs it and keeps the daemon alive. The marker is
        written only after the pass completes, so a crash mid-pass leaves
        no marker and the next tick re-runs.
        """
        settings = self._rt.settings
        if not settings.reflection.enabled:
            return False
        from paa.improvement.reflection import (
            ReflectionEngine,
            reflection_health,
            write_reflection_last_run,
        )
        from paa.storage.relational.database import utc_now

        when = now or utc_now()
        if not reflection_health(settings.home, enabled=True, now=when)["due"]:
            return False
        # Ordering (plan §J): the pass acquires the curation lock the
        # maintenance loop holds around its prune tick, so the weekly
        # playbook write runs after the prune, never parallel to it.
        async with self._curation_lock:
            engine = ReflectionEngine(self._rt.ledger, vault_path=settings.storage.vault_path)
            report = await engine.run_weekly(now=when)
            write_reflection_last_run(settings.home, when)
        log.info(
            "daemon.reflection_completed",
            domains=len(report.domains),
            rules_written=len(report.rules_written),
        )
        return True

    async def _allocation_loop(self) -> None:
        # PLAN:integration-fix:P007 — the attention-allocation loop, supervised
        # like the reflection loop. The tick itself is gated by
        # ``AllocatorSettings.enabled`` (default ``False``), so a deployment
        # that never sets the field pays only one early-return per interval
        # and the ``workers:alloc`` queue is never claimed.
        while not self._stop.is_set():
            try:
                await self._run_allocation_tick()
            except Exception as exc:
                log.error("daemon.allocation_failed", error=redact(str(exc)))
            await self._sleep(self._allocation_interval)

    async def _run_allocation_tick(self) -> AllocationResult | None:
        """Run one attention-allocation pass; return its result or ``None``.

        Gated by ``AllocatorSettings.enabled`` and delegated to
        :meth:`ChiefOrchestrator.run_allocation` (chain: daemon ->
        orchestrator -> :func:`paa.context.allocator.allocate_attention`),
        which guards the queue claim itself: a down queue degrades to an
        ``underfed`` result, never a raise (R-W4-007), and claim-failure
        underfed semantics are preserved here because the result is logged,
        never reinterpreted. The curation lock serialises the pass after the
        maintenance prune exactly like the reflection tick (never parallel
        to it). A tick failure propagates to :meth:`_allocation_loop`, which
        logs ``daemon.allocation_failed`` and keeps the daemon alive.
        """
        settings = self._rt.settings
        if not settings.allocator.enabled:
            return None
        async with self._curation_lock:
            result = await self._rt.orchestrator.run_allocation(
                settings=self._rt.settings.allocator
            )
        log.info(
            "daemon.allocation_completed",
            outcome=result.outcome,
            claimed=result.claimed,
            scored=result.scored,
            poisoned=result.poisoned,
            emitted=result.emitted,
            notified=result.notified,
            acked=result.acked,
            nacked=result.nacked,
        )
        return result

    def _reflection_delay_seconds(self, *, now: datetime | None = None) -> float:
        """How long the reflection loop sleeps before its next check.

        Not due (fresh marker) -> the time remaining until the weekly due
        point. Due (a failed tick left no marker, or the marker aged out
        between checks) or disabled -> the poll interval, so a retry happens
        within the hour and an operator enabling the flag takes effect
        without a daemon restart.
        """
        settings = self._rt.settings
        if not settings.reflection.enabled:
            return self._reflection_poll_interval
        from paa.improvement.reflection import (
            REFLECTION_INTERVAL_DAYS,
            reflection_last_run_age_days,
        )

        age = reflection_last_run_age_days(settings.home, now=now)
        if age is None or age > REFLECTION_INTERVAL_DAYS:
            return self._reflection_poll_interval
        return max(0.0, (REFLECTION_INTERVAL_DAYS - age) * 86400.0)

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
        the tick falls back to a bare ``DecaySweeper.sweep()`` with a warning,
        and the fallback honours the same preview discipline: its sweep is
        ``dry_run`` on the first tick and it consumes the preview flag after
        that successful preview, so the next tick deletes. A fallback sweep
        that raises propagates to the loop and does not consume the preview.
        A ``run_maintenance`` exception is never masked by a fallback sweep
        (that would double-run decay); it propagates to the loop, which logs
        ``daemon.maintenance_failed`` and keeps the daemon alive. This tick
        runs in the maintenance task only — never inline in ingestion.

        Liveness trade-off (R-W4-002): ``MemoryCurator.run_maintenance``
        isolates failures per phase (``report.errors`` increments and the
        loop continues), so ``completed_fully`` stays True when a phase
        errors. The daemon nevertheless gates the preview flag on
        ``completed_fully and errors == 0``. One permanently-erroring phase
        therefore blocks every delete forever (preview-forever, the safe
        direction): ``CurationReport`` carries no per-phase outcome fields,
        so the daemon cannot know whether the DECAY phase itself previewed
        cleanly or was the one that errored, and relaxing to
        ``completed_fully``-only could delete phases that were never
        successfully previewed. The incomplete preview is logged loudly
        (``daemon.curation_preview_incomplete``) with the full summary;
        the proper recovery (per-phase tracking) needs a curator.py report
        field and is out of this wave's write-set. Budget stops are distinct:
        they resume cleanly via ``_curate_next_phase`` and can still
        complete on a later tick.
        """
        settings = self._rt.settings.memory
        budget = settings.curation_max_runtime_hours * 3600.0
        dry_run = not self._curate_preview_done
        try:
            from paa.memory.curator import MemoryCurator

            curator = MemoryCurator(
                self._rt.db,
                settings=settings,
                cost_gate=self._rt.settings.cost_gate,
                vector_store=self._rt.vector_store,
                archive_writer=getattr(self._rt, "archive_writer", None),
            )
        except Exception as exc:
            log.warning("daemon.curator_unavailable", error=redact(str(exc)))
            from paa.memory.decay import DecaySweeper

            # FINAL-02: forward the injected runtime settings' explicit floor
            # exactly as the curator path does (curator.py:135), instead of
            # letting DecaySweeper fall back to the global singleton. An
            # unset (None) floor still defers to the global read inside
            # DecaySweeper, so default deployments are byte-identical;
            # without this the fallback tick would silently mix settings
            # sources whenever the runtime Settings differ from the global.
            await DecaySweeper(
                self._rt.db,
                prune_confidence_floor=settings.explicit_prune_floor(),
            ).sweep(dry_run=dry_run)
            if dry_run:
                # R-W4-001: consume the preview after a *successful* fallback
                # preview so the next tick deletes. Without this a permanently
                # broken curator degraded to preview-forever (retention never
                # ran). Each fallback tick already warns via
                # ``curator_unavailable`` above, and the sweep logs its
                # would-delete counts, so the operator sees every fallback
                # tick. A raising sweep propagates and never reaches here, so
                # the preview is only consumed on success.
                self._curate_preview_done = True
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
        elif dry_run:
            # R-W4-002: the preview was not fully previewed (budget stop or a
            # phase error). Stay in preview — the safe direction — but make
            # the liveness trap visible instead of silent: without this line a
            # permanently-erroring phase blocks deletes with no daemon-level
            # signal beyond the per-tick summary carried in
            # ``daemon.curation_completed`` below.
            log.warning(
                "daemon.curation_preview_incomplete",
                dry_run=dry_run,
                completed_fully=report.completed_fully,
                errors=report.errors,
                stopped_early_at=(
                    report.stopped_early_at.value if report.stopped_early_at else None
                ),
            )
        log.info("daemon.curation_completed", dry_run=dry_run, **report.summary())
        return report
