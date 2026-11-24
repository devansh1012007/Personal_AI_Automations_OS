"""``builtin.observe`` as pipeline #4 on the facade (C7.1 / P1.4).

The external-harness spec's §6 rule, pinned end to end: an observed CLI
is a first-class pipeline — same ``PipelineTask``, same host-minted
facade discipline as the other three — that differs from them in
exactly two declared facts (``resume="none"``, ``evidence_tier="T1"``).
What these tests hold:

* **the journal is the run** — PROPOSED lands before the child is
  spawned (ledger-before-effect), the COMPLETED result carries the
  bundle's hash and never its text, and the run rows carry the two
  external-harness facts a reader needs;
* **the observer's own crash is audited** — a core that raises mid-run
  still leaves the FAILED row, and the recovery sweep later closes the
  lineage as pipeline-only, never re-driving it;
* **the door is narrow** — a foreign facade is refused by name, an
  unparseable or empty goal is refused, and a bool is not an int.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from paa.core.errors import PaaError, SandboxError
from paa.ledger.store import LedgerStore
from paa.observe.observer import ObserverCore
from paa.observe.pipeline import ObserveFacade, ObservePipeline
from paa.pipelines.base import PipelineStatus, PipelineTask
from paa.pipelines.journal import CallJournal, CallOutcome, read_call_journal
from paa.sandbox.subprocess_backend import SubprocessSandbox


async def _facade(
    ledger: LedgerStore, run_id: uuid.UUID, workspace: Path, tmp_path: Path
) -> ObserveFacade:
    """A facade on its own lineage, with the run rows the driver writes."""
    journal = CallJournal(ledger, run_id)
    await journal.run_started(
        ObservePipeline.name,
        ObservePipeline.version,
        run_kind="observe",
        resume="none",
        evidence_tier="T1",
    )
    return ObserveFacade(
        sandbox=SubprocessSandbox(),
        journal=journal,
        workspace=workspace,
        bundle_home=tmp_path / "bundles",
    )


def _task(run_id: uuid.UUID, goal: str, params: dict[str, Any] | None = None) -> PipelineTask:
    return PipelineTask(
        correlation_id=run_id, session_id=None, goal=goal, params=params or {}
    )


async def _events(ledger: LedgerStore, run_id: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    rows = await ledger.read_correlation(run_id)
    return [(e.event_type.value, dict(e.payload)) for e in rows]


class TestTheFacadeJournalsTheRun:
    async def test_proposed_lands_before_the_child_and_completed_after(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """Ledger-before-effect, read off the lineage's own row order."""
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        outcome = await facade.observe_command(
            [sys.executable, "-c", "print('ran')"]
        )

        kinds = [k for k, _ in await _events(ledger, run)]
        assert kinds.index("PIPELINE_CALL_PROPOSED") < kinds.index(
            "PIPELINE_CALL_COMPLETED"
        )
        assert kinds == [
            "PIPELINE_RUN_STARTED",
            "PIPELINE_CALL_PROPOSED",
            "PIPELINE_CALL_COMPLETED",
        ]
        # The result row carries the bundle's identity, never its text.
        completed = await read_call_journal(ledger, run)
        assert [r.method for r in completed] == ["observe.run"]
        result = completed[0].result
        assert result["state"] == "clean_exit"
        assert result["bundle_run_id"] == str(run)
        assert result["bundle_sha256"] == outcome.bundle.sha256()
        assert result["evidence_tier"] == "T1"
        assert result["resume"] == "none"
        assert "text" not in result  # no transcript rides the ledger

    async def test_the_run_rows_carry_the_two_external_harness_facts(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        await facade.observe_command([sys.executable, "-c", "pass"])

        events = await _events(ledger, run)
        started = next(p for k, p in events if k == "PIPELINE_RUN_STARTED")
        assert started["resume"] == "none"
        assert started["evidence_tier"] == "T1"
        assert started["run_kind"] == "observe"

    async def test_the_bundle_run_id_is_the_lineage_run_id(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        outcome = await facade.observe_command(
            [sys.executable, "-c", "pass"], timeout_s=30
        )
        assert outcome.run_id == str(run)
        assert outcome.bundle_path.name == "bundle.json"
        assert str(run) in str(outcome.bundle_path)

    async def test_an_observer_crash_leaves_the_failed_row(
        self, ledger: LedgerStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()

        async def explode(self: ObserverCore, *a: object, **k: object) -> Any:
            raise SandboxError("observer vanished mid-run")

        monkeypatch.setattr(ObserverCore, "observe", explode)
        facade = await _facade(ledger, run, ws, tmp_path)
        with pytest.raises(SandboxError):
            await facade.observe_command([sys.executable, "-c", "pass"])

        events = await _events(ledger, run)
        kinds = [k for k, _ in events]
        assert kinds == ["PIPELINE_RUN_STARTED", "PIPELINE_CALL_PROPOSED", "PIPELINE_CALL_FAILED"]
        failed = next(p for k, p in events if k == "PIPELINE_CALL_FAILED")
        assert "observer did not resolve" in failed["reason"]


class TestThePipeline:
    async def test_clean_exit_maps_to_completed_with_the_artifact(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "seed.txt").write_text("s", encoding="utf-8")
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        result = await ObservePipeline().run(
            _task(run, f"{sys.executable} -c pass"), facade
        )
        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "clean exit"
        assert result.metrics["state"] == "clean_exit"
        assert result.metrics["resume"] == "none"
        assert result.metrics["evidence_tier"] == "T1"
        assert result.metrics["bundle_path"].endswith("bundle.json")
        assert len(result.artifacts) == 1
        assert result.artifacts[0].kind == "observe_bundle"
        assert result.artifacts[0].sha256 is not None

    async def test_a_killed_child_maps_to_failed_with_the_classification_riding(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        result = await ObservePipeline().run(
            _task(
                run,
                f"{sys.executable} -c \"import os, signal; "
                f"os.kill(os.getpid(), signal.SIGKILL)\"",
            ),
            facade,
        )
        assert result.status is PipelineStatus.FAILED
        assert result.metrics["state"] == "signalled"
        assert result.metrics["signal"] == 9
        assert "SIGKILL" in result.summary

    async def test_a_foreign_facade_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        class NotTheObserveFacade:
            pass

        run = uuid.uuid4()
        with pytest.raises(PaaError, match="ObserveFacade"):
            await ObservePipeline().run(
                _task(run, "echo hi"), NotTheObserveFacade()  # type: ignore[arg-type]
            )

    async def test_an_unparseable_goal_is_refused_not_guessed(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        with pytest.raises(PaaError, match="could not"):
            await ObservePipeline().run(_task(run, "echo 'unclosed"), facade)

    async def test_an_empty_goal_is_refused(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        with pytest.raises(PaaError, match="non-empty command"):
            await ObservePipeline().run(_task(run, "   "), facade)

    async def test_quoting_in_the_goal_survives_the_split(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """A goal is a command line: quoted words stay one argv word."""
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        result = await ObservePipeline().run(
            _task(run, f"{sys.executable} -c 'import sys; print(len(sys.argv))' x"),
            facade,
        )
        # the observed child printed its argc; the bundle is the proof
        from paa.observe.bundle import read_bundle

        bundle = read_bundle(Path(result.metrics["bundle_path"]))
        assert bundle.transcript_stdout is not None
        assert bundle.transcript_stdout.text.strip().isdigit()

    async def test_a_bool_is_not_an_int_knob(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        result = await ObservePipeline().run(
            _task(run, f"{sys.executable} -c pass", {"timeout_s": True}), facade
        )
        assert result.status is PipelineStatus.COMPLETED  # default 600 applied


class TestRecoveryRefusesTheObserveLineage:
    """``resume = "none"`` is structural, not aspirational."""

    async def test_the_sweep_skips_and_never_requeues(
        self, ledger: LedgerStore, db, requeuer, tmp_path: Path
    ) -> None:
        from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome

        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        facade = await _facade(ledger, run, ws, tmp_path)
        await facade.observe_command([sys.executable, "-c", "pass"])

        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 1
        assert requeuer.calls == []

    async def test_an_interrupted_observe_call_is_closed_never_redriven(
        self, ledger: LedgerStore, db, tmp_path: Path
    ) -> None:
        """A crash between PROPOSED and the outcome closes as FAILED."""
        from paa.ledger.recovery import RecoveryEngine, RecoveryOutcome

        ws = tmp_path / "ws"
        ws.mkdir()
        run = uuid.uuid4()
        journal = CallJournal(ledger, run)
        await journal.run_started(
            ObservePipeline.name,
            ObservePipeline.version,
            run_kind="observe",
            resume="none",
            evidence_tier="T1",
        )
        await journal.call_proposed(1, "observe.run", {"command": ["x"]})
        # no outcome: the crash landed here

        report = await RecoveryEngine(ledger, db).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_PIPELINE_LINEAGE) == 1
        records = await read_call_journal(ledger, run)
        assert [r.outcome for r in records] == [CallOutcome.FAILED]
