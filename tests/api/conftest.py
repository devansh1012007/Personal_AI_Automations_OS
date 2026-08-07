"""Fixtures for API and daemon tests.

A real Runtime wired with a scripted model — no network, no model server — so
the edge and the daemon are tested against genuine ledger/storage behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.runtime import Runtime

pytest.importorskip("fastapi", reason="needs the api extra")


class ScriptedModel:
    """Deterministic ModelLike returning valid structured outputs."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, prompt: str, schema: dict, **kw: Any) -> dict:
        self.calls += 1
        props = schema.get("properties", {})
        if "execution_steps" in props:
            return {
                "execution_steps": [{"index": 0, "action": "do it", "agent": "worker"}],
                "step_requirements": {},
            }
        if "verdict" in props:
            return {"verdict": "PASS", "findings": []}
        if "sub_requests" in props:
            return {"modality": "STANDARD", "sub_requests": [prompt], "candidate_agents": []}
        return {}


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()

    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


@pytest.fixture
def client(runtime: Runtime):  # noqa: ANN201
    from fastapi.testclient import TestClient

    from paa.api import create_app

    return TestClient(create_app(runtime))


@pytest.fixture
async def runtime_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    """Runtime with the optional backends (queue, vector, graph) enabled.

    Needed by daemon tests that exercise the queue-drain loop, which is a no-op
    without a queue backend.
    """
    home = tmp_path / "paa_home_full"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()

    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=True,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()
