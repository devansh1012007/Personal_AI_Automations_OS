"""Unit tests for ``paa.runtime._build_memory_creator``.

Pins the extractor-wiring behaviour:

* rule-based default (``extraction_model`` empty) → ``RuleBasedExtractor``
* LLM opt-in (``extraction_model`` non-empty) → ``LLMFactExtractor`` wired from
  ``model_adapter`` with ``max_transport_retries`` and ``request_timeout_seconds``
* degradation: missing ``LLMFactExtractor`` class → rule-based fallback
* degradation: extractor constructor failure → rule-based fallback + warning log
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paa.config import MemorySettings, Settings
from paa.runtime import _build_memory_creator


class _FakeDB:
    """Minimal stand-in for a RelationalBackend — _build_memory_creator only
    passes ``db`` through to ``MemoryCreator(db, ...)``, which stores it without
    connecting in ``__init__``."""

    def __init__(self) -> None:
        pass


class _FakeModel:
    """Stand-in model adapter with the ``complete_structured`` signature."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {"facts": []}


def _settings(tmp_path: Path, extraction_model: str = "") -> Settings:
    """Build a Settings against an isolated tmp_path so get_settings() is not
    involved (no lru_cache contamination). The nested ``memory`` sub-model
    carries ``extraction_model``.

    Uses the pytest ``tmp_path`` fixture (R-W1-09) so the directory is
    removed with the test instead of leaking via ``tempfile.mkdtemp``.
    """
    home = tmp_path / "settings_home"
    home.mkdir(parents=True, exist_ok=True)
    return Settings(
        home=home,
        memory=MemorySettings(extraction_model=extraction_model),
    )


class TestBuildMemoryCreatorRuleBasedDefault:
    """extraction_model empty → rule-based extractor (the production default)."""

    async def test_empty_extraction_model_uses_rule_based(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, extraction_model="")
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        from paa.memory.creator import MemoryCreator, RuleBasedExtractor

        assert isinstance(creator, MemoryCreator)
        assert isinstance(creator._extractor, RuleBasedExtractor)


class TestBuildMemoryCreatorLLMOptIn:
    """extraction_model non-empty → LLMFactExtractor wired from model_adapter."""

    async def test_non_empty_extraction_model_uses_llm_extractor(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path, extraction_model="echo-llm")
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        from paa.memory.creator import LLMFactExtractor, MemoryCreator

        assert isinstance(creator, MemoryCreator)
        assert isinstance(creator._extractor, LLMFactExtractor)
        assert creator._extractor._model is model_adapter

    async def test_llm_extractor_honors_max_transport_retries(self, tmp_path: Path) -> None:
        """max_retries comes from settings.models.max_transport_retries."""
        settings = _settings(tmp_path, extraction_model="echo-llm")
        settings.models.max_transport_retries = 5
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        from paa.memory.creator import LLMFactExtractor

        assert isinstance(creator._extractor, LLMFactExtractor)
        assert creator._extractor._max_retries == 5

    async def test_llm_extractor_honors_request_timeout(self, tmp_path: Path) -> None:
        """timeout comes from settings.models.request_timeout_seconds."""
        settings = _settings(tmp_path, extraction_model="echo-llm")
        settings.models.request_timeout_seconds = 42.0
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        from paa.memory.creator import LLMFactExtractor

        assert isinstance(creator._extractor, LLMFactExtractor)
        assert creator._extractor._timeout == 42.0


class TestBuildMemoryCreatorDegradation:
    """Degradation paths never crash boot."""

    async def test_missing_llm_fact_extractor_class_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LLMFactExtractor is absent from paa.memory.creator, fall back
        to rule-based instead of crashing."""
        settings = _settings(tmp_path, extraction_model="echo-llm")
        db = _FakeDB()
        model_adapter = _FakeModel()

        import paa.memory.creator as creator_mod

        original = getattr(creator_mod, "LLMFactExtractor", None)
        monkeypatch.delattr(creator_mod, "LLMFactExtractor", raising=False)

        try:
            creator = _build_memory_creator(settings, db, model_adapter)
        finally:
            if original is not None:
                creator_mod.LLMFactExtractor = original

        from paa.memory.creator import RuleBasedExtractor

        assert isinstance(creator._extractor, RuleBasedExtractor)

    async def test_extractor_constructor_failure_degrades_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LLMFactExtractor.__init__ raises, fall back to rule-based and
        log a warning (redacted). structlog is used, so monkeypatch the log
        method directly rather than relying on caplog."""
        settings = _settings(tmp_path, extraction_model="echo-llm")
        db = _FakeDB()
        model_adapter = _FakeModel()

        import paa.memory.creator as creator_mod
        import paa.runtime as runtime_mod

        original = creator_mod.LLMFactExtractor

        class _BrokenExtractor:
            def __init__(self, **kwargs: Any) -> None:
                raise RuntimeError("sk-or-v1-abcdef0123456789 boom")

        creator_mod.LLMFactExtractor = _BrokenExtractor  # type: ignore[assignment]

        captured_events: list[str] = []
        captured_kwargs: list[dict[str, Any]] = []
        original_warning = runtime_mod.log.warning

        def _capture_warning(event: str, **kw: Any) -> None:
            captured_events.append(event)
            captured_kwargs.append(kw)

        monkeypatch.setattr(runtime_mod.log, "warning", _capture_warning)
        try:
            creator = _build_memory_creator(settings, db, model_adapter)
        finally:
            creator_mod.LLMFactExtractor = original
            runtime_mod.log.warning = original_warning

        from paa.memory.creator import RuleBasedExtractor

        assert isinstance(creator._extractor, RuleBasedExtractor)
        # The warning was emitted.
        assert "runtime.llm_extractor_unavailable" in captured_events
        # Assert on the specific warning event rather than positional [-1]
        # indexing, which is fragile if a second warning is ever added.
        target_kwargs = [
            kw for event, kw in zip(captured_events, captured_kwargs, strict=True)
            if event == "runtime.llm_extractor_unavailable"
        ]
        assert target_kwargs, "no kwargs captured for runtime.llm_extractor_unavailable"
        # The secret must be redacted in the logged kwargs.
        logged_error = target_kwargs[-1].get("error", "")
        assert "sk-or-v1" not in logged_error, f"secret leaked into log: {logged_error!r}"


class _FakeVectorStore:
    """Minimal vector-store stand-in — _build_memory_creator only threads it
    into VectorEmbeddingSink, which stores it without touching it."""

    def __init__(self) -> None:
        pass


class _FakeEmbedder:
    """Minimal embedder stand-in exposing model_name like the real encoders."""

    model_name = "fake-embedder-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]


class TestBuildMemoryCreatorEmbeddingSink:
    """C01: VectorEmbeddingSink wired only when store AND embedder present.

    The helper-level wired test is a true reversal (TypeError pre-fix). The
    no-backend case is a pinning test (R-W1-07): sink was always None
    pre-fix; its docstring no longer claims otherwise. Composition through
    ``Runtime.build`` is proved separately in TestRuntimeBuildEmbeddingSink.
    """

    async def test_wires_sink_when_vector_store_and_embedder_available(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()
        store = _FakeVectorStore()
        embedder = _FakeEmbedder()

        creator = _build_memory_creator(
            settings, db, model_adapter, vector_store=store, embedder=embedder
        )

        from paa.memory.creator import VectorEmbeddingSink

        assert isinstance(creator._sink, VectorEmbeddingSink)
        assert creator._sink._store is store
        assert creator._sink._embedder is embedder

    async def test_degrades_to_none_without_vector_backends(self, tmp_path: Path) -> None:
        """Pinning (R-W1-07): pre-fix the no-backend case was already None.

        Pins the degrade-never-raise contract; it does not reverse on the
        C01 wiring change. The wiring reversal is the test above.
        """
        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        assert creator._sink is None

    async def test_degrades_when_only_store_present(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(
            settings, db, model_adapter, vector_store=_FakeVectorStore(), embedder=None
        )

        assert creator._sink is None

    async def test_degrades_when_only_embedder_present(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(
            settings, db, model_adapter, vector_store=None, embedder=_FakeEmbedder()
        )

        assert creator._sink is None

    async def test_sink_construction_failure_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import paa.memory.creator as creator_mod

        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()

        class _BrokenSink:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("no index")

        monkeypatch.setattr(creator_mod, "VectorEmbeddingSink", _BrokenSink)
        creator = _build_memory_creator(
            settings,
            db,
            model_adapter,
            vector_store=_FakeVectorStore(),
            embedder=_FakeEmbedder(),
        )

        assert creator._sink is None


class TestBuildMemoryCreatorReturnsMemoryCreator:
    """The return value is always a MemoryCreator, never None."""

    async def test_returns_memory_creator_instance(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        db = _FakeDB()
        model_adapter = _FakeModel()

        creator = _build_memory_creator(settings, db, model_adapter)

        from paa.memory.creator import MemoryCreator

        assert isinstance(creator, MemoryCreator)
        assert creator._db is db


class TestRuntimeBuildEmbeddingSink:
    """R-W1-001a / R-W1-02 (TREV P1-2): ``Runtime.build`` threads the
    ``_try_vector`` pair into the memory creator.

    Reversal: removing the ``vector_store=`` / ``embedder=`` kwargs at
    ``runtime.py`` in ``Runtime.build`` leaves the creator sink-less; this
    test fails. The helper-level tests above cannot catch that — they call
    ``_build_memory_creator`` directly.
    """

    async def test_runtime_build_wires_sink_when_backends_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from paa.core.types import PermissionMode
        from paa.runtime import Runtime

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("PAA_HOME", str(home))

        from paa.config import Settings, reset_settings_cache

        reset_settings_cache()
        try:
            settings = Settings(home=home)
            settings.policy.mode = PermissionMode.AUTO
            settings.sandbox.backend = "subprocess"

            store = _FakeVectorStore()
            embedder = _FakeEmbedder()

            def _fake_try_vector(settings: Settings) -> tuple[Any, Any]:
                return store, embedder

            import paa.runtime as runtime_mod

            monkeypatch.setattr(runtime_mod, "_try_vector", _fake_try_vector)
            monkeypatch.setattr(
                runtime_mod, "_try_graph", lambda settings, db: None
            )
            monkeypatch.setattr(
                runtime_mod, "_try_queue", lambda settings, db: None
            )

            rt = await Runtime.build(
                settings,
                model_adapter=_FakeModel(),
                run_recovery=False,
                enable_optional_backends=True,
            )
            try:
                from paa.memory.creator import VectorEmbeddingSink

                sink = rt.memory_creator._sink
                assert isinstance(sink, VectorEmbeddingSink)
                # The sink wraps the *same* pair _try_vector built — no
                # double-construction (see _build_memory_creator DECISION).
                assert sink._store is store
                assert sink._embedder is embedder
            finally:
                await rt.close()
        finally:
            reset_settings_cache()
