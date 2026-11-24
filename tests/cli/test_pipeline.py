"""``paa pipeline list/inspect/run/trace`` through Typer's CliRunner (C6.5).

The same throwaway-home discipline ``tests/cli/test_cli.py`` applies to
``submit``: an isolated ``PAA_HOME``, the offline echo provider, and the
AUTO policy — a real runtime over a real ledger, driven offline, so the
commands are proven as commands (exit codes, tables, refusals), not as
functions.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

HAS_UVICORN = importlib.util.find_spec("uvicorn") is not None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pipeline-CLI test gets a throwaway home + offline echo."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    # Rich sizes its tables to the terminal; the runner's narrow default
    # truncates the builtin names mid-word. A wide explicit width keeps
    # the table assertions about content, not wrapping.
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()


_LOCAL_MANIFEST = """
[pipeline]
name = "tdd-loop"
version = "0.1.0"
description = "Write a failing test, make it pass."
requires_runtime = ">=4.1,<5"

[run]
command = ["python", "-m", "tdd_loop"]
resume = "journal"
"""


class TestListAndInspect:
    def test_list_names_the_three_builtins(self) -> None:
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        for name in (
            "builtin.plan-execute-critique",
            "builtin.chat",
            "coder.tool-loop",
        ):
            assert name in result.stdout
        assert "builtin" in result.stdout

    def test_list_names_off_contract_local_manifests(self) -> None:
        from paa.config import get_settings

        broken = get_settings().home / "pipelines" / "broken"
        broken.mkdir(parents=True)
        (broken / "pipeline.toml").write_text(
            "[banana]\nx = 1\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        assert "broken" in result.stdout
        assert "off-contract" in result.stdout

    def test_inspect_shows_the_declaration(self) -> None:
        result = runner.invoke(app, ["pipeline", "inspect", "builtin.chat"])
        assert result.exit_code == 0
        assert "builtin.chat" in result.stdout
        assert "context_provider" in result.stdout
        assert "recall_k" in result.stdout

    def test_inspect_unknown_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "inspect", "nope"])
        assert result.exit_code == 1
        assert "no registered pipeline" in result.stdout


class TestRun:
    def test_a_local_manifest_refuses_with_the_p3_reason(self) -> None:
        from paa.config import get_settings

        local = get_settings().home / "pipelines" / "tdd-loop"
        local.mkdir(parents=True)
        (local / "pipeline.toml").write_text(_LOCAL_MANIFEST, encoding="utf-8")
        result = runner.invoke(
            app, ["pipeline", "run", "tdd-loop", "--goal", "x"]
        )
        assert result.exit_code == 1
        assert "P3" in result.stdout

    def test_out_of_scope_refuses(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "coder.tool-loop", "--goal", "x",
                "--allow", "builtin.chat",
            ],
        )
        assert result.exit_code == 1
        assert "outside this mandate" in result.stdout

    def test_unknown_pipeline_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "run", "nope", "--goal", "x"])
        assert result.exit_code == 1
        assert "no registered pipeline" in result.stdout

    def test_a_widening_set_refuses(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--set", "recall_k=99999",
            ],
        )
        assert result.exit_code == 1
        assert "above the declared maximum" in result.stdout

    def test_chat_runs_offline_and_is_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = runner.invoke(
            app, ["pipeline", "run", "builtin.chat", "--goal", "hello there"]
        )
        assert run.exit_code == 0
        assert "hello there" in run.stdout  # the echo reply carries the message
        # The session id is printed for the trace.
        session = None
        for line in run.stdout.splitlines():
            if line.startswith("session:"):
                session = line.split("session:")[1].strip()
        assert session

        trace = runner.invoke(app, ["pipeline", "trace", session])
        assert trace.exit_code == 0
        assert "PIPELINE_RUN_STARTED" in trace.stdout
        assert "PIPELINE_RUN_COMPLETED" in trace.stdout
        assert "CHAT_TURN" in trace.stdout

    def test_trace_of_an_unknown_lineage_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "trace", str(uuid.uuid4())])
        assert result.exit_code == 1
        assert "no lineage" in result.stdout

    def test_coder_tool_loop_runs_offline(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "coder.tool-loop",
                "--goal", "say done and stop",
                "--mode", "bypass",
                "--set", "max_steps=2",
            ],
        )
        assert result.exit_code == 0
        assert "correlation:" in result.stdout
        assert "session:" in result.stdout

    def test_task_pipeline_runs_through_the_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same offline-plan patch submit's test uses: the echo model
        # proposes a plan when asked for one and echoes otherwise.
        from tests.cli.test_cli import _patch_offline_plan

        _patch_offline_plan(monkeypatch)
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.plan-execute-critique",
                "--goal", "write a greeting",
            ],
        )
        assert result.exit_code == 0
        assert "correlation:" in result.stdout
        assert "COMMITTED" in result.stdout
