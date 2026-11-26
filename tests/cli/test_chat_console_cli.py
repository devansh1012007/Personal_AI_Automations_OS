"""The ``paa chat --console`` door, driven through Typer's CliRunner (C8.5).

The Loop Console is a screen of the chat TUI, and ``--console`` is the
flag that opens the app on it (loop management first, Esc climbing back
to the conversation). The TUI itself is pilot-tested in
:mod:`tests.tui.test_console_app` — this file pins the CLI seam the way
:mod:`tests.cli.test_skills_cli` pins the ``paa skills`` door: the flag
is registered and documented, and its one refusal — combining it with
``--plain``, a loop that has no screens — exits 2 with a message that
names the CLI verbs the plain operator has instead.

The bare ``paa chat`` (and ``paa chat --console``) happy path is not
invoked here: it would start the real TUI inside the runner. The
start-on-console behaviour it wires is what the pilot suite pins, through
``ChatApp(wiring, start_on_console=True)``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Every test gets a throwaway home + the offline stack.

    ``PAA_OBSERVABILITY__LOG_LEVEL`` is raised to WARNING because the root
    CLI callback configures structlog per settings — the suite-level
    ``logging.disable`` cannot reach the handler the callback installs
    inside the CliRunner's in-process run, and the tests assert on stdout.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
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


class TestChatConsoleFlag:
    def test_help_names_the_console_flag(self) -> None:
        result = runner.invoke(app, ["chat", "--help"])
        assert result.exit_code == 0
        assert "--console" in result.stdout
        # The help carries the row's own vocabulary, not a private one.
        assert "Loop Console" in result.stdout
        assert "conformance" in result.stdout

    def test_console_refuses_the_plain_combination(self) -> None:
        """A screen flag on the screen-less loop is a usage error, exit 2."""
        result = runner.invoke(app, ["chat", "--console", "--plain"])
        assert result.exit_code == 2
        assert "--console is a TUI screen" in result.stdout
        # The refusal names the doors the plain operator has instead.
        assert "paa skills" in result.stdout
        assert "paa pipeline" in result.stdout
