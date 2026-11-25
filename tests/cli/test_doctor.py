"""The ``paa doctor`` chat/MCP rows (SPEC 2 §3.1 C4.6).

The doctor's charter is "never overstates": every row reports what is live,
not what configuration wishes were live. These tests pin the three product
surface rows to that charter:

* **chat model** — ``echo`` is the declared offline fallback and always
  passes; any other registry row is resolved through
  :func:`paa.models.registry.resolve_spec` (a dict lookup, deliberately not a
  network call) and its API key is reported by env-var *name* only — a test
  asserts the value never reaches the output.
* **mcp token env** — the configured ``PAA_MCP__TOKEN_ENV`` variable is
  reported set/unset without its value, with the unset case naming the
  spawn-pipe trust boundary.
* **chat tui** — a ``find_spec`` presence probe for Textual. The probe is
  monkeypatched both ways: the row must render the install hint when the
  extra is absent and must never fail the doctor in either state.

Each invocation drives the real command, which builds a real Runtime against
an isolated ``PAA_HOME`` — the same wiring the rest of the CLI suite exercises.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a throwaway home, the echo provider, and clean chat/MCP env."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    # A wide console so the table renders each cell on one line: assertions
    # pin row *content*, and a wrapped cell would split a phrase across the
    # box-drawing borders (the same trick the escalation-rendering tests use).
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("PAA_CHAT__MODEL", raising=False)
    monkeypatch.delenv("PAA_MCP__TOKEN_ENV", raising=False)
    monkeypatch.delenv("PAA_MCP_TOKEN", raising=False)
    from paa.config import reset_settings_cache

    reset_settings_cache()


def _invoke(monkeypatch: pytest.MonkeyPatch, *, textual: bool) -> str:
    """Run ``paa doctor`` with the Textual presence probe forced to ``textual``.

    The probe is patched on :mod:`importlib.util` — the exact seam the doctor
    uses — so the row is deterministic regardless of how this environment is
    installed. The real :func:`importlib.util.find_spec` is kept for every
    other name, because the runtime build itself probes optional packages.
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "textual":
            # The doctor only asks "is it there?", so a truthy sentinel is
            # enough — the spec object itself is never touched.
            return object() if textual else None
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type,misc]

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.mark.slow
class TestChatModelRow:
    """The chat model row: echo declared, registry resolution, key by name."""

    def test_the_echo_default_is_the_declared_offline_fallback(self) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "echo (offline fallback declared" in result.stdout
        # The offline-first claim is about the whole product: no network, no key.
        assert "no network" in result.stdout

    def test_a_known_hosted_model_reports_a_set_key_by_name_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_CHAT__MODEL", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
        from paa.config import reset_settings_cache

        reset_settings_cache()

        out = _invoke(monkeypatch, textual=True)

        assert "anthropic ok" in out
        assert "ANTHROPIC_API_KEY set" in out
        # The charter: the name is reportable, the value never is.
        assert "sk-ant-not-a-real-key" not in out

    def test_a_known_hosted_model_with_the_key_unset_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_CHAT__MODEL", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from paa.config import reset_settings_cache

        reset_settings_cache()

        out = _invoke(monkeypatch, textual=True)

        assert "anthropic ok" in out
        assert "ANTHROPIC_API_KEY unset" in out

    def test_an_unknown_model_is_reported_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo in ``PAA_CHAT__MODEL`` is a doctor row, not a traceback.

        The doctor's whole point is to survive a misconfigured deployment and
        describe it; the unresolved row names the error, which carries the
        list of known providers.
        """
        monkeypatch.setenv("PAA_CHAT__MODEL", "not-a-provider")
        from paa.config import reset_settings_cache

        reset_settings_cache()

        out = _invoke(monkeypatch, textual=True)

        assert "unresolved" in out
        assert "not-a-provider" in out

    def test_a_local_row_reports_that_no_key_is_needed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_CHAT__MODEL", "ollama")
        from paa.config import reset_settings_cache

        reset_settings_cache()

        out = _invoke(monkeypatch, textual=True)

        assert "ollama ok" in out
        assert "no key env" in out


@pytest.mark.slow
class TestMCPTokenRow:
    """The MCP token row: set/unset by name, with the unset reminder."""

    def test_the_default_token_env_unset_names_the_spawn_pipe_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _invoke(monkeypatch, textual=True)

        assert "PAA_MCP_TOKEN unset" in out
        assert "spawn pipe is the trust boundary" in out

    def test_a_configured_token_env_that_is_set_is_reported_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_MCP__TOKEN_ENV", "MY_HARNESS_TOKEN")
        monkeypatch.setenv("MY_HARNESS_TOKEN", "super-secret-token-value")
        from paa.config import reset_settings_cache

        reset_settings_cache()

        out = _invoke(monkeypatch, textual=True)

        assert "MY_HARNESS_TOKEN set" in out
        assert "super-secret-token-value" not in out


@pytest.mark.slow
class TestChatTuiRow:
    """The Textual row: a presence probe that can never fail the doctor."""

    def test_installed_reports_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = _invoke(monkeypatch, textual=True)

        assert "textual installed" in out

    def test_missing_is_a_pass_with_the_plain_fallback_note(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row renders the escape hatch, and the doctor still exits 0.

        ``paa[tui]`` is escaped before it reaches the table — rich would
        otherwise eat the bracketed extra name as a markup tag and the note
        would recommend installing a package that does not exist.
        """
        out = _invoke(monkeypatch, textual=False)

        assert "not installed" in out
        assert "`paa chat --plain` works" in out
        assert 'uv tool install "paa[tui]"' in out
