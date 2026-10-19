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

    def test_agent_handles_model_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timed-out or unavailable model must not dump a traceback.

        The agent still records every attempt in its transcript; this only
        cleans up the CLI output the operator sees. The run against a real
        provider is exercised separately.
        """
        from paa.models.base import ModelUnavailableError
        from paa.models.router import EscalatingModelRouter

        async def boom(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
            raise ModelUnavailableError("request to the model endpoint timed out")

        monkeypatch.setattr(EscalatingModelRouter, "complete_structured", boom)

        result = runner.invoke(app, ["agent", "tell me something"])
        assert result.exit_code == 1
        assert "error" in result.stdout.lower()

    def test_agent_catches_generic_paa_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A raised PaaError must be reported cleanly, not dumped."""
        from paa.coder.agent import CoderAgent
        from paa.core.errors import PaaError

        def boom(self: object, _prompt: str) -> None:  # pragma: no cover
            raise PaaError("boom: something went wrong")

        monkeypatch.setattr(CoderAgent, "ask", boom)

        result = runner.invoke(app, ["agent", "hello"])
        assert result.exit_code == 1
        assert "error" in result.stdout.lower()

    def test_agent_renders_a_blocked_escalation_readably(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escalation failure has to read as a diagnosis, not a dict dump.

        The router now carries the whole diagnosis on the error — which rule
        blocked the escalation, whether a frontier provider exists at all, and
        what the provider said when it failed. Rendering it as one flat
        ``key='value'`` line buries all three.
        """
        from paa.models.base import ModelUnavailableError
        from paa.models.router import EscalatingModelRouter

        monkeypatch.setenv("COLUMNS", "200")

        async def boom(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
            raise ModelUnavailableError(
                "local provider failed and escalation is not permitted",
                provider="openrouter",
                escalation_block_reason="modality_below_escalation_floor:COMPLEX",
                escalation_block_reasons=["modality_below_escalation_floor:COMPLEX"],
                underlying_error="ModelUnavailableError",
                underlying_message="model endpoint returned HTTP 429",
                underlying_status_code=429,
                frontier_configured=False,
                escalation_unavailable_reason="no_frontier_configured",
                modality="STANDARD",
                permission_mode="ASK",
                policy_reason="modality_below_escalation_floor:COMPLEX",
                escalation_allowed=False,
            )

        monkeypatch.setattr(EscalatingModelRouter, "complete_structured", boom)

        result = runner.invoke(app, ["agent", "hello"])

        assert result.exit_code == 1
        assert "escalation blocked: modality_below_escalation_floor:COMPLEX" in result.stdout
        assert "escalation unavailable: no_frontier_configured" in result.stdout
        assert "provider failure: model endpoint returned HTTP 429" in result.stdout
        assert "modality STANDARD (floor COMPLEX)" in result.stdout
        assert "permission mode ASK" in result.stdout
        # The noise keys are implied by the block; repeating them is noise.
        assert "policy_reason" not in result.stdout


class TestAgentModality:
    def test_the_modality_flag_reaches_the_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag is only worth having if it changes what the router is told.

        ``CoderAgent.build`` forwards the modality to the router adapter, and
        the router compares it against the escalation floor. A CLI that
        accepted the flag and dropped it would look like it worked.
        """
        from paa.core.types import ComplexityModality
        from paa.models.router import EscalatingModelRouter

        seen: list[object] = []

        async def capture(
            self: object, *args: object, **kwargs: object
        ) -> dict[str, object]:
            seen.append(kwargs.get("modality"))
            return {"text": "ok", "tool_calls": []}

        monkeypatch.setattr(EscalatingModelRouter, "complete_structured", capture)

        result = runner.invoke(app, ["agent", "--modality", "COMPLEX", "hello"])

        assert result.exit_code == 0
        assert seen == [ComplexityModality.COMPLEX]


class TestAgentSessions:
    """Continuity between invocations.

    Every ``paa agent`` run used to start a brand-new session, so the model
    had no memory of the previous conversation: "my name is what?" was a
    question with no context to answer it from.
    """

    def test_continue_without_a_previous_session_fails_cleanly(self) -> None:
        result = runner.invoke(app, ["agent", "--continue", "hello"])

        assert result.exit_code == 1
        assert "no previous session" in result.stdout

    def test_continue_resumes_the_most_recent_transcript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from paa.coder.session import Session

        monkeypatch.setenv("COLUMNS", "200")
        sessions = Path(os.environ["PAA_HOME"]) / "sessions"
        older = Session("zzz-older", transcript_dir=sessions)
        older.user("the older conversation")
        newer = Session("aaa-newer", transcript_dir=sessions)
        newer.user("the newer conversation")
        # The newest transcript is the one that sorts *first* by id, so a
        # lexicographic "latest" would pick the wrong file.
        os.utime(sessions / "zzz-older.jsonl", (1_000_000, 1_000_000))
        os.utime(sessions / "aaa-newer.jsonl", (2_000_000, 2_000_000))
        transcript = sessions / "aaa-newer.jsonl"
        before = transcript.stat().st_size

        result = runner.invoke(app, ["agent", "--continue", "what did we discuss?"])

        assert result.exit_code == 0
        assert "continued session aaa-newer" in result.stdout
        assert "paa agent --resume aaa-newer" in result.stdout
        # The resumed run appended to the same transcript.
        assert transcript.stat().st_size > before
        assert len(Session.resume("aaa-newer", sessions).turns) == 3


def _extract_cid(text: str) -> str:
    for line in text.splitlines():
        if "correlation:" in line:
            return line.split("correlation:")[1].strip().split()[-1]
    raise AssertionError(f"no correlation id in output:\n{text}")
