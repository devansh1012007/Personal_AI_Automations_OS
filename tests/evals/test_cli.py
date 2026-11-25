"""The ``paa eval`` doors, driven through Typer's CliRunner (C7.4).

Only the offline door is driven here (``tasks``): ``run`` fronts the
full matrix — real children, real sandbox, minutes of wall clock — and
driving it through the CLI would re-run the acceptance test behind a
different door for no new evidence. What this file pins is the wiring:
the group is registered on the ``paa`` app, the listing door finds the
repository's task set, and a refusal (here: a task set that is not one)
exits 2 and names the problem rather than crashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


class TestTheTasksDoor:
    def test_the_listing_names_every_task_with_its_probes(self) -> None:
        result = runner.invoke(app, ["eval", "tasks"])
        assert result.exit_code == 0, result.output
        for task_id in (
            "fizzbuzz-off-by-one",
            "greet-cli-flag",
            "inventory-report",
            "palindrome-guard",
            "reverse-util",
        ):
            assert task_id in result.output
        # The only task with both probes states them.
        assert "-create:1" in result.output
        assert "-change:1" in result.output

    def test_a_missing_task_set_is_a_refusal_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_EVAL_TASKS_DIR", str(tmp_path / "nowhere"))
        result = runner.invoke(app, ["eval", "tasks"])
        assert result.exit_code == 2
        assert "eval refused" in result.output

    def test_the_run_door_refuses_an_unknown_task_id(self) -> None:
        result = runner.invoke(app, ["eval", "run", "--tasks", "no-such-task"])
        assert result.exit_code == 2
        assert "unknown task id(s): no-such-task" in result.output
        assert "known tasks:" in result.output

    def test_the_run_door_refuses_all_in_company(self) -> None:
        result = runner.invoke(app, ["eval", "run", "--all", "--tasks", "reverse-util"])
        assert result.exit_code == 2
        assert "refuses company" in result.output
