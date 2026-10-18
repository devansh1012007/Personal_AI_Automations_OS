"""The coder agent as a composition: real loop, real runtime, scripted model.

Nothing here touches a provider. What is exercised is the wiring this repo was
missing — a :class:`~paa.runtime.Runtime` handed to the tools, the tool loop
driving them, and the CLI command that puts a human in front of it. The model is
the only seam that is doubled, and it is doubled exactly the way
:meth:`~paa.runtime.Runtime.build` allows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from paa.cli.main import app
from paa.coder.agent import CoderAgent
from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode
from paa.config import Settings


class ScriptedModel:
    """Emits a pre-programmed sequence of turns, one per propose() call."""

    def __init__(self, *turns: ModelTurn) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls += 1
        return self._turns.pop(0) if self._turns else ModelTurn(text="nothing left to do")


class LoopingModel:
    """Always proposes another tool call, so nothing but ``max_steps`` stops it."""

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        return ModelTurn(text="again", tool_calls=[ToolCall("check_runtime", {})])


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated ``PAA_HOME`` so the agent's runtime never touches real state."""
    root = tmp_path / "paa_home"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def agent_settings(home: Path) -> Settings:
    settings = Settings(home=home)
    settings.ensure_directories()
    return settings


class TestAgentTurn:
    async def test_runs_tools_then_finishes(self, agent_settings: Settings) -> None:
        model = ScriptedModel(
            ModelTurn(
                text="checking the runtime",
                tool_calls=[ToolCall("check_runtime", {})],
            ),
            ModelTurn(text="The ledger has no open tasks."),
        )
        agent = await CoderAgent.build(
            agent_settings, model=model, mode=PermissionMode.BYPASS
        )
        try:
            result = await agent.ask("what is live?")
        finally:
            await agent.close()

        assert result.stopped_reason == "model_finished"
        assert result.tool_calls == 1
        assert result.final_text == "The ledger has no open tasks."

    async def test_tool_result_reaches_the_transcript(
        self, agent_settings: Settings
    ) -> None:
        """The observation has to land in the session, or the next turn is blind."""

        model = ScriptedModel(ModelTurn(tool_calls=[ToolCall("check_runtime", {})]))
        agent = await CoderAgent.build(
            agent_settings, model=model, mode=PermissionMode.BYPASS
        )
        try:
            await agent.ask("what is live?")
            results = [
                t for t in agent.session.turns if t.role.value == "tool_result"
            ]
        finally:
            await agent.close()

        assert len(results) == 1
        assert "open_tasks" in results[0].content

    async def test_the_runtime_is_really_wired(self, agent_settings: Settings) -> None:
        agent = await CoderAgent.build(
            agent_settings, model=ScriptedModel(), mode=PermissionMode.BYPASS
        )
        try:
            assert agent.runtime.ledger is not None
            assert agent.runtime.orchestrator is not None
            assert set(agent.tools) >= {"check_runtime", "read_ledger", "run_task"}
        finally:
            await agent.close()

    async def test_transcript_is_written_under_paa_home(self, agent_settings: Settings) -> None:
        agent = await CoderAgent.build(
            agent_settings, model=ScriptedModel(), mode=PermissionMode.BYPASS
        )
        try:
            await agent.ask("hello")
            session_id = agent.session.session_id
        finally:
            await agent.close()

        assert (agent_settings.home / "sessions" / f"{session_id}.jsonl").exists()

    async def test_max_steps_bounds_an_endless_loop(self, agent_settings: Settings) -> None:
        """Every turn proposes another tool call; ``max_steps`` is the stop."""

        agent = await CoderAgent.build(
            agent_settings, model=LoopingModel(), mode=PermissionMode.BYPASS, max_steps=3
        )
        try:
            result = await agent.ask("loop forever")
        finally:
            await agent.close()

        assert result.stopped_reason == "max_steps_reached"
        assert result.steps == 3
        assert result.tool_calls == 3

    async def test_plan_mode_refuses_the_mutating_tools(self, agent_settings: Settings) -> None:
        model = ScriptedModel(
            ModelTurn(tool_calls=[ToolCall("submit_task", {"goal": "do a thing"})]),
            ModelTurn(text="I cannot submit in plan mode."),
        )
        agent = await CoderAgent.build(
            agent_settings, model=model, mode=PermissionMode.PLAN
        )
        try:
            result = await agent.ask("submit a task")
        finally:
            await agent.close()

        denied = [t for t in agent.session.turns if t.role.value == "tool_result"]
        assert result.denied_calls == 1
        assert "denied" in denied[0].content

    async def test_an_explicit_deny_wins_over_bypass(self, agent_settings: Settings) -> None:
        model = ScriptedModel(
            ModelTurn(tool_calls=[ToolCall("run_task", {"correlation_id": "x"})]),
            ModelTurn(text="stopped."),
        )
        agent = await CoderAgent.build(
            agent_settings,
            model=model,
            mode=PermissionMode.BYPASS,
            deny=["run_task"],
        )
        try:
            result = await agent.ask("run it")
        finally:
            await agent.close()

        assert result.denied_calls == 1

    async def test_no_confirm_callback_means_no_silent_approval(
        self, agent_settings: Settings
    ) -> None:
        """Default ASK posture with no UI must stop, not proceed."""

        model = ScriptedModel(
            ModelTurn(tool_calls=[ToolCall("submit_task", {"goal": "g"})]),
            ModelTurn(text="halted."),
        )
        agent = await CoderAgent.build(agent_settings, model=model)
        try:
            result = await agent.ask("submit a task")
        finally:
            await agent.close()

        assert result.denied_calls == 1


class TestCli:
    def test_command_is_registered(self) -> None:
        result = CliRunner().invoke(app, ["agent", "--help"])

        assert result.exit_code == 0
        assert "--max-steps" in result.output

    def test_unknown_mode_is_rejected_before_building_anything(self) -> None:
        result = CliRunner().invoke(app, ["agent", "hi", "--mode", "yolo"])

        assert result.exit_code == 1
        assert "unknown mode" in result.output