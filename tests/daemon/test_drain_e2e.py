"""End-to-end daemon cold-lake drain: signal to hot-serving facts via the ingestion loop.

Proves that a row landed in ``cold_lake_signals`` is drained by the live
``Daemon`` ingestion loop, written to ``hot_serving_active_facts`` with
``source_signal_id`` pointing back at the signal, and the signal marked
``sync_status = 'processed'``.

The database is an isolated SQLite file under ``tmp_path`` (one per test), so no
shared DB, fixed port, or network is required. The test would fail without the
daemon ingestion loop (see ``test_drain_requires_daemon_loop``), satisfying the
reversal criterion.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.daemon import Daemon
from paa.runtime import Runtime

pytestmark = pytest.mark.asyncio


class _ScriptedModel:
    """Stand-in model so ``Runtime.build`` never touches a provider or the network.

    The drain path never consults the model; this exists only so the router is
    not built (which would reach for a configured provider).
    """

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {}


async def _wait_for(
    condition: Any,
    *,
    timeout_seconds: float = 5.0,
    period: float = 0.02,
) -> None:
    """Poll ``condition`` (a zero-arg async callable returning bool) until true.

    Uses a bounded polling loop rather than a fixed sleep so the test is not
    timing-sensitive either way. Raises :class:`AssertionError` if the
    deadline passes without the condition becoming true, which fails the test
    instead of hanging.
    """
    attempts = max(1, int(timeout_seconds / period))
    for _ in range(attempts):
        if await condition():
            return
        await asyncio.sleep(period)
    raise AssertionError("condition did not become true before timeout")


@pytest.fixture
async def runtime(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """A real Runtime against an isolated tmp_path SQLite DB, no backends, no network."""
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()

    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=_ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


async def _seed_signal(runtime: Runtime, *, channel: str = "test") -> str:
    """Insert one cold-lake signal row with an extractable triple payload."""
    signal_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    payload = {"facts": [{"subject": "Alpha", "predicate": "status", "object": "live"}]}
    raw = json.dumps(payload)
    await runtime.db.execute(
        "INSERT INTO cold_lake_signals "
        "(id, received_at, channel, external_id, raw_payload, content_hash, "
        "blob_uri, sync_status, processed_at, error_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 'unprocessed', NULL, NULL)",
        (
            signal_id,
            now,
            channel,
            None,
            raw,
            sha256(raw.encode()).hexdigest(),
        ),
    )
    return signal_id


async def _status(runtime: Runtime, signal_id: str) -> str | None:
    return await runtime.db.fetch_value(
        "SELECT sync_status FROM cold_lake_signals WHERE id = ?", (signal_id,)
    )


async def _fact_count_for_signal(runtime: Runtime, signal_id: str) -> int:
    return await runtime.db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE source_signal_id = ?",
        (signal_id,),
    ) or 0


async def test_drain_signal_to_hot_serving_end_to_end(runtime: Runtime) -> None:
    signal_id = await _seed_signal(runtime)

    daemon = Daemon(runtime, dispatch_interval=10.0, ingestion_interval=0.02)
    await daemon.start()
    try:
        async def drained() -> bool:
            return (
                await _status(runtime, signal_id) == "processed"
                and await _fact_count_for_signal(runtime, signal_id) >= 1
            )

        await _wait_for(drained, timeout_seconds=5.0)

        assert await _status(runtime, signal_id) == "processed"

        rows = await runtime.db.fetch_all(
            "SELECT id, source_signal_id FROM hot_serving_active_facts "
            "WHERE source_signal_id = ?",
            (signal_id,),
        )
        assert len(rows) >= 1
        assert all(row["source_signal_id"] == signal_id for row in rows)
    finally:
        await daemon.stop()


async def test_drain_requires_daemon_loop(runtime: Runtime) -> None:
    """Reversal: without the ingestion loop, nothing is written and status stays unprocessed."""
    signal_id = await _seed_signal(runtime)

    # Deliberately do not start the daemon.
    await asyncio.sleep(0.25)

    assert await _status(runtime, signal_id) == "unprocessed"
    assert await _fact_count_for_signal(runtime, signal_id) == 0


async def test_drain_uses_llm_creator_when_extraction_model_set(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon ingestion loop consumes self._rt.memory_creator when
    extraction_model is set, writing llm-provenance facts (confidence 0.70).

    Reversal: reverting _memory_creator to MemoryCreator(self._rt.db) yields the
    rule-based extractor (confidence 0.95) and zero facts for a payload with no
    rule-extractable triples, so this test fails without the fix.
    """
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
        signal_id = await _seed_signal(rt)

        daemon = Daemon(rt, dispatch_interval=10.0, ingestion_interval=0.02)
        await daemon.start()
        try:
            async def drained() -> bool:
                return (
                    await _status(rt, signal_id) == "processed"
                    and await _fact_count_for_signal(rt, signal_id) >= 1
                )

            await _wait_for(drained, timeout_seconds=5.0)

            rows = await rt.db.fetch_all(
                "SELECT initial_confidence FROM hot_serving_active_facts "
                "WHERE source_signal_id = ? AND object_value = 'live'",
                (signal_id,),
            )
            assert rows, "no facts written — LLM creator not consulted"
            assert all(r["initial_confidence"] == 0.70 for r in rows), rows
        finally:
            await daemon.stop()
    finally:
        await rt.close()
        reset_settings_cache()
