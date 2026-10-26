"""Memory API routes — facts, search, signals, drain.

Reversal check: every route returns 200/202 with shaped JSON here, and would
return 404 without the four routes registered in ``src/paa/api/app.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from paa.runtime import Runtime

pytestmark = pytest.mark.requires_api

ENTITY_ID = str(uuid4())
OTHER_ENTITY = str(uuid4())


def _iso() -> str:
    return datetime.now(UTC).isoformat()


async def _seed_fact(
    runtime: Runtime,
    *,
    entity_id: str,
    predicate: str,
    object_value: str,
    fact_id: str | None = None,
    domain: str = "semantic",
) -> str:
    """Insert one entity + one fact row into the hot-serving tables."""
    fact_id = fact_id or str(uuid4())
    now = _iso()
    await runtime.db.execute(
        "INSERT INTO hot_serving_entity_index "
        "(id, class, canonical_name, aliases, attributes, created_at, updated_at) "
        "VALUES (?, 'concept', ?, '[]', '{}', ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (entity_id, entity_id, now, now),
    )
    await runtime.db.execute(
        "INSERT INTO hot_serving_active_facts "
        "(id, entity_id, predicate, object_value, memory_domain, memory_scope, "
        "initial_confidence, importance, use_count, provenance, embedding_status, "
        "created_at, last_queried_at) "
        "VALUES (?, ?, ?, ?, ?, 'global', 1.0, 0.5, 0, '{}', 'pending', ?, ?)",
        (fact_id, entity_id, predicate, object_value, domain, now, now),
    )
    return fact_id


async def _seed_signal(
    runtime: Runtime,
    *,
    channel: str = "test",
    sync_status: str = "unprocessed",
    payload: dict[str, Any] | None = None,
    external_id: str | None = None,
) -> str:
    """Insert one cold-lake signal row (inline payload, no CAS)."""
    signal_id = str(uuid4())
    now = _iso()
    raw = json.dumps(payload or {"k": "v"})
    await runtime.db.execute(
        "INSERT INTO cold_lake_signals "
        "(id, received_at, channel, external_id, raw_payload, content_hash, "
        "blob_uri, sync_status, processed_at, error_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)",
        (
            signal_id,
            now,
            channel,
            external_id,
            raw,
            sha256(raw.encode()).hexdigest(),
            sync_status,
        ),
    )
    return signal_id


FACT_FIELDS = {
    "id",
    "entity_id",
    "predicate",
    "object_value",
    "confidence",
    "domain",
    "source_signal_id",
}
SIGNAL_FIELDS = {
    "id",
    "channel",
    "external_id",
    "sync_status",
    "received_at",
    "processed_at",
    "error_detail",
}
REPORT_FIELDS = {
    "claimed",
    "processed",
    "ignored",
    "malformed",
    "quarantined",
    "errors",
    "facts_written",
}


class TestFacts:
    async def test_facts_returns_shaped_list(self, client: TestClient, runtime: Runtime) -> None:
        await _seed_fact(runtime, entity_id=ENTITY_ID, predicate="knows", object_value="Bob")
        await _seed_fact(runtime, entity_id=ENTITY_ID, predicate="works_at", object_value="Acme")
        await _seed_fact(runtime, entity_id=OTHER_ENTITY, predicate="knows", object_value="Ann")

        resp = client.get("/memory/facts", params={"entity": ENTITY_ID})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        for fact in body:
            assert set(fact) == FACT_FIELDS
            assert fact["entity_id"] == ENTITY_ID
        assert {f["predicate"] for f in body} == {"knows", "works_at"}

    async def test_facts_empty_when_no_match(self, client: TestClient) -> None:
        resp = client.get("/memory/facts", params={"entity": str(uuid4())})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_facts_without_entity_returns_all(
        self, client: TestClient, runtime: Runtime
    ) -> None:
        await _seed_fact(runtime, entity_id=ENTITY_ID, predicate="p", object_value="v")
        resp = client.get("/memory/facts")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


class TestSearch:
    async def test_search_returns_matching_facts(
        self, client: TestClient, runtime: Runtime
    ) -> None:
        await _seed_fact(
            runtime,
            entity_id=ENTITY_ID,
            predicate="likes",
            object_value="dark chocolate",
        )

        resp = client.get("/memory/search", params={"q": "chocolate"})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        assert any(f["object_value"] == "dark chocolate" for f in body)

    async def test_search_empty_when_none_match(
        self, client: TestClient, runtime: Runtime
    ) -> None:
        await _seed_fact(
            runtime,
            entity_id=ENTITY_ID,
            predicate="likes",
            object_value="dark chocolate",
        )

        resp = client.get("/memory/search", params={"q": "zzznothingzzz"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_search_empty_term_returns_empty(self, client: TestClient) -> None:
        resp = client.get("/memory/search", params={"q": ""})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_search_missing_query_is_rejected(self, client: TestClient) -> None:
        assert client.get("/memory/search").status_code == 422


class TestSignals:
    async def test_signals_list_includes_sync_status(
        self, client: TestClient, runtime: Runtime
    ) -> None:
        await _seed_signal(runtime, sync_status="processed")
        await _seed_signal(runtime, sync_status="unprocessed")

        resp = client.get("/memory/signals")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 2
        for signal in body:
            assert set(signal) == SIGNAL_FIELDS
            assert "sync_status" in signal

    async def test_signals_status_filter(self, client: TestClient, runtime: Runtime) -> None:
        await _seed_signal(runtime, sync_status="processed")
        await _seed_signal(runtime, sync_status="unprocessed")
        await _seed_signal(runtime, sync_status="processing")

        resp = client.get("/memory/signals", params={"status": "processed"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["sync_status"] == "processed"

    async def test_signals_invalid_status_returns_400(
        self, client: TestClient, runtime: Runtime
    ) -> None:
        """An unknown sync_status is rejected with HTTP 400, not a 500."""
        resp = client.get("/memory/signals", params={"status": "bogus-status"})
        assert resp.status_code == 400, resp.text


class TestDrain:
    async def test_drain_returns_batch_report(self, client: TestClient, runtime: Runtime) -> None:
        await _seed_signal(
            runtime,
            sync_status="unprocessed",
            payload={"facts": [{"subject": "Alpha", "predicate": "status", "object": "live"}]},
        )

        resp = client.post("/memory/drain")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == REPORT_FIELDS
        assert body["claimed"] >= 1
        assert body["facts_written"] >= 1
        assert body["claimed"] == (
            body["processed"]
            + body["ignored"]
            + body["malformed"]
            + body["quarantined"]
            + body["errors"]
        )

    async def test_drain_no_unprocessed_returns_zeros(self, client: TestClient) -> None:
        resp = client.post("/memory/drain")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == REPORT_FIELDS
        assert body["claimed"] == 0
        assert body["facts_written"] == 0

    async def test_drain_uses_llm_creator_when_extraction_model_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /memory/drain consumes runtime.memory_creator when extraction_model
        is set, so the LLM extractor writes llm-provenance facts (confidence 0.70).

        Reversal: reverting the route to MemoryCreator(runtime.db) yields the
        rule-based extractor (confidence 0.95) and zero facts for a payload with
        no rule-extractable triples, so this test fails without the fix.
        """
        from fastapi.testclient import TestClient

        from paa.api import create_app
        from paa.config import PermissionMode, Settings, reset_settings_cache
        from paa.runtime import Runtime

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("PAA_HOME", str(home))
        monkeypatch.setenv("PAA_MEMORY__EXTRACTION_MODEL", "echo-llm")
        monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
        monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
        monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
        monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
        reset_settings_cache()

        class TripleModel:
            async def complete_structured(
                self, prompt: str, schema: dict[str, Any], **kw: Any
            ) -> dict[str, Any]:
                if "facts" in schema.get("properties", {}):
                    return {
                        "facts": [
                            {
                                "subject": "Alpha",
                                "predicate": "status",
                                "object": "live",
                            }
                        ]
                    }
                return {}

        settings = Settings(home=home)
        settings.policy.mode = PermissionMode.AUTO
        settings.sandbox.backend = "subprocess"
        rt = await Runtime.build(
            settings,
            model_adapter=TripleModel(),
            run_recovery=False,
            enable_optional_backends=False,
        )
        try:
            await rt.db.execute(
                "INSERT INTO cold_lake_signals "
                "(id, received_at, channel, external_id, raw_payload, content_hash, "
                "blob_uri, sync_status, processed_at, error_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 'unprocessed', NULL, NULL)",
                (
                    str(uuid4()),
                    _iso(),
                    "test",
                    None,
                    json.dumps({"k": "v"}),
                    sha256(b"{}").hexdigest(),
                ),
            )

            client = TestClient(create_app(rt))
            resp = client.post("/memory/drain")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["claimed"] >= 1
            assert body["facts_written"] >= 1

            rows = await rt.db.fetch_all(
                "SELECT initial_confidence FROM hot_serving_active_facts "
                "WHERE object_value = 'live'"
            )
            assert rows, "no facts written — LLM creator not consulted"
            assert all(r["initial_confidence"] == 0.70 for r in rows), rows
        finally:
            await rt.close()
            reset_settings_cache()


class TestBackpressure:
    async def test_429_under_queue_overload(self, runtime_full: Runtime) -> None:
        from fastapi.testclient import TestClient

        from paa.api import create_app
        from paa.storage.queue.base import StreamName

        assert runtime_full.queue is not None
        client = TestClient(create_app(runtime_full))
        for _ in range(60):
            await runtime_full.queue.enqueue(StreamName.ORCHESTRATOR_CORE, {"k": "v"})
        resp = client.get("/memory/facts", params={"entity": ENTITY_ID})
        assert resp.status_code == 429
