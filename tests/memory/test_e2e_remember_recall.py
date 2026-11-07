"""T-007 e2e remember -> recall after restart + observability pins (W4).

T-007a: write a fact through the real MemoryCreator into a real
NumpyVectorStore, close/reopen the relational store (restart), then let the
planner _semantic tier retrieve it by paraphrase (synonym + word-order
aware fake embedder). HashEmbedder is lexical-only, so it cannot pass this
test; the fake maps synonyms to canonical tokens before hashing, which is
what a sentence embedder does implicitly.

T-007b + MINORs: numeric pending_count on every embedding_drain INFO line
and on the drain-failed ERROR line (R-W4-006), idle drains skip the COUNT
when the queue is known empty (R-W4-003), INFO signal_processed on the
IGNORED path with a uniform ``outcome`` on both branches (R-W4-004),
daemon preview discipline (first-tick fallback honours dry-run and then
consumes the preview, preview flag only on clean complete passes, and a
loud preview-incomplete warning while an erroring phase blocks deletes),
and the existing daemon.curation_completed duration_seconds.

Reversal: reverting the daemon dry-run passthrough deletes on the first
tick; reverting the fallback preview consumption leaves the second tick
dry-run forever (R-W4-001); reverting the flag gate consumes preview on
partial/errored passes; reverting pending_count removes the gauge;
reverting the idle skip re-runs the COUNT on idle ticks; reverting the
IGNORED INFO line returns to DEBUG-only; reverting the payload keys
(C01b) empties _semantic content; removing the *creator-level* embedding
sink wiring leaves the fact pending. Runtime-level wiring
(``_build_memory_creator`` threading) is pinned separately by
``test_runtime_threaded_creator_paraphrase_retrieved_after_restart`` below
and by ``tests/runtime/test_memory_creator.py`` at suite scope.
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
    # R-W4-T-04: pin the fake width to the collection spec, so a spec change
    # fails here with a legible assertion instead of a vector-shape error.
    assert embedder.dimensions == ACTIVE_FACTS.dimensions

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


async def test_recall_facts_synonym_only_miss(db: Database, tmp_path: Path) -> None:
    """Keyword recall misses a synonym-only query; the P-004 fallback does not.

    Two halves, and both matter (ADR-0039 degrade-never-raise): with the
    vector store and the synonym embedder live, ``recall_facts("rapid")``
    runs the vector fallback and returns the "swift" fact, touched. With
    either backend absent — the same query against a bare ``db`` namespace —
    it degrades to the keyword-only result (count 0, no error). DECISION
    T-007 (keyword-only) is superseded by ADR-0039; the old single-half
    form pinned that decision and is updated deliberately here.
    """
    from paa.coder.runtime_tools import _RecallFacts
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    embedder = _SynonymEmbedder()
    assert embedder.dimensions == ACTIVE_FACTS.dimensions
    store = NumpyVectorStore(tmp_path / "vectors")
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))
        result = await creator.process_signal(
            _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
        )
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]

        # Half 1: store + embedder present -> vector fallback hits.
        tool = _RecallFacts(SimpleNamespace(db=db, vector_store=store, embedder=embedder))  # type: ignore[arg-type]
        payload = json.loads(str(await tool.run({"query": "rapid"})))
        assert payload["count"] == 1
        assert payload["facts"] != []
        assert payload["facts"][0]["object_value"] == "swift"
        # The fallback's hit is touched like every other tier's.
        use_count = await db.fetch_value(
            "SELECT use_count FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert use_count == 1
        # The note states the tiered algorithm verbatim (FINAL-01 then
        # ADR-0039 changed it twice; assert the current text, not a
        # substring anything). Reversal: restoring the stale planner-side
        # note fails this.
        assert payload["note"] == (
            "keyword recall first; a vector (semantic) fallback runs "
            "only on a keyword miss when the vector backend and "
            "embedder are live — otherwise keyword-only "
            "(degrade-never-raise)"
        )

        # Half 2: same query, no backends -> degrade to keyword-only, count 0.
        tool_absent = _RecallFacts(SimpleNamespace(db=db))  # type: ignore[arg-type]
        payload_absent = json.loads(str(await tool_absent.run({"query": "rapid"})))
        assert payload_absent["count"] == 0
        assert payload_absent["facts"] == []
        assert payload_absent["note"] == payload["note"]
    finally:
        await store.close()


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
    """R-W2-D-01 + R-W4-001: the fallback previews, consumes, then deletes.

    With MemoryCurator construction broken, the first real fallback tick
    previews (stale fact remains, preview consumed) and the second real
    tick deletes. Two ticks are driven with no manual flag flip: pre-fix
    the second tick stayed dry-run (preview never consumed) and this test
    fails with remaining 1 == 0.
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
    assert daemon._curate_preview_done is True

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


async def test_processed_emits_outcome_processed(db: Database) -> None:
    """R-W4-004: the PROCESSED INFO line carries outcome="processed".

    IGNORED already carried outcome="ignored"; dashboards counting
    memory.signal_processed as "processed" overcounted ignored signals.
    Both branches now share the key. Reversal: removing outcome from the
    PROCESSED line makes ``entry.get("outcome")`` fall to None and fails.
    """
    creator = MemoryCreator(db)
    with structlog.testing.capture_logs() as captured:
        result = await creator.process_signal(
            _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
        )
    assert result.outcome is ProcessingOutcome.PROCESSED
    lines = _events(captured, "memory.signal_processed")
    assert len(lines) == 1
    entry = lines[0]
    assert entry.get("log_level") == "info"
    assert entry.get("outcome") == "processed"
    assert entry.get("facts") == 1
    assert entry.get("dropped") == result.drop_counts.total
    assert entry.get("drop_counts") == result.drop_counts.as_dict()


class _FailingSink:
    """Sink whose enqueue always raises (drain-failure gauge probe)."""

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        raise RuntimeError("vector backend on fire")


async def test_drain_failure_line_carries_pending_count(db: Database) -> None:
    """R-W4-006: embedding_drain_failed carries the numeric gauge.

    Two pending rows, sink raises: drain returns 0, facts stay pending, and
    the ERROR line reports pending_count==2 and backlog_remaining True
    (derived, not hardcoded). Pre-fix the line had no pending_count key.
    """
    await _seed_pending(db, n=2, prefix="gauge-fail")
    creator = MemoryCreator(db, embedding_sink=_FailingSink())
    with structlog.testing.capture_logs() as captured:
        assert await creator.drain_pending_embeddings() == 0
    lines = _events(captured, "memory.embedding_drain_failed")
    assert len(lines) == 1
    assert lines[0].get("log_level") == "error"
    assert lines[0].get("batch_size") == 2
    assert lines[0].get("indexed") == 0
    assert lines[0].get("pending_count") == 2
    assert lines[0].get("backlog_remaining") is True
    remaining = await db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE embedding_status = 'pending'"
    )
    assert remaining == 2


async def test_idle_drain_skips_pending_count(db: Database) -> None:
    """R-W4-003: an idle drain reports 0 without querying the COUNT.

    The drain is spied via _pending_embedding_count: with zero pending the
    spy must not fire at all (the same-predicate fetch already proved the
    queue empty), yet the INFO line still carries pending_count==0. With
    pending rows and no sink the COUNT does run (spy fires once, gauge 2).
    Pre-fix both cases COUNTed once, so the idle spy assertion fails.
    """
    creator = MemoryCreator(db, embedding_sink=_RecordingSink())
    calls: list[int] = []
    original = creator._pending_embedding_count

    async def _spy() -> int:
        calls.append(1)
        return await original()

    creator._pending_embedding_count = _spy  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as captured:
        assert await creator.drain_pending_embeddings() == 0
    lines = _events(captured, "memory.embedding_drain")
    assert len(lines) == 1
    assert lines[0].get("batch_size") == 0
    assert lines[0].get("pending_count") == 0
    assert lines[0].get("backlog_remaining") is False
    assert calls == []


async def test_pending_count_gauge_when_sink_absent_counts_backlog(db: Database) -> None:
    """R-W4-003 companion: a non-empty queue with no sink still COUNTs once.

    The idle skip must not swallow the backlog gauge: two pending, no sink,
    one spy hit, pending_count==2. Guards against an over-broad skip that
    zeroes the gauge whenever the sink is absent.
    """
    await _seed_pending(db, n=2, prefix="gauge-spy")
    creator = MemoryCreator(db)
    calls: list[int] = []
    original = creator._pending_embedding_count

    async def _spy() -> int:
        calls.append(1)
        return await original()

    creator._pending_embedding_count = _spy  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as captured:
        assert await creator.drain_pending_embeddings() == 0
    lines = _events(captured, "memory.embedding_drain")
    assert len(lines) == 1
    assert lines[0].get("pending_count") == 2
    assert lines[0].get("backlog_remaining") is True
    assert len(calls) == 1


async def test_errored_preview_blocks_deletes_and_warns(
    db: Database, monkeypatch: Any
) -> None:
    """R-W4-002: one permanently-erroring phase blocks deletes (safe).

    Curator isolates per-phase errors (errors increments, loop continues),
    but the daemon gate stays ``completed_fully and errors == 0`` because
    CurationReport has no per-phase outcome: relaxing to
    completed_fully-only could delete phases that never previewed cleanly.
    A clean DECAY-phase stale fact therefore survives two errored preview
    ticks, the flag never sets, and each tick logs
    daemon.curation_preview_incomplete with errors==1.
    """
    import paa.memory.curator as curator_mod
    from paa.daemon import Daemon
    from paa.memory.curator import CurationReport

    stale_id = await _stale_fact(db, "ErrorBlocked")

    now = utc_now()

    class _AlwaysErrored:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run_maintenance(self, **kwargs: Any) -> CurationReport:
            return CurationReport(started_at=now, finished_at=now, errors=1)

    monkeypatch.setattr(curator_mod, "MemoryCurator", _AlwaysErrored)
    daemon = Daemon(_stub_runtime(db))

    for _ in range(2):
        with structlog.testing.capture_logs() as captured:
            report = await daemon._run_curation_tick()
        assert report is not None
        assert report.errors == 1
        assert daemon._curate_preview_done is False
        warnings = _events(captured, "daemon.curation_preview_incomplete")
        assert len(warnings) == 1
        assert warnings[0].get("errors") == 1
        assert warnings[0].get("completed_fully") is True

    remaining = await db.fetch_value(
        "SELECT COUNT(*) FROM hot_serving_active_facts WHERE id = ?", (stale_id,)
    )
    assert remaining == 1


async def test_runtime_threaded_creator_paraphrase_retrieved_after_restart(
    tmp_path: Path,
) -> None:
    """T-01: the production threading path (_build_memory_creator) recalls.

    Builds the creator via ``paa.runtime._build_memory_creator`` with the
    real store + synonym embedder, writes through it, restarts, and lets
    _semantic hit by paraphrase. Pre-fix the headline claim ("removing the
    embedding sink wiring leaves the fact pending") was only true at
    creator level; this test fails if the runtime threading stops attaching
    the sink (identity + indexed + hit assertions).
    """
    from paa.agents.context_agents import PlannerContextBuilder
    from paa.config import MemorySettings, Settings
    from paa.runtime import _build_memory_creator
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    home = tmp_path / "settings_home"
    home.mkdir(parents=True, exist_ok=True)
    settings = Settings(home=home, memory=MemorySettings())

    db_path = tmp_path / "e2e.db"
    vec_path = tmp_path / "vectors"
    embedder = _SynonymEmbedder()
    assert embedder.dimensions == ACTIVE_FACTS.dimensions

    db = Database(db_path)
    await db.connect()
    store = NumpyVectorStore(vec_path)
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        creator = _build_memory_creator(
            settings, db, SimpleNamespace(), vector_store=store, embedder=embedder
        )
        assert isinstance(creator._sink, VectorEmbeddingSink)
        assert creator._sink._store is store
        assert creator._sink._embedder is embedder
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

    db2 = Database(db_path)
    await db2.connect()
    store2 = NumpyVectorStore(vec_path)
    try:
        builder = PlannerContextBuilder(db=db2, vector_store=store2, embedder=embedder)
        elements = await builder._semantic("rapid state aurora")
        assert elements
        assert elements[0].id == fact_id
        assert elements[0].content == "swift"
    finally:
        await db2.close()
        await store2.close()


async def test_paraphrase_decoy_ranking(tmp_path: Path) -> None:
    """T-02: a 2-fact decoy pins ranking, not just retrieval.

    Two facts share predicate/status and object/swift and differ only in
    the subject: Aurora (the queried entity) vs Borealis (the decoy).
    Query "rapid state aurora" must rank the Aurora fact first with
    strictly higher relevance. A single-candidate e2e cannot catch an
    inverted sort or a truncation bug (mutation g). The subject-change
    shape (not same-entity/different-object) keeps the decoy out of the
    contradiction quarantine: incumbents are filtered by entity_id, so a
    same-entity second value would quarantine instead of ranking.
    """
    from paa.agents.context_agents import PlannerContextBuilder
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    db_path = tmp_path / "decoy.db"
    vec_path = tmp_path / "vectors"
    embedder = _SynonymEmbedder()
    assert embedder.dimensions == ACTIVE_FACTS.dimensions

    db = Database(db_path)
    await db.connect()
    store = NumpyVectorStore(vec_path)
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        creator = MemoryCreator(db, embedding_sink=VectorEmbeddingSink(store, embedder))
        swift = await creator.process_signal(
            _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
        )
        decoy = await creator.process_signal(
            _row({"facts": [{"subject": "Borealis", "predicate": "status", "object": "swift"}]})
        )
        assert swift.outcome is ProcessingOutcome.PROCESSED
        assert decoy.outcome is ProcessingOutcome.PROCESSED
        builder = PlannerContextBuilder(db=db, vector_store=store, embedder=embedder)
        elements = await builder._semantic("rapid state aurora")
        assert len(elements) >= 2
        assert elements[0].id == swift.facts_written[0]
        assert elements[0].content == "swift"
        by_id = {element.id: element for element in elements}
        decoy_element = by_id[decoy.facts_written[0]]
        assert elements[0].relevance > decoy_element.relevance
    finally:
        await db.close()
        await store.close()


async def test_drain_backlog_then_recall(tmp_path: Path) -> None:
    """R-W4-005 / P1: the backlog leg (write pending -> drain -> recall).

    The synchronous write-path e2e never exercises drain_pending_embeddings
    (process_signal indexes immediately when a sink is attached). This test
    writes with NO sink (fact stays pending, semantic misses), restarts,
    then drains with a sink attached and asserts indexed + paraphrase hit.
    """
    from paa.agents.context_agents import PlannerContextBuilder
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    db_path = tmp_path / "backlog.db"
    vec_path = tmp_path / "vectors"
    embedder = _SynonymEmbedder()
    assert embedder.dimensions == ACTIVE_FACTS.dimensions

    db = Database(db_path)
    await db.connect()
    store = NumpyVectorStore(vec_path)
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        creator = MemoryCreator(db)
        result = await creator.process_signal(
            _row({"facts": [{"subject": "Aurora", "predicate": "status", "object": "swift"}]})
        )
        assert result.outcome is ProcessingOutcome.PROCESSED
        fact_id = result.facts_written[0]
        status = await db.fetch_value(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert status == "pending"
        builder = PlannerContextBuilder(db=db, vector_store=store, embedder=embedder)
        assert await builder._semantic("rapid state aurora") == []
    finally:
        await db.close()
        await store.close()

    db2 = Database(db_path)
    await db2.connect()
    store2 = NumpyVectorStore(vec_path)
    try:
        await store2.ensure_spec(ACTIVE_FACTS)
        draining = MemoryCreator(db2, embedding_sink=VectorEmbeddingSink(store2, embedder))
        assert await draining.drain_pending_embeddings() == 1
        status = await db2.fetch_value(
            "SELECT embedding_status FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert status == "indexed"
        builder2 = PlannerContextBuilder(db=db2, vector_store=store2, embedder=embedder)
        elements = await builder2._semantic("rapid state aurora")
        assert elements
        assert elements[0].id == fact_id
        assert elements[0].content == "swift"
    finally:
        await db2.close()
        await store2.close()


async def test_paraphrase_recall_via_handle_advances_use_count(
    tmp_path: Path,
) -> None:
    """FINAL-03: the flagship paraphrase path proves touch through handle().

    The other e2e legs call ``builder._semantic(...)`` directly, which never
    touches the fact — touch lives in ``handle()`` over ``packet.elements``
    (context_agents.py:328). This test drives the real planner handle path
    with the same paraphrase goal and asserts ``use_count`` advanced by one
    for the delivered fact. Reversal: removing the touch call from
    ``PlannerContextBuilder.handle`` fails the ``use_count == 1`` assertion
    while the direct ``_semantic`` legs still pass.
    """
    from paa.agents.base import AgentContext, AgentMessage, MessageType
    from paa.agents.context_agents import PlannerContextBuilder
    from paa.core.types import ComplexityModality, PermissionMode
    from paa.storage.vector import ACTIVE_FACTS, NumpyVectorStore

    db_path = tmp_path / "handle_touch.db"
    vec_path = tmp_path / "vectors"
    embedder = _SynonymEmbedder()
    assert embedder.dimensions == ACTIVE_FACTS.dimensions

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
        before = await db.fetch_value(
            "SELECT use_count FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert before == 0

        builder = PlannerContextBuilder(db=db, vector_store=store, embedder=embedder)
        message = AgentMessage(
            task_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            sender="orchestrator",
            recipient="context_builder_planner",
            intent=MessageType.CONTEXT_REQUEST,
            payload={"goal": "rapid state aurora"},
        )
        ctx = AgentContext(
            correlation_id=uuid.uuid4(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.AUTO,
        )
        handled = await builder.handle(message, ctx)
        assert handled.ok
        after = await db.fetch_value(
            "SELECT use_count FROM hot_serving_active_facts WHERE id = ?",
            (fact_id,),
        )
        assert after == 1
    finally:
        await db.close()
        await store.close()
