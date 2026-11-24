"""The coder tool loop as pipeline #3 — C6.1 / P1.3.

What these tests pin (the spec's own done-when is that ``tests/coder/``
pass unchanged — these prove the re-expression is faithful rather than
merely present):

* **equivalence** — the same scripted model and tools driven through the
  classic :class:`~paa.coder.loop.AgentLoop` and through
  :class:`~paa.coder.pipeline.CoderToolLoopPipeline` produce the same
  metrics (``stopped_reason``/``steps``/``tool_calls``/``denied_calls``/
  ``tokens``), the same final text, the same session transcript shape,
  and the same ``TOOL_CALL_*`` journal rows — kind order, model call
  ids, args hashes, isolation levels and outcomes — so the safety
  ordering is the pipeline's too;
* **the grant set is the only door** — every ungranted capability fails
  loudly, and ``think`` refuses schemas exactly as the chat facade does;
* **think journals through the CallJournal** — ``PIPELINE_CALL_*`` rows
  land on the run lineage with the window's fingerprint, and the
  recorded result carries the model's turn;
* **params** — ``max_steps``/``max_tokens`` come from the task params
  with the classic defaults, and ``True`` is not 1.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from paa.coder.hooks import HookEvent, HookRegistry, deny_tool_hook
from paa.coder.loop import (
    AgentLoop,
    IsolationLevel,
    ModelTurn,
    Tool,
    ToolCall,
    ToolCallingModel,
    ToolCtx,
    ToolJournal,
)
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.coder.pipeline import (
    CODER_CAPABILITIES,
    CapabilityNotGrantedError,
    CoderLoopFacade,
    CoderToolLoopPipeline,
)
from paa.coder.session import Session
from paa.core.types import EventType
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    InvalidCallParamsError,
    PipelineStatus,
    PipelineTask,
)
from paa.pipelines.journal import CallJournal, CallOutcome, read_call_journal

# ---------------------------------------------------------------- fixtures


class ScriptedModel:
    """Emits a pre-programmed sequence of turns, one per propose() call."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = turns
        self.windows: list[list[dict[str, Any]]] = []

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.windows.append(window)
        return self._turns.pop(0) if self._turns else ModelTurn(text="done")


class RecordingTool:
    def __init__(self, name: str, result: str = "ok") -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, arguments: Mapping[str, Any]) -> str:
        self.calls.append(dict(arguments))
        return self.result


class ExplodingTool(RecordingTool):
    async def run(self, arguments: Mapping[str, Any]) -> str:
        raise RuntimeError("boom")


class RecordingToolJournal:
    """A ToolJournal that records every row it is handed, verbatim."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def before(self, ctx: ToolCtx) -> None:
        self.rows.append(self._row("PROPOSED", ctx))

    async def denied(self, ctx: ToolCtx, reason: str) -> None:
        self.rows.append(self._row("DENIED", ctx, reason=reason))

    async def after(self, ctx: ToolCtx, ok: bool, result_sha: str | None) -> None:
        self.rows.append(self._row("COMPLETED" if ok else "FAILED", ctx, result_sha=result_sha))

    @staticmethod
    def _row(
        kind: str, ctx: ToolCtx, *, reason: str | None = None, result_sha: str | None = None
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "kind": kind,
            "tool": ctx.tool,
            "call_id": ctx.call_id,
            "args_sha256": ctx.args_sha256,
            "isolation": ctx.isolation_level.name,
        }
        if reason is not None:
            row["reason"] = reason
        if result_sha is not None:
            row["result_sha256"] = result_sha
        return row


def _tools_map(tools: list[Tool]) -> dict[str, Tool]:
    return {t.name: t for t in tools}


async def run_classic(
    model: ToolCallingModel,
    tools: list[Tool],
    goal: str,
    *,
    journal: ToolJournal | None = None,
    **kw: Any,
) -> tuple[Any, Session, list[dict[str, Any]]]:
    session = Session()
    loop = AgentLoop(
        model, _tools_map(tools), session=session, journal=journal, **kw
    )
    result = await loop.run(goal)
    return result, session, model.windows


async def run_pipeline(
    model: ToolCallingModel,
    tools: list[Tool],
    goal: str,
    *,
    params: dict[str, Any] | None = None,
    journal: ToolJournal | None = None,
    **facade_kw: Any,
) -> tuple[Any, Session, list[dict[str, Any]]]:
    session = Session()
    facade = CoderLoopFacade(
        model=model,
        tools=_tools_map(tools),
        session=session,
        tool_journal=journal,
        **facade_kw,
    )
    task = PipelineTask(
        correlation_id=uuid.uuid4(),
        session_id=None,
        goal=goal,
        params=dict(params or {}),
    )
    result = await CoderToolLoopPipeline().run(task, facade)
    return result, session, model.windows


def _transcript(session: Session) -> list[tuple[str, str, str | None]]:
    """The comparable transcript shape: role, content, tool name."""
    return [
        (turn.role.value, turn.content, turn.tool_name) for turn in session.turns
    ]


def _assert_equivalent(
    classic: Any,
    piped: Any,
    classic_session: Session,
    piped_session: Session,
) -> None:
    """The same loop through both doors — the C6.1 done-when."""
    assert piped.summary == classic.final_text
    assert piped.metrics["stopped_reason"] == classic.stopped_reason
    assert piped.metrics["steps"] == classic.steps
    assert piped.metrics["tool_calls"] == classic.tool_calls
    assert piped.metrics["denied_calls"] == classic.denied_calls
    assert piped.metrics["tokens"] == classic.tokens
    assert _transcript(piped_session) == _transcript(classic_session)
    expected_status = (
        PipelineStatus.COMPLETED
        if classic.stopped_reason == "model_finished"
        else PipelineStatus.FAILED
    )
    assert piped.status is expected_status


BYPASS = {"permissions": ToolPermissionPolicy(mode=PermissionMode.BYPASS)}


# ---------------------------------------------------------------- equivalence


class TestEquivalence:
    async def test_text_only_final(self) -> None:
        turns = [ModelTurn(text="hello")]

        classic, cs, _ = await run_classic(ScriptedModel(list(turns)), [], "hi")
        piped, ps, _ = await run_pipeline(ScriptedModel(list(turns)), [], "hi", **BYPASS)

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.tool_calls == 0

    async def test_tool_call_then_final(self) -> None:
        tool = RecordingTool("read_file", result="file contents")
        turns = [
            ModelTurn(tool_calls=[ToolCall("read_file", {"path": "a.py"}, call_id="c1")]),
            ModelTurn(text="I read it"),
        ]

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [tool], "read a.py", **BYPASS
        )
        assert tool.calls == [{"path": "a.py"}]  # the classic run consumed the tool

        tool2 = RecordingTool("read_file", result="file contents")
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)), [tool2], "read a.py", **BYPASS
        )
        assert tool2.calls == [{"path": "a.py"}]

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.tool_calls == 1

    async def test_denied_tool_continues_the_loop(self) -> None:
        perms = ToolPermissionPolicy(mode=PermissionMode.BYPASS, deny=["bash(rm*)"])
        turns = [
            ModelTurn(tool_calls=[ToolCall("bash", {"command": "rm -rf /"}, call_id="d1")]),
            ModelTurn(text="understood, I won't"),
        ]

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [RecordingTool("bash")], "clean up", permissions=perms
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)), [RecordingTool("bash")], "clean up", permissions=perms
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.denied_calls == 1
        assert piped.metrics["denied_calls"] == 1

    async def test_ask_without_confirm_declines(self) -> None:
        turns = [
            ModelTurn(tool_calls=[ToolCall("bash", {"command": "ls"}, call_id="a1")]),
            ModelTurn(text="done"),
        ]
        kw = {"permissions": ToolPermissionPolicy(mode=PermissionMode.ASK)}

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [RecordingTool("bash")], "go", **kw
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)), [RecordingTool("bash")], "go", **kw
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.denied_calls == 1

    async def test_hook_block_is_a_result_not_an_abort(self) -> None:
        def make_hooks() -> HookRegistry:
            hooks = HookRegistry()
            hooks.register(
                HookEvent.PRE_TOOL_USE, deny_tool_hook("send_email", "no outbound mail")
            )
            return hooks

        turns = [
            ModelTurn(tool_calls=[ToolCall("send_email", {"to": "x"}, call_id="h1")]),
            ModelTurn(text="cannot send"),
        ]
        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)),
            [RecordingTool("send_email")],
            "mail",
            hooks=make_hooks(),
            **BYPASS,
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("send_email")],
            "mail",
            hooks=make_hooks(),
            **BYPASS,
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.denied_calls == 1

    async def test_tool_error_is_fed_back_not_raised(self) -> None:
        turns = [
            ModelTurn(tool_calls=[ToolCall("bad", {}, call_id="e1")]),
            ModelTurn(text="it failed, sorry"),
        ]
        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [ExplodingTool("bad")], "go", **BYPASS
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)), [ExplodingTool("bad")], "go", **BYPASS
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.denied_calls == 1

    async def test_no_such_tool(self) -> None:
        turns = [
            ModelTurn(tool_calls=[ToolCall("ghost", {}, call_id="g1")]),
            ModelTurn(text="no such tool then"),
        ]
        classic, cs, _ = await run_classic(ScriptedModel(list(turns)), [], "go", **BYPASS)
        piped, ps, _ = await run_pipeline(ScriptedModel(list(turns)), [], "go", **BYPASS)

        _assert_equivalent(classic, piped, cs, ps)

    async def test_max_steps_reached(self) -> None:
        turns = [ModelTurn(tool_calls=[ToolCall("read_file", {})])] * 30

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [RecordingTool("read_file")], "loop", **BYPASS
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)), [RecordingTool("read_file")], "loop", **BYPASS
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.stopped_reason == "max_steps_reached"
        assert classic.final_text == ""

    async def test_token_budget_exhausted(self) -> None:
        # Turns carry text alongside the tool calls: the session records
        # tokens on the assistant row, and only a row records them.
        turns = [
            ModelTurn(text="working", tool_calls=[ToolCall("read_file", {})], tokens=500),
            ModelTurn(text="still working", tool_calls=[ToolCall("read_file", {})], tokens=500),
        ]
        kw = {**BYPASS, "max_tokens": 600}

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [RecordingTool("read_file")], "budget", **kw
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("read_file")],
            "budget",
            params={"max_tokens": 600},
            **BYPASS,
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.stopped_reason == "token_budget_exhausted"

    async def test_multi_call_turn_runs_in_order(self) -> None:
        tool_a = RecordingTool("alpha")
        tool_b = RecordingTool("beta")
        turns = [
            ModelTurn(
                tool_calls=[
                    ToolCall("alpha", {"n": 1}, call_id="m1"),
                    ToolCall("beta", {"n": 2}, call_id="m2"),
                ]
            ),
            ModelTurn(text="both done"),
        ]

        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)), [tool_a, tool_b], "go", **BYPASS
        )
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("alpha"), RecordingTool("beta")],
            "go",
            **BYPASS,
        )

        _assert_equivalent(classic, piped, cs, ps)
        assert classic.tool_calls == 2
        assert classic.denied_calls == 0


# ------------------------------------------------------- journal equivalence


class TestJournalEquivalence:
    async def test_tool_call_rows_are_identical(self) -> None:
        """The TOOL_CALL_* rows the two paths write are the same rows."""
        turns = [
            ModelTurn(
                tool_calls=[
                    ToolCall("read_file", {"path": "x.py"}, call_id="j1"),
                    ToolCall("bash", {"command": "rm"}, call_id="j2"),
                ]
            ),
            ModelTurn(text="done"),
        ]
        perms = ToolPermissionPolicy(mode=PermissionMode.BYPASS, deny=["bash(rm*)"])

        cj = RecordingToolJournal()
        classic, cs, _ = await run_classic(
            ScriptedModel(list(turns)),
            [RecordingTool("read_file"), RecordingTool("bash")],
            "go",
            journal=cj,
            permissions=perms,
        )
        pj = RecordingToolJournal()
        piped, ps, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("read_file"), RecordingTool("bash")],
            "go",
            journal=pj,
            permissions=perms,
        )

        _assert_equivalent(classic, piped, cs, ps)
        # The rows the ledger sees: PROPOSED+COMPLETED for read_file,
        # PROPOSED+DENIED for bash — same kinds, same call ids, same hashes.
        assert pj.rows == cj.rows
        assert [r["kind"] for r in pj.rows] == [
            "PROPOSED",
            "COMPLETED",
            "PROPOSED",
            "DENIED",
        ]
        assert [r["call_id"] for r in pj.rows] == ["j1", "j1", "j2", "j2"]

    async def test_isolation_level_is_attested_not_guessed(self) -> None:
        class SandboxedTool(RecordingTool):
            isolation_level = IsolationLevel.NAMESPACE

        turns = [
            ModelTurn(tool_calls=[ToolCall("boxed", {}, call_id="s1")]),
            ModelTurn(text="done"),
        ]
        cj = RecordingToolJournal()
        classic, _, _ = await run_classic(
            ScriptedModel(list(turns)), [SandboxedTool("boxed")], "go", journal=cj, **BYPASS
        )
        pj = RecordingToolJournal()
        piped, _, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [SandboxedTool("boxed")],
            "go",
            journal=pj,
            **BYPASS,
        )
        assert classic.stopped_reason == piped.metrics["stopped_reason"]
        assert all(r["isolation"] == "NAMESPACE" for r in cj.rows)
        assert pj.rows == cj.rows


# ------------------------------------------------------------ facade contract


class TestFacadeContract:
    async def test_grant_set_covers_think_and_use_skill_only(self) -> None:
        assert {c.value for c in CODER_CAPABILITIES} == {"think", "use_skill"}

    async def test_ungranted_capabilities_fail_loudly(self) -> None:
        facade = CoderLoopFacade(
            model=ScriptedModel([]), tools={}, session=Session()
        )
        with pytest.raises(CapabilityNotGrantedError, match="recall"):
            await facade.recall("anything")
        with pytest.raises(CapabilityNotGrantedError, match="remember"):
            await facade.remember({"fact": "x"})

    async def test_think_refuses_schemas(self) -> None:
        facade = CoderLoopFacade(
            model=ScriptedModel([]), tools={}, session=Session()
        )
        with pytest.raises(InvalidCallParamsError, match="free-form"):
            await facade.think("hello", schema={"type": "object"})

    async def test_foreign_facade_is_refused(self) -> None:
        class NotTheCoderFacade:
            """Satisfies nothing in particular; that is the point."""

        task = PipelineTask(
            correlation_id=uuid.uuid4(), session_id=None, goal="x", params={}
        )
        from paa.core.errors import PaaError

        with pytest.raises(PaaError, match="CoderLoopFacade"):
            await CoderToolLoopPipeline().run(task, NotTheCoderFacade())  # type: ignore[arg-type]

    async def test_project_reads_refuse_honestly(self) -> None:
        from paa.core.errors import PaaError

        facade = CoderLoopFacade(
            model=ScriptedModel([]), tools={}, session=Session()
        )
        with pytest.raises(PaaError, match="no task lineage"):
            await facade.project()
        assert await facade.stage_question(None) is None  # type: ignore[arg-type]

    async def test_random_is_seeded_and_reproducible(self) -> None:
        run_id = uuid.uuid4()
        a = CoderLoopFacade(
            model=ScriptedModel([]), tools={}, session=Session(), run_id=run_id
        )
        b = CoderLoopFacade(
            model=ScriptedModel([]), tools={}, session=Session(), run_id=run_id
        )
        draws_a = [await a.random() for _ in range(3)]
        draws_b = [await b.random() for _ in range(3)]
        assert draws_a == draws_b


# --------------------------------------------------------------- CallJournal


class TestThinkJournals:
    async def test_think_rows_land_with_the_window_fingerprint(
        self, ledger: LedgerStore
    ) -> None:
        """think → PIPELINE_CALL_PROPOSED + COMPLETED on the run lineage."""
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("coder.tool-loop", "4.1.0")

        tool = RecordingTool("read_file")
        turns = [
            ModelTurn(tool_calls=[ToolCall("read_file", {"path": "a"}, call_id="k1")]),
            ModelTurn(text="done"),
        ]
        facade = CoderLoopFacade(
            model=ScriptedModel(list(turns)),
            tools={tool.name: tool},
            session=Session(),
            run_id=run_id,
            call_journal=journal,
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="go", params={}
        )
        result = await CoderToolLoopPipeline().run(task, facade)

        assert result.status is PipelineStatus.COMPLETED
        records = await read_call_journal(ledger, run_id)
        # Two thinks: the tool-call turn, then the final text turn.
        assert [r.seq for r in records] == [1, 2]
        assert [r.method for r in records] == ["think", "think"]
        assert all(r.outcome is CallOutcome.COMPLETED for r in records)
        first = records[0]
        assert first.fingerprint  # sha256 of the canonical {method, params}
        # The recorded result carries the model's proposed turn — the
        # value replay (C6.3) will serve back without re-invoking.
        assert isinstance(first.result, dict)
        assert first.result["tool_calls"][0]["name"] == "read_file"

    async def test_call_journal_kinds_on_the_lineage(
        self, ledger: LedgerStore
    ) -> None:
        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("coder.tool-loop", "4.1.0")
        facade = CoderLoopFacade(
            model=ScriptedModel([ModelTurn(text="hi")]),
            tools={},
            session=Session(),
            run_id=run_id,
            call_journal=journal,
        )
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="go", params={}
        )
        await CoderToolLoopPipeline().run(task, facade)
        await journal.run_completed("COMPLETED", metrics={"steps": 1})

        rows = await ledger.read_correlation(run_id)
        kinds = [e.event_type for e in rows]
        assert EventType.PIPELINE_RUN_STARTED.value in kinds
        assert kinds.count(EventType.PIPELINE_CALL_PROPOSED.value) == 1
        assert kinds.count(EventType.PIPELINE_CALL_COMPLETED.value) == 1
        assert EventType.PIPELINE_RUN_COMPLETED.value in kinds


# -------------------------------------------------------------------- params


class TestParams:
    async def test_max_steps_from_params(self) -> None:
        turns = [ModelTurn(tool_calls=[ToolCall("read_file", {})])] * 5
        piped, _, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("read_file")],
            "go",
            params={"max_steps": 2},
            **BYPASS,
        )
        assert piped.metrics["stopped_reason"] == "max_steps_reached"
        assert piped.metrics["steps"] == 2

    async def test_true_is_not_one(self) -> None:
        piped, _, _ = await run_pipeline(
            ScriptedModel([ModelTurn(text="ok")]), [], "go", params={"max_steps": True}
        )
        assert piped.metrics["stopped_reason"] == "model_finished"
        assert piped.metrics["steps"] == 1  # the default 25, not bool-1 abuse

    async def test_max_tokens_from_params(self) -> None:
        turns = [
            ModelTurn(text="working", tool_calls=[ToolCall("read_file", {})], tokens=900),
        ]
        piped, _, _ = await run_pipeline(
            ScriptedModel(list(turns)),
            [RecordingTool("read_file")],
            "go",
            params={"max_tokens": 500},
            **BYPASS,
        )
        assert piped.metrics["stopped_reason"] == "token_budget_exhausted"
        assert piped.metrics["tokens"] >= 500
