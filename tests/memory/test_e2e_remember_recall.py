"""T-007 e2e remember -> recall after restart + observability pins (W4).

T-007a: write a fact through the real MemoryCreator into a real
NumpyVectorStore, close/reopen the relational store (restart), then let the
planner _semantic tier retrieve it by paraphrase (synonym + word-order
aware fake embedder). HashEmbedder is lexical-only, so it cannot pass this
test; the fake maps synonyms to canonical tokens before hashing, which is
what a sentence embedder does implicitly.

T-007b + MINORs: numeric pending_count on every embedding_drain INFO line,
INFO signal_processed on the IGNORED path, daemon preview discipline
(first-tick fallback honours dry-run; preview flag only on clean complete
passes), and the existing daemon.curation_completed duration_seconds.

Reversal: reverting the daemon dry-run passthrough deletes on the first
tick; reverting the flag gate consumes preview on partial/errored passes;
reverting pending_count removes the gauge; reverting the IGNORED INFO line
returns to DEBUG-only; reverting the payload keys (C01b) empties _semantic
content; removing the embedding sink wiring leaves the fact pending.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from paa.memory.creator import MemoryCreator, ProcessingOutcome, VectorEmbeddingSink
from paa.memory.facts import Fact, FactRepository
from paa.storage.relational.database import Database, to_iso, utc_now

_TOKEN_RE = re.compile(r"[a-z]+")

#: Synonyms the real sentence model would fold together. The fake maps
#: each to the token the stored fact actually carries, so a paraphrase
#: query embeds like the statement while a lexical hasher sees strangers.
_SYNONYMS: dict[str, str] = {
    "rapid": "swift",
    "quick": "swift",
    "state": "status",
    "condition": "status",
}


class _SynonymEmbedder:
    """Deterministic 384-d synonym/word-order-aware embedder.

    Bag-of-words hashing over synonym-normalised tokens: word order is
    irrelevant by construction (as with HashEmbedder) but synonyms collide
    into one bucket by design (unlike HashEmbedder). Dimensions match
    ACTIVE_FACTS (384) so a real NumpyVectorStore accepts the vectors.
    """

    dimensions = 384
    model_name = "synonym-fake-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(text) for text in texts]

    def _encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text.lower()):
            canonical = _SYNONYMS.get(token, token)
            digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest, "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


def _row(payload: Any, *, channel: str = "test") -> dict[str, Any]:
    """A signal shaped like a cold_lake_signals row (unpersisted)."""
    return {
        "id": str(uuid.uuid4()),
        "channel": channel,
        "external_id": None,
        "raw_payload": json.dumps(payload),
        "received_at": to_iso(utc_now()),
        "persisted": False,
    }


class _RecordingSink:
    """Sink returning every id it was handed (indexes the whole batch)."""

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        return [fact.id for fact in facts]


def _events(
    captured: list[dict[str, Any]], name: str
) -> list[dict[str, Any]]:
    return [entry for entry in captured if entry.get("event") == name]


async def _seed_pending(db: Database, *, n: int, prefix: str) -> list[str]:
    """Seed n pending facts via the repository (schema default pending)."""
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", f"{prefix}-entity")
    ids: list[str] = []
    for index in range(n):
        ids.append(await repo.add_fact(entity_id, "status", f"{prefix}-v{index}"))
    return ids


async def _stale_fact(db: Database, name: str) -> str:
    """One fact idle long enough to sit below its prune floor."""
    repo = FactRepository(db)
    entity_id = await repo.upsert_entity("concept", name)
    fact_id = await repo.add_fact(entity_id, "status", "stale", confidence=1.0)
    await db.execute(
        "UPDATE hot_serving_active_facts SET last_queried_at = ? WHERE id = ?",
        (to_iso(utc_now() - timedelta(days=3000)), fact_id),
    )
    return fact_id


def _stub_runtime(db: Database) -> Any:
    """The slice of Runtime the daemon curation tick reads."""
    from paa.config import MemorySettings

    return SimpleNamespace(
        settings=SimpleNamespace(memory=MemorySettings()),
        db=db,
        vector_store=None,
        archive_writer=None,
    )


async def test_e2e_paraphrase_retrieved_after_restart(tmp_path: Path) -> None:
    """Write -> close/reopen -> _semantic retrieves by synonym paraphrase.

    The vector store is the real NumpyVectorStore (exact cosine, persisted
    sidecar) and the creator is the real MemoryCreator, so the only fake is
    the embedder. The paraphrase shares no literal token with the fact
    except the entity name: "swift" arrives as "rapid", "status" as
    "state", and the order is shuffled.
    """
    from paa.agents.context_agents import PlannerContextBuilder
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    db_path = tmp_path / "e2e.db"
    vec_path = tmp_path / "vectors"
    embedder = _SynonymEmbedder()

    db = Database(db_path)
    await db.connect()
    store = NumpyVectorStore(vec_path)
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))
        result = await creator.process_signal(
            _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
        )
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]
        status = await db.fetch_value(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert status == "indexed"
    finally:
        await db.close()
        await store.close()

    # Restart: new connections, same files.
    db2 = Database(db_path)
    await db2.connect()
    store2 = NumpyVectorStore(vec_path)
    try:
        builder = PlannerContextBuilder(db=db2, vector_store=store2, embedder=embedder)
        elements = await builder._semantic("rapid state aurora")
        assert elements, "paraphrase _semantic returned nothing"
        assert elements[0].id == fact_id
        assert elements[0].content == "swift"
        assert elements[0].content != ""
        status = await db2.fetch_value(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert status == "indexed"
    finally:
        await db2.close()
        await store2.close()


async def test_recall_facts_synonym_only_miss(db: Database) -> None:
    """Keyword recall must miss a synonym-only query (documented boundary).

    Vector recall finds "rapid" -> "swift" via the synonym map; the coder
    recall_facts tool (DECISION T-007: keyword-only) resolves "rapid" to
    no entity and LIKE-matches no predicate/object, so it must return zero.
    """
    from paa.coder.runtime_tools import _RecallFacts

    creator = MemoryCreator(db)
    result = await creator.process_signal(
        _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
    )
    assert result.outcome is ProcessingOutcome.PROCESSED

    tool = _RecallFacts(SimpleNamespace(db=db))  # type: ignore[arg-type]
    payload = json.loads(str(await tool.run({"query": "rapid"})))
    assert payload["count"] == 0
    assert payload["facts"] == []


async def test_ignored_emits_info_signal_processed(db: Database) -> None:
    """R-DELTA-002: an all-dropped signal logs INFO signal_processed.

    Same event name and key shape as the PROCESSED line (facts=0) so one
    dashboard query sees every outcome. Reversal: restoring the DEBUG
    signal_ignored line leaves no INFO line and this test fails.
    """
    creator = MemoryCreator(db)
    with structlog.testing.capture_logs() as captured:
        result = await creator.process_signal(_row("thanks, that's great!"))
    assert result.outcome is ProcessingOutcome.IGNORED
    lines = _events(captured, "memory.signal_processed")
    assert len(lines) == 1
    entry = lines[0]
    assert entry.get("log_level") == "info"
    assert entry.get("facts") == 0
    assert entry.get("dropped") == result.drop_counts.total
    assert entry.get("drop_counts") == result.drop_counts.as_dict()
    assert entry.get("outcome") == "ignored"
    assert not _events(captured, "memory.signal_ignored")


async def test_pending_count_gauge_after_partial_drain(db: Database) -> None:
    """T-007b: the drain INFO line carries the true remaining pending count.

    Three pending, limit=2 drains two, so pending_count must be 1 (not the
    batch size 2) — a COUNT(*) read after mark_embedded, not len(batch).
    """
    await _seed_pending(db, n=3, prefix="gauge")
    creator = MemoryCreator(db, embedding_sink=_RecordingSink())
    with structlog.testing.capture_logs() as captured:
        assert await creator.drain_pending_embeddings(limit=2) == 2
    lines = _events(captured, "memory.embedding_drain")
    assert len(lines) == 1
    assert lines[0].get("batch_size") == 2
    assert lines[0].get("indexed") == 2
    assert lines[0].get("pending_count") == 1


async def test_pending_count_gauge_when_sink_absent(db: Database) -> None:
    """The early INFO line (no sink) also carries the numeric gauge."""
    await _seed_pending(db, n=2, prefix="gauge-nop")
    creator = MemoryCreator(db)
    with structlog.testing.capture_logs() as captured:
        assert await creator.drain_pending_embeddings() == 0
    lines = _events(captured, "memory.embedding_drain")
    assert len(lines) == 1
    assert lines[0].get("pending_count") == 2
    assert lines[0].get("backlog_remaining") is True


async def test_construction_failure_fallback_respects_preview(
    db: Database, monkeypatch: Any
) -> None:
    """R-W2-D-01: the fallback sweep previews on the first tick.

    With MemoryCurator construction broken and preview unconsumed, the
    fallback must NOT delete the stale fact; once the preview is consumed
    the same fallback deletes it.
    """
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon

    stale_id = await _stale_fact(db, "FallbackPreview")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("curator wiring exploded")

    monkeypatch.setattr(curator_mod, "MemoryCurator", _boom)
    daemon = Daemon(_stub_runtime(db))

    assert await daemon._run_curation_tick() is None
    remaining = await db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 1

    daemon._curate_preview_done = True
    assert await daemon._run_curation_tick() is None
    remaining = await db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 0


async def test_partial_preview_does_not_consume_flag(
    db: Database, monkeypatch: Any
) -> None:
    """R-W2-D-02: a budget-stopped preview leaves the flag unset."""
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon
    from paa.memory.curator import CurationPhase, CurationReport

    now = utc_now()

    class _Partial:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_maintenance(self, **kwargs: Any) -> CurationReport:
            return CurationReport(
                started_at=now,
                finished_at=now,
                stopped_early_at=CurationPhase.PRUNE_EDGES,
            )

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Partial)
    daemon = Daemon(_stub_runtime(db))
    report = await daemon._run_curation_tick()
    assert report is not None
    assert report.completed_fully is False
    assert daemon._curate_preview_done is False
    assert daemon._curate_next_phase is CurationPhase.PRUNE_EDGES


async def test_errored_preview_does_not_consume_flag(
    db: Database, monkeypatch: Any
) -> None:
    """R-W2-D-02: a complete-but-errored preview leaves the flag unset."""
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon
    from paa.memory.curator import CurationReport

    now = utc_now()

    class _Errored:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_maintenance(self, **kwargs: Any) -> CurationReport:
            return CurationReport(started_at=now, finished_at=now, errors=1)

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Errored)
    daemon = Daemon(_stub_runtime(db))
    report = await daemon._run_curation_tick()
    assert report is not None
    assert report.completed_fully is True
    assert report.errors == 1
    assert daemon._curate_preview_done is False


async def test_complete_preview_consumes_flag(
    db: Database, monkeypatch: Any
) -> None:
    """R-W2-D-02: a clean complete preview consumes the flag."""
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon
    from paa.memory.curator import CurationReport

    now = utc_now()

    class _Complete:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_maintenance(self, **kwargs: Any) -> CurationReport:
            return CurationReport(started_at=now, finished_at=now)

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Complete)
    daemon = Daemon(_stub_runtime(db))
    report = await daemon._run_curation_tick()
    assert report is not None
    assert daemon._curate_preview_done is True


async def test_curation_completed_pins_duration_seconds(
    db: Database, monkeypatch: Any
) -> None:
    """T-007b: daemon.curation_completed carries duration_seconds (total).

    Per-phase timings do not exist on CurationReport (curator.py is
    read-only) — the total duration is the honest boundary, pinned here.
    """
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon
    from paa.memory.curator import CurationReport

    started = utc_now()

    class _Timed:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_maintenance(self, **kwargs: Any) -> CurationReport:
            return CurationReport(
                started_at=started,
                finished_at=started + timedelta(seconds=1.5),
                facts_evicted=2,
            )

    monkeypatch.setattr(curator_mod, "MemoryCurator", _Timed)
    daemon = Daemon(_stub_runtime(db))
    with structlog.testing.capture_logs() as captured:
        report = await daemon._run_curation_tick()
    assert report is not None
    lines = _events(captured, "daemon.curation_completed")
    assert len(lines) == 1
    assert lines[0].get("duration_seconds") == 1.5
    assert lines[0].get("facts_evicted") == 2
