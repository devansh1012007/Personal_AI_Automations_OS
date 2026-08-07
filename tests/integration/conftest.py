"""Fixtures for end-to-end runtime tests.

These build a *real* :class:`~paa.runtime.Runtime` — real ledger, real storage,
real sandbox, real deterministic validation — and drive it through the
orchestrator. The only fake is the model, because the point of the integration
suite is to prove the *wiring* and the *guarantees*, not the reasoning quality of
whatever model happens to be installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.runtime import Runtime


class ScriptedModel:
    """A deterministic ``ModelLike`` that returns valid structured outputs.

    It answers by inspecting the requested schema — a plan schema gets a plan, a
    review schema gets a verdict — so one instance serves planner, critic and
    router. Every call is recorded so a test can assert *whether the model was
    consulted at all*, which is how the "no model in the security loop"
    guarantee (RFC §13) is checked: a blocked task must reach its verdict with
    the model call count unchanged.
    """

    def __init__(
        self,
        *,
        steps: list[dict[str, Any]] | None = None,
        verdict: str = "PASS",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._steps = steps or [
            {"index": 0, "action": "do the thing", "agent": "worker", "mutates": True}
        ]
        self._verdict = verdict

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "kwargs": kwargs})
        props = schema.get("properties", {})
        if "execution_steps" in props:
            return {"execution_steps": self._steps, "step_requirements": {}}
        if "verdict" in props:
            return {"verdict": self._verdict, "findings": []}
        if "sub_requests" in props:
            return {"modality": "STANDARD", "sub_requests": [prompt], "candidate_agents": []}
        return {}

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def scripted_model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def integration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    return home


@pytest.fixture
def build_runtime(
    integration_home: Path,
) -> Callable[..., Any]:
    """Return an async builder so each test can pick mode / model / backends.

    Yields a factory rather than a runtime because the recovery tests need to
    build, tear down, and rebuild against the *same* PAA_HOME to simulate a
    restart.
    """

    async def _build(
        *,
        model: Any = None,
        mode: PermissionMode = PermissionMode.AUTO,
        run_recovery: bool = True,
        optional_backends: bool = False,
    ) -> Runtime:
        settings = Settings(home=integration_home)
        settings.policy.mode = mode
        # Subprocess sandbox keeps tests fast and off the WSL/Docker probe path.
        settings.sandbox.backend = "subprocess"
        return await Runtime.build(
            settings,
            model_adapter=model or ScriptedModel(),
            run_recovery=run_recovery,
            enable_optional_backends=optional_backends,
        )

    return _build


@pytest.fixture
async def runtime(
    build_runtime: Callable[..., Any], scripted_model: ScriptedModel
) -> AsyncIterator[Runtime]:
    """A ready runtime in AUTO mode with the scripted model. Closed on teardown."""
    rt = await build_runtime(model=scripted_model)
    try:
        yield rt
    finally:
        await rt.close()
