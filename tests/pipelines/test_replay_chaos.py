"""The C6.3 chaos matrix: kill -9 at every call boundary, resume, no dupes.

One scripted ``coder.tool-loop`` lineage — two tools, three thinks — and
five crash points, one per call boundary the in-process driver can reach:

* ``think-1`` / ``think-2`` / ``think-3`` — the model dies *inside* the
  nth propose, after ``PIPELINE_CALL_PROPOSED`` and before any outcome
  row can exist (the exact state a ``kill -9`` between the two journal
  writes leaves: real params, real fingerprint, no outcome);
* ``tool-1`` / ``tool-2`` — the tool dies *inside* its execution, after
  the session's ``tool_call`` row and before any ``tool_result`` (the
  effect never happened, so the transcript has nothing to serve).

After every crash the suite runs the production startup sweep
(:func:`close_interrupted_pipeline_calls`) — twice, because a sweep that
is not idempotent would itself be a duplicate-effect source — then
resumes the run through :class:`ReplayView` + :class:`ServedToolResults`
and pins the C6.3 acceptance verbatim:

* the resumed run **completes** with the same final answer;
* every tool effect happened **exactly once** across crash + resume
  (zero duplicate effects — the property, not the aspiration);
* the model was invoked exactly ``3 + 1`` times for a think-boundary
  crash (the interrupted *pure* call re-executes once) and exactly ``3``
  times for a tool-boundary crash (nothing re-executes);
* the folded journal stays three calls wide, and a seq the sweep closed
  as ``interrupted`` reads ``FAILED`` even after its re-execution
  completes — the conservative precedence that keeps a *second* crash
  honest.

What this suite deliberately does not reach in-process: the
``OUTCOME_UNKNOWN`` row of the decision table for a *mutating* call —
``coder.tool-loop`` journals only ``think`` (pure) through the
``CallJournal``; mutating calls ride the ``TOOL_CALL_*`` vocabulary and
the coder recovery engine's own table. That row is pinned in
``test_replay.py`` at the table level and becomes integration-reachable
when a facade gains a mutating ``CallJournal`` method.
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
from paa.pipelines.base import PipelineStatus, PipelineTask
from paa.pipelines.journal import CallJournal, CallOutcome
from paa.pipelines.replay import ReplayView, ServedToolResults


class Kill9(BaseException):
    """A hard kill, in process: nothing catches it, no cleanup row is
    written, the run simply stops."""


class CrashingModel:
    """Answers scripted turns until the nth propose, which dies like a kill."""

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


class CountingModel:
    """A scripted model that counts its invocations."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = list(turns)
        self.invocations = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.invocations += 1
        return self._turns.pop(0)


class Tool:
    """A tool that counts the executions its effect actually happened in."""

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


class DyingTool:
    """Dies like a kill on its first execution — before the effect.

    The death does not count as an execution: the effect never happened,
    which is what the resume must make up for.
    """

    def __init__(self, inner: Tool) -> None:
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.died = False

    def schema(self) -> dict[str, Any]:
        return self._inner.schema()

    async def run(self, arguments: dict[str, Any]) -> str:
        if not self.died:
            self.died = True
            raise Kill9("simulated SIGKILL inside the tool, before the effect")
        return await self._inner.run(arguments)


def _alpha() -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall("alpha", {"n": 1}, call_id="t1")])


def _beta() -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall("beta", {"n": 2}, call_id="t2")])


async def _drive(
    ledger: LedgerStore,
    run_id: Any,
    tmp_path: Any,
    dirname: str,
    model: Any,
    tools: dict[str, Any],
    *,
    replay: ReplayView | None = None,
    served_tools: ServedToolResults | None = None,
) -> Any:
    """One pipeline drive on the lineage, in a fresh transcript dir."""
    journal = CallJournal(ledger, run_id)
    await journal.run_started("coder.tool-loop", "4.1.0")
    transcript = tmp_path / dirname
    transcript.mkdir()
    facade = CoderLoopFacade(
        model=model,
        tools=tools,
        session=Session(session_id="sess-chaos", transcript_dir=transcript),
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
    return result, facade


class TestKillAtEveryBoundary:
    """The matrix: one crash point per reachable call boundary."""

    @pytest.mark.parametrize(
        "crash_at",
        ["think-1", "tool-1", "think-2", "tool-2", "think-3"],
    )
    async def test_crash_sweep_resume_completes_with_zero_duplicate_effects(
        self, ledger: LedgerStore, tmp_path: Any, crash_at: str
    ) -> None:
        run_id = uuid.uuid4()

        # -- run A: drive until the kill --------------------------------
        crash_think = crash_at.startswith("think-")
        crash_tool = crash_at.removeprefix("tool-") if crash_at.startswith("tool-") else None
        nth = int(crash_at.rsplit("-", 1)[1])

        alpha, beta = Tool("alpha"), Tool("beta")
        if crash_think:
            scripted = {1: [_alpha()], 2: [_alpha(), _beta()], 3: [_alpha(), _beta()]}
            model_a = CrashingModel(scripted[nth], crash_on=nth)
            tools_a: dict[str, Any] = {"alpha": alpha, "beta": beta}
        else:
            model_a = CountingModel([_alpha(), _beta(), ModelTurn(text="all done")])
            dying = DyingTool(alpha if crash_tool == "1" else beta)
            tools_a = {
                "alpha": dying if crash_tool == "1" else alpha,
                "beta": dying if crash_tool == "2" else beta,
            }

        with pytest.raises(Kill9):
            await _drive(ledger, run_id, tmp_path, "a", model_a, tools_a)

        # -- the startup sweep, twice: idempotence is part of the gate --
        events = await ledger.read_correlation(run_id)
        interrupted = find_interrupted_pipeline_calls(events)
        if crash_think:
            # The kill left exactly the nth think dangling.
            assert [row.payload.get("seq") for row in interrupted] == [nth]
        else:
            # A tool death leaves no pipeline-call row dangling: thinks
            # journal outcomes around the tool call, and tools ride the
            # TOOL_CALL_* vocabulary, not this one.
            assert interrupted == []
        await close_interrupted_pipeline_calls(ledger, interrupted)
        await close_interrupted_pipeline_calls(ledger, interrupted)  # idempotent

        # -- run B: resume through the record ---------------------------
        view = await ReplayView.load(ledger, run_id)
        served = ServedToolResults.from_transcript(tmp_path / "a" / "sess-chaos.jsonl")

        if crash_think:
            tail = {1: [_alpha(), _beta(), ModelTurn(text="all done")],
                    2: [_beta(), ModelTurn(text="all done")],
                    3: [ModelTurn(text="all done")]}[nth]
            model_b = CountingModel(tail)
        else:
            tail = {1: [_beta(), ModelTurn(text="all done")],
                    2: [ModelTurn(text="all done")]}[int(crash_tool or 1)]
            model_b = CountingModel(tail)
        tools_b: dict[str, Any] = {"alpha": alpha, "beta": beta}

        result_b, _ = await _drive(
            ledger, run_id, tmp_path, "b", model_b, tools_b,
            replay=view, served_tools=served,
        )

        # -- the gate ----------------------------------------------------
        assert result_b.status is PipelineStatus.COMPLETED
        assert result_b.summary == "all done"

        # Zero duplicate effects: each tool's effect happened exactly
        # once across crash + resume. (A DyingTool's death is not an
        # execution; its inner Tool counts the real one.)
        assert alpha.executions + beta.executions == 2
        assert alpha.executions == 1
        assert beta.executions == 1

        # The model's arithmetic: a think-boundary crash re-executes the
        # interrupted pure call once (3 + 1); a tool-boundary crash
        # re-executes nothing (3).
        total_invocations = model_a.invocations + model_b.invocations
        if crash_think:
            assert total_invocations == 4
        else:
            assert total_invocations == 3

        # The fold stays three calls wide; a swept-then-re-executed seq
        # reads FAILED (the conservative precedence, pinned on purpose).
        refolded = await ReplayView.load(ledger, run_id)
        assert len(refolded) == 3
        for seq in (1, 2, 3):
            record = refolded.record_at(seq)
            assert record is not None
            if crash_think and seq == nth:
                assert record.outcome is CallOutcome.FAILED
                assert record.error == "interrupted"
            else:
                assert record.outcome is CallOutcome.COMPLETED
