"""P006 — additive panel fields on GET /health (T-006-P006)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_api


class TestHealthPanels:
    def test_health_carries_the_five_panel_keys_additively(
        self, client: TestClient
    ) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        # Old keys intact (the additive-only lock).
        assert body["status"] == "ok"
        assert "version" in body
        assert "backends" in body
        assert "queue" in body["backends"]
        assert "open_tasks" in body
        # New keys present; values may be null (attention_top is null until
        # the P-07 allocator lands — that null is correct, not missing).
        for key in (
            "attention_top",
            "memory_browser_counts",
            "decision_tracker",
            "autonomy_state",
            "agent_monitor",
        ):
            assert key in body, f"missing panel key {key}"
        assert body["attention_top"] is None
        autonomy = body["autonomy_state"]
        assert autonomy is not None
        assert "mode" in autonomy
        assert "awaiting_gates" in autonomy
        assert body["agent_monitor"] is not None
        assert "agents" in body["agent_monitor"]
        counts = body["memory_browser_counts"]
        assert counts is not None
        assert counts["total"] == 0

    def test_health_stays_200_when_a_panel_source_fails(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A raising counts source empties only its panel; the response stays
        # 200 with the old keys intact (per-panel isolation reaches the API).
        from paa.memory.facts import FactRepository

        async def exploding(self: FactRepository) -> dict:
            raise RuntimeError("counts exploded")

        monkeypatch.setattr(FactRepository, "count_facts_by_domain", exploding)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["memory_browser_counts"] is None
        assert body["autonomy_state"] is not None
