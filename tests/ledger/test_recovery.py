"""Crash recovery: boot sweep, workspace reconciliation, rollback.

This file covers DoD item 1 — "forcing a hard system termination mid-execution
resolves perfectly on boot to the exact pre-crash state through ledger replay".

Crashes are simulated by closing the Database mid-lineage and reopening it,
which is exactly what a power cut looks like to SQLite in WAL mode: committed
transactions survive, uncommitted ones vanish.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from paa.core.types import EventType, new_correlation_id, new_session_id
from paa.ledger.events import LedgerEvent
from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome
from paa.ledger.replay import TaskPhase, project
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database
from tests.conftest import FakeSnapshotter, RecordingRequeuer


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A small source tree.

    Written as bytes so the on-disk content is exactly what the manifest
    hashes — Windows text-mode newline translation would otherwise make every
    file look drifted.
    """
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "auth.py").write_bytes(b"def login():\n    return True\n")
    (ws / "README.md").write_bytes(b"# project\n")
    return ws


def engine(
    ledger: LedgerStore,
    db: Database,
    snapshotter: FakeSnapshotter | None = None,
    requeuer: RecordingRequeuer | None = None,
    **kw,
) -> RecoveryEngine:
    return RecoveryEngine(ledger, db, snapshotter=snapshotter, requeuer=requeuer, **kw)


async def start_task(ledger: LedgerStore, *, workspace: Path | None = None):
    """Drive a lineage up to mid-execution, where a crash would hurt most."""
    cid = new_correlation_id()
    sid = new_session_id()
    payload = {"request": {"goal": "refactor"}}
    if workspace:
        payload["workspace_path"] = str(workspace)

    for etype, pl in [
        (EventType.TASK_REQUESTED, payload),
        (EventType.CONTEXT_HYDRATED, {"context_packet": {"density": 0.9}}),
        (EventType.PLAN_COMPILED, {"execution_steps": [{"id": 0}, {"id": 1}]}),
        (EventType.POLICY_CLEARED, {"decision": "STATUS_APPROVED"}),
    ]:
        await ledger.append(LedgerEvent.create(cid, etype, session_id=sid, payload=pl))
    return cid, sid


class TestBootSweep:
    async def test_clean_system_sweeps_empty(self, ledger: LedgerStore, db: Database) -> None:
        report = await engine(ledger, db).boot_sweep()
        assert report.scanned == 0
        assert report.recoveries == []

    async def test_in_flight_task_is_resumed(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        cid, _ = await start_task(ledger)
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )

        report = await engine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.scanned == 1
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
        assert requeuer.calls[0]["payload"]["recovered"] is True
        assert requeuer.calls[0]["payload"]["resume_phase"] == TaskPhase.EXECUTING.value

    async def test_completed_task_is_not_swept(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        cid, _ = await start_task(ledger)
        await ledger.append(LedgerEvent.create(cid, EventType.MUTATION_COMMITTED))
        report = await engine(ledger, db).boot_sweep()
        assert report.scanned == 0

    async def test_human_gate_stays_parked(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        """A crash must never be treated as approval."""
        cid, _ = await start_task(ledger)
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.AWAITING_HUMAN_ATTESTATION,
                payload={"reason": "deletes production files"},
            )
        )

        report = await engine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.count(RecoveryOutcome.PARKED_HUMAN) == 1
        assert requeuer.calls == [], "a parked task must not be re-queued"

    async def test_retry_ceiling_abandons(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        cid, _ = await start_task(ledger)
        for attempt in range(4):
            await ledger.append(
                LedgerEvent.create(
                    cid, EventType.EXECUTION_STARTED, attempt=attempt, payload={"step_index": 0}
                )
            )

        report = await engine(ledger, db, requeuer=requeuer, max_attempts=3).boot_sweep()

        assert report.count(RecoveryOutcome.ABANDONED) == 1
        assert requeuer.calls == []
        assert (await project(ledger, cid)).phase is TaskPhase.ABANDONED

    async def test_sweep_is_idempotent(
        self, ledger: LedgerStore, db: Database, requeuer: RecordingRequeuer
    ) -> None:
        """A crash during recovery is possible; running it twice must be safe."""
        cid, _ = await start_task(ledger)
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )

        eng = engine(ledger, db, requeuer=requeuer)
        first = await eng.boot_sweep()
        count_after_first = await ledger.count()
        second = await eng.boot_sweep()

        assert first.count(RecoveryOutcome.RESUMED) == 1
        assert second.count(RecoveryOutcome.RESUMED) == 1
        # The RECOVERY_REPLAY_COMPLETED event dedupes on its discriminator.
        assert await ledger.count() == count_after_first

    async def test_one_bad_lineage_does_not_abort_the_sweep(
        self, ledger: LedgerStore, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good_a, _ = await start_task(ledger)
        bad, _ = await start_task(ledger)
        good_b, _ = await start_task(ledger)

        eng = engine(ledger, db)
        original = eng.recover_correlation

        async def explode(head):
            if head.correlation_id == bad:
                raise RuntimeError("simulated corruption")
            return await original(head)

        monkeypatch.setattr(eng, "recover_correlation", explode)
        report = await eng.boot_sweep()

        assert report.scanned == 3
        assert report.count(RecoveryOutcome.UNRECOVERABLE) == 1
        assert report.count(RecoveryOutcome.RESUMED) == 2

    async def test_chain_problems_are_reported(
        self, ledger: LedgerStore, db: Database
    ) -> None:
        cid, _ = await start_task(ledger)
        await db.execute(
            "UPDATE system_state_ledger SET payload='{\"tampered\":1}' "
            "WHERE correlation_id = ? AND state_version = 2",
            (str(cid),),
        )
        report = await engine(ledger, db).boot_sweep()
        assert str(cid) in report.chain_problems


class TestWorkspaceReconciliation:
    async def test_untouched_workspace_reports_no_drift(
        self,
        ledger: LedgerStore,
        db: Database,
        workspace: Path,
        snapshotter: FakeSnapshotter,
        requeuer: RecordingRequeuer,
    ) -> None:
        cid, _ = await start_task(ledger, workspace=workspace)
        eng = engine(ledger, db, snapshotter, requeuer)
        await eng.checkpoint(cid, 4, workspace)
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )

        report = await eng.boot_sweep()

        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert report.recoveries[0].drifted_paths == []

    async def test_drift_triggers_rollback(
        self,
        ledger: LedgerStore,
        db: Database,
        workspace: Path,
        snapshotter: FakeSnapshotter,
        requeuer: RecordingRequeuer,
    ) -> None:
        """The core sad-path: a crash left a half-written file on disk."""
        cid, _ = await start_task(ledger, workspace=workspace)
        original = (workspace / "src" / "auth.py").read_bytes()
        snapshotter.remember_tree(workspace)

        eng = engine(ledger, db, snapshotter, requeuer)
        await eng.checkpoint(cid, 4, workspace)
        await ledger.append(
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )

        # Simulate the crash: a partial write plus an orphaned temp file.
        (workspace / "src" / "auth.py").write_bytes(b"def login(:\n  # truncated")
        (workspace / "src" / ".tmp_partial").write_bytes(b"garbage")

        report = await eng.boot_sweep()
        recovery = report.recoveries[0]

        assert recovery.outcome is RecoveryOutcome.ROLLED_BACK
        assert "src/auth.py" in recovery.drifted_paths
        assert "src/.tmp_partial" in recovery.drifted_paths
        # Orphan removed, original bytes restored exactly.
        assert not (workspace / "src" / ".tmp_partial").exists()
        assert (workspace / "src" / "auth.py").read_bytes() == original

    async def test_rollback_is_recorded_in_the_ledger(
        self,
        ledger: LedgerStore,
        db: Database,
        workspace: Path,
        snapshotter: FakeSnapshotter,
    ) -> None:
        cid, _ = await start_task(ledger, workspace=workspace)
        snapshotter.remember_tree(workspace)

        eng = engine(ledger, db, snapshotter)
        await eng.checkpoint(cid, 4, workspace)
        await ledger.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))
        (workspace / "README.md").write_bytes(b"corrupted")

        await eng.boot_sweep()

        types = [e.event_type for e in await ledger.read_correlation(cid)]
        assert EventType.STATE_ROLLBACK_TRIGGERED in types
        assert EventType.RECOVERY_REPLAY_COMPLETED in types

    async def test_missing_checkpoint_is_not_an_error(
        self, ledger: LedgerStore, db: Database, workspace: Path, snapshotter: FakeSnapshotter
    ) -> None:
        """Tasks that never touched disk are the common case."""
        cid, _ = await start_task(ledger, workspace=workspace)
        await ledger.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))

        report = await engine(ledger, db, snapshotter).boot_sweep()
        assert report.count(RecoveryOutcome.RESUMED) == 1

    async def test_deleted_workspace_is_survivable(
        self, ledger: LedgerStore, db: Database, workspace: Path, snapshotter: FakeSnapshotter
    ) -> None:
        import shutil

        cid, _ = await start_task(ledger, workspace=workspace)
        eng = engine(ledger, db, snapshotter)
        await eng.checkpoint(cid, 4, workspace)
        await ledger.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))
        shutil.rmtree(workspace)

        report = await eng.boot_sweep()
        assert report.scanned == 1  # reported, not crashed


class TestHardCrashSimulation:
    """Kill the process, reopen the database, prove nothing was lost."""

    async def test_state_survives_process_death(self, tmp_path: Path) -> None:
        db_path = tmp_path / "crash.db"
        cid = new_correlation_id()

        db1 = Database(db_path)
        await db1.connect()
        ledger1 = LedgerStore(db1)
        # Per-step events share an event type, so they need a discriminator to
        # avoid being deduplicated into one another.
        for etype, pl, disc in [
            (EventType.TASK_REQUESTED, {"request": {"goal": "x"}}, None),
            (EventType.CONTEXT_HYDRATED, {"context_packet": {}}, None),
            (EventType.PLAN_COMPILED, {"execution_steps": [{"id": 0}, {"id": 1}]}, None),
            (EventType.EXECUTION_STARTED, {"step_index": 0}, "step-0"),
            (EventType.EXECUTION_COMPLETED, {"step_index": 0, "tokens_consumed": 99}, "step-0"),
            (EventType.EXECUTION_STARTED, {"step_index": 1}, "step-1"),
        ]:
            await ledger1.append(
                LedgerEvent.create(cid, etype, payload=pl, discriminator=disc)
            )
        before = await project(ledger1, cid)
        await db1.close()  # <-- the "crash"

        db2 = Database(db_path)
        await db2.connect()
        ledger2 = LedgerStore(db2)
        try:
            after = await project(ledger2, cid)

            assert after.model_dump() == before.model_dump()
            assert after.phase is TaskPhase.EXECUTING
            assert after.completed_steps == [0]
            assert after.current_step_index == 1
            assert after.tokens_consumed == 99

            ok, problems = await ledger2.verify_chain(cid)
            assert ok, problems

            report = await RecoveryEngine(ledger2, db2).boot_sweep()
            assert report.count(RecoveryOutcome.RESUMED) == 1
        finally:
            await db2.close()

    async def test_many_lineages_recover_independently(self, tmp_path: Path) -> None:
        db_path = tmp_path / "multi.db"
        db1 = Database(db_path)
        await db1.connect()
        ledger1 = LedgerStore(db1)

        committed, in_flight, gated = [], [], []
        for i in range(15):
            cid = new_correlation_id()
            await ledger1.append(
                LedgerEvent.create(cid, EventType.TASK_REQUESTED, payload={"i": i})
            )
            await ledger1.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))
            if i % 3 == 0:
                await ledger1.append(LedgerEvent.create(cid, EventType.MUTATION_COMMITTED))
                committed.append(cid)
            elif i % 3 == 1:
                await ledger1.append(
                    LedgerEvent.create(cid, EventType.AWAITING_HUMAN_ATTESTATION)
                )
                gated.append(cid)
            else:
                in_flight.append(cid)
        await db1.close()

        db2 = Database(db_path)
        await db2.connect()
        try:
            requeuer = RecordingRequeuer()
            report = await RecoveryEngine(
                LedgerStore(db2), db2, requeuer=requeuer
            ).boot_sweep()

            assert report.scanned == len(in_flight) + len(gated)
            assert report.count(RecoveryOutcome.RESUMED) == len(in_flight)
            assert report.count(RecoveryOutcome.PARKED_HUMAN) == len(gated)
            requeued = {uuid.UUID(c["payload"]["correlation_id"]) for c in requeuer.calls}
            assert requeued == set(in_flight)
        finally:
            await db2.close()

    async def test_uncommitted_transaction_leaves_no_trace(self, tmp_path: Path) -> None:
        """A crash mid-transaction must not leave a partial event."""
        db_path = tmp_path / "partial.db"
        cid = new_correlation_id()

        db1 = Database(db_path)
        await db1.connect()
        ledger1 = LedgerStore(db1)
        await ledger1.append(LedgerEvent.create(cid, EventType.TASK_REQUESTED))

        with pytest.raises(RuntimeError, match="power loss"):
            async with db1.transaction() as conn:
                await conn.execute(
                    "INSERT INTO system_state_ledger (event_id, correlation_id, state_version,"
                    " idempotency_key, event_type, execution_mode, payload, prev_hash,"
                    " event_hash, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), str(cid), 2, "half-written",
                        EventType.PLAN_COMPILED.value, "STANDARD", "{}",
                        "0" * 64, "f" * 64, "2026-01-01T00:00:00+00:00",
                    ),
                )
                raise RuntimeError("power loss")
        await db1.close()

        db2 = Database(db_path)
        await db2.connect()
        try:
            ledger2 = LedgerStore(db2)
            assert await ledger2.count() == 1
            ok, problems = await ledger2.verify_chain(cid)
            assert ok, problems
        finally:
            await db2.close()


class TestCheckpointing:
    async def test_checkpoint_records_a_mark(
        self, ledger: LedgerStore, db: Database, workspace: Path, snapshotter: FakeSnapshotter
    ) -> None:
        cid = new_correlation_id()
        digest = await engine(ledger, db, snapshotter).checkpoint(cid, 1, workspace)

        assert len(digest) == 64
        stored = await db.fetch_value(
            "SELECT manifest_hash FROM hot_serving_recovery_marks WHERE correlation_id = ?",
            (str(cid),),
        )
        assert stored == digest

    async def test_checkpoint_without_snapshotter_degrades(
        self, ledger: LedgerStore, db: Database, workspace: Path
    ) -> None:
        assert await engine(ledger, db).checkpoint(new_correlation_id(), 1, workspace) == ""

    async def test_latest_checkpoint_wins(
        self, ledger: LedgerStore, db: Database, workspace: Path, snapshotter: FakeSnapshotter
    ) -> None:
        cid = new_correlation_id()
        eng = engine(ledger, db, snapshotter)
        await eng.checkpoint(cid, 1, workspace)
        (workspace / "new.txt").write_text("added", encoding="utf-8")
        second = await eng.checkpoint(cid, 5, workspace)

        mark = await eng._latest_mark(cid)
        assert mark["state_version"] == 5
        assert mark["manifest_hash"] == second
