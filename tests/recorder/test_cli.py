"""``paa record`` as commands (C7.3): exit codes, artifacts, refusals.

The throwaway-home discipline the observe and pipeline CLI suites use:
an isolated ``PAA_HOME``, the subprocess sandbox, real children — the
spec's done-when driven through the front door a human would type, not
only the core's Python API.
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


class TestRecordRun:
    def test_the_spec_front_door_produces_the_full_artifact_set(
        self, tmp_path: Path
    ) -> None:
        """``paa record run -- <cmd>``: record, observe bundle, attested bundle."""
        ws = tmp_path / "ws"
        ws.mkdir()
        harness = (
            "import pathlib; "
            "pathlib.Path('solo.txt').write_text('harness hands'); "
            "print('harness spoke')"
        )
        result = runner.invoke(
            app,
            [
                "record", "run",
                "--workspace", str(ws),
                "--timeout-s", "60",
                "--env", "PAA_SANDBOX__BACKEND=subprocess",
                "--", sys.executable, "-c", harness,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "clean_exit" in result.output

        home = Path(__import__("os").environ["PAA_HOME"])
        record_dirs = list((home / "record").iterdir())
        assert len(record_dirs) == 1
        run_dir = record_dirs[0]
        assert (run_dir / "record.jsonl").exists()
        assert (run_dir / "observe-bundle.json").exists()
        # The attested bundle is cited in the run's output and exists.
        attest_dirs = list((home / "attest").glob("record-*"))
        assert len(attest_dirs) == 1

    def test_a_crashed_harness_exits_one_and_still_records(
        self, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            [
                "record", "run",
                "--workspace", str(ws),
                "--timeout-s", "60",
                "--", sys.executable, "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
            ],
        )
        assert result.exit_code == 1
        assert "signalled" in result.output

    def test_the_json_door_names_every_artifact(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            [
                "record", "run", "--json",
                "--workspace", str(ws),
                "--timeout-s", "60",
                "--no-attest",
                "--", sys.executable, "-c", "print('x')",
            ],
        )
        assert result.exit_code == 0, result.output
        # The sandbox's own logs ride stderr (CliRunner mixes it in), so
        # the payload is read from its opening brace — the observe CLI
        # suite's own discipline.
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["state"] == "clean_exit"
        assert payload["attest_bundle"] is None
        assert payload["run_dir"].endswith(payload["run_id"])

    def test_a_bad_mode_is_refused_before_anything_runs(
        self, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app,
            ["record", "run", "--mode", "yolo", "--", "true"],
        )
        assert result.exit_code == 2
        assert "yolo" in result.output

    def test_a_bad_env_spec_is_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["record", "run", "--env", "NO_EQUALS_SIGN", "--", "true"],
        )
        assert result.exit_code == 2


class TestRecordVerify:
    def _run(self, tmp_path: Path) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        result = runner.invoke(
            app,
            [
                "record", "run", "--json",
                "--workspace", str(ws),
                "--timeout-s", "60",
                "--no-attest",
                "--", sys.executable, "-c", "print('x')",
            ],
        )
        assert result.exit_code == 0, result.output
        return Path(json.loads(result.output[result.output.index("{"):])["run_dir"])

    def test_a_sound_record_verifies_green(self, tmp_path: Path) -> None:
        run_dir = self._run(tmp_path)
        result = runner.invoke(app, ["record", "verify", str(run_dir)])
        assert result.exit_code == 0, result.output
        assert "final" in result.output

    def test_a_tampered_record_verifies_red(self, tmp_path: Path) -> None:
        run_dir = self._run(tmp_path)
        lines = (run_dir / "record.jsonl").read_text().splitlines()
        event = json.loads(lines[1])
        event["data"]["mode"] = "bypass"
        lines[1] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        (run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        result = runner.invoke(app, ["record", "verify", str(run_dir)])
        assert result.exit_code == 1
        assert "digest" in result.output

    def test_the_json_door_reports_the_histogram(self, tmp_path: Path) -> None:
        run_dir = self._run(tmp_path)
        result = runner.invoke(app, ["record", "verify", "--json", str(run_dir)])
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["ok"] is True
        assert payload["histogram"]["run_started"] == 1
        assert payload["histogram"]["record_finalised"] == 1


class TestRecordProxy:
    def test_a_missing_run_dir_is_refused_cleanly(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            ["record", "proxy", "--run-dir", str(tmp_path / "nowhere")],
        )
        assert result.exit_code == 2
