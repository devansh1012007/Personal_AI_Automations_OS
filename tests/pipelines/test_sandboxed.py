"""The installed-pipeline adapter — the loading contract, pinned (C8.6).

SPEC 2 Wave 8's pipeline-package row pinned the contract here first:
an installed pipeline's bytes execute as a **sandboxed subprocess**
(:class:`~paa.pipelines.sandboxed.SandboxedPipelineRunner`), never as
an import, and the run journals through the same
:class:`~paa.pipelines.journal.CallJournal` every other pipeline
vocabulary uses — so ``paa pipeline trace`` reads it and the replay
discipline (serve, diverge, never auto-repeat the unknown) applies in
full, because executing the entrypoint is a mutating journaled call.

The tests drive a real child (the fixture entrypoint below) through a
real :class:`~paa.sandbox.subprocess_backend.SubprocessSandbox` over a
real ledger — no double for the containment, the child or the journal:
the property under test is the *contract*, and the contract is only
real against the machinery it names.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    OutcomeUnknownError,
    PipelineDivergedError,
    PipelineStatus,
)
from paa.pipelines.journal import CallJournal, CallOutcome, fingerprint, read_call_journal
from paa.pipelines.replay import ReplayedCallFailedError, ReplayView
from paa.pipelines.sandboxed import (
    ENTRYPOINT_METHOD,
    PIPELINE_ENTRYPOINT,
    SandboxedPipelineRunner,
    run_result_shape,
)
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxError,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)
from paa.sandbox.subprocess_backend import SubprocessSandbox

# The fixture entrypoint, in the shape the contract pins: read the args
# file ``--paa-args`` names (the smoke gate probes with ``{}``; a run
# passes ``{"goal": ..., "params": {...}}`` — an entrypoint must survive
# both), answer one JSON object on stdout. The summary folds the goal in
# so a served replay result is distinguishable from a fresh execution.
_ENTRYPOINT = '''\
import json, sys

args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
goal = args.get("goal") or ""
params = args.get("params") or {}
print(json.dumps({
    "status": "completed",
    "summary": "ran: " + goal,
    "metrics": {"params": len(params)},
}))
'''


def _staged_package(root: Path, *, entrypoint: str = _ENTRYPOINT) -> Path:
    """A staged installed-pipeline directory: ``pipeline.py`` at the root."""
    staged = root / f"pkg-{uuid.uuid4().hex[:6]}"
    staged.mkdir(parents=True)
    (staged / PIPELINE_ENTRYPOINT).write_text(entrypoint, encoding="utf-8")
    return staged


class _ScriptedSandbox(Sandbox):
    """Answers with a fixed result — the seam the runner must fold by rule.

    The runner's budget rule (an exhausted containment clock is a FAILED
    run, never a crash) is pinned against the shape every real backend's
    watchdog returns; the backends' own clocks are pinned in
    ``tests/sandbox``.
    """

    def __init__(self, result: SandboxResult) -> None:
        self._result = result
        self.specs: list[SandboxSpec] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        return self._result

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        raise SandboxError("the scripted double cannot open sessions", backend=self.name)

    async def healthcheck(self) -> bool:
        return True


class _HeldSandbox(Sandbox):
    """Holds the run window open so a cancel lands inside the call."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    @property
    def name(self) -> str:
        return "held"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        self.entered.set()
        await asyncio.sleep(5.0)
        return SandboxResult(exit_code=0, stdout="{}", backend=self.name)

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        raise SandboxError("the held double cannot open sessions", backend=self.name)

    async def healthcheck(self) -> bool:
        return True


class TestResultShape:
    """The stdout contract: whole payload, then the last non-empty line."""

    def test_the_whole_payload_first(self) -> None:
        assert run_result_shape('{"status": "completed"}') == {"status": "completed"}

    def test_the_last_line_when_diagnostics_precede_it(self) -> None:
        stdout = 'working...\n{"status": "completed", "summary": "ok"}'
        assert run_result_shape(stdout) == {"status": "completed", "summary": "ok"}

    def test_empty_stdout_is_none(self) -> None:
        assert run_result_shape("   \n") is None

    def test_non_json_is_none(self) -> None:
        assert run_result_shape("just words") is None

    def test_non_object_json_is_none(self) -> None:
        assert run_result_shape("[1, 2, 3]") is None


class TestOneRun:
    async def test_a_completed_run_journals_one_call(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        sandbox = SubprocessSandbox()
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("fixture.echo", "1.0.0", run_kind="installed")
        runner = SandboxedPipelineRunner(
            sandbox=sandbox, journal=journal, staged=staged, declaration=_declaration()
        )
        result = await runner.run("say hello")
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))

        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "ran: say hello"
        assert result.metrics == {"params": 0}
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1
        record = records[0]
        assert record.method == ENTRYPOINT_METHOD
        assert record.outcome is CallOutcome.COMPLETED
        assert record.fingerprint == fingerprint(
            ENTRYPOINT_METHOD, {"goal": "say hello", "params": {}}
        )
        assert record.result == {
            "status": "completed",
            "summary": "ran: say hello",
            "metrics": {"params": 0},
        }

    async def test_the_child_runs_under_the_pinned_command_shape(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        """The argv the contract pins: python <staged>/pipeline.py --paa-args."""
        staged = _staged_package(tmp_path)
        scripted = _ScriptedSandbox(
            SandboxResult(exit_code=0, stdout='{"status": "completed"}', backend="s")
        )
        journal = CallJournal(ledger, uuid.uuid4())
        runner = SandboxedPipelineRunner(
            sandbox=scripted, journal=journal, staged=staged, declaration=_declaration()
        )
        await runner.run("anything")
        (spec,) = scripted.specs
        assert spec.command[1].endswith(PIPELINE_ENTRYPOINT)
        assert spec.command[2] == "--paa-args"
        assert spec.read_only_mounts == (staged.resolve(),)

    async def test_a_crashed_child_fails_by_rule_not_by_crash(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(
            tmp_path, entrypoint="import sys\nsys.exit(3)\n"
        )
        journal = CallJournal(ledger, uuid.uuid4())
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        result = await runner.run("go")
        assert result.status is PipelineStatus.FAILED
        assert "exit 3" in result.summary
        records = await read_call_journal(ledger, runner.journal.run_id)
        assert records[0].outcome is CallOutcome.COMPLETED  # classified, not crashed

    async def test_no_json_on_stdout_fails_by_rule(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path, entrypoint="print('not json')\n")
        journal = CallJournal(ledger, uuid.uuid4())
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        result = await runner.run("go")
        assert result.status is PipelineStatus.FAILED
        assert result.metrics == {"state": "no-json"}

    async def test_an_unknown_status_word_fails_by_rule(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(
            tmp_path,
            entrypoint='import json\nprint(json.dumps({"status": "so-so"}))\n',
        )
        journal = CallJournal(ledger, uuid.uuid4())
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        result = await runner.run("go")
        assert result.status is PipelineStatus.FAILED
        assert result.metrics == {"state": "bad-status"}

    async def test_an_exhausted_clock_fails_by_rule(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        exhausted = _ScriptedSandbox(
            SandboxResult(
                exit_code=None,
                stdout="",
                stderr="",
                timed_out=True,
                killed_reason="timeout",
                backend="s",
            )
        )
        journal = CallJournal(ledger, uuid.uuid4())
        runner = SandboxedPipelineRunner(
            sandbox=exhausted, journal=journal, staged=staged, declaration=_declaration()
        )
        result = await runner.run("go")
        assert result.status is PipelineStatus.FAILED
        assert result.summary == "entrypoint run failed (timeout)"
        assert result.metrics == {"state": "timeout"}

    async def test_a_missing_entrypoint_is_a_construction_refusal(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        from paa.pipelines.sandboxed import SandboxedPipelineError

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(SandboxedPipelineError, match="entrypoint is missing"):
            SandboxedPipelineRunner(
                sandbox=SubprocessSandbox(),
                journal=CallJournal(ledger, uuid.uuid4()),
                staged=empty,
                declaration=_declaration(),
            )


class TestReplayDiscipline:
    """Serve, diverge, and never auto-repeat the unknown (D-6 in full)."""

    async def _drive_once(
        self, ledger: LedgerStore, staged: Path, goal: str
    ) -> tuple[uuid.UUID, SandboxedPipelineRunner]:
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        result = await runner.run(goal)
        assert result.status is PipelineStatus.COMPLETED
        return run_id, runner

    async def test_a_re_drive_is_served_without_re_executing(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        run_id, runner = await self._drive_once(ledger, staged, "same goal")
        before = await read_call_journal(ledger, run_id)
        view = await ReplayView.load(ledger, run_id)

        served = await runner.run("same goal", replay=view)

        assert served.status is PipelineStatus.COMPLETED
        assert served.summary == "ran: same goal"
        after = await read_call_journal(ledger, run_id)
        assert after == before  # zero re-execution: the rows did not move

    async def test_a_changed_call_diverges_loudly(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        from paa.core.types import EventType

        staged = _staged_package(tmp_path)
        run_id, runner = await self._drive_once(ledger, staged, "first goal")
        view = await ReplayView.load(ledger, run_id)

        with pytest.raises(PipelineDivergedError):
            await runner.run("a DIFFERENT goal", replay=view)

        events = await ledger.read_correlation(run_id)
        assert EventType.PIPELINE_RUN_DIVERGED in [e.event_type for e in events]
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1  # no second call was journalled

    async def test_an_interrupted_call_is_never_auto_repeated(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        # The crash shape: the proposal landed, the child's answer never did.
        await journal.call_proposed(
            1, ENTRYPOINT_METHOD, {"goal": "interrupted", "params": {}}
        )
        view = await ReplayView.load(ledger, run_id)
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        with pytest.raises(OutcomeUnknownError):
            await runner.run("interrupted", replay=view)
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1  # still exactly the proposal — nothing re-ran

    async def test_a_recorded_failure_stands(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.call_proposed(
            1, ENTRYPOINT_METHOD, {"goal": "failed once", "params": {}}
        )
        await journal.call_failed(1, ENTRYPOINT_METHOD, "exit 3")
        view = await ReplayView.load(ledger, run_id)
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=staged,
            declaration=_declaration(),
        )
        with pytest.raises(ReplayedCallFailedError, match="exit 3"):
            await runner.run("failed once", replay=view)


class TestCancelWithinGrace:
    async def test_a_cancelled_run_closes_its_call_and_propagates(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        staged = _staged_package(tmp_path)
        held = _HeldSandbox()
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("fixture.echo", "1.0.0", run_kind="installed")
        runner = SandboxedPipelineRunner(
            sandbox=held, journal=journal, staged=staged, declaration=_declaration()
        )
        task = asyncio.create_task(runner.run("slow goal"))
        await held.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1
        assert records[0].outcome is CallOutcome.FAILED
        assert records[0].error == "cancelled"


class TestBoundedResults:
    def _runner_with(self, tmp_path: Path, ledger: LedgerStore, stdout: str) -> Any:
        staged = _staged_package(tmp_path)
        scripted = _ScriptedSandbox(
            SandboxResult(exit_code=0, stdout=stdout, backend="s")
        )
        return SandboxedPipelineRunner(
            sandbox=scripted,
            journal=CallJournal(ledger, uuid.uuid4()),
            staged=staged,
            declaration=_declaration(),
        )

    async def test_the_summary_is_bounded(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        runner = self._runner_with(
            tmp_path,
            ledger,
            json.dumps({"status": "completed", "summary": "x" * 5000}),
        )
        result = await runner.run("g")
        assert len(result.summary) == 200

    async def test_metrics_keep_scalars_and_drop_documents(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        runner = self._runner_with(
            tmp_path,
            ledger,
            json.dumps(
                {
                    "status": "completed",
                    "metrics": {
                        "n": 3,
                        "ok": True,
                        "name": "echo",
                        "nested": {"a": 1},
                        "long": "y" * 500,
                    },
                }
            ),
        )
        result = await runner.run("g")
        assert result.metrics == {"n": 3, "ok": True, "name": "echo"}


def _declaration() -> Any:
    """The minimal declaration the runner reads (name/version/resume)."""
    from paa.pipelines.slots import PipelineDeclaration

    return PipelineDeclaration(
        name="fixture.echo",
        version="1.0.0",
        requires_runtime=">=4.1,<5",
        description="the test fixture",
        trust="installed",
        resume="journal",
        evidence_tier="unrated",
    )
