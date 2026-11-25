"""The replay decision table and its integration into coder.tool-loop (C6.3 / P2.2).

What these tests pin:

* **the decision table** (:class:`~paa.pipelines.replay.ReplayView.check`)
  — every row: unrecorded seq executes; a fingerprint mismatch diverges;
  COMPLETED serves the recorded result; an interrupted *pure* call (think)
  re-executes; an interrupted *mutating* call reports ``-32008`` and is
  never auto-repeated (D-6); a real recorded failure stands;
* **the integration** — a crashed run's journal and transcript, folded
  into a :class:`ReplayView` and a :class:`ServedToolResults`, answer the
  re-run's calls: the model is invoked only for calls past the record
  (zero re-invocation), tools are served, not executed (zero
  re-execution), the rebuilt transcript matches the crashed run's, and
  the ledger's call rows do not grow while calls are served;
* **divergence stops the run** — a re-run that issues a different call at
  a recorded seq raises :class:`PipelineDivergedError` and appends
  ``PIPELINE_RUN_DIVERGED``;
* **the served-tool queue is order-checked** — a re-run that swaps tool
  order diverges rather than being served the wrong result.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.coder.pipeline import CoderLoopFacade, CoderToolLoopPipeline
from paa.coder.session import Session
from paa.ledger.recovery import (
    close_interrupted_pipeline_calls,
    find_interrupted_pipeline_calls,
)
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    PipelineDivergedError,
    PipelineStatus,
    PipelineTask,
)
from paa.pipelines.journal import CallJournal, CallOutcome, CallRecord, fingerprint
from paa.pipelines.replay import (
    INTERRUPTED_REASON,
    PURE_METHODS,
    ReplayAction,
    ReplayedCallFailedError,
    ReplayView,
    ServedToolResults,
    ToolSequenceDivergedError,
)


def _record(
    seq: int,
    method: str = "think",
    *,
    params: dict[str, Any] | None = None,
    outcome: CallOutcome = CallOutcome.COMPLETED,
    error: str | None = None,
    result: Any = None,
) -> CallRecord:
    return CallRecord(
        seq=seq,
        method=method,
        fingerprint=fingerprint(method, params or {"role": "worker"}),
        outcome=outcome,
        result=result,
        error=error,
    )


class TestDecisionTable:
    def test_unrecorded_seq_executes(self) -> None:
        view = ReplayView({})
        decision = view.check(7, "think", {"role": "worker"})
        assert decision.action is ReplayAction.EXECUTE

    def test_completed_is_served(self) -> None:
        view = ReplayView({1: _record(1, result={"text": "hi", "tokens": 3})})
        decision = view.check(1, "think", {"role": "worker"})
        assert decision.action is ReplayAction.SERVE
        assert decision.result == {"text": "hi", "tokens": 3}

    def test_attested_null_result_is_served_as_null(self) -> None:
        """A real JSON null with a digest is a value, not an unknown outcome."""
        record = _record(1, result=None)
        record = CallRecord(
            seq=record.seq,
            method=record.method,
            fingerprint=record.fingerprint,
            outcome=record.outcome,
            result=None,
            result_sha256="0" * 64,
        )
        view = ReplayView({1: record})
        decision = view.check(1, "think", {"role": "worker"})
        assert decision.action is ReplayAction.SERVE
        assert decision.result is None

    def test_completed_without_an_attested_result_is_outcome_unknown(self) -> None:
        """An unresolvable blob is not a recorded ``null`` result (D-6)."""
        view = ReplayView({1: _record(1, result=None)})
        decision = view.check(1, "think", {"role": "worker"})
        assert decision.action is ReplayAction.OUTCOME_UNKNOWN
        assert "unavailable" in (decision.reason or "")

    def test_fingerprint_mismatch_diverges(self) -> None:
        view = ReplayView({1: _record(1, params={"role": "worker"})})
        decision = view.check(1, "think", {"role": "planner"})
        assert decision.action is ReplayAction.DIVERGED
        assert decision.expected_fingerprint != decision.actual_fingerprint

    def test_interrupted_pure_call_reexecutes(self) -> None:
        assert "think" in PURE_METHODS
        for outcome, error in (
            (CallOutcome.PROPOSED, None),
            (CallOutcome.FAILED, INTERRUPTED_REASON),
        ):
            view = ReplayView({2: _record(2, outcome=outcome, error=error)})
            decision = view.check(2, "think", {"role": "worker"})
            assert decision.action is ReplayAction.EXECUTE

    def test_interrupted_mutating_call_never_auto_repeats(self) -> None:
        for outcome, error in (
            (CallOutcome.PROPOSED, None),
            (CallOutcome.FAILED, INTERRUPTED_REASON),
        ):
            view = ReplayView({3: _record(3, "commit", outcome=outcome, error=error)})
            decision = view.check(3, "commit", {"role": "worker"})
            assert decision.action is ReplayAction.OUTCOME_UNKNOWN

    def test_real_recorded_failure_stands(self) -> None:
        view = ReplayView({4: _record(4, outcome=CallOutcome.FAILED, error="ProviderError: 429")})
        decision = view.check(4, "think", {"role": "worker"})
        assert decision.action is ReplayAction.FAIL
        assert "ProviderError" in (decision.reason or "")


class TestServedToolResults:
    def test_fifo_order_and_exhaustion(self) -> None:
        queue = ServedToolResults([("read_file", "contents"), ("bash", "5 passed")])
        assert queue.pop_next("read_file") == "contents"
        assert queue.pop_next("bash") == "5 passed"
        assert queue.pop_next("bash") is None  # empty: execute normally

    def test_wrong_tool_diverges(self) -> None:
        queue = ServedToolResults([("read_file", "contents")])
        with pytest.raises(ToolSequenceDivergedError, match="read_file"):
            queue.pop_next("bash")

    def test_from_transcript_reads_tool_results_in_order(
        self, tmp_path: Any
    ) -> None:
        lines = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "working"},
            {"role": "tool_call", "content": "", "tool_name": "read_file"},
            {"role": "tool_result", "content": "the bytes", "tool_name": "read_file"},
            {"role": "tool_call", "content": "", "tool_name": "bash"},
            {"role": "tool_result", "content": "5 passed", "tool_name": "bash"},
        ]
        import json

        path = tmp_path / "session.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
        queue = ServedToolResults.from_transcript(path)
        assert len(queue) == 2
        assert queue.pop_next("read_file") == "the bytes"
        assert queue.pop_next("bash") == "5 passed"


# ------------------------------------------------------------ the integration


class CountingModel:
    """A scripted model that counts its invocations."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = list(turns)
        self.invocations = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.invocations += 1
        return self._turns.pop(0) if self._turns else ModelTurn(text="done")


class Kill9(BaseException):
    """A hard kill, in process: nothing catches it, no cleanup row is
    written, the run simply stops — the honest shape of ``SIGKILL`` for
    a journal that only ever closes rows it can reach."""


class CrashingModel:
    """Answers scripted turns until the nth propose, which dies like a kill.

    The crash lands *inside* the model call, after the facade has
    journalled ``PIPELINE_CALL_PROPOSED`` and before any outcome row can
    exist — the exact state a ``kill -9`` between the two journal writes
    leaves behind (real params, real fingerprint, no outcome).
    """

    def __init__(self, turns: list[ModelTurn], crash_on: int) -> None:
        self._turns = list(turns)
        self._crash_on = crash_on
        self.invocations = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.invocations += 1
        if self.invocations == self._crash_on:
            raise Kill9("simulated SIGKILL between PROPOSED and the outcome")
        return self._turns.pop(0)


class ExplodingModel:
    """Fails its only propose with a real, journaled provider error."""

    invocations = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.invocations += 1
        raise RuntimeError("ProviderError: 429 after retries")


class CountingTool:
    def __init__(self, name: str, result: str = "ok") -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.result = result
        self.executions = 0

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, arguments: dict[str, Any]) -> str:
        self.executions += 1
        return self.result


def _turns() -> list[ModelTurn]:
    """Three thinks: two tool turns, then a final answer."""
    return [
        ModelTurn(tool_calls=[ToolCall("alpha", {"n": 1}, call_id="t1")]),
        ModelTurn(tool_calls=[ToolCall("beta", {"n": 2}, call_id="t2")]),
        ModelTurn(text="all done"),
    ]


async def _run(
    ledger: LedgerStore,
    run_id: uuid.UUID,
    model: CountingModel,
    tools: list[CountingTool],
    session: Session,
    *,
    replay: ReplayView | None = None,
    served_tools: ServedToolResults | None = None,
) -> Any:
    journal = CallJournal(ledger, run_id)
    await journal.run_started("coder.tool-loop", "4.1.0")
    facade = CoderLoopFacade(
        model=model,
        tools={t.name: t for t in tools},
        session=session,
        run_id=run_id,
        call_journal=journal,
        replay=replay,
        served_tools=served_tools,
        permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
    )
    task = PipelineTask(
        correlation_id=run_id, session_id=None, goal="go", params={}
    )
    result = await CoderToolLoopPipeline().run(task, facade)
    await journal.run_completed(result.status.value, metrics=dict(result.metrics))
    return result


def _transcript_shape(session: Session) -> list[tuple[str, str, str | None]]:
    return [
        (turn.role.value, turn.content, turn.tool_name) for turn in session.turns
    ]


class TestFullReplay:
    async def test_completed_calls_are_served_never_re_executed(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        run_id = uuid.uuid4()

        # Run A: the "crashed" run that actually completed everything.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        session_a = Session(session_id="sess-replay", transcript_dir=dir_a)
        model_a = CountingModel(_turns())
        tools_a = [CountingTool("alpha"), CountingTool("beta")]
        result_a = await _run(ledger, run_id, model_a, tools_a, session_a)
        assert result_a.status is PipelineStatus.COMPLETED
        assert model_a.invocations == 3
        rows_before = await ledger.read_correlation(run_id)

        # Run B: the replay — same lineage, same session id, fresh
        # transcript, the record answering.
        view = await ReplayView.load(ledger, run_id)
        assert len(view) == 3
        served = ServedToolResults.from_transcript(
            dir_a / "sess-replay.jsonl"
        )
        assert len(served) == 2

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        session_b = Session(session_id="sess-replay", transcript_dir=dir_b)
        model_b = CountingModel(_turns())
        tools_b = [CountingTool("alpha"), CountingTool("beta")]
        result_b = await _run(
            ledger, run_id, model_b, tools_b, session_b,
            replay=view, served_tools=served,
        )

        assert result_b.status is PipelineStatus.COMPLETED
        assert result_b.summary == result_a.summary
        # Zero re-execution, the strong form: the record answers every
        # call — including the final turn — so the model is never
        # consulted at all. Replaying a completed lineage is N cheap
        # round-trips against the host's folded view, exactly the ADR's
        # cost model.
        assert model_b.invocations == 0
        # ...and no tool executed — both were served from the transcript.
        assert tools_b[0].executions == 0
        assert tools_b[1].executions == 0
        assert result_b.metrics["served_calls"] == 2
        assert result_b.metrics["denied_calls"] == 0
        # The rebuilt transcript is the crashed run's transcript.
        assert _transcript_shape(session_b) == _transcript_shape(session_a)
        # The ledger did not grow while calls were served: the re-run
        # appended only its run lifecycle rows, no new call rows.
        rows_after = await ledger.read_correlation(run_id)
        call_rows_before = [
            r for r in rows_before if r.event_type.startswith("PIPELINE_CALL")
        ]
        call_rows_after = [
            r for r in rows_after if r.event_type.startswith("PIPELINE_CALL")
        ]
        assert call_rows_after == call_rows_before

    async def test_partial_record_executes_only_the_tail(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        """A crash after think 2's outcome: thinks 1-2 served, 3 executes."""
        run_id = uuid.uuid4()

        # Drive the crashed prefix directly through the facade: two
        # thinks and their tool calls, then abandon the run.
        journal = CallJournal(ledger, run_id)
        await journal.run_started("coder.tool-loop", "4.1.0")
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        session_a = Session(session_id="sess-partial", transcript_dir=dir_a)
        facade_a = CoderLoopFacade(
            model=CountingModel(_turns()),
            tools={t.name: t for t in [CountingTool("alpha"), CountingTool("beta")]},
            session=session_a,
            run_id=run_id,
            call_journal=journal,
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        await facade_a.think("go")
        await facade_a.use_skill("alpha", {"n": 1}, call_id="t1")
        await facade_a.think("")
        await facade_a.use_skill("beta", {"n": 2}, call_id="t2")
        # "crash" here: nothing of think 3 exists.

        view = await ReplayView.load(ledger, run_id)
        assert len(view) == 2
        served = ServedToolResults.from_transcript(dir_a / "sess-partial.jsonl")
        assert len(served) == 2

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        session_b = Session(session_id="sess-partial", transcript_dir=dir_b)
        # Only the tail is unrecorded: the re-run's model is scripted with
        # exactly the turns it must still produce — think 3's answer.
        model_b = CountingModel([ModelTurn(text="all done")])
        tools_b = [CountingTool("alpha"), CountingTool("beta")]
        result_b = await _run(
            ledger, run_id, model_b, tools_b, session_b,
            replay=view, served_tools=served,
        )

        assert result_b.status is PipelineStatus.COMPLETED
        assert result_b.summary == "all done"
        assert model_b.invocations == 1  # only think 3
        assert all(t.executions == 0 for t in tools_b)
        assert result_b.metrics["served_calls"] == 2

    async def test_divergence_stops_the_run_and_appends_the_row(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        run_id = uuid.uuid4()
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        session_a = Session(session_id="sess-div", transcript_dir=dir_a)
        result_a = await _run(
            ledger, run_id, CountingModel(_turns()),
            [CountingTool("alpha"), CountingTool("beta")], session_a,
        )
        assert result_a.status is PipelineStatus.COMPLETED

        view = await ReplayView.load(ledger, run_id)
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        session_b = Session(session_id="sess-div", transcript_dir=dir_b)
        # A re-run that is not replaying its history: a different mandate
        # renders a different first window, so think 1's fingerprint no
        # longer matches the record. (A differently-scripted *model* cannot
        # diverge a replay — served calls never consult the model, and the
        # fingerprint pins the call's inputs, not its outputs. The window
        # is the only divergence vector, which is the point of pinning it.)
        facade = CoderLoopFacade(
            model=CountingModel(_turns()),
            tools={"alpha": CountingTool("alpha"), "beta": CountingTool("beta")},
            session=session_b,
            run_id=run_id,
            call_journal=CallJournal(ledger, run_id),
            replay=view,
            served_tools=ServedToolResults.from_transcript(
                dir_a / "sess-div.jsonl"
            ),
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        task = PipelineTask(
            correlation_id=run_id,
            session_id=None,
            goal="a materially different goal",
            params={},
        )
        with pytest.raises(PipelineDivergedError):
            await CoderToolLoopPipeline().run(task, facade)

        rows = await ledger.read_correlation(run_id)
        assert any(r.event_type == "PIPELINE_RUN_DIVERGED" for r in rows)
        # Nothing happened after the divergence: no model call, no tool
        # effect, no served call.
        assert facade._model.invocations == 0
        assert facade.tool_calls == 0
        assert facade.served_calls == 0

    async def test_interrupted_think_reexecutes_as_a_pure_call(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        """A kill between PROPOSED and the outcome: the sweep closes the
        call as ``interrupted``, and because ``think`` is pure the re-run
        resolves it by re-execution — the model is invoked for that seq
        exactly once more, every tool effect happens exactly once, and
        the run completes."""
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("coder.tool-loop", "4.1.0")
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        session_a = Session(session_id="sess-int", transcript_dir=dir_a)
        model_a = CrashingModel(
            [ModelTurn(tool_calls=[ToolCall("alpha", {"n": 1}, call_id="t1")])],
            crash_on=2,
        )
        tool_a = CountingTool("alpha")
        facade_a = CoderLoopFacade(
            model=model_a,
            tools={tool_a.name: tool_a},
            session=session_a,
            run_id=run_id,
            call_journal=journal,
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        await facade_a.think("go")  # think 1: completed, real params
        await facade_a.use_skill("alpha", {"n": 1}, call_id="t1")
        with pytest.raises(Kill9):
            await facade_a.think("")  # think 2: PROPOSED, then the kill
        assert model_a.invocations == 2
        assert tool_a.executions == 1

        # The startup sweep, the production order: close what the kill
        # left dangling before anything re-runs.
        events = await ledger.read_correlation(run_id)
        interrupted = find_interrupted_pipeline_calls(events)
        assert [row.payload.get("seq") for row in interrupted] == [2]
        await close_interrupted_pipeline_calls(ledger, interrupted)

        view = await ReplayView.load(ledger, run_id)
        record = view.record_at(2)
        assert (
            record is not None
            and record.outcome is CallOutcome.FAILED
            and record.error == INTERRUPTED_REASON
        )
        served = ServedToolResults.from_transcript(dir_a / "sess-int.jsonl")
        assert len(served) == 1  # alpha's result; beta never ran

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        session_b = Session(session_id="sess-int", transcript_dir=dir_b)
        model_b = CountingModel(
            [
                ModelTurn(tool_calls=[ToolCall("beta", {"n": 2}, call_id="t2")]),
                ModelTurn(text="recovered"),
            ]
        )
        tool_b = CountingTool("beta")
        facade_b = CoderLoopFacade(
            model=model_b,
            tools={"alpha": CountingTool("alpha"), "beta": tool_b},
            session=session_b,
            run_id=run_id,
            call_journal=CallJournal(ledger, run_id),
            replay=view,
            served_tools=served,
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="go", params={}
        )
        result = await CoderToolLoopPipeline().run(task, facade_b)

        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "recovered"
        # Think 1 was served; think 2 — the interrupted pure call —
        # re-executed; think 3 is new. Two model calls, no more.
        assert model_b.invocations == 2
        # Zero duplicate effects: alpha served (its run-A execution stands
        # as the only one), beta executed exactly once — here.
        assert facade_b.served_calls == 1
        assert tool_b.executions == 1
        assert result.metrics["served_calls"] == 1
        assert result.metrics["executed_calls"] == 1

        # The fold stays three calls wide, and the re-executed seq reads
        # FAILED — the conservative precedence (a seq carrying both an
        # interrupted-FAILED and a later COMPLETED reads FAILED), which is
        # what makes a *second* crash after this one re-execute think 2
        # again rather than trust a stale completion.
        refolded = await ReplayView.load(ledger, run_id)
        assert len(refolded) == 3
        seq2 = refolded.record_at(2)
        assert seq2 is not None and seq2.outcome is CallOutcome.FAILED

    async def test_replayed_failure_stands(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        """A call that genuinely failed — a real provider error, not a
        crash — keeps failing on replay: the re-run hits the same wall
        deterministically instead of re-attempting an effect whose record
        already says "no"."""
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("coder.tool-loop", "4.1.0")
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        session_a = Session(session_id="sess-fail", transcript_dir=dir_a)
        model_a = ExplodingModel()
        facade_a = CoderLoopFacade(
            model=model_a,
            tools={},
            session=session_a,
            run_id=run_id,
            call_journal=journal,
        )
        with pytest.raises(RuntimeError, match="ProviderError"):
            await facade_a.think("go")  # PROPOSED + FAILED, real reason

        view = await ReplayView.load(ledger, run_id)
        record = view.record_at(1)
        assert (
            record is not None
            and record.outcome is CallOutcome.FAILED
            and "ProviderError" in (record.error or "")
        )

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        session_b = Session(session_id="sess-fail", transcript_dir=dir_b)
        model_b = ExplodingModel()
        facade_b = CoderLoopFacade(
            model=model_b,
            tools={},
            session=session_b,
            run_id=run_id,
            call_journal=CallJournal(ledger, run_id),
            replay=view,
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="go", params={}
        )
        with pytest.raises(ReplayedCallFailedError, match="ProviderError"):
            await CoderToolLoopPipeline().run(task, facade_b)
        # The wall stood: the re-run never consulted its model, and no
        # divergence row was written — the record was followed, not fought.
        assert model_b.invocations == 0
        rows = await ledger.read_correlation(run_id)
        assert not any(r.event_type == "PIPELINE_RUN_DIVERGED" for r in rows)
