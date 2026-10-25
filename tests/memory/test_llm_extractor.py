"""LLM-backed fact extraction seam: scripted model → facts, malformed → quarantine.

Proves the :class:`~paa.memory.creator.FactExtractor` seam (creator.py:238)
respects containment and the one-transaction-per-signal rule when driven by a
scripted model, mirroring the contract the real `LLMFactExtractor` (T-003) must
honor. The scripted extractor is injected into :class:`~paa.memory.creator.MemoryCreator`
via its ``extractor=`` constructor parameter, so no change to ``creator.py`` is
required.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from paa.memory.creator import (
    CandidateFact,
    MalformedSignalError,
    MemoryCreator,
    ProcessingOutcome,
    SignalEnvelope,
    _reject_suspicious,
    _scalar_to_text,
)
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


class ScriptedLLMExtractor:
    """A :class:`~paa.memory.creator.FactExtractor` driven by a script.

    Each script step is either a list of triples to return or a
    :class:`BaseException` to raise; steps are consumed in order (cycling), so
    a batch test can make the first signal succeed and the second crash,
    modeling one poisoning signal among healthy ones.

    Honors the same containment contract the real `LLMFactExtractor` must
    honor: C0 control characters (incl. NUL), ANSI escapes, managed-block
    markers, and oversized object values are refused with
    :class:`MalformedSignalError` and nothing is written.
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self._script = list(script or [])
        self._index = 0
        self.calls: list[SignalEnvelope] = []

    async def extract(self, envelope: SignalEnvelope) -> list[CandidateFact]:
        self.calls.append(envelope)
        if not self._script:
            return []
        step = self._script[self._index % len(self._script)]
        self._index += 1
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, dict):
            # A single triple is treated as a one-item script step.
            step = [step]
        raw = envelope.raw if isinstance(envelope.raw, str) else ""
        _reject_suspicious(raw, signal_id=envelope.id)
        candidates: list[CandidateFact] = []
        for triple in step:
            subject = triple.get("subject")
            predicate = triple.get("predicate")
            obj = triple.get("object")
            if not isinstance(subject, str) or not subject.strip():
                raise MalformedSignalError("triple missing subject", signal_id=envelope.id)
            if not isinstance(predicate, str) or not predicate.strip():
                raise MalformedSignalError("triple missing predicate", signal_id=envelope.id)
            if obj is None:
                raise MalformedSignalError("triple missing object", signal_id=envelope.id)
            value = _scalar_to_text(obj, signal_id=envelope.id)
            if value is None:
                continue
            candidates.append(
                CandidateFact(
                    entity_name=" ".join(subject.split()),
                    predicate=" ".join(predicate.split()),
                    object_value=value,
                )
            )
        return candidates


class TestLLMExtraction:
    async def test_structured_triples_become_facts(self, db: Database) -> None:
        extractor = ScriptedLLMExtractor(
            script=[
                [
                    {"subject": "Project Alpha", "predicate": "status", "object": "active"},
                    {"subject": "Project Alpha", "predicate": "owner", "object": "Ada"},
                ]
            ]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2
        stored = await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts")
        assert stored == 2

    async def test_extractor_is_consulted_once_per_signal(self, db: Database) -> None:
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "Beta", "predicate": "status", "object": "live"}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(extractor.calls) == 1
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1


class TestLLMMalformedContainment:
    async def test_malformed_triple_raises_and_writes_nothing(self, db: Database) -> None:
        """A scripted model returning an incomplete triple must abort the signal."""
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "", "predicate": "status", "object": "active"}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_nul_byte_is_contained_with_zero_facts(self, db: Database) -> None:
        """A NUL byte in the raw payload is hostile: abort, zero facts, malformed."""
        envelope = SignalEnvelope(
            id=str(uuid.uuid4()),
            channel="test",
            payload="text",
            raw="triple \x00 injection",
            external_id=None,
            received_at=None,
            persisted=False,
        )
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "X", "predicate": "status", "object": "active"}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(envelope)

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_oversized_value_is_contained_with_zero_facts(self, db: Database) -> None:
        """An object value past the 8 KiB ceiling is refused, not truncated."""
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "X", "predicate": "status", "object": "x" * 9000}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_managed_block_marker_is_contained_with_zero_facts(
        self, db: Database
    ) -> None:
        """A managed-block marker in the raw payload is suspicious: abort, zero facts."""
        envelope = SignalEnvelope(
            id=str(uuid.uuid4()),
            channel="test",
            payload="text",
            raw="note <!-- paa:managed: block -->",
            external_id=None,
            received_at=None,
            persisted=False,
        )
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "X", "predicate": "status", "object": "active"}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(envelope)

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0


class TestLLMBatchIsolation:
    async def test_one_crashing_signal_writes_zero_facts_and_spares_siblings(
        self, db: Database
    ) -> None:
        """One transaction per signal: a crashing extractor leaves its signal
        with zero facts while a sibling signal in the same batch still commits.

        Models a crash mid-signal: the claimed signal never reaches PROCESSED
        and contributes no facts, and the next batch can redo it.
        """
        from paa.storage.relational.database import dumps

        async def insert(payload: Any, received_at: str) -> None:
            await db.execute(
                "INSERT INTO cold_lake_signals "
                "(id, received_at, channel, raw_payload, content_hash, sync_status) "
                "VALUES (?,?,?,?,?, 'unprocessed')",
                (
                    str(uuid.uuid4()),
                    received_at,
                    "test",
                    dumps(payload),
                    "h" + uuid.uuid4().hex[:20],
                ),
            )

        stamp = to_iso(utc_now())
        await insert(
            {"facts": [{"subject": "Good", "predicate": "status", "object": "ok"}]},
            stamp,
        )
        await insert({"fakes": []}, to_iso(utc_now()))

        good_triples = [{"subject": "Good", "predicate": "status", "object": "active"}]
        extractor = ScriptedLLMExtractor(
            script=[good_triples, RuntimeError("inference worker died")]
        )
        creator = MemoryCreator(db, extractor=extractor)
        report = await creator.run_batch(limit=10)

        assert report.claimed == 2
        assert report.processed == 1
        assert report.errors == 1
        # Only the good signal contributed facts; the crashing signal wrote none.
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 1
