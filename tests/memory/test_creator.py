"""The Memory Creator: real-time ETL from cold-lake signal to hot serving.

The RFC-critical behaviours: extract structured facts, ignore conversational
noise, contain malformed input without partial writes, and quarantine (never
auto-resolve) contradictions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from paa.memory.creator import MemoryCreator, ProcessingOutcome
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, to_iso, utc_now


def _row(payload: Any, *, channel: str = "test", persisted: bool = False) -> dict[str, Any]:
    """A signal shaped like a cold_lake_signals row, for direct processing."""
    return {
        "id": str(uuid.uuid4()),
        "channel": channel,
        "external_id": None,
        "raw_payload": json.dumps(payload),
        "received_at": to_iso(utc_now()),
        "persisted": persisted,
    }


@pytest.fixture
def creator(db: Database) -> MemoryCreator:
    return MemoryCreator(db)


class TestExtraction:
    async def test_explicit_facts_are_written(self, creator: MemoryCreator, db: Database) -> None:
        signal = _row(
            {"facts": [{"subject": "Project Alpha", "predicate": "status", "object": "active"}]}
        )
        result = await creator.process_signal(signal)

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        stored = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert stored == 1

    async def test_conversational_noise_is_ignored(self, creator: MemoryCreator) -> None:
        result = await creator.process_signal(_row("thanks, that's great!"))
        assert result.outcome is ProcessingOutcome.IGNORED

    async def test_multiple_facts_from_one_signal(self, creator: MemoryCreator) -> None:
        signal = _row(
            {
                "facts": [
                    {"subject": "Alpha", "predicate": "status", "object": "active"},
                    {"subject": "Alpha", "predicate": "owner", "object": "Ada"},
                ]
            }
        )
        result = await creator.process_signal(signal)
        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2


class TestMalformedContainment:
    async def test_bare_scalar_is_malformed_and_writes_nothing(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """A payload asserting nothing must be marked malformed with zero writes."""
        result = await creator.process_signal(_row(42))
        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_no_partial_commit_on_malformed(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        # A payload that is neither noise nor a valid assertion set.
        result = await creator.process_signal(_row(None))
        assert result.outcome in (ProcessingOutcome.MALFORMED, ProcessingOutcome.IGNORED)
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0


class TestContradictionQuarantine:
    async def test_conflicting_fact_is_quarantined_not_written(
        self, creator: MemoryCreator, db: Database
    ) -> None:
        """An incumbent 'colour=blue' plus an incoming 'colour=red' for the same
        entity must quarantine, not overwrite — RFC §4.2 forbids auto-resolution."""
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Alpha")
        await repo.add_fact(eid, "colour", "blue", confidence=0.95)

        signal = _row(
            {"facts": [{"subject": "Alpha", "predicate": "colour", "object": "red"}]}
        )
        result = await creator.process_signal(signal)

        assert result.outcome is ProcessingOutcome.QUARANTINED
        assert result.requires_human_attestation
        # The incumbent stands; the challenger was NOT written as a live fact.
        live = await db.fetch_all(
            "SELECT object_value FROM hot_serving_active_facts WHERE superseded_by IS NULL"
        )
        values = {r["object_value"] for r in live}
        assert "red" not in values


class TestBatchIsolation:
    async def test_one_poison_signal_does_not_abort_the_batch(self, db: Database) -> None:
        from paa.storage.relational.database import dumps

        # Insert three real signals: good, poison (bare scalar), good.
        async def insert(payload: Any) -> None:
            await db.execute(
                "INSERT INTO cold_lake_signals "
                "(id, received_at, channel, raw_payload, content_hash, sync_status) "
                "VALUES (?,?,?,?,?, 'unprocessed')",
                (str(uuid.uuid4()), to_iso(utc_now()), "test", dumps(payload), "h" + uuid.uuid4().hex[:20]),
            )

        await insert({"facts": [{"subject": "A", "predicate": "p", "object": "v1"}]})
        await insert(99)  # poison: bare scalar
        await insert({"facts": [{"subject": "B", "predicate": "p", "object": "v2"}]})

        report = await MemoryCreator(db).run_batch(limit=10)

        assert report.claimed == 3
        # Both good signals produced facts despite the poison in the middle.
        written = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert written == 2
