"""N3.3: cancellation of a running tool is an outcome, not a silent death.

What is pinned here
-------------------
* A task cancel (Ctrl-C) during a long-running tool call re-raises
  :exc:`asyncio.CancelledError` promptly (well under the 5 s budget), and
  the loop journals the call FAILED before propagating — so the ledger
  carries the cancel as an outcome row, and the recovery sweep does not
  later misclassify the outcome-less PROPOSED as a crash ("interrupted").
* The bash tool's cancel seam names the cancellation and re-raises, letting
  the loop's FAILED journal write happen; it fabricates no result.
* A cancel that arrives before any PROPOSED row exists (e.g. inside the
  model's ``propose``) writes no outcome at all — there is no call to close.
* The normal completion path is unchanged: a fast call still completes and
  journals COMPLETED.

The "10-minute command" is a fake that would run for 600 s; the suite never
waits it out. The real subprocess backend's tree-kill on cancel is owned by
the sandbox package, not this suite: it is verified by an external probe in
the N3.3 report, not here, because the backend fix belongs to a different
write-set (filed as a NEED).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import pytest

from paa.coder.journal import LedgerToolJournal
from paa.coder.loop import AgentLoop, ModelTurn, ToolCall, ToolCtx
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.coder.session import Session, TurnRole
from paa.coder.shell_tools import BashTool
from paa.coder.workspace import Workspace
from paa.core.types import EventType, new_correlation_id
from paa.ledger.recovery import find_interrupted_calls
from paa.ledger.store import LedgerStore
from paa.sandbox.base import IsolationLevel, Sandbox, SandboxResult, SandboxSpec

#: A "long" command stands in for the brief's 10-minute sleep; nothing in
#: this suite waits it out — a cancel must land far sooner than the
#: 5-second acceptance budget.
_LONG_RUN_SECONDS = 600.0


class ScriptedModel:
    """One bash call, then final — the cancel lands inside the call."""

    def __init__(self) -> None:
        self._turns: list[ModelTurn] = [
            ModelTurn(tool_calls=[ToolCall("bash", {"command": "sleep 600"})]),
            ModelTurn(text="done"),
        ]

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        return self._turns.pop(0) if self._turns else ModelTurn(text="done")


class RecordingJournal:
    """Fake ToolJournal that records every outcome it is asked to write."""

    def __init__(self) -> None:
        self.before_calls: list[ToolCtx] = []
        self.denied: list[tuple[ToolCtx, str]] = []
        self.after_calls: list[tuple[ToolCtx, bool, str | None]] = []

    async def before(self, ctx: ToolCtx) -> None:
        self.before_calls.append(ctx)

    async def denied(self, ctx: ToolCtx, reason: str) -> None:
        self.denied.append((ctx, reason))

    async def after(self, ctx: ToolCtx, ok: bool, result_sha: str | None) -> None:
        self.after_calls.append((ctx, ok, result_sha))


class HangingTool:
    """A tool that runs 'forever': only a cancel can end it."""

    name = "bash"
    description = "the hanging bash tool"

    def __init__(self) -> None:
        self.cancel_seen = False

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, arguments: Mapping[str, Any]) -> str:
        try:
            await asyncio.sleep(_LONG_RUN_SECONDS)
        except asyncio.CancelledError:
            self.cancel_seen = True
            raise
        return "slept"  # pragma: no cover - never reached in this suite


class HangingSandbox(Sandbox):
    """Fake backend whose run stands in for a 10-minute command.

    It prints nothing, sleeps 600 s, and lets the CancelledError propagate
    exactly as a real task cancel would deliver it — the loop's and bash
    tool's seams are the units under test, not this fake's kill logic.
    """

    def __init__(self) -> None:
        self.cancel_seen = False

    @property
    def name(self) -> str:
        return "hanging-fake"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    async def healthcheck(self) -> bool:
        return True

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        try:
            await asyncio.sleep(_LONG_RUN_SECONDS)
        except asyncio.CancelledError:
            self.cancel_seen = True
            raise
        raise AssertionError("hanging sandbox returned without a cancel")  # pragma: no cover


def _loop(
    tool: Any, session: Session, journal: Any, **over: Any
) -> AgentLoop:
    return AgentLoop(
        ScriptedModel(),
        {tool.name: tool},
        session=session,
        permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        journal=journal,
        **over,
    )


class TestLoopCancellation:
    async def test_cancel_during_tool_run_journals_failed_and_reraises(self) -> None:
        """The cancel is an outcome (FAILED), not a silent death.

        Without the seam the CancelledError propagated while the PROPOSED
        row stayed outcome-less and the recovery sweep later wrote
        FAILED(reason="interrupted") for an operator cancel. The FAILED
        row now exists before propagation, the session transcript names
        the cancel, and the CancelledError still reaches the caller.
        """
        tool = HangingTool()
        journal = RecordingJournal()
        session = Session()
        loop = _loop(tool, session, journal)

        started = time.perf_counter()
        task = asyncio.create_task(loop.run("run a long command"))
        await asyncio.sleep(0.1)  # let the call start and PROPOSED land
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - started

        assert elapsed < 5.0, f"cancel took {elapsed:.2f}s, budget is 5s"
        assert tool.cancel_seen, "the cancel never reached the running tool"
        assert len(journal.before_calls) == 1
        assert journal.after_calls and journal.after_calls[0][1] is False
        assert journal.after_calls[0][2] is None, "a cancel attests no result hash"
        results = [
            turn
            for turn in session.turns
            if turn.role is TurnRole.TOOL_RESULT and turn.tool_name == "bash"
        ]
        assert results and "cancelled" in results[-1].content

    async def test_cancel_before_any_call_writes_no_outcome(self) -> None:
        """A cancel in the model has no in-flight call to close.

        Everything is a new frozen value; regurgitating a fabricated outcome
        row for a call that never started would forge ledger history.
        """

        class BlockingModel:
            async def propose(
                self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
            ) -> ModelTurn:
                await asyncio.sleep(_LONG_RUN_SECONDS)
                return ModelTurn(text="done")  # pragma: no cover

        journal = RecordingJournal()
        loop = AgentLoop(
            BlockingModel(),
            {"bash": HangingTool()},
            session=Session(),
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
            journal=journal,
        )
        task = asyncio.create_task(loop.run("go"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.before_calls == []
        assert journal.after_calls == []

    async def test_normal_completion_is_unchanged(self) -> None:
        """The regression half: a fast call still completes as COMPLETED.

        Written against the fix so a revert of the seam (or an accidental
        broadening of the CancelledError arm into normal flow) fails here.
        """

        class FastTool:
            name = "bash"
            description = "fast"

            def schema(self) -> dict[str, Any]:
                return {"type": "object", "properties": {}}

            async def run(self, arguments: Mapping[str, Any]) -> str:
                return "data"

        journal = RecordingJournal()
        session = Session()
        loop = _loop(FastTool(), session, journal)
        result = await loop.run("read it")
        assert result.stopped_reason == "model_finished"
        assert len(journal.after_calls) == 1
        assert journal.after_calls[0][1] is True
        assert journal.after_calls[0][2] is not None

    async def test_cancel_writes_a_failed_row_to_the_real_ledger(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        """End to end: the cancel row lands in the ledger and is excluded
        from the recovery sweep's interrupted set.
        """
        from pathlib import Path

        workspace = Workspace(root=Path(tmp_path))
        sandbox = HangingSandbox()
        bash = BashTool(workspace, sandbox=sandbox)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid)
        session = Session()
        loop = _loop(bash, session, journal, workspace=workspace)

        task = asyncio.create_task(loop.run("run a long command"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        rows = await ledger.read_correlation(cid)
        types = [row.event_type for row in rows]
        assert types == [EventType.TOOL_CALL_PROPOSED, EventType.TOOL_CALL_FAILED]
        assert rows[-1].payload["result_sha256"] is None
        # The sweep only closes PROPOSED rows with no outcome; a FAILED
        # row already present means this cancel is never "interrupted".
        assert find_interrupted_calls(rows) == []
        assert sandbox.cancel_seen


class TestBashCancellation:
    async def test_bash_reraises_cancel_promptly(self, tmp_path: Any) -> None:
        """A cancelled bash run raises CancelledError naming the cancel;
        it does not return a fabricated timeout result to the model."""
        from pathlib import Path

        workspace = Workspace(root=Path(tmp_path))
        bash = BashTool(workspace, sandbox=HangingSandbox())
        started = time.perf_counter()
        task = asyncio.create_task(bash.run({"command": "sleep 600"}))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.perf_counter() - started < 5.0
