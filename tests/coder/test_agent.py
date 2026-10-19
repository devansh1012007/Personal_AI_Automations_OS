"""The coder agent as a composition: real loop, real runtime, scripted model.

Nothing here touches a provider. What is exercised is the wiring this repo was
missing — a :class:`~paa.runtime.Runtime` handed to the tools, the tool loop
driving them, and the CLI command that puts a human in front of it. The model is
the only seam that is doubled, and it is doubled exactly the way
:meth:`~paa.runtime.Runtime.build` allows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from paa.cli.main import app
from paa.coder.agent import CoderAgent
from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode
from paa.coder.session import Session, Turn, TurnRole
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

    def test_unknown_modality_is_rejected_before_settings_are_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad flag must not cost a runtime build.

        The settings are what the runtime is built from, so a validation that
        happens after they are loaded has already paid for the thing it was
        meant to prevent.
        """
        import paa.config

        def _boom() -> Settings:  # pragma: no cover - must never be reached
            raise AssertionError("settings were loaded before the modality was validated")

        monkeypatch.setattr(paa.config, "get_settings", _boom)

        result = CliRunner().invoke(app, ["agent", "hi", "--modality", "bogus"])

        assert result.exit_code == 1
        assert "unknown modality" in result.output

    def test_resume_and_continue_are_mutually_exclusive(self) -> None:
        result = CliRunner().invoke(app, ["agent", "hi", "--resume", "abc", "--continue"])

        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_help_lists_the_session_and_modality_flags(self) -> None:
        result = CliRunner().invoke(app, ["agent", "--help"])

        assert result.exit_code == 0
        assert "--continue" in result.output
        assert "--modality" in result.output


class TestLatestSession:
    """Which transcript is the most recent one.

    The ordering is by the transcript's own last-turn ``created_at``, and
    only falls back to the file's mtime for a transcript whose turns cannot
    be read. Session ids are UUID hex, so the lexicographic order
    :meth:`Session.list_sessions` returns says nothing about time — which is
    exactly why ``--continue`` cannot use it. The mtime is not enough on its
    own either: it has one-second granularity on ext3, HFS+ and exFAT, so two
    runs in the same second tie and the id breaks the tie at random.
    """

    def test_the_newest_transcript_wins(self, tmp_path: Path) -> None:
        older = tmp_path / "zzz.jsonl"
        newer = tmp_path / "aaa.jsonl"
        older.write_text("{}\n", encoding="utf-8")
        newer.write_text("{}\n", encoding="utf-8")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        # The newest is the one that sorts *first* by id, so a lexicographic
        # "latest" would answer "zzz" and be wrong.
        assert Session.latest_session(tmp_path) == "aaa"

    def test_a_missing_directory_has_no_latest_session(self, tmp_path: Path) -> None:
        assert Session.latest_session(tmp_path / "does-not-exist") is None

    def test_an_empty_directory_has_no_latest_session(self, tmp_path: Path) -> None:
        assert Session.latest_session(tmp_path) is None

    def test_files_that_are_not_transcripts_are_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "transcript.jsonl.bak").write_text("{}\n", encoding="utf-8")

        assert Session.latest_session(tmp_path) is None

    def test_the_turn_timestamp_beats_the_mtime(self, tmp_path: Path) -> None:
        """Same mtime, different turn timestamps — the newer turn wins.

        This is the bug the mtime-only ordering had: the filesystem's mtime
        has one-second granularity on ext3, HFS+ and exFAT, so two runs in
        the same second tie, and the tie-break is the session id — a random
        UUID hex. The transcript's own ``created_at`` is microsecond
        resolution and already on disk, so it is the signal that can tell
        the two runs apart.
        """
        # The ids are chosen so the mtime-only tie-break answers "zzz" —
        # the *older* transcript — and the correct answer is "aaa".
        older = _transcript(tmp_path, "zzz", "2026-01-01T00:00:00.000000+00:00")
        newer = _transcript(tmp_path, "aaa", "2026-02-02T00:00:00.000000+00:00")
        for path in (older, newer):
            os.utime(path, (1_000_000, 1_000_000))

        assert Session.latest_session(tmp_path) == "aaa"

    def test_a_torn_last_line_still_yields_the_session(self, tmp_path: Path) -> None:
        """A crash mid-write must not hide the session.

        The final line is a partial JSON object — the write was interrupted
        — so the last *good* turn is the one that says when the session was
        active. The file's own mtime is older than the other transcript's,
        so a mtime-only ordering would pick the wrong file.
        """
        torn = _transcript(tmp_path, "torn", "2026-03-03T00:00:00.000000+00:00")
        with torn.open("a", encoding="utf-8") as fh:
            fh.write('{"role": "user", "content": "half a tur')
        other = _transcript(tmp_path, "other", "2026-01-01T00:00:00.000000+00:00")
        os.utime(torn, (1_000_000, 1_000_000))
        os.utime(other, (2_000_000, 2_000_000))

        assert Session.latest_session(tmp_path) == "torn"

    def test_a_full_tie_breaks_on_the_id_not_the_glob_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identical mtime *and* identical timestamp: still one answer.

        The tie-break is the session id, so the answer is the same on every
        call and does not depend on the order the filesystem hands the
        directory back.
        """
        _transcript(tmp_path, "aaa", "2026-04-04T00:00:00.000000+00:00")
        _transcript(tmp_path, "zzz", "2026-04-04T00:00:00.000000+00:00")
        for path in tmp_path.glob("*.jsonl"):
            os.utime(path, (1_000_000, 1_000_000))

        first = Session.latest_session(tmp_path)
        assert first == "zzz"  # the id breaks the tie, and it is deterministic

        original_glob = Path.glob

        def _reversed(self: Path, pattern: str) -> list[Path]:
            return list(reversed(list(original_glob(self, pattern))))

        monkeypatch.setattr(Path, "glob", _reversed)

        assert Session.latest_session(tmp_path) == first


def _transcript(directory: Path, session_id: str, created_at: str) -> Path:
    """A one-turn transcript whose turn carries an explicit timestamp."""
    session = Session(session_id, transcript_dir=directory)
    session.append(
        Turn(TurnRole.USER, "hello", created_at=created_at),
    )
    return directory / f"{session_id}.jsonl"
