"""Qdrant backend — embedded by default, server if you have one.

SPEC DEVIATION (docs/adr/0002): the RFC provisions Qdrant as a Docker service.
The target machine has no Docker and ~3.5 GB of free RAM (ADR-0001), so the
default here is qdrant-client's *local* mode — ``QdrantClient(path=...)`` — which
runs the same index in-process against a directory. The collection definitions,
HNSW parameters and query semantics are the RFC's; only the deployment topology
differs. Setting ``StorageSettings.qdrant_url`` switches to a real server with
no other change at the call site.

What local mode costs you, honestly
-----------------------------------
* **There is no HNSW graph.** ``QdrantLocal`` is a pure-Python brute-force
  implementation. The index parameters are still declared and persisted (see
  ``_sync_ensure``) so that pointing at a real server later builds the graph the
  RFC specified, but embedded mode scans. Which means the numpy backend in
  :mod:`~paa.storage.vector.numpy_backend` is not the recall downgrade it looks
  like — against embedded Qdrant it is the *same* algorithm in faster BLAS.
* **Payload indexes are inert.** The local implementation accepts
  ``create_payload_index`` and warns that it does nothing; filtering is a scan.
  The calls are still issued so the same code provisions a real server
  correctly.
* **One process at a time.** Local mode takes an exclusive file lock on the
  storage directory. A second :class:`QdrantVectorStore` on the same path will
  fail to open until the first is closed.
* Every client call is synchronous, so all of them are handed to
  :func:`asyncio.to_thread` and serialised behind one lock — local mode is not
  safe under concurrent mutation, and at single-user scale the parallelism a
  server would allow buys nothing.

Point ids
---------
SPEC DEVIATION (docs/adr/0003): Qdrant only accepts unsigned integers or UUIDs
as point ids, while this runtime's ids are opaque strings (``VectorPoint.id``,
which for ``active_facts`` is ``hot_serving_active_facts.id``). Ids that already
parse as UUIDs are used verbatim; anything else is mapped through a deterministic
UUIDv5 and the original string is carried in a reserved payload key, stripped
again on the way out. Callers therefore see the ids they wrote, and the mapping
is stable across processes and restarts.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from paa.core.errors import StorageError
from paa.storage.vector.embeddings import FloatArray
from paa.storage.vector.store import (
    Filters,
    NormalisedFilters,
    SearchHit,
    VectorPoint,
    VectorStore,
    as_unit_vector,
    is_unsatisfiable,
    normalise_filters,
    spec_for,
    validate_collection_name,
    validate_payload,
)

if TYPE_CHECKING:
    from paa.config import Settings, StorageSettings

__all__ = ["QdrantVectorStore", "qdrant_available"]

log = structlog.get_logger(__name__)

_SUBSTRATE = "qdrant"

#: Namespace for the UUIDv5 id mapping. Fixed forever: changing it would
#: silently orphan every point already written under the old namespace.
_ID_NAMESPACE = uuid.UUID("1b1f9c4e-6a4f-5f27-9c3a-7d2e5b8f0a11")

#: Reserved payload key carrying the caller's original id. Declared in
#: store.RESERVED_PAYLOAD_KEYS so both backends reject callers using it.
_ORIGINAL_ID_KEY = "__paa_point_id"


def qdrant_available() -> bool:
    """Whether the ``vector`` extra is installed, without importing it."""
    import importlib.util

    try:
        return importlib.util.find_spec("qdrant_client") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def _import_qdrant() -> tuple[Any, Any]:
    """Import qdrant-client lazily, with an actionable error if it is absent."""
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise StorageError(
            'qdrant-client is not installed; run `pip install "paa[vector]"` '
            "or set storage.backend_vector to 'numpy'",
            substrate=_SUBSTRATE,
        ) from exc
    return QdrantClient, models


def _encode_id(raw: str) -> str:
    """Map an arbitrary string id onto a Qdrant-acceptable UUID. Deterministic."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return str(uuid.uuid5(_ID_NAMESPACE, raw))


class QdrantVectorStore(VectorStore):
    """Vector recall backed by Qdrant's HNSW index.

    ``path`` selects embedded mode, ``url`` selects a server. Exactly one is
    required; passing both is a configuration error rather than a precedence
    puzzle for whoever debugs it later.
    """

    substrate: ClassVar[str] = _SUBSTRATE

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if (path is None) == (url is None):
            raise StorageError(
                "QdrantVectorStore needs exactly one of path (embedded) or url (server)",
                substrate=_SUBSTRATE,
                path=str(path) if path else None,
                url=url,
            )
        self._path = Path(path) if path is not None else None
        self._url = url
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

        self._client: Any | None = None
        self._models: Any | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def is_embedded(self) -> bool:
        return self._path is not None

    @property
    def location(self) -> str:
        return str(self._path) if self._path is not None else str(self._url)

    # -- lifecycle ---------------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking client call in a worker thread, one at a time."""
        async with self._lock:
            if self._closed:
                raise StorageError("vector store is closed", substrate=_SUBSTRATE)
            if self._client is None:
                await asyncio.to_thread(self._connect)
            return await asyncio.to_thread(fn, *args, **kwargs)

    def _connect(self) -> None:
        """Open the client. Blocking (embedded mode touches the filesystem)."""
        if self._client is not None:
            return
        client_cls, models = _import_qdrant()
        self._models = models
        try:
            if self._path is not None:
                self._path.mkdir(parents=True, exist_ok=True)
                self._client = client_cls(path=str(self._path))
            else:
                self._client = client_cls(
                    url=self._url, api_key=self._api_key, timeout=int(self._timeout_seconds)
                )
        except Exception as exc:
            raise StorageError(
                f"failed to open qdrant: {exc}",
                substrate=_SUBSTRATE,
                location=self.location,
                embedded=self.is_embedded,
            ) from exc
        log.info("vector.qdrant_connected", location=self.location, embedded=self.is_embedded)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.to_thread(client.close)
        except Exception as exc:  # shutdown is best-effort by design
            log.warning("vector.qdrant_close_error", error=str(exc), location=self.location)
        else:
            log.info("vector.qdrant_closed", location=self.location)

    # -- schema ------------------------------------------------------------

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        hnsw_m: int = 16,
        hnsw_ef_construct: int = 100,
    ) -> None:
        validate_collection_name(name, substrate=_SUBSTRATE)
        await self._call(self._sync_ensure, name, dim, hnsw_m, hnsw_ef_construct)

    def _sync_ensure(self, name: str, dim: int, hnsw_m: int, ef_construct: int) -> None:
        client, models = self._require()

        if client.collection_exists(name):
            existing = self._existing_dimensions(name)
            if existing is not None and existing != dim:
                raise StorageError(
                    "collection already exists with a different vector width; "
                    "recreate it or point at a different collection",
                    substrate=_SUBSTRATE,
                    collection=name,
                    existing_dimensions=existing,
                    requested_dimensions=dim,
                )
            self._ensure_payload_indexes(name)
            return

        try:
            client.create_collection(
                collection_name=name,
                # The HNSW parameters are set twice on purpose. The
                # collection-level ``hnsw_config`` is what a real server treats
                # as the default, but qdrant-client's local mode discards it and
                # always reports m=16/ef_construct=100. The per-vector config
                # survives local mode *and* takes precedence server-side, so
                # setting both means the RFC's numbers are honoured and
                # introspectable in either deployment.
                vectors_config=models.VectorParams(
                    size=dim,
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=hnsw_m, ef_construct=ef_construct),
                ),
                hnsw_config=models.HnswConfigDiff(m=hnsw_m, ef_construct=ef_construct),
            )
        except Exception as exc:
            # Benign race: another writer created it between the check and here.
            if client.collection_exists(name):
                self._ensure_payload_indexes(name)
                return
            raise StorageError(
                f"failed to create collection: {exc}",
                substrate=_SUBSTRATE,
                collection=name,
                dimensions=dim,
            ) from exc

        log.info(
            "vector.collection_created",
            collection=name,
            dimensions=dim,
            distance="cosine",
            hnsw_m=hnsw_m,
            hnsw_ef_construct=ef_construct,
        )
        self._ensure_payload_indexes(name)

    def _existing_dimensions(self, name: str) -> int | None:
        client, _ = self._require()
        try:
            params = client.get_collection(name).config.params.vectors
        except Exception:  # pragma: no cover - non-fatal introspection
            return None
        if params is None:
            return None
        if isinstance(params, dict):  # named vectors; not used by this runtime
            first = next(iter(params.values()), None)
            return int(first.size) if first is not None else None
        return int(params.size)

    def _ensure_payload_indexes(self, name: str) -> None:
        """Create keyword payload indexes for the collection's RFC schema.

        Idempotent — Qdrant treats a repeat as an update of the same index.
        In embedded mode the client warns that indexes have no effect; the
        warning is suppressed for the duration of the call and reported once as
        a debug line instead of 5 identical warnings per boot.
        """
        spec = spec_for(name)
        if spec is None or not spec.keyword_payload_fields:
            return
        client, models = self._require()

        with warnings.catch_warnings():
            # Narrow, and inside this store's own lock. Embedded qdrant raises a
            # UserWarning per field; it is expected and already documented above.
            if self.is_embedded:
                warnings.simplefilter("ignore", UserWarning)
            for field in spec.keyword_payload_fields:
                try:
                    client.create_payload_index(
                        collection_name=name,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
                except Exception as exc:  # pragma: no cover - server-only path
                    log.warning(
                        "vector.payload_index_failed",
                        collection=name,
                        field=field,
                        error=str(exc),
                    )

        log.debug(
            "vector.payload_indexes_ensured",
            collection=name,
            fields=list(spec.keyword_payload_fields),
            effective=not self.is_embedded,
        )

    async def collection_exists(self, name: str) -> bool:
        result = await self._call(self._sync_exists, name)
        return bool(result)

    def _sync_exists(self, name: str) -> bool:
        client, _ = self._require()
        return bool(client.collection_exists(name))

    # -- data --------------------------------------------------------------

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> int:
        if not points:
            return 0
        result = await self._call(self._sync_upsert, collection, list(points))
        return int(result)

    def _sync_upsert(self, collection: str, points: list[VectorPoint]) -> int:
        client, models = self._require()
        dimensions = self._require_collection(collection)

        structs = []
        for point in points:
            payload = validate_payload(point.payload, substrate=_SUBSTRATE, point_id=point.id)
            payload[_ORIGINAL_ID_KEY] = point.id
            vector = as_unit_vector(
                point.as_array(),
                dimensions,
                substrate=_SUBSTRATE,
                context=f"upsert {collection}/{point.id}",
            )
            structs.append(
                models.PointStruct(
                    id=_encode_id(point.id), vector=vector.tolist(), payload=payload
                )
            )

        try:
            client.upsert(collection_name=collection, points=structs, wait=True)
        except Exception as exc:
            raise StorageError(
                f"upsert failed: {exc}",
                substrate=_SUBSTRATE,
                collection=collection,
                point_count=len(structs),
            ) from exc
        return len(structs)

    async def search(
        self,
        collection: str,
        query_vector: Sequence[float] | FloatArray,
        limit: int = 10,
        score_threshold: float | None = None,
        filters: Filters | None = None,
    ) -> list[SearchHit]:
        self._validated_limit(limit)
        normalised = normalise_filters(filters, substrate=_SUBSTRATE)
        if is_unsatisfiable(normalised):
            return []
        result = await self._call(
            self._sync_search, collection, query_vector, limit, score_threshold, normalised
        )
        return list(result)

    def _sync_search(
        self,
        collection: str,
        query_vector: Sequence[float] | FloatArray,
        limit: int,
        score_threshold: float | None,
        normalised: NormalisedFilters | None,
    ) -> list[SearchHit]:
        client, _ = self._require()
        dimensions = self._require_collection(collection)
        query = as_unit_vector(
            query_vector, dimensions, substrate=_SUBSTRATE, context=f"search {collection}"
        )

        try:
            response = client.query_points(
                collection_name=collection,
                query=query.tolist(),
                limit=limit,
                score_threshold=score_threshold,
                query_filter=self._build_filter(normalised),
                with_payload=True,
            )
        except Exception as exc:
            raise StorageError(
                f"search failed: {exc}",
                substrate=_SUBSTRATE,
                collection=collection,
                limit=limit,
            ) from exc

        hits: list[SearchHit] = []
        for point in response.points:
            payload = dict(point.payload or {})
            original = payload.pop(_ORIGINAL_ID_KEY, None)
            hits.append(
                SearchHit(
                    id=str(original) if original is not None else str(point.id),
                    # Clamp: cosine is defined on [-1, 1] and float noise at the
                    # boundary would otherwise leak into callers comparing
                    # against thresholds.
                    score=max(-1.0, min(1.0, float(point.score))),
                    payload=payload,
                )
            )
        return hits

    def _build_filter(self, normalised: NormalisedFilters | None) -> Any:
        """Translate normalised filters into a Qdrant ``Filter``.

        Multi-value conditions become a nested ``should`` rather than
        ``MatchAny``: ``MatchAny`` only accepts homogeneous str/int lists, so a
        bool membership filter would fail validation there while the numpy
        backend happily answered it. One uniform form, no divergence.
        """
        if not normalised:
            return None
        _, models = self._require()

        conditions = []
        for field, accepted in normalised.items():
            if len(accepted) == 1:
                conditions.append(
                    models.FieldCondition(
                        key=field, match=models.MatchValue(value=accepted[0])
                    )
                )
            else:
                conditions.append(
                    models.Filter(
                        should=[
                            models.FieldCondition(key=field, match=models.MatchValue(value=v))
                            for v in accepted
                        ]
                    )
                )
        return models.Filter(must=conditions)

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        result = await self._call(self._sync_delete, collection, list(ids))
        return int(result)

    def _sync_delete(self, collection: str, ids: list[str]) -> int:
        client, models = self._require()
        self._require_collection(collection)
        encoded = [_encode_id(i) for i in ids]

        # Qdrant's delete is idempotent and reports no count, so presence is
        # probed first. The numpy backend can answer "how many existed" for
        # free, and the two must return the same number.
        try:
            present = client.retrieve(
                collection_name=collection,
                ids=encoded,
                with_payload=False,
                with_vectors=False,
            )
            removed = len(present)
            if removed:
                client.delete(
                    collection_name=collection,
                    points_selector=models.PointIdsList(points=encoded),
                    wait=True,
                )
        except Exception as exc:
            raise StorageError(
                f"delete failed: {exc}",
                substrate=_SUBSTRATE,
                collection=collection,
                id_count=len(ids),
            ) from exc
        return removed

    async def count(self, collection: str) -> int:
        result = await self._call(self._sync_count, collection)
        return int(result)

    def _sync_count(self, collection: str) -> int:
        client, _ = self._require()
        self._require_collection(collection)
        try:
            return int(client.count(collection_name=collection, exact=True).count)
        except Exception as exc:
            raise StorageError(
                f"count failed: {exc}", substrate=_SUBSTRATE, collection=collection
            ) from exc

    # -- internals ---------------------------------------------------------

    def _require(self) -> tuple[Any, Any]:
        if self._client is None or self._models is None:  # pragma: no cover - guarded by _call
            raise StorageError("qdrant client is not connected", substrate=_SUBSTRATE)
        return self._client, self._models

    def _require_collection(self, name: str) -> int:
        """Assert the collection exists and return its width."""
        client, _ = self._require()
        if not client.collection_exists(name):
            raise StorageError(
                "collection does not exist; call ensure_collection first",
                substrate=_SUBSTRATE,
                collection=name,
            )
        dimensions = self._existing_dimensions(name)
        if dimensions is None:  # pragma: no cover - introspection failure
            raise StorageError(
                "could not determine collection vector width",
                substrate=_SUBSTRATE,
                collection=name,
            )
        return dimensions

    @classmethod
    def from_settings(cls, settings: Settings | StorageSettings) -> QdrantVectorStore:
        """Build from config, preferring a configured server over embedded mode."""
        storage = getattr(settings, "storage", settings)
        if storage.qdrant_url:
            return cls(url=storage.qdrant_url)
        return cls(path=storage.qdrant_path)
