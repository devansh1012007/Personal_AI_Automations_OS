"""The agentic tool-use loop: the gate ordering and the safety guarantees.

The whole loop is exercised with a scripted model and fake tools, so the
permission and hook behaviour that carries the safety properties is proven
deterministically, without a model server or a live tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from paa.coder.hooks import HookRegistry, deny_tool_hook
from paa.coder.loop import AgentLoop, ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.coder.session import Session


class ScriptedModel:
    """Emits a pre-programmed sequence of turns, one per propose() call."""

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = turns
        self.windows: list[list[dict]] = []

    async def propose(self, window: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
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


def _loop(model, tools, **kw) -> AgentLoop:  # noqa: ANN001, ANN003
    return AgentLoop(model, {t.name: t for t in tools}, session=Session(), **kw)


class TestBasicFlow:
    async def test_final_text_ends_the_loop(self) -> None:
        loop = _loop(ScriptedModel([ModelTurn(text="hello")]), [])
        result = await loop.run("hi")
        assert result.final_text == "hello"
        assert result.stopped_reason == "model_finished"
        assert result.tool_calls == 0

    async def test_tool_call_then_final(self) -> None:
        tool = RecordingTool("read_file", result="file contents")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("read_file", {"path": "a.py"})]),
                ModelTurn(text="I read it"),
            ]
        )
        loop = _loop(model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS))
        result = await loop.run("read a.py")

        assert result.tool_calls == 1
        assert result.final_text == "I read it"
        assert tool.calls == [{"path": "a.py"}]

    async def test_tool_result_is_fed_back_to_model(self) -> None:
        tool = RecordingTool("read_file", result="SECRET_CONTENT")
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("read_file", {})]), ModelTurn(text="ok")]
        )
        loop = _loop(model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS))
        await loop.run("go")
        # The model's second call saw the tool result in its window.
        second_window = model.windows[1]
        assert any("SECRET_CONTENT" in str(m.get("content", "")) for m in second_window)


class TestPermissionGate:
    async def test_denied_tool_does_not_execute_but_loop_continues(self) -> None:
        tool = RecordingTool("bash")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("bash", {"command": "rm -rf /"})]),
                ModelTurn(text="understood, I won't"),
            ]
        )
        loop = _loop(
            model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS, deny=["bash(rm*)"])
        )
        result = await loop.run("clean up")

        assert tool.calls == [], "denied tool must not execute"
        assert result.denied_calls == 1
        assert result.final_text == "understood, I won't"  # loop continued past the denial

    async def test_ask_without_confirm_callback_denies(self) -> None:
        tool = RecordingTool("bash")
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("bash", {"command": "ls"})]), ModelTurn(text="done")]
        )
        loop = _loop(model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.ASK))
        await loop.run("list files")
        assert tool.calls == [], "ASK with no UI callback must default to deny"

    async def test_ask_with_approving_confirm_executes(self) -> None:
        tool = RecordingTool("bash")
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("bash", {"command": "ls"})]), ModelTurn(text="done")]
        )

        async def approve(name, args, reason):  # noqa: ANN001, ANN202
            return True

        loop = _loop(
            model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.ASK), confirm=approve
        )
        await loop.run("list files")
        assert len(tool.calls) == 1


class TestHooks:
    async def test_pre_hook_can_block(self) -> None:
        tool = RecordingTool("send_email")
        hooks = HookRegistry()
        from paa.coder.hooks import HookEvent

        hooks.register(HookEvent.PRE_TOOL_USE, deny_tool_hook("send_email", "no outbound mail"))
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("send_email", {"to": "x"})]), ModelTurn(text="ok")]
        )
        loop = _loop(
            model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS), hooks=hooks
        )
        result = await loop.run("email someone")
        assert tool.calls == []
        assert result.denied_calls == 1

    async def test_pre_hook_can_modify_arguments(self) -> None:
        from paa.coder.hooks import HookContext, HookEvent, HookResult

        tool = RecordingTool("write_file")
        hooks = HookRegistry()

        async def inject(ctx: HookContext) -> HookResult:
            args = dict(ctx.mutable_arguments)
            args["path"] = "safe/" + args.get("path", "")
            return HookResult(modified_arguments=args)

        hooks.register(HookEvent.PRE_TOOL_USE, inject)
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("write_file", {"path": "x.py"})]), ModelTurn(text="ok")]
        )
        loop = _loop(
            model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS), hooks=hooks
        )
        await loop.run("write a file")
        assert tool.calls == [{"path": "safe/x.py"}]

    async def test_post_hook_observes_result(self) -> None:
        from paa.coder.hooks import HookContext, HookEvent, HookResult

        observed: list[Any] = []
        tool = RecordingTool("read_file", result="data")
        hooks = HookRegistry()

        async def observe(ctx: HookContext) -> HookResult:
            observed.append(ctx.result)
            return HookResult()

        hooks.register(HookEvent.POST_TOOL_USE, observe)
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("read_file", {})]), ModelTurn(text="ok")]
        )
        loop = _loop(
            model, [tool], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS), hooks=hooks
        )
        await loop.run("read")
        assert observed == ["data"]


class TestBounds:
    async def test_max_steps_stops_a_runaway(self) -> None:
        # A model that always wants to call a tool, never finishing.
        tool = RecordingTool("noop")

        class Loopy:
            async def propose(self, window, tools):  # noqa: ANN001, ANN202
                return ModelTurn(tool_calls=[ToolCall("noop", {})])

        loop = AgentLoop(
            Loopy(),
            {"noop": tool},
            session=Session(),
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
            max_steps=3,
        )
        result = await loop.run("go forever")
        assert result.stopped_reason == "max_steps_reached"
        assert result.steps == 3

    async def test_unknown_tool_is_reported_not_fatal(self) -> None:
        model = ScriptedModel(
            [ModelTurn(tool_calls=[ToolCall("does_not_exist", {})]), ModelTurn(text="oh well")]
        )
        loop = _loop(model, [], permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS))
        result = await loop.run("use a missing tool")
        assert result.final_text == "oh well"
        assert result.denied_calls == 1
