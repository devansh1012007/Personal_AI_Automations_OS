"""Vector recall layer.

The point of this file is *behavioural equivalence*. The runtime picks its
vector backend at boot from ``StorageSettings.backend_vector``, so an integrator
reading ``search()`` must not have to know which engine answered. Almost every
test here is therefore parametrised over both backends and asserts on the
contract, not the implementation — plus one test that runs both engines side by
side over the same data and diffs the results directly, because "each passed its
own copy of the suite" is a weaker claim than "they returned the same rows".

Qdrant tests are marked ``requires_vector`` and skip when the extra is absent.
"""

from __future__ import annotations

import importlib.util
import itertools
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from paa.config import ModelSettings, Settings, StorageSettings
from paa.core.errors import StorageError
from paa.storage.vector import (
    ABSOLUTE_FACTS_INDEX,
    ACTIVE_FACTS,
    RFC_COLLECTIONS,
    HashEmbedder,
    NumpyVectorStore,
    QdrantVectorStore,
    SearchHit,
    VectorPoint,
    VectorStore,
    cosine_similarity,
    get_embedder,
    get_vector_store,
)

HAS_QDRANT = importlib.util.find_spec("qdrant_client") is not None
HAS_SENTENCE_TRANSFORMERS = importlib.util.find_spec("sentence_transformers") is not None

BACKENDS = [
    pytest.param("numpy", id="numpy"),
    pytest.param(
        "qdrant",
        id="qdrant",
        marks=[
            pytest.mark.requires_vector,
            pytest.mark.skipif(not HAS_QDRANT, reason="qdrant-client is not installed"),
        ],
    ),
]

DIM = 8
COLLECTION = "active_facts"

StoreOpener = Callable[[], Awaitable[VectorStore]]


def build_store(backend: str, root: Path) -> VectorStore:
    if backend == "numpy":
        return NumpyVectorStore(root / "numpy")
    return QdrantVectorStore(path=root / "qdrant")


def unit(*values: float) -> list[float]:
    """A DIM-wide unit vector from the leading components given."""
    vector = np.zeros(DIM, dtype=np.float32)
    vector[: len(values)] = values
    norm = float(np.linalg.norm(vector))
    assert norm > 0, "test vectors must not be degenerate"
    return (vector / norm).tolist()


def ids_of(hits: list[SearchHit]) -> list[str]:
    return [hit.id for hit in hits]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
async def open_store(backend: str, tmp_path: Path):
    """Open a store at a stable path; calling again reopens the same data.

    The previous handle is closed first. Embedded Qdrant holds an exclusive file
    lock on its directory, so a reopen test that left the old client alive would
    deadlock rather than fail informatively.
    """
    live: list[VectorStore] = []

    async def _open() -> VectorStore:
        for store in live:
            await store.close()
        live.clear()
        store = build_store(backend, tmp_path)
        live.append(store)
        return store

    yield _open

    for store in live:
        await store.close()


@pytest.fixture
async def store(open_store: StoreOpener) -> VectorStore:
    """A store with the ``active_facts`` collection already provisioned."""
    instance = await open_store()
    await instance.ensure_collection(COLLECTION, DIM)
    return instance


async def seed(store: VectorStore) -> None:
    """Four points along distinguishable axes with mixed payloads."""
    await store.upsert(
        COLLECTION,
        [
            VectorPoint(
                id="fact-north",
                vector=unit(1.0, 0.0),
                payload={"entity_id": "e1", "memory_scope": "global", "predicate": "owns"},
            ),
            VectorPoint(
                id="fact-northeast",
                vector=unit(1.0, 1.0),
                payload={"entity_id": "e1", "memory_scope": "session-a", "predicate": "owns"},
            ),
            VectorPoint(
                id="fact-east",
                vector=unit(0.0, 1.0),
                payload={"entity_id": "e2", "memory_scope": "session-b", "predicate": "reads"},
            ),
            VectorPoint(
                id="fact-south",
                vector=unit(-1.0, 0.0),
                payload={"entity_id": "e2", "memory_scope": "global", "predicate": "reads"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Collection lifecycle
# ---------------------------------------------------------------------------


async def test_ensure_collection_is_idempotent(open_store: StoreOpener) -> None:
    store = await open_store()

    assert await store.collection_exists(COLLECTION) is False
    await store.ensure_collection(COLLECTION, DIM)
    assert await store.collection_exists(COLLECTION) is True

    await store.upsert(
        COLLECTION, [VectorPoint(id="p1", vector=unit(1.0), payload={"entity_id": "e1"})]
    )

    # A second ensure must be a no-op, not a truncate.
    await store.ensure_collection(COLLECTION, DIM)
    await store.ensure_collection(COLLECTION, DIM)
    assert await store.count(COLLECTION) == 1


async def test_ensure_collection_rejects_a_width_change(store: VectorStore) -> None:
    with pytest.raises(StorageError) as excinfo:
        await store.ensure_collection(COLLECTION, DIM * 2)
    assert excinfo.value.substrate in {"qdrant", "numpy"}


async def test_rfc_collections_are_provisioned(open_store: StoreOpener) -> None:
    store = await open_store()
    await store.ensure_rfc_collections()

    for spec in RFC_COLLECTIONS:
        assert await store.collection_exists(spec.name) is True
        assert await store.count(spec.name) == 0


async def test_operations_on_a_missing_collection_raise(open_store: StoreOpener) -> None:
    store = await open_store()
    for operation in (
        store.count("nope"),
        store.search("nope", unit(1.0)),
        store.delete("nope", ["x"]),
    ):
        with pytest.raises(StorageError):
            await operation


async def test_invalid_collection_name_is_refused(open_store: StoreOpener) -> None:
    store = await open_store()
    with pytest.raises(StorageError):
        await store.ensure_collection("../escape", DIM)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_returns_nearest_first(store: VectorStore) -> None:
    await seed(store)

    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=3)

    assert ids_of(hits) == ["fact-north", "fact-northeast", "fact-east"]
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].score == pytest.approx(0.7071, abs=1e-3)
    assert hits[2].score == pytest.approx(0.0, abs=1e-5)
    # Scores must be monotonically non-increasing.
    assert all(a.score >= b.score for a, b in itertools.pairwise(hits))


async def test_search_respects_limit(store: VectorStore) -> None:
    await seed(store)
    assert len(await store.search(COLLECTION, unit(1.0, 0.0), limit=2)) == 2
    assert len(await store.search(COLLECTION, unit(1.0, 0.0), limit=99)) == 4


async def test_search_returns_payloads_verbatim(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=1)
    assert hits[0].payload == {
        "entity_id": "e1",
        "memory_scope": "global",
        "predicate": "owns",
    }


async def test_score_threshold_is_inclusive_and_drops_the_rest(store: VectorStore) -> None:
    await seed(store)

    strict = await store.search(COLLECTION, unit(1.0, 0.0), limit=10, score_threshold=0.9)
    assert ids_of(strict) == ["fact-north"]

    loose = await store.search(COLLECTION, unit(1.0, 0.0), limit=10, score_threshold=0.5)
    assert ids_of(loose) == ["fact-north", "fact-northeast"]

    # Nothing clears a threshold above the cosine ceiling.
    assert await store.search(COLLECTION, unit(1.0, 0.0), score_threshold=1.5) == []


async def test_search_on_an_empty_collection_is_empty(store: VectorStore) -> None:
    assert await store.search(COLLECTION, unit(1.0, 0.0)) == []


async def test_search_rejects_a_wrong_width_query(store: VectorStore) -> None:
    with pytest.raises(StorageError):
        await store.search(COLLECTION, [1.0, 0.0])


async def test_search_rejects_a_zero_limit(store: VectorStore) -> None:
    with pytest.raises(StorageError):
        await store.search(COLLECTION, unit(1.0), limit=0)


# ---------------------------------------------------------------------------
# Payload filters
# ---------------------------------------------------------------------------


async def test_filter_equality(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=10, filters={"entity_id": "e2"})
    assert set(ids_of(hits)) == {"fact-east", "fact-south"}


async def test_filter_membership(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(
        COLLECTION,
        unit(1.0, 0.0),
        limit=10,
        filters={"memory_scope": ["global", "session-b"]},
    )
    assert set(ids_of(hits)) == {"fact-north", "fact-east", "fact-south"}


async def test_multiple_filter_fields_are_anded(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(
        COLLECTION,
        unit(1.0, 0.0),
        limit=10,
        filters={"entity_id": "e1", "memory_scope": ["session-a", "session-z"]},
    )
    assert ids_of(hits) == ["fact-northeast"]


async def test_filter_on_a_missing_field_matches_nothing(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(
        COLLECTION, unit(1.0, 0.0), limit=10, filters={"never_set": "anything"}
    )
    assert hits == []


async def test_empty_membership_list_matches_nothing(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=10, filters={"entity_id": []})
    assert hits == []


async def test_empty_filter_dict_is_no_filter(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=10, filters={})
    assert len(hits) == 4


async def test_filters_compose_with_threshold(store: VectorStore) -> None:
    await seed(store)
    hits = await store.search(
        COLLECTION,
        unit(1.0, 0.0),
        limit=10,
        score_threshold=0.5,
        filters={"predicate": "owns"},
    )
    assert ids_of(hits) == ["fact-north", "fact-northeast"]


async def test_float_filter_values_are_refused(store: VectorStore) -> None:
    """See normalise_filters: floats would make the two backends disagree."""
    await seed(store)
    with pytest.raises(StorageError):
        await store.search(COLLECTION, unit(1.0, 0.0), filters={"importance": 0.5})


async def test_bool_filter_values_are_refused(store: VectorStore) -> None:
    """Embedded Qdrant says ``True == 1``; a Qdrant server does not.

    Rather than pick a winner and have the meaning of a filter change on the day
    someone sets ``qdrant_url``, the ambiguous input is rejected. See
    ``normalise_filters``.
    """
    await seed(store)
    with pytest.raises(StorageError) as excinfo:
        await store.search(COLLECTION, unit(1.0, 0.0), filters={"pinned": True})
    assert "bool" in str(excinfo.value)


async def test_int_filters_work(store: VectorStore) -> None:
    await store.upsert(
        COLLECTION,
        [
            VectorPoint(id="v1", vector=unit(1.0), payload={"version": 1}),
            VectorPoint(id="v2", vector=unit(0.0, 1.0), payload={"version": 2}),
        ],
    )
    assert ids_of(await store.search(COLLECTION, unit(1.0), filters={"version": 1})) == ["v1"]
    assert set(
        ids_of(await store.search(COLLECTION, unit(1.0), filters={"version": [1, 2]}))
    ) == {"v1", "v2"}
    # Ints and their string forms are distinct in both backends.
    assert await store.search(COLLECTION, unit(1.0), filters={"version": "1"}) == []


# ---------------------------------------------------------------------------
# Upsert, delete, count
# ---------------------------------------------------------------------------


async def test_upsert_replaces_by_id(store: VectorStore) -> None:
    await seed(store)
    await store.upsert(
        COLLECTION,
        [
            VectorPoint(
                id="fact-north",
                vector=unit(0.0, 0.0, 1.0),
                payload={"entity_id": "e9", "memory_scope": "global"},
            )
        ],
    )

    assert await store.count(COLLECTION) == 4
    hits = await store.search(COLLECTION, unit(0.0, 0.0, 1.0), limit=1)
    assert hits[0].id == "fact-north"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[0].payload["entity_id"] == "e9"


async def test_upsert_of_nothing_is_a_no_op(store: VectorStore) -> None:
    assert await store.upsert(COLLECTION, []) == 0
    assert await store.count(COLLECTION) == 0


async def test_upsert_rejects_a_wrong_width_vector(store: VectorStore) -> None:
    with pytest.raises(StorageError):
        await store.upsert(COLLECTION, [VectorPoint(id="bad", vector=[1.0, 0.0])])


async def test_upsert_rejects_a_zero_vector(store: VectorStore) -> None:
    with pytest.raises(StorageError):
        await store.upsert(COLLECTION, [VectorPoint(id="zero", vector=[0.0] * DIM)])


async def test_upsert_rejects_a_reserved_payload_key(store: VectorStore) -> None:
    with pytest.raises(StorageError):
        await store.upsert(
            COLLECTION,
            [VectorPoint(id="sneaky", vector=unit(1.0), payload={"__paa_point_id": "spoof"})],
        )


async def test_upsert_normalises_unnormalised_input(store: VectorStore) -> None:
    """Callers may hand over raw vectors; the store owns normalisation."""
    await store.upsert(
        COLLECTION, [VectorPoint(id="big", vector=[5.0] + [0.0] * (DIM - 1))]
    )
    hits = await store.search(COLLECTION, unit(1.0), limit=1)
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)


async def test_upsert_accepts_a_numpy_vector(store: VectorStore) -> None:
    await store.upsert(
        COLLECTION,
        [VectorPoint(id="arr", vector=np.asarray(unit(1.0), dtype=np.float32))],
    )
    assert await store.count(COLLECTION) == 1


async def test_delete_removes_and_reports_what_existed(store: VectorStore) -> None:
    await seed(store)

    assert await store.delete(COLLECTION, ["fact-north", "never-existed"]) == 1
    assert await store.count(COLLECTION) == 3
    assert "fact-north" not in ids_of(await store.search(COLLECTION, unit(1.0, 0.0), limit=10))

    # Deleting again reports zero rather than failing.
    assert await store.delete(COLLECTION, ["fact-north"]) == 0
    assert await store.delete(COLLECTION, []) == 0


async def test_delete_leaves_the_rest_searchable(store: VectorStore) -> None:
    await seed(store)
    await store.delete(COLLECTION, ["fact-northeast", "fact-east"])

    hits = await store.search(COLLECTION, unit(1.0, 0.0), limit=10)
    assert ids_of(hits) == ["fact-north", "fact-south"]
    assert await store.count(COLLECTION) == 2


async def test_count_tracks_writes(store: VectorStore) -> None:
    assert await store.count(COLLECTION) == 0
    await seed(store)
    assert await store.count(COLLECTION) == 4
    await store.upsert(COLLECTION, [VectorPoint(id="fact-north", vector=unit(1.0))])
    assert await store.count(COLLECTION) == 4


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_data_survives_a_reopen(open_store: StoreOpener) -> None:
    store = await open_store()
    await store.ensure_collection(COLLECTION, DIM)
    await seed(store)
    await store.delete(COLLECTION, ["fact-south"])

    reopened = await open_store()

    assert await reopened.collection_exists(COLLECTION) is True
    assert await reopened.count(COLLECTION) == 3

    hits = await reopened.search(COLLECTION, unit(1.0, 0.0), limit=10)
    assert ids_of(hits) == ["fact-north", "fact-northeast", "fact-east"]
    assert hits[0].payload == {
        "entity_id": "e1",
        "memory_scope": "global",
        "predicate": "owns",
    }
    # Filters must still work against payloads that came off disk.
    filtered = await reopened.search(
        COLLECTION, unit(1.0, 0.0), limit=10, filters={"entity_id": "e1"}
    )
    assert set(ids_of(filtered)) == {"fact-north", "fact-northeast"}


async def test_empty_collection_survives_a_reopen(open_store: StoreOpener) -> None:
    store = await open_store()
    await store.ensure_collection(COLLECTION, DIM)

    reopened = await open_store()
    assert await reopened.count(COLLECTION) == 0
    await reopened.upsert(COLLECTION, [VectorPoint(id="late", vector=unit(1.0))])
    assert await reopened.count(COLLECTION) == 1


async def test_close_is_idempotent(open_store: StoreOpener) -> None:
    store = await open_store()
    await store.ensure_collection(COLLECTION, DIM)
    await store.close()
    await store.close()
    with pytest.raises(StorageError):
        await store.count(COLLECTION)


# ---------------------------------------------------------------------------
# Cross-backend agreement
# ---------------------------------------------------------------------------


FILTER_CASES: list[dict[str, Any]] = [
    {},
    {"entity_id": "e1"},
    {"entity_id": "e2"},
    {"entity_id": ["e1", "e2"]},
    {"entity_id": ["e1", "missing"]},
    {"entity_id": []},
    {"memory_scope": ["global", "session-b"]},
    {"entity_id": "e1", "predicate": "owns"},
    {"entity_id": "e1", "predicate": "reads"},
    {"never_set": "x"},
]


@pytest.mark.requires_vector
@pytest.mark.skipif(not HAS_QDRANT, reason="qdrant-client is not installed")
async def test_backends_agree_row_for_row(tmp_path: Path) -> None:
    """Run both engines over identical data and diff the answers.

    The parametrised suite proves each backend satisfies the contract
    separately. This proves they satisfy it *the same way* — which is the claim
    an integrator actually relies on when ``backend_vector='auto'`` silently
    picks one.
    """
    numpy_store = NumpyVectorStore(tmp_path / "np")
    qdrant_store = QdrantVectorStore(path=tmp_path / "qd")

    try:
        for engine in (numpy_store, qdrant_store):
            await engine.ensure_collection(COLLECTION, DIM)
            await seed(engine)

        query = unit(1.0, 0.2)

        for filters in FILTER_CASES:
            left = await numpy_store.search(
                COLLECTION, query, limit=10, filters=filters or None
            )
            right = await qdrant_store.search(
                COLLECTION, query, limit=10, filters=filters or None
            )
            assert ids_of(left) == ids_of(right), f"ordering diverged for {filters!r}"
            assert [h.payload for h in left] == [h.payload for h in right]
            for a, b in zip(left, right, strict=True):
                assert a.score == pytest.approx(b.score, abs=1e-5), f"score diverged for {a.id}"

        for threshold in (-1.0, 0.0, 0.5, 0.95, 1.0):
            left = await numpy_store.search(COLLECTION, query, limit=10, score_threshold=threshold)
            right = await qdrant_store.search(
                COLLECTION, query, limit=10, score_threshold=threshold
            )
            assert ids_of(left) == ids_of(right), f"threshold {threshold} diverged"

        for limit in (1, 2, 4, 10):
            left = await numpy_store.search(COLLECTION, query, limit=limit)
            right = await qdrant_store.search(COLLECTION, query, limit=limit)
            assert ids_of(left) == ids_of(right), f"limit {limit} diverged"

        assert await numpy_store.delete(COLLECTION, ["fact-east", "ghost"]) == await (
            qdrant_store.delete(COLLECTION, ["fact-east", "ghost"])
        )
        assert await numpy_store.count(COLLECTION) == await qdrant_store.count(COLLECTION)
    finally:
        await numpy_store.close()
        await qdrant_store.close()


# ---------------------------------------------------------------------------
# Collection specs
# ---------------------------------------------------------------------------


def test_rfc_collection_specs_match_the_rfc() -> None:
    """RFC §3.2 pins these numbers; drift here is a spec violation, not a tweak."""
    assert ACTIVE_FACTS.name == "active_facts"
    assert ACTIVE_FACTS.dimensions == 384
    assert ACTIVE_FACTS.distance == "cosine"
    assert ACTIVE_FACTS.hnsw_m == 16
    assert ACTIVE_FACTS.hnsw_ef_construct == 100

    assert ABSOLUTE_FACTS_INDEX.name == "absolute_facts_index"
    assert ABSOLUTE_FACTS_INDEX.dimensions == 384
    assert ABSOLUTE_FACTS_INDEX.distance == "cosine"
    assert ABSOLUTE_FACTS_INDEX.hnsw_m == 8
    assert ABSOLUTE_FACTS_INDEX.hnsw_ef_construct == 128

    for spec in RFC_COLLECTIONS:
        assert spec.keyword_payload_fields, "every collection needs indexable keywords"


def test_collection_width_matches_the_configured_embedder() -> None:
    """A 384-dim collection and a 768-dim encoder is a silent-corruption bug."""
    assert ModelSettings().embedding_dimensions == ACTIVE_FACTS.dimensions


@pytest.mark.requires_vector
@pytest.mark.skipif(not HAS_QDRANT, reason="qdrant-client is not installed")
async def test_qdrant_applies_the_specified_hnsw_parameters(tmp_path: Path) -> None:
    """Read the parameters back out of Qdrant rather than trusting the call.

    The assertion is on the *per-vector* hnsw config, not the collection-level
    one: ``QdrantLocal`` discards the latter and always reports its own defaults
    (m=16, ef_construct=100). The backend sets both, so this is the field that
    proves the RFC's numbers actually landed in embedded mode.
    """
    store = QdrantVectorStore(path=tmp_path / "qd")
    try:
        await store.ensure_rfc_collections()
        # Reaching into the private client on purpose: the assertion is about
        # what Qdrant actually stored, not what our wrapper believes.
        client = store._client
        for spec in RFC_COLLECTIONS:
            params = client.get_collection(spec.name).config.params.vectors
            assert params.size == spec.dimensions
            assert params.distance.lower() == "cosine"
            assert params.hnsw_config is not None, f"{spec.name} lost its hnsw config"
            assert params.hnsw_config.m == spec.hnsw_m
            assert params.hnsw_config.ef_construct == spec.hnsw_ef_construct
    finally:
        await store.close()


async def test_numpy_autoflush_off_defers_writes_until_flush(tmp_path: Path) -> None:
    """The documented escape hatch for callers doing many small writes."""
    root = tmp_path / "np"
    store = NumpyVectorStore(root, autoflush=False)
    await store.ensure_collection(COLLECTION, DIM)
    await store.upsert(COLLECTION, [VectorPoint(id="deferred", vector=unit(1.0))])

    # ensure_collection always persists, so the files exist but are still empty.
    cold = NumpyVectorStore(root)
    assert await cold.count(COLLECTION) == 0

    await store.flush()

    warm = NumpyVectorStore(root)
    assert await warm.count(COLLECTION) == 1
    await store.close()


async def test_numpy_close_flushes_pending_writes(tmp_path: Path) -> None:
    root = tmp_path / "np"
    store = NumpyVectorStore(root, autoflush=False)
    await store.ensure_collection(COLLECTION, DIM)
    await store.upsert(COLLECTION, [VectorPoint(id="pending", vector=unit(1.0))])
    await store.close()

    assert await NumpyVectorStore(root).count(COLLECTION) == 1


async def test_numpy_detects_a_corrupt_sidecar(tmp_path: Path) -> None:
    """A truncated write must surface as an error, not as silently missing rows."""
    import json

    root = tmp_path / "np"
    store = NumpyVectorStore(root)
    await store.ensure_collection(COLLECTION, DIM)
    await store.upsert(
        COLLECTION,
        [
            VectorPoint(id="a", vector=unit(1.0)),
            VectorPoint(id="b", vector=unit(0.0, 1.0)),
        ],
    )
    await store.close()

    sidecar = root / f"{COLLECTION}.json"
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    meta["points"] = meta["points"][:1]  # matrix now has one row too many
    sidecar.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(StorageError, match="corrupt"):
        await NumpyVectorStore(root).count(COLLECTION)


async def test_numpy_sidecar_records_the_hnsw_parameters(tmp_path: Path) -> None:
    """Recorded but unused, so a later migration can rebuild the real index."""
    import json

    store = NumpyVectorStore(tmp_path / "np")
    try:
        await store.ensure_rfc_collections()
    finally:
        await store.close()

    sidecar = json.loads(
        (tmp_path / "np" / f"{ABSOLUTE_FACTS_INDEX.name}.json").read_text(encoding="utf-8")
    )
    assert sidecar["dimensions"] == 384
    assert sidecar["hnsw_m"] == 8
    assert sidecar["hnsw_ef_construct"] == 128
    assert sidecar["distance"] == "cosine"


# ---------------------------------------------------------------------------
# HashEmbedder
# ---------------------------------------------------------------------------


async def test_hash_embedder_shape_and_dtype() -> None:
    embedder = HashEmbedder(384)
    matrix = await embedder.embed(["alpha beta", "gamma", ""])

    assert matrix.shape == (3, 384)
    assert matrix.dtype == np.float32
    assert embedder.dimensions == 384


async def test_hash_embedder_is_deterministic() -> None:
    """Across instances, and — critically — across processes: no PYTHONHASHSEED."""
    texts = ["the deploy pipeline is owned by devansh", "unrelated", ""]
    first = await HashEmbedder(384).embed(texts)
    second = await HashEmbedder(384).embed(texts)

    np.testing.assert_array_equal(first, second)


async def test_hash_embedder_produces_unit_vectors() -> None:
    embedder = HashEmbedder(64)
    matrix = await embedder.embed(
        [
            "a normal sentence",
            "repeated repeated repeated repeated",
            "",  # no tokens at all
            "!!! ??? ...",  # punctuation only
            "x",
        ]
    )
    norms = np.linalg.norm(matrix, axis=1)
    np.testing.assert_allclose(norms, np.ones(len(norms)), atol=1e-5)


async def test_hash_embedder_scores_identical_text_at_one() -> None:
    embedder = HashEmbedder(384)
    vector = await embedder.embed_one("the ledger is the only source of truth")
    assert cosine_similarity(vector, vector) == pytest.approx(1.0, abs=1e-6)

    again = await embedder.embed_one("the ledger is the only source of truth")
    assert cosine_similarity(vector, again) == pytest.approx(1.0, abs=1e-6)


async def test_hash_embedder_ranks_related_above_unrelated() -> None:
    """Lexical overlap only — which is precisely the documented limitation."""
    embedder = HashEmbedder(384)
    anchor = await embedder.embed_one("the deploy pipeline runs nightly on the build server")
    related = await embedder.embed_one("the deploy pipeline runs on the build server")
    unrelated = await embedder.embed_one("photosynthesis converts light into chemical energy")

    assert cosine_similarity(anchor, related) > 0.8
    assert cosine_similarity(anchor, unrelated) < 0.2
    assert cosine_similarity(anchor, related) > cosine_similarity(anchor, unrelated)


async def test_hash_embedder_has_no_semantics() -> None:
    """Pinning the known weakness so nobody mistakes it for a real encoder."""
    embedder = HashEmbedder(384)
    a = await embedder.embed_one("car")
    b = await embedder.embed_one("automobile")
    assert abs(cosine_similarity(a, b)) < 0.5


async def test_hash_embedder_empty_batch() -> None:
    matrix = await HashEmbedder(32).embed([])
    assert matrix.shape == (0, 32)


async def test_hash_embedder_large_batch_uses_a_thread() -> None:
    """The >64 branch hands off to asyncio.to_thread; assert it still works."""
    texts = [f"document number {i} about topic {i % 7}" for i in range(200)]
    matrix = await HashEmbedder(128).embed(texts)
    assert matrix.shape == (200, 128)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


def test_hash_embedder_rejects_a_useless_width() -> None:
    with pytest.raises(ValueError, match="at least 8"):
        HashEmbedder(4)


async def test_hash_embeddings_round_trip_through_a_store(tmp_path: Path) -> None:
    """The encoder and the store agree on width and orientation end to end."""
    embedder = HashEmbedder(ACTIVE_FACTS.dimensions)
    store = NumpyVectorStore(tmp_path / "np")
    try:
        await store.ensure_spec(ACTIVE_FACTS)
        documents = {
            "f1": "the nightly deploy pipeline is owned by the platform team",
            "f2": "invoices are reconciled every friday afternoon",
            "f3": "the deploy pipeline runs after the nightly build",
        }
        vectors = await embedder.embed(list(documents.values()))
        await store.upsert(
            ACTIVE_FACTS.name,
            [
                VectorPoint(id=key, vector=vectors[row], payload={"entity_id": "e1"})
                for row, key in enumerate(documents)
            ],
        )

        query = await embedder.embed_one("who owns the nightly deploy pipeline")
        hits = await store.search(ACTIVE_FACTS.name, query, limit=3)

        assert hits[0].id in {"f1", "f3"}
        assert ids_of(hits)[-1] == "f2"
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def test_get_embedder_falls_back_to_hash_when_transformers_absent() -> None:
    if HAS_SENTENCE_TRANSFORMERS:
        pytest.skip("sentence-transformers is installed; the real encoder is selected")

    embedder = get_embedder(ModelSettings())
    assert isinstance(embedder, HashEmbedder)
    assert embedder.dimensions == 384
    assert "hash-embedder" in embedder.model_name


def test_get_embedder_raises_when_fallback_is_disabled() -> None:
    if HAS_SENTENCE_TRANSFORMERS:
        pytest.skip("sentence-transformers is installed; there is nothing to fall back from")

    with pytest.raises(StorageError) as excinfo:
        get_embedder(ModelSettings(allow_hash_embedder_fallback=False))
    assert excinfo.value.substrate == "embeddings"


def test_get_embedder_accepts_full_settings(tmp_path: Path) -> None:
    embedder = get_embedder(Settings(home=tmp_path))
    assert embedder.dimensions == 384


def test_get_vector_store_honours_an_explicit_numpy_backend(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    settings.storage.backend_vector = "numpy"
    store = get_vector_store(settings)
    assert isinstance(store, NumpyVectorStore)
    # Sibling of qdrant_path, never inside it — the two engines never share a dir.
    assert store.path == settings.storage.qdrant_path.with_name("vectors")


def test_get_vector_store_auto_prefers_qdrant_when_available(tmp_path: Path) -> None:
    store = get_vector_store(Settings(home=tmp_path))
    expected = QdrantVectorStore if HAS_QDRANT else NumpyVectorStore
    assert isinstance(store, expected)


def test_get_vector_store_qdrant_server_requires_a_url(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    settings.storage.backend_vector = "qdrant_server"
    with pytest.raises(StorageError) as excinfo:
        get_vector_store(settings)
    assert excinfo.value.substrate == "qdrant"


def test_get_vector_store_accepts_storage_settings_alone(tmp_path: Path) -> None:
    storage = StorageSettings(backend_vector="numpy", qdrant_path=tmp_path / "state" / "qdrant")
    assert isinstance(get_vector_store(storage), NumpyVectorStore)


@pytest.mark.requires_vector
@pytest.mark.skipif(not HAS_QDRANT, reason="qdrant-client is not installed")
def test_qdrant_store_requires_exactly_one_location() -> None:
    with pytest.raises(StorageError):
        QdrantVectorStore()
    with pytest.raises(StorageError):
        QdrantVectorStore(path="/tmp/x", url="http://localhost:6333")
