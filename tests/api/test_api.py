"""The ingestion API — endpoints, the human-gate flow, and signal dedup."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_api


class TestHealth:
    def test_health_reports_ok_and_backends(self, client) -> None:  # noqa: ANN001
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "queue" in body["backends"]


class TestTasks:
    def test_submit_and_run_reaches_committed(self, client) -> None:  # noqa: ANN001
        resp = client.post("/tasks", json={"goal": "summarise the notes", "run": True})
        assert resp.status_code == 201
        body = resp.json()
        assert body["phase"] == "COMMITTED"
        assert not body["queued"]

    def test_submit_without_run_is_queued(self, client) -> None:  # noqa: ANN001
        resp = client.post("/tasks", json={"goal": "later task", "run": False})
        assert resp.status_code == 201
        assert resp.json()["queued"] is True

    def test_status_round_trip(self, client) -> None:  # noqa: ANN001
        cid = client.post("/tasks", json={"goal": "do a thing", "run": True}).json()[
            "correlation_id"
        ]
        resp = client.get(f"/tasks/{cid}")
        assert resp.status_code == 200
        assert resp.json()["is_terminal"] is True

    def test_status_unknown_task_404(self, client) -> None:  # noqa: ANN001
        import uuid

        resp = client.get(f"/tasks/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_invalid_correlation_id_400(self, client) -> None:  # noqa: ANN001
        assert client.get("/tasks/not-a-uuid").status_code == 400

    def test_empty_goal_rejected(self, client) -> None:  # noqa: ANN001
        assert client.post("/tasks", json={"goal": ""}).status_code == 422


class TestLedger:
    def test_ledger_returns_event_chain(self, client) -> None:  # noqa: ANN001
        cid = client.post("/tasks", json={"goal": "trace me", "run": True}).json()[
            "correlation_id"
        ]
        resp = client.get(f"/ledger/{cid}")
        assert resp.status_code == 200
        events = resp.json()
        types = [e["event_type"] for e in events]
        assert "TASK_REQUESTED" in types
        assert "MUTATION_COMMITTED" in types

    def test_ledger_unknown_404(self, client) -> None:  # noqa: ANN001
        import uuid

        assert client.get(f"/ledger/{uuid.uuid4()}").status_code == 404


class TestHumanGate:
    def test_clearing_a_non_gated_task_conflicts(self, client) -> None:  # noqa: ANN001
        cid = client.post("/tasks", json={"goal": "x", "run": True}).json()["correlation_id"]
        resp = client.post(f"/tasks/{cid}/gate", json={"approved": True})
        # A committed task is not awaiting attestation -> 409.
        assert resp.status_code == 409


class TestSignalIngestion:
    def test_ingest_signal(self, client) -> None:  # noqa: ANN001
        resp = client.post(
            "/signals",
            json={"channel": "email", "payload": {"facts": [{"subject": "A", "predicate": "p", "object": "v"}]}},
        )
        assert resp.status_code == 201
        assert resp.json()["channel"] == "email"
        assert resp.json()["duplicate"] is False

    def test_duplicate_external_id_is_deduped(self, client) -> None:  # noqa: ANN001
        payload = {"channel": "telegram", "payload": {"m": 1}, "external_id": "msg-42"}
        first = client.post("/signals", json=payload)
        second = client.post("/signals", json=payload)
        assert first.status_code == 201
        assert second.json()["duplicate"] is True  # idempotent on (channel, external_id)
        assert first.json()["signal_id"] == second.json()["signal_id"]

    def test_correction_recorded(self, client) -> None:  # noqa: ANN001
        cid = client.post("/tasks", json={"goal": "x", "run": True}).json()["correlation_id"]
        resp = client.post(f"/tasks/{cid}/correction", json={"correction": "wrong output"})
        assert resp.status_code == 202
