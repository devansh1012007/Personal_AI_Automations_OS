"""The ``paa pipeline test`` door, driven through Typer's CliRunner (C9.3).

P3.5's conformance door: the six-check standard against a named registered
pipeline, on a throwaway ledger, rendered with the standard's own renderer.
What these tests pin is the operator-facing contract: the exit codes
(0 all green / 1 a check failed or an unknown pipeline), the help text
(read through ``_plain`` — the runner's Rich-styled help wraps tokens
mid-token, the class the repo has met three times), and the honest
no-runner report a registered-but-uncovered pipeline earns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _plain(output: str) -> str:
    """Rendered CLI output as content: no escape sequences, no line breaks."""
    return _ANSI.sub("", output).replace("\r", "").replace("\n", "")


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Every test gets a throwaway home; the operator's is never touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    monkeypatch.setenv("PAA_OBSERVABILITY__LOG_LEVEL", "WARNING")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    yield home
    reset_settings_cache()


class TestTheHelp:
    def test_help_names_the_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "test", "--help"])
        assert result.exit_code == 0
        help_text = _plain(result.stdout)
        assert "six-check" in help_text
        assert "conformance" in help_text
        assert "all" in help_text


class TestTheExitContract:
    def test_a_green_pipeline_exits_zero(self) -> None:
        result = runner.invoke(app, ["pipeline", "test", "builtin.chat"])
        assert result.exit_code == 0, result.stdout
        assert "builtin.chat: GREEN" in _plain(result.stdout)

    def test_an_unknown_pipeline_exits_one_and_names_the_registered(self) -> None:
        result = runner.invoke(app, ["pipeline", "test", "no.such.pipeline"])
        assert result.exit_code == 1
        plain = _plain(result.stdout)
        assert "no.such.pipeline" in plain
        # The registered set is named so the typo is fixable from the output.
        assert "builtin.chat" in plain

    def test_all_runs_every_runnable_and_exits_zero(self) -> None:
        result = runner.invoke(app, ["pipeline", "test", "all"])
        assert result.exit_code == 0, result.stdout
        plain = _plain(result.stdout)
        assert "builtin.chat: GREEN" in plain
        assert "coder.tool-loop: GREEN" in plain


class TestTheHonestyRules:
    def test_a_registered_pipeline_without_a_runner_reads_red(self) -> None:
        """The no-runner report is RED, never missing (the standard's rule).

        A personal-declaration overlay registers a pipeline nobody wrote a
        runner for; the door must run it through the same six-check
        standard and fail honestly.
        """
        import os

        pipelines = Path(os.environ["PAA_HOME"]) / "pipelines" / "local.uncovered"
        pipelines.mkdir(parents=True)
        (pipelines / "pipeline.toml").write_text(
            '[pipeline]\n'
            'name = "local.uncovered"\n'
            'version = "0.1.0"\n'
            'protocol = "paa-pipeline/1"\n'
            'description = "A fixture nobody wrote a runner for."\n'
            'requires_runtime = ">=4.1,<5"\n'
            "\n"
            "[run]\n"
            "command = [\"python\", \"-m\", \"uncovered\"]\n"
            "resume = \"none\"\n"
            "\n"
            "[limits]\n"
            "max_tokens = 1000\n"
            "max_wall_s = 10\n"
            "max_calls = 50\n"
            "max_sub_runs = 1\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["pipeline", "test", "local.uncovered"])
        assert result.exit_code == 1
        plain = _plain(result.stdout)
        assert "local.uncovered: RED" in plain
        assert "no conformance runner registered" in plain
