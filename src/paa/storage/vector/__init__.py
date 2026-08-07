"""Vector recall — the semantic half of context construction.

RFC §3.2 gives this subsystem two collections and a job: given a query vector,
return the facts nearest to it so the context gatherer can fill its slots. This
package supplies the encoder (:mod:`~paa.storage.vector.embeddings`), the
contract (:mod:`~paa.storage.vector.store`) and two interchangeable backends.

Wiring
------
::

    settings = get_settings()
    embedder = get_embedder(settings)
    store = get_vector_store(settings)
    async with store:
        await store.ensure_rfc_collections()
        vector = await embedder.embed_one("who owns the deploy pipeline?")
        hits = await store.search(
            ACTIVE_FACTS.name,
            vector,
            limit=8,
            score_threshold=settings.context.relevance_floor,
            filters={"memory_scope": ["global", str(session_id)]},
        )

Backend selection follows ``StorageSettings.backend_vector``. ``"auto"`` — the
default — prefers Qdrant and falls back to the brute-force numpy store with a
warning, so a machine without the ``vector`` extra still boots with working (and
exact, if slower) recall rather than a stack trace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from paa.core.errors import StorageError
from paa.storage.vector.embeddings import (
    Embedder,
    FloatArray,
    HashEmbedder,
    SentenceTransformerEmbedder,
    cosine_similarity,
    get_embedder,
)
from paa.storage.vector.numpy_backend import NumpyVectorStore
from paa.storage.vector.qdrant_backend import QdrantVectorStore, qdrant_available
from paa.storage.vector.store import (
    ABSOLUTE_FACTS_INDEX,
    ACTIVE_FACTS,
    RFC_COLLECTIONS,
    CollectionSpec,
    Filters,
    FilterValue,
    SearchHit,
    VectorPoint,
    VectorStore,
    normalise_filters,
    payload_matches,
    spec_for,
)

if TYPE_CHECKING:
    from paa.config import Settings, StorageSettings

__all__ = [
    "ABSOLUTE_FACTS_INDEX",
    "ACTIVE_FACTS",
    "RFC_COLLECTIONS",
    "CollectionSpec",
    "Embedder",
    "FilterValue",
    "Filters",
    "FloatArray",
    "HashEmbedder",
    "NumpyVectorStore",
    "QdrantVectorStore",
    "SearchHit",
    "SentenceTransformerEmbedder",
    "VectorPoint",
    "VectorStore",
    "cosine_similarity",
    "get_embedder",
    "get_vector_store",
    "normalise_filters",
    "payload_matches",
    "qdrant_available",
    "spec_for",
]

log = structlog.get_logger(__name__)


def get_vector_store(settings: Settings | StorageSettings) -> VectorStore:
    """Build the configured vector store.

    Accepts the full :class:`~paa.config.Settings` or just its
    :class:`~paa.config.StorageSettings` sub-model.

    ``backend_vector`` values:

    * ``"auto"`` — Qdrant if the extra is importable (server when
      ``qdrant_url`` is set, otherwise embedded at ``qdrant_path``), else numpy
      with a warning.
    * ``"qdrant_local"`` / ``"qdrant_server"`` / ``"numpy"`` — exactly that,
      raising :class:`~paa.core.errors.StorageError` if it cannot be honoured.
      An explicit choice is a statement of intent, so a silent downgrade would
      be the wrong behaviour.

    The store is returned unconnected; the first operation opens it.
    """
    storage = getattr(settings, "storage", settings)
    backend = storage.backend_vector

    if backend == "numpy":
        return NumpyVectorStore.from_settings(storage)

    if backend == "qdrant_server":
        if not storage.qdrant_url:
            raise StorageError(
                "backend_vector='qdrant_server' requires storage.qdrant_url",
                substrate="qdrant",
            )
        return QdrantVectorStore(url=storage.qdrant_url)

    if backend == "qdrant_local":
        return QdrantVectorStore(path=storage.qdrant_path)

    # -- auto --------------------------------------------------------------
    if qdrant_available():
        if storage.qdrant_url:
            log.debug("vector.auto_selected", backend="qdrant_server", url=storage.qdrant_url)
            return QdrantVectorStore(url=storage.qdrant_url)
        log.debug("vector.auto_selected", backend="qdrant_local", path=str(storage.qdrant_path))
        return QdrantVectorStore(path=storage.qdrant_path)

    store = NumpyVectorStore.from_settings(storage)
    log.warning(
        "vector.fallback_to_numpy",
        path=str(store.path),
        reason="qdrant-client is not installed",
        impact=(
            "Recall is exact but brute-force: O(n) per query and the whole "
            "matrix stays resident (~1.5 KB per 384-dim point). Fine to about "
            '100k points; past that install `pip install "paa[vector]"`.'
        ),
    )
    return store
