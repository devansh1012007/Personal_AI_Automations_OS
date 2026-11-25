"""``paa observe`` as a command (C7.1).

The same throwaway-home discipline ``tests/cli/test_pipeline.py`` applies:
an isolated ``PAA_HOME``, the subprocess sandbox, a real runtime over a
real ledger — driven offline with a scripted fake harness, so the command
is proven as a command: exit codes, the bundle on disk, the refusal
paths. The spec's done-when ("a scripted run of a fake harness produces
a complete T1 bundle") is pinned here at the front door, not only in
the unit suites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    return home


class TestObserveCommand:
    def test_a_scripted_fake_harness_run_leaves_a_complete_t1_bundle(
        self, tmp_path: Path
    ) -> None:
        """The C7.1 done-when, driven through the CLI front door."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "existing.txt").write_text("before\n", encoding="utf-8")
        harness = (
            "import pathlib, sys; "
            "pathlib.Path('made.txt').write_text('made'); "
            "pathlib.Path('existing.txt').write_text('after'); "
            "print('harness spoke'); print('to stderr', file=sys.stderr)"
        )
        result = runner.invoke(
            app,
            ["observe", "--workspace", str(ws), "--", sys.executable, "-c", harness],
        )
        assert result.exit_code == 0, result.output
        assert "clean_exit" in result.output

        # The bundle is under PAA_HOME/observe/<run-id>/bundle.json and is
        # the complete T1 record.
        from paa.observe.bundle import read_bundle

        bundles = list((tmp_path / "home" / "observe").rglob("bundle.json"))
        assert len(bundles) == 1
        bundle = read_bundle(bundles[0])
        assert bundle.phase == "final"
        assert bundle.resume == "none"
        assert bundle.evidence_tier == "T1"
        assert [f["path"] for f in bundle.manifest["created"]] == ["made.txt"]
        assert [m["after"]["path"] for m in bundle.manifest["modified"]] == [
            "existing.txt"
        ]
        assert bundle.transcript_stdout is not None
        assert "harness spoke" in bundle.transcript_stdout.text
        assert bundle.transcript_stderr is not None
        assert "to stderr" in bundle.transcript_stderr.text
        assert bundle.classification["state"] == "clean_exit"

    def test_a_failed_harness_is_a_failed_subject_not_a_failed_observation(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            ["observe", "--workspace", str(ws), "--", sys.executable, "-c",
             "import sys; sys.exit(3)"],
        )
        assert result.exit_code == 1
        assert "error_exit" in result.output

    def test_json_prints_the_metrics(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            ["observe", "--json", "--workspace", str(ws), "--",
             sys.executable, "-c", "pass"],
        )
        assert result.exit_code == 0
        # structlog's backend-selection line rides the same captured
        # stdout; the JSON block is everything from its first brace.
        metrics = json.loads(result.output[result.output.index("{"):])
        assert metrics["state"] == "clean_exit"
        assert metrics["evidence_tier"] == "T1"
        assert metrics["resume"] == "none"
        assert metrics["bundle_path"].endswith("bundle.json")

    def test_a_malformed_env_is_refused_with_an_exit_code(self) -> None:
        result = runner.invoke(
            app, ["observe", "--env", "NOEQUALS", "--", sys.executable, "-c", "pass"]
        )
        assert result.exit_code == 2
        assert "KEY=VAL" in result.output

    @pytest.mark.parametrize(
        "assignment",
        [
            "PATH=/tmp/attacker",
            "path=/tmp/attacker",
            "LD_PRELOAD=/tmp/evil.so",
            "HOME=/tmp/attacker",
            "PYTHONPATH=/tmp/attacker",
        ],
    )
    def test_reserved_child_env_keys_are_refused(self, assignment: str) -> None:
        """`--env` may add variables, never undo the sandbox's sanitisation."""
        result = runner.invoke(
            app,
            ["observe", "--env", assignment, "--", sys.executable, "-c", "pass"],
        )
        assert result.exit_code == 2
        assert "reserved child key" in result.output

    def test_an_empty_command_is_refused(self) -> None:
        result = runner.invoke(app, ["observe", "--"])
        assert result.exit_code == 2  # typer: the argument is required

    def test_the_pipeline_trace_sees_the_observe_lineage(self, tmp_path: Path) -> None:
        """First-class in the registry: the run is traceable like any other."""
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            ["observe", "--workspace", str(ws), "--", sys.executable, "-c",
             "print('traced')"],
        )
        assert result.exit_code == 0
        # The correlation id is printed last; feed it to the trace door.
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        correlation = lines[-1].split()[-1]
        trace = runner.invoke(app, ["pipeline", "trace", correlation])
        assert trace.exit_code == 0
        assert "builtin.observe" in trace.output
        assert "observe.run" in trace.output

    def test_pipeline_run_drives_builtin_observe_too(self, tmp_path: Path) -> None:
        """The registry's own door produces the same kind of run."""
        import os

        ws = tmp_path / "ws2"
        ws.mkdir()
        cwd = os.getcwd()
        try:
            os.chdir(ws)
            result = runner.invoke(
                app,
                ["pipeline", "run", "builtin.observe", "--goal",
                 f"{sys.executable} -c pass"],
            )
        finally:
            os.chdir(cwd)
        assert result.exit_code == 0, result.output
        assert "clean_exit" in result.output
        assert "bundle:" in result.output
