"""The background daemon: boot recovery, queue drain, graceful stop."""

from __future__ import annotations

import asyncio

import pytest

from paa.daemon import Daemon
from paa.ledger.replay import TaskPhase

pytestmark = pytest.mark.requires_api  # shares the runtime fixture in this dir


class TestDaemon:
    async def test_start_runs_boot_recovery_then_stops_cleanly(self, runtime) -> None:  # noqa: ANN001
        daemon = Daemon(runtime, dispatch_interval=0.05, ingestion_interval=0.05)
        await daemon.start()
        assert runtime._boot_report is not None  # recovery ran on start
        await daemon.stop()

    async def test_dispatch_loop_drains_a_queued_task(self, runtime_full) -> None:  # noqa: ANN001
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

    async def test_ingestion_loop_processes_a_signal(self, runtime) -> None:  # noqa: ANN001
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

    async def test_stop_is_idempotent(self, runtime) -> None:  # noqa: ANN001
        daemon = Daemon(runtime)
        await daemon.start()
        await daemon.stop()
        await daemon.stop()  # must not raise
