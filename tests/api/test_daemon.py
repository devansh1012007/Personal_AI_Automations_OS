"""The background daemon: boot recovery, queue drain, graceful stop."""

from __future__ import annotations

import asyncio

import pytest

from paa.daemon import Daemon
from paa.ledger.replay import TaskPhase
from paa.runtime import Runtime

pytestmark = pytest.mark.requires_api  # shares the runtime fixture in this dir


class TestDaemon:
    async def test_start_runs_boot_recovery_then_stops_cleanly(self, runtime: Runtime) -> None:
        daemon = Daemon(runtime, dispatch_interval=0.05, ingestion_interval=0.05)
        await daemon.start()
        assert runtime._boot_report is not None  # recovery ran on start
        await daemon.stop()

    async def test_dispatch_loop_drains_a_queued_task(self, runtime_full: Runtime) -> None:
        runtime = runtime_full
        # Submit without running — it lands on the queue for the daemon.
        cid = await runtime.submit("do it")
        assert runtime.queue is not None

        daemon = Daemon(runtime, dispatch_interval=0.02, ingestion_interval=10)
        await daemon.start()
        try:
            # Poll until the daemon drives the task to a terminal phase.
            for _ in range(200):
                proj = await runtime.project(cid)
                if proj.is_terminal:
                    break
                await asyncio.sleep(0.02)
            proj = await runtime.project(cid)
            assert proj.phase is TaskPhase.COMMITTED
        finally:
            await daemon.stop()

    async def test_ingestion_loop_processes_a_signal(self, runtime: Runtime) -> None:
        from paa.storage.coldlake.cas import ContentAddressedStore
        from paa.storage.coldlake.signals import SignalRepository

        cas = ContentAddressedStore(runtime.settings.storage.cold_lake_path)
        repo = SignalRepository(runtime.db, cas)
        await repo.record(
            "email", {"facts": [{"subject": "Alpha", "predicate": "status", "object": "live"}]}
        )

        daemon = Daemon(runtime, dispatch_interval=10, ingestion_interval=0.02)
        await daemon.start()
        try:
            for _ in range(200):
                n = await runtime.db.fetch_value(
                    "SELECT COUNT(*) FROM hot_serving_active_facts"
                )
                if n and n > 0:
                    break
                await asyncio.sleep(0.02)
            facts = await runtime.db.fetch_value(
                "SELECT COUNT(*) FROM hot_serving_active_facts"
            )
            assert facts >= 1
        finally:
            await daemon.stop()

    async def test_stop_is_idempotent(self, runtime: Runtime) -> None:
        daemon = Daemon(runtime)
        await daemon.start()
        await daemon.stop()
        await daemon.stop()  # must not raise

    @pytest.mark.slow
    async def test_a_crashed_loop_is_restarted(
        self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A loop that raises must be started again, and the others must live on.

        The daemon's promise is that one failing loop cannot silently take
        the daemon down, so the supervisor restarts the loop that crashed
        (after a one-second backoff) and leaves the remaining loops running.
        The test waits for the *second* attempt rather than sleeping a fixed
        amount, so it asserts the restart happened without depending on how
        fast the machine is.
        """
        daemon = Daemon(runtime, dispatch_interval=0.05, ingestion_interval=0.05)
        attempts = 0
        restarted = asyncio.Event()

        async def flaky_dispatch() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("dispatch blew up")
            restarted.set()
            # Park, so the supervisor does not spin the loop hot while the
            # test asserts on it.
            await asyncio.sleep(3600)

        monkeypatch.setattr(daemon, "_dispatch_loop", flaky_dispatch)
        await daemon.start()
        try:
            await asyncio.wait_for(restarted.wait(), timeout=10)
            # The other two loops are untouched by their sibling's crash.
            # Checked before ``stop()``, which empties the task list.
            assert all(not task.done() for task in daemon._tasks)
        finally:
            await daemon.stop()

        assert attempts >= 2  # the crash was survived, not terminal
