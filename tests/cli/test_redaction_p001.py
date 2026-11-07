"""P-001 redaction + bound at ``paa memory drain``'s failure line.

Drives the real ``paa memory drain`` command path (Typer CliRunner against a
built Runtime whose ``MemoryCreator.drain_pending_embeddings`` raises) — not
``redact()`` itself — and asserts on the captured operator-console output:

* secret-shaped exception text never survives (``sk-`` / Bearer fixtures),
* the rendered error body never exceeds 200 chars (ADR-0036 §P01),
* truncation carries an explicit marker noting best-effort redaction (T01).

Rich folds lines at the console width, so length/marker assertions run on
whitespace-stripped output; every fixture body below is a single word, so
the strip reconstructs the rendered body exactly.

Reversal: restoring ``console.print(f"[red]embedding drain failed: {exc}[/red]")``
fails the secret-absence and bound assertions (10 KB of raw text, no marker).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from paa.cli.main import _MAX_DRAIN_ERROR_CHARS, _TRUNCATION_MARKER, app
from paa.config import PermissionMode, Settings, reset_settings_cache
from paa.runtime import Runtime

runner = CliRunner()

_SK_FIXTURE = "sk-or-v1-abcdef0123456789abcdef0123456789"
_BEARER_FIXTURE = "Bearer tok-ABCDEF1234567890ABCDEF1234"

#: Single-word bodies keep the whitespace-strip reconstruction exact.
_MARKER_NORMALIZED = "".join(_TRUNCATION_MARKER.split())
_BODY_X_RUN = _MAX_DRAIN_ERROR_CHARS - len(_TRUNCATION_MARKER)


@pytest.fixture
def paa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated PAA_HOME + offline model/sandbox settings (mirrors tests/cli/test_memory.py)."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    reset_settings_cache()
    yield home
    # R-W1-09: teardown reset so a failing test cannot leak cached settings.
    reset_settings_cache()


def _normalized(output: str) -> str:
    return "".join(output.split())


def _error_body_tail(output: str) -> str:
    """Normalized text after the drain-failure prefix (body + marker + table)."""
    _, _, tail = _normalized(output).partition("embeddingdrainfailed:")
    return tail


def _invoke_drain_with_raising_drain(
    paa_home: Path, monkeypatch: pytest.MonkeyPatch, exc_text: str
) -> Any:
    """Run ``paa memory drain`` with drain_pending_embeddings raising `exc_text`."""

    async def _boom(self: Any) -> Any:
        raise RuntimeError(exc_text)

    monkeypatch.setattr("paa.memory.creator.MemoryCreator.drain_pending_embeddings", _boom)

    async def fake_build_runtime(*, run_recovery: bool = False) -> Runtime:
        settings = Settings(home=paa_home)
        settings.policy.mode = PermissionMode.AUTO
        settings.sandbox.backend = "subprocess"
        return await Runtime.build(
            settings,
            run_recovery=False,
            enable_optional_backends=False,
        )

    monkeypatch.setattr("paa.cli.main._build_runtime", fake_build_runtime)
    return runner.invoke(app, ["memory", "drain", "--limit", "25"])


class TestDrainErrorRedactionBound:
    def test_secret_shaped_exception_text_is_redacted(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _invoke_drain_with_raising_drain(
            paa_home,
            monkeypatch,
            f"boom {_SK_FIXTURE} {_BEARER_FIXTURE}",
        )
        assert result.exit_code == 0, result.output
        assert "embedding drain failed:" in result.stdout
        # Positive control: redaction ran.
        assert "***REDACTED***" in result.stdout
        # No raw fixture survives anywhere in the captured console output.
        assert "sk-" not in result.stdout
        assert "tok-ABCDEF1234567890ABCDEF1234" not in result.stdout
        assert _SK_FIXTURE not in result.stdout
        assert _BEARER_FIXTURE not in result.stdout
        # The failure is isolated: the batch report still prints.
        assert "claimed" in result.stdout

    def test_boundary_200_chars_no_truncation(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "x" * _MAX_DRAIN_ERROR_CHARS
        result = _invoke_drain_with_raising_drain(paa_home, monkeypatch, body)
        assert result.exit_code == 0, result.output
        tail = _error_body_tail(result.stdout)
        assert tail.startswith(body), tail[:300]
        assert "truncated" not in result.stdout

    def test_boundary_201_chars_truncates(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = "x" * (_MAX_DRAIN_ERROR_CHARS + 1)
        result = _invoke_drain_with_raising_drain(paa_home, monkeypatch, body)
        assert result.exit_code == 0, result.output
        tail = _error_body_tail(result.stdout)
        assert tail.startswith("x" * _BODY_X_RUN + _MARKER_NORMALIZED), tail[:300]
        # The (201st) char did not make it through.
        assert ("x" * (_BODY_X_RUN + 1)) not in tail

    def test_huge_exception_bounded_and_redacted_before_slice(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """10 KB exception with the sk- fixture straddling the slice point.

        Truncation AFTER redaction is the only order that hides the token:
        slice-first would keep "sk-or-v1-..." in the render, redacted-first
        replaces the whole token before the cut.
        """
        # Token straddles the cut: starts inside the kept region (140 < 162),
        # ends beyond it (140 + 40 > 162), and its redaction fits fully inside.
        assert 140 < _BODY_X_RUN < 140 + len(_SK_FIXTURE)
        # NB: the "!" separator matters — an sk- token directly followed by a
        # run of [A-Za-z0-9_-] would make redact() eat the whole run (the
        # character class has no word-boundary or length cap), which is the
        # true redact() behaviour, not the truncation path under test here.
        body = "x" * 140 + _SK_FIXTURE + "!" + "x" * 10000
        result = _invoke_drain_with_raising_drain(paa_home, monkeypatch, body)
        assert result.exit_code == 0, result.output
        tail = _error_body_tail(result.stdout)
        # Rendered body region is exactly the bound (x-run + marker).
        assert tail.startswith("x" * 140 + "***REDACTED***"), tail[:300]
        assert _MARKER_NORMALIZED in tail
        # No raw token fragment survived: the full token AND its tail.
        assert "sk-" not in result.stdout
        assert "abcdef0123456789" not in result.stdout
        assert _SK_FIXTURE not in result.stdout
        assert ("x" * 10000) not in result.stdout

    def test_hostile_text_does_not_break_command(
        self, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-ASCII + NUL + ANSI + a rich-markup attack ([/red]) + a secret.

        Without markup=False the '[/red]' in the exception text raises
        MarkupError out of console.print and the command exits 1; the fix
        prints it literally. Raw escapes/NULs pass through (see report §4 —
        console-escape sanitization is a sibling finding, not P-001), but
        the command must survive, stay bounded and leak nothing.
        """
        body = "éclair\x00\x1b[31m[/red]" + _SK_FIXTURE + "!" + "x" * 300
        result = _invoke_drain_with_raising_drain(paa_home, monkeypatch, body)
        assert result.exit_code == 0, result.output
        normalized = _normalized(result.stdout)
        assert "embeddingdrainfailed:" in normalized
        assert "sk-" not in normalized
        assert _SK_FIXTURE not in normalized
        # Positive control: the secret was redacted, not dropped wholesale.
        assert "REDACTED" in normalized
        assert _MARKER_NORMALIZED in normalized
        assert "claimed" in result.stdout
