"""Fixtures for API and daemon tests.

A real Runtime wired with a scripted model — no network, no model server — so
the edge and the daemon are tested against genuine ledger/storage behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.runtime import Runtime

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

pytest.importorskip("fastapi", reason="needs the api extra")

#: The sandbox command the default scripted step runs. It prints a minimal,
#: well-formed new-file patch — ``/dev/null`` source, one hunk, one insertion —
#: which the validation engine can actually check (the patcher's own
#: CREATE/DELETE/MODIFY vocabulary), so a scripted "full run" exercises the
#: same mutating-step path a real diff-producing step takes instead of riding
#: an empty artifact through a vacuous PASS (the honest engine refuses an
#: artifact that enables no check — same rule as the integration conftest).
_SUMMARY_PATCH_COMMAND = (
    "import sys; sys.stdout.write('--- /dev/null\\n"
    "+++ b/summary.txt\\n"
    "@@ -0,0 +1 @@\\n"
    "+notes: three items\\n"
    "')"
)


class ScriptedModel:
    """Deterministic ModelLike returning valid structured outputs."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kw: Any
    ) -> dict[str, Any]:
        self.calls += 1
        props = schema.get("properties", {})
        if "execution_steps" in props:
            return {
                "execution_steps": [
                    {
                        "index": 0,
                        "action": "write the summary file",
                        "agent": "worker",
                        "mutates": True,
                        # A real mutating step must leave something for
                        # validation to check; the command prints a minimal,
                        # well-formed new-file patch and the WorkerCell
                        # surfaces sandbox stdout as the step's patch.
                        "command": ["python3", "-c", _SUMMARY_PATCH_COMMAND],
                    }
                ],
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
def client(runtime: Runtime) -> TestClient:
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
