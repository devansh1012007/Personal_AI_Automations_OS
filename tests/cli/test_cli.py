"""The ``paa`` CLI, driven through Typer's CliRunner.

These are synchronous tests: each command runs its own ``asyncio.run`` internally
and builds a real Runtime against an isolated PAA_HOME with the echo provider, so
they exercise the actual wiring end-to-end without a model server.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI test gets a throwaway home + offline echo provider."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()


class TestSimpleCommands:
    def test_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "paa" in result.stdout

    def test_help_lists_core_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in ("submit", "status", "ledger", "recover", "gate", "doctor", "serve"):
            assert cmd in result.stdout

    def test_mode_valid(self) -> None:
        result = runner.invoke(app, ["mode", "lockdown"])
        assert result.exit_code == 0
        assert "PAA_POLICY__MODE=LOCKDOWN" in result.stdout

    def test_mode_invalid(self) -> None:
        result = runner.invoke(app, ["mode", "nonsense"])
        assert result.exit_code == 1


@pytest.mark.slow
class TestTaskLifecycle:
    def test_doctor_reports_backends(self) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "sqlite" in result.stdout
        assert "sandbox" in result.stdout

    def test_submit_runs_to_committed(self) -> None:
        result = runner.invoke(app, ["submit", "write a greeting", "--agent", "worker"])
        assert result.exit_code == 0
        assert "correlation:" in result.stdout
        assert "COMMITTED" in result.stdout

    def test_submit_then_status_and_ledger(self) -> None:
        submit = runner.invoke(app, ["submit", "do a thing", "--agent", "worker"])
        assert submit.exit_code == 0
        cid = _extract_cid(submit.stdout)

        status = runner.invoke(app, ["status", cid])
        assert status.exit_code == 0
        assert "phase" in status.stdout

        ledger = runner.invoke(app, ["ledger", cid])
        assert ledger.exit_code == 0
        assert "TASK_REQUESTED" in ledger.stdout
        assert "MUTATION_COMMITTED" in ledger.stdout
        assert "ok" in ledger.stdout  # chain integrity

    def test_recover_on_clean_home(self) -> None:
        result = runner.invoke(app, ["recover"])
        assert result.exit_code == 0
        assert "scanned" in result.stdout


class TestErrorHandling:
    def test_status_invalid_uuid(self) -> None:
        result = runner.invoke(app, ["status", "not-a-uuid"])
        assert result.exit_code == 1

    @pytest.mark.slow
    def test_status_unknown_task(self) -> None:
        import uuid

        result = runner.invoke(app, ["status", str(uuid.uuid4())])
        assert result.exit_code == 1
        assert "no such task" in result.stdout


def _extract_cid(text: str) -> str:
    for line in text.splitlines():
        if "correlation:" in line:
            return line.split("correlation:")[1].strip().split()[-1]
    raise AssertionError(f"no correlation id in output:\n{text}")
