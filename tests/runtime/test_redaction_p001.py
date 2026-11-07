"""P-001 redaction at ``runtime.py``'s ``memory.embedding_sink`` warning.

Drives :func:`paa.runtime._build_memory_creator`'s except branch (the
``VectorEmbeddingSink`` construction failure path) — not ``redact()`` itself —
and asserts the captured structlog warning carries no secret-shaped text.

Reversal: restoring ``error=str(exc)`` at the site fails the secret-absence
assertions below, because the fixture strings travel verbatim into the log
kwargs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paa.config import MemorySettings, Settings
from paa.runtime import _build_memory_creator

_SK_FIXTURE = "sk-or-v1-abcdef0123456789abcdef0123456789"
_BEARER_FIXTURE = "Bearer tok-ABCDEF1234567890ABCDEF1234"
_API_KEY_FIXTURE = "api_key: K42_SECRET_VALUE_9"


class _FakeDB:
    """Minimal stand-in for a RelationalBackend — _build_memory_creator only
    passes ``db`` through to ``MemoryCreator(db, ...)``, which stores it
    without connecting in ``__init__``."""

    def __init__(self) -> None:
        pass


class _FakeModel:
    """Stand-in model adapter with the ``complete_structured`` signature."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {"facts": []}


class _FakeVectorStore:
    """Minimal vector-store stand-in."""

    def __init__(self) -> None:
        pass


class _FakeEmbedder:
    """Minimal embedder stand-in exposing model_name like the real encoders."""

    model_name = "fake-embedder-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]


def _settings(tmp_path: Path) -> Settings:
    home = tmp_path / "settings_home"
    home.mkdir(parents=True, exist_ok=True)
    return Settings(
        home=home,
        memory=MemorySettings(extraction_model=""),
    )


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[dict[str, Any]]]:
    import paa.runtime as runtime_mod

    captured_events: list[str] = []
    captured_kwargs: list[dict[str, Any]] = []

    def _capture_warning(event: str, **kw: Any) -> None:
        captured_events.append(event)
        captured_kwargs.append(kw)

    monkeypatch.setattr(runtime_mod.log, "warning", _capture_warning)
    return captured_events, captured_kwargs


class TestEmbeddingSinkWarningRedaction:
    """The degraded-sink warning must redact secret-shaped exception text."""

    async def test_secret_shaped_exception_text_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import paa.memory.creator as creator_mod

        settings = _settings(tmp_path)

        class _BrokenSink:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError(
                    f"constructor failed: {_SK_FIXTURE} {_BEARER_FIXTURE} {_API_KEY_FIXTURE}"
                )

        monkeypatch.setattr(creator_mod, "VectorEmbeddingSink", _BrokenSink)
        captured_events, captured_kwargs = _capture_warnings(monkeypatch)

        creator = _build_memory_creator(
            settings,
            _FakeDB(),
            _FakeModel(),
            vector_store=_FakeVectorStore(),
            embedder=_FakeEmbedder(),
        )

        assert creator._sink is None
        target_kwargs = [
            kw
            for event, kw in zip(captured_events, captured_kwargs, strict=True)
            if event == "memory.embedding_sink"
        ]
        assert target_kwargs, "no kwargs captured for memory.embedding_sink"
        target = target_kwargs[-1]
        assert target.get("status") == "degraded"
        logged_error = target.get("error", "")
        assert isinstance(logged_error, str)
        # Positive control: redaction ran (the marker is present).
        assert "***REDACTED***" in logged_error, f"no redaction marker: {logged_error!r}"
        # No raw fixture survives anywhere in the logged warning.
        assert "sk-" not in logged_error, f"sk- fixture leaked into log: {logged_error!r}"
        assert "tok-ABCDEF1234567890ABCDEF1234" not in logged_error
        assert "K42_SECRET_VALUE_9" not in logged_error
        assert "Bearer " not in logged_error
        assert _SK_FIXTURE not in logged_error
        assert _BEARER_FIXTURE not in logged_error
        assert _API_KEY_FIXTURE not in logged_error

    async def test_benign_exception_text_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinning: with no secret-shaped text the logged error is the text
        itself — redaction must not mangle ordinary degradation messages."""
        import paa.memory.creator as creator_mod

        settings = _settings(tmp_path)

        class _BrokenSink:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("no vector index configured")

        monkeypatch.setattr(creator_mod, "VectorEmbeddingSink", _BrokenSink)
        captured_events, captured_kwargs = _capture_warnings(monkeypatch)

        creator = _build_memory_creator(
            settings,
            _FakeDB(),
            _FakeModel(),
            vector_store=_FakeVectorStore(),
            embedder=_FakeEmbedder(),
        )

        assert creator._sink is None
        target_kwargs = [
            kw
            for event, kw in zip(captured_events, captured_kwargs, strict=True)
            if event == "memory.embedding_sink"
        ]
        assert target_kwargs, "no kwargs captured for memory.embedding_sink"
        assert target_kwargs[-1].get("error") == "no vector index configured"
