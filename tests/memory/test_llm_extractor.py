"""LLM-backed fact extraction seam: scripted model → facts, malformed → quarantine.

Proves the :class:`~paa.memory.creator.FactExtractor` seam (creator.py:238)
respects containment and the one-transaction-per-signal rule when driven by a
scripted model, mirroring the contract the real `LLMFactExtractor` (T-003) must
honor. The scripted extractor is injected into :class:`~paa.memory.creator.MemoryCreator`
via its ``extractor=`` constructor parameter, so no change to ``creator.py`` is
required.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from paa.memory.creator import (
    CandidateFact,
    LLMFactExtractor,
    MalformedSignalError,
    MemoryCreator,
    ProcessingOutcome,
    SignalEnvelope,
    _reject_suspicious,
    _scalar_to_text,
)
from paa.models.base import ModelUnavailableError
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


class ScriptedModel:
    """A minimal ``complete_structured(prompt, schema, **kwargs) -> dict`` adapter.

    Returns a scripted dict or raises a scripted exception, recording every
    call so tests can assert the model was (or was not) consulted.
    """

    def __init__(self, response: dict[str, Any] | BaseException) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((prompt, schema))
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class TestLLMFactExtractor:
    async def test_structured_triples_become_facts(self, db: Database) -> None:
        model = ScriptedModel(
            {
                "facts": [
                    {"subject": "Project Alpha", "predicate": "status", "object": "active"},
                    {"subject": "Project Alpha", "predicate": "owner", "object": "Ada"},
                ]
            }
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2
        assert len(model.calls) == 1
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 2

    async def test_confidence_and_provenance_are_llm(self, db: Database) -> None:
        model = ScriptedModel(
            {"facts": [{"subject": "Nebula", "predicate": "status", "object": "live"}]}
        )
        extractor = LLMFactExtractor(model=model)
        envelope = SignalEnvelope(
            id=str(uuid.uuid4()),
            channel="test",
            payload="text",
            raw="Nebula status live",
            external_id=None,
            received_at=None,
            persisted=False,
        )
        candidates = await extractor.extract(envelope)

        assert len(candidates) == 1
        c = candidates[0]
        assert c.entity_name == "Nebula"
        assert c.predicate == "status"
        assert c.object_value == "live"
        assert c.confidence == 0.70
        assert c.provenance["extractor"] == "llm"
        assert c.provenance["rule"] == "triple"

    async def test_empty_facts_array_is_ignored_not_errored(self, db: Database) -> None:
        model = ScriptedModel({"facts": []})
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.IGNORED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_missing_facts_key_is_ignored_not_errored(self, db: Database) -> None:
        model = ScriptedModel({"assertions": []})
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.IGNORED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_non_list_facts_is_ignored_not_errored(self, db: Database) -> None:
        model = ScriptedModel({"facts": "not an array"})
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.IGNORED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_model_failure_is_malformed_with_zero_facts(self, db: Database) -> None:
        model = ScriptedModel(RuntimeError("inference worker died"))
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_raw_containment_refuses_before_model_is_called(self, db: Database) -> None:
        model = ScriptedModel(
            {"facts": [{"subject": "X", "predicate": "status", "object": "active"}]}
        )
        envelope = SignalEnvelope(
            id=str(uuid.uuid4()),
            channel="test",
            payload="text",
            raw="note <!-- paa:managed: block -->",
            external_id=None,
            received_at=None,
            persisted=False,
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(envelope)

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert model.calls == []  # containment refused the payload before inference
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_oversized_object_value_is_refused_not_truncated(
        self, db: Database
    ) -> None:
        model = ScriptedModel(
            {"facts": [{"subject": "X", "predicate": "status", "object": "x" * 9000}]}
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_bad_triples_are_skipped_not_fatal(self, db: Database) -> None:
        model = ScriptedModel(
            {
                "facts": [
                    {"subject": "Good", "predicate": "status", "object": "active"},
                    {"subject": "", "predicate": "status", "object": "bad subject"},
                    {"subject": "AlsoBad", "predicate": "", "object": "bad predicate"},
                    {"subject": "NoObject", "predicate": "status", "object": None},
                    "not a triple",
                    {"subject": "Fine", "predicate": "ok", "object": "live"},
                ]
            }
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 2
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 2

    async def test_candidate_ceiling_is_malformed(self, db: Database) -> None:
        triples = [
            {"subject": f"S{i}", "predicate": "status", "object": "ok"}
            for i in range(201)
        ]
        model = ScriptedModel({"facts": triples})
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_model_exception_is_redacted_before_it_leaks(self, db: Database) -> None:
        """A provider-echoed API key must not reach MalformedSignalError, logs
        or error_detail: it is redacted at the raise site in extract."""
        key = "sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
        model = ScriptedModel(
            ModelUnavailableError(
                f"upstream rejected key {key}",
                provider="openrouter",
                status_code=401,
            )
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert result.error is not None
        assert key not in result.error
        assert "sk-or-v1" not in result.error
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_transient_timeout_is_retried_then_succeeds(self, db: Database) -> None:
        """A timeout is transient: one retry later the model answers and the
        signal is processed instead of quarantined."""
        responses: list[Any] = [
            TimeoutError("upstream timed out"),
            {"facts": [{"subject": "S", "predicate": "status", "object": "active"}]},
        ]

        class RotatingModel:
            def __init__(self, script: list[Any]) -> None:
                self._script = list(script)
                self._index = 0
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def complete_structured(
                self, prompt: str, schema: dict[str, Any], **kwargs: Any
            ) -> dict[str, Any]:
                self.calls.append((prompt, schema))
                step = self._script[self._index % len(self._script)]
                self._index += 1
                if isinstance(step, BaseException):
                    raise step
                return step

        model = RotatingModel(responses)
        creator = MemoryCreator(
            db,
            extractor=LLMFactExtractor(
                model=model,
                max_retries=1,
                retry_backoff_base=0.0,
                retry_backoff_cap=0.0,
            ),
        )
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert len(model.calls) == 2  # first attempt timed out, second answered

    async def test_transient_429_exhaustion_is_quarantined_redacted(
        self, db: Database
    ) -> None:
        """A 429 is retried up to max_transport_retries, then the signal is
        quarantined with a redacted message (never a crash, never the key)."""
        key = "sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567"
        model = ScriptedModel(
            ModelUnavailableError(
                f"rate limited on key {key}",
                provider="openrouter",
                status_code=429,
            )
        )
        creator = MemoryCreator(
            db,
            extractor=LLMFactExtractor(
                model=model,
                max_retries=2,
                retry_backoff_base=0.0,
                retry_backoff_cap=0.0,
            ),
        )
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert result.error is not None
        assert len(model.calls) == 3  # initial attempt + 2 retries
        assert key not in result.error
        assert "sk-or-v1" not in result.error
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_per_call_timeout_is_bounded(self, db: Database) -> None:
        """complete_structured is bounded by a per-call timeout: a hung model
        cannot stall the batch forever."""

        async def slow(prompt: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(30.0)
            return {"facts": []}

        model = ScriptedModel({})
        model.complete_structured = slow  # type: ignore[method-assign]
        creator = MemoryCreator(
            db, extractor=LLMFactExtractor(model=model, timeout=0.1, max_retries=0)
        )
        # A bounded wait around the whole call guarantees the test itself cannot hang.
        async with asyncio.timeout(2.0):  # type: ignore[attr-defined]
            result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0

    async def test_non_transient_error_is_not_retried(self, db: Database) -> None:
        """A deterministic failure (bad credentials, malformed schema) is not
        retried: it is quarantined on the first attempt."""
        model = ScriptedModel(ValueError("bad configuration"))
        creator = MemoryCreator(
            db,
            extractor=LLMFactExtractor(
                model=model,
                max_retries=3,
                retry_backoff_base=0.0,
                retry_backoff_cap=0.0,
            ),
        )
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert len(model.calls) == 1  # no retries for a non-transient failure

    async def test_nul_byte_containment_refuses_before_model_is_called(
        self, db: Database
    ) -> None:
        """LLMFactExtractor must refuse a NUL byte in the raw payload *before*
        consulting the model — containment applies to the production extractor,
        not just the test double. Zero facts, model never called."""
        model = ScriptedModel(
            {"facts": [{"subject": "X", "predicate": "status", "object": "active"}]}
        )
        envelope = SignalEnvelope(
            id=str(uuid.uuid4()),
            channel="test",
            payload="text",
            raw="triple \x00 injection",
            external_id=None,
            received_at=None,
            persisted=False,
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(envelope)

        assert result.outcome is ProcessingOutcome.MALFORMED
        assert model.calls == []  # containment refused the payload before inference
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_active_facts") == 0


class TestLLMExtractorDropCounts:
    """C05: the LLM extractor's skipped entries are counted too.

    Reversal: removing the counters leaves every field 0. The extractor
    still skips bad entries (never fatal) -- only the observability
    changed, which is also why every pre-existing test above stays green.
    """

    async def test_skipped_malformed_entries_are_counted(self, db: Database) -> None:
        model = ScriptedModel(
            {
                "facts": [
                    {"subject": "Good", "predicate": "status", "object": "active"},
                    {"subject": "", "predicate": "status", "object": "bad subject"},
                    {"subject": "NoObject", "predicate": "status", "object": None},
                    "not a triple",
                    {"subject": "Empty", "predicate": "status", "object": ""},
                ]
            }
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert result.drop_counts.malformed_skipped == 4
        assert result.drop_counts.total == 4

    async def test_non_list_facts_response_is_counted_once(self, db: Database) -> None:
        model = ScriptedModel({"facts": "not an array"})
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.IGNORED
        assert result.drop_counts.malformed_skipped == 1
        assert result.drop_counts.total == 1

    async def test_extractor_without_drop_counts_yields_zero(self, db: Database) -> None:
        """Third-party extractors (protocol-compatible, no diagnostics
        attribute) still work: the creator probes and falls back to zero
        rather than failing. ScriptedLLMExtractor is exactly such an
        extractor."""
        extractor = ScriptedLLMExtractor(
            script=[{"subject": "Beta", "predicate": "status", "object": "live"}]
        )
        creator = MemoryCreator(db, extractor=extractor)
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert result.drop_counts.total == 0
        assert result.drop_counts.as_dict() == {
            "questions": 0,
            "hedges": 0,
            "pronouns": 0,
            "noise": 0,
            "no_match": 0,
            "subjectless": 0,
            "malformed_skipped": 0,
            "non_storable": 0,
        }


class TestLLMFirstPersonParity:
    """R-W3-003: LLM triples with first-person subjects map to ``user``,
    exactly like the rule path.

    Reversal: removing the mapping at the LLM seam creates entity "I"
    instead of "user" and the DB assertions fail.
    """

    async def test_llm_i_subject_lands_on_user_entity_row(self, db: Database) -> None:
        model = ScriptedModel(
            {"facts": [{"subject": "I", "predicate": "status", "object": "active"}]}
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        entities = await db.fetch_all(
            "SELECT canonical_name, importance_weight FROM hot_serving_entity_index"
        )
        assert [(r["canonical_name"], r["importance_weight"]) for r in entities] == [
            ("user", 0.3)
        ]

        # And a rule-path first-person fact lands on the very same row.
        rule_creator = MemoryCreator(db)
        rule_result = await rule_creator.process_signal(_row("I am based in Berlin"))
        assert rule_result.outcome is ProcessingOutcome.PROCESSED
        assert await db.fetch_value("SELECT COUNT(*) FROM hot_serving_entity_index") == 1


class TestLLMValidInputZeroDrops:
    """M1/TREV mutation (c): valid LLM input processes with zero drops.

    Reversal: an extractor that always returns empty would strand this
    test (IGNORED, 0 facts) -- it pins the new-suite contract rather than
    relying on the pre-existing LLM tests to catch the mutant.
    """

    async def test_valid_triple_processes_with_zero_drops(self, db: Database) -> None:
        model = ScriptedModel(
            {"facts": [{"subject": "Acme", "predicate": "status", "object": "active"}]}
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=model))
        result = await creator.process_signal(_row({"fakes": []}))

        assert result.outcome is ProcessingOutcome.PROCESSED
        assert len(result.facts_written) == 1
        assert result.drop_counts.total == 0


class _GatedModel:
    """A model that forces two concurrent extracts to interleave.

    Records entry order, releases both callers only once both are inside
    the model await, then returns scripted dicts in entry order. The
    barrier is synchronisation (an Event both must reach), not a timed
    sleep, so the interleaving is deterministic on every run.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._barrier = asyncio.Event()
        self._entered = 0

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        idx = self._entered
        self._entered += 1
        if self._entered >= 2:
            self._barrier.set()
        await self._barrier.wait()
        return self._responses[idx]


class TestConcurrentDrainCounters:
    """R-W3-004: two concurrent drains on a shared creator attribute
    drop counters to their own signals.

    Both drains skip a *nonzero* number of entries (2 vs 5) on purpose:
    with one shared mutable counters object, the second drain's reset
    lands between the first drain's model await and its increments, so
    at least one result necessarily reads a cumulative total and the
    sorted pair cannot be [2, 5] in *either* interleaving order
    (2-then-7 or 5-then-7). Both nonzero counts make the failure
    order-independent, hence deterministic.
    """

    async def test_concurrent_drains_keep_drop_totals_separate(self, db: Database) -> None:
        gated = _GatedModel(
            [
                {
                    "facts": [
                        {"subject": "Acme", "predicate": "status", "object": "active"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                        {"subject": "NoObject", "predicate": "status", "object": None},
                    ]
                },
                {
                    "facts": [
                        {"subject": "Beta", "predicate": "status", "object": "live"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                        {"subject": "", "predicate": "status", "object": "bad"},
                    ]
                },
            ]
        )
        creator = MemoryCreator(db, extractor=LLMFactExtractor(model=gated))
        first, second = await asyncio.gather(
            creator.process_signal(_row({"fakes": []})),
            creator.process_signal(_row({"fakes": []})),
        )

        assert first.outcome is ProcessingOutcome.PROCESSED
        assert second.outcome is ProcessingOutcome.PROCESSED
        assert len(first.facts_written) == 1
        assert len(second.facts_written) == 1
        assert sorted([first.drop_counts.total, second.drop_counts.total]) == [2, 5]
