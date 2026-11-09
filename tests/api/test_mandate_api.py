"""N2.4: mandates through the ingestion API.

What is pinned here
-------------------
* ``POST /tasks`` with a mandate asserts it at the lineage head, with
  the effective policy version stamped by the server (never taken from
  the client).
* ``POST /tasks`` without a mandate submits as implicit.
* In strict mode (LOCKDOWN forces it) a submission without an explicit
  mandate is refused with 409 and the refusal lands in the ledger.
* A malformed mandate (unknown basis) is a 422, never a submission.

The runtime fixture mirrors ``tests/api/conftest.py`` so the LOCKDOWN
runtime below stays independent of the shared one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import EventType, PermissionMode
from paa.runtime import Runtime

pytest.importorskip("fastapi", reason="needs the api extra")


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
                "execution_steps": [{"index": 0, "action": "do it", "agent": "worker"}],
                "step_requirements": {},
            }
        if "verdict" in props:
            return {"verdict": "PASS", "findings": []}
        if "sub_requests" in props:
            return {"modality": "STANDARD", "sub_requests": [prompt], "candidate_agents": []}
        return {}


async def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: PermissionMode) -> Runtime:
    home = tmp_path / f"paa_home_{mode.value.lower()}"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    settings = Settings(home=home)
    settings.policy.mode = mode
    settings.sandbox.backend = "subprocess"
    return await Runtime.build(
        settings,
        model_adapter=ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    rt = await _build(tmp_path, monkeypatch, PermissionMode.AUTO)
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


@pytest.fixture
async def lockdown_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    rt = await _build(tmp_path, monkeypatch, PermissionMode.LOCKDOWN)
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


@pytest.fixture
def client(runtime: Runtime) -> Any:
    from fastapi.testclient import TestClient

    from paa.api import create_app

    return TestClient(create_app(runtime))


@pytest.fixture
def lockdown_client(lockdown_runtime: Runtime) -> Any:
    from fastapi.testclient import TestClient

    from paa.api import create_app

    return TestClient(create_app(lockdown_runtime))


class TestSubmitWithMandate:
    def test_explicit_mandate_grants_at_head(self, client: Any) -> None:
        resp = client.post(
            "/tasks",
            json={
                "goal": "do something",
                "mandate": {"kind": "api", "id": "alice", "basis": "explicit"},
            },
        )
        assert resp.status_code == 201, resp.text
        cid = resp.json()["correlation_id"]
        events = client.get(f"/ledger/{cid}").json()
        assert events[0]["event_type"] == "MANDATE_GRANTED"
        payload = events[0]["payload"]
        assert payload["basis"] == "explicit"
        assert payload["requester_asserted"] == {"kind": "api", "id": "alice"}
        assert payload["identity_assurance"] == "asserted"
        assert payload["v"] == 1
        # The server stamped the effective policy; the client never did.
        assert len(payload["policy_version"]) == 64
        assert payload["policy_label"] == "AUTO"

    def test_no_mandate_submits_implicit(self, client: Any) -> None:
        resp = client.post("/tasks", json={"goal": "do something"})
        assert resp.status_code == 201, resp.text
        cid = resp.json()["correlation_id"]
        events = client.get(f"/ledger/{cid}").json()
        assert events[0]["event_type"] == "MANDATE_GRANTED"
        assert events[0]["payload"]["basis"] == "implicit"

    def test_inferred_basis_is_recorded(self, client: Any) -> None:
        resp = client.post(
            "/tasks",
            json={
                "goal": "do something",
                "mandate": {"kind": "api", "id": "alice", "basis": "inferred"},
            },
        )
        assert resp.status_code == 201, resp.text
        cid = resp.json()["correlation_id"]
        events = client.get(f"/ledger/{cid}").json()
        assert events[0]["payload"]["basis"] == "inferred"

    def test_unknown_basis_is_a_422(self, client: Any) -> None:
        resp = client.post(
            "/tasks",
            json={
                "goal": "do something",
                "mandate": {"kind": "api", "id": "alice", "basis": "magical"},
            },
        )
        assert resp.status_code == 422

    def test_whitespace_only_requester_is_a_422(self, client: Any) -> None:
        resp = client.post(
            "/tasks",
            json={
                "goal": "do something",
                "mandate": {"kind": "api", "id": "  ", "basis": "explicit"},
            },
        )
        assert resp.status_code == 422

    def test_legacy_submission_shape_still_works(self, client: Any) -> None:
        """A body without the mandate field (every old client) is unchanged."""
        resp = client.post(
            "/tasks",
            json={
                "goal": "do something",
                "modality": "SIMPLE",
                "workspace_path": None,
                "target_agent": None,
                "run": False,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["phase"] == "QUEUED"


class TestStrictMode:
    def test_lockdown_refuses_implicit_submission(
        self, lockdown_client: Any, lockdown_runtime: Runtime
    ) -> None:
        import asyncio

        resp = lockdown_client.post("/tasks", json={"goal": "do something"})
        assert resp.status_code == 409, resp.text
        # Check the ledger directly: the refusal must have landed as its
        # own lineage, with no work under it. The HTTP layer already
        # proved the refusal; this proves the record.
        refused_list = asyncio.run(
            lockdown_runtime.ledger.events_since(
                datetime(1970, 1, 1, tzinfo=UTC),
                event_types=[EventType.MANDATE_REFUSED],
            )
        )
        assert len(refused_list) == 1
        refused = refused_list[0]
        assert refused.payload["reason"] == "require_explicit_mandate"
        assert refused.payload["requester_asserted"] == {
            "kind": "runtime",
            "id": "implicit",
        }

    def test_lockdown_accepts_explicit_submission(self, lockdown_client: Any) -> None:
        resp = lockdown_client.post(
            "/tasks",
            json={
                "goal": "do something",
                "mandate": {"kind": "api", "id": "alice", "basis": "explicit"},
            },
        )
        assert resp.status_code == 201, resp.text
        cid = resp.json()["correlation_id"]
        events = lockdown_client.get(f"/ledger/{cid}").json()
        assert events[0]["event_type"] == "MANDATE_GRANTED"
        assert events[0]["payload"]["basis"] == "explicit"
