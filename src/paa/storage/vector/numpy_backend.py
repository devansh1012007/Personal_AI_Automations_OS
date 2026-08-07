"""Brute-force vector recall over an in-memory numpy matrix.

SPEC DEVIATION (docs/adr/0002): the RFC mandates Qdrant with HNSW. This backend
is the zero-dependency fallback that keeps the runtime whole when the ``vector``
extra is not installed — and the honest observation is that at this system's
scale, it is not much of a downgrade.

Why brute force is fine here
----------------------------
Cosine over an L2-normalised ``(n, 384)`` float32 matrix is one BLAS ``matvec``:
``n * 384`` multiply-adds. At n = 10,000 that is ~3.8 M FLOPs, which lands in
low single-digit milliseconds on a laptop core — comfortably inside the 50 ms
policy latency budget (:class:`~paa.config.PolicySettings`) with room to spare,
and it is *exact* rather than approximate. HNSW wins decisively at millions of
vectors; a single person's fact store does not get there. Memory is the real
ceiling: 384 float32 = 1.5 KB per point, so ~15 MB at 10k and ~150 MB at 100k.
Past roughly 100k points, install ``paa[vector]``.

What this backend does not have: no HNSW graph, so ``hnsw_m`` and
``hnsw_ef_construct`` are recorded in the sidecar but unused; and no payload
index, so filters are evaluated by scanning payloads — which is what embedded
Qdrant does too.

Persistence
-----------
One ``.npy`` for the matrix plus one ``.json`` sidecar for ids, payloads and the
collection spec, per collection, written atomically via a temp file and
``os.replace``. The sidecar's row order is the matrix's row order; a mismatch
between the two is treated as corruption rather than silently truncated.

Concurrency
-----------
All state is guarded by a single :class:`asyncio.Lock`, and every filesystem
touch happens inside :func:`asyncio.to_thread` so the event loop never blocks on
a flush. The lock is held across the flush, so a reader can never observe a
half-applied upsert.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
import structlog

from paa.core.errors import StorageError
from paa.storage.vector.embeddings import FloatArray
from paa.storage.vector.store import (
    CollectionSpec,
    Filters,
    NormalisedFilters,
    SearchHit,
    VectorPoint,
    VectorStore,
    as_unit_vector,
    is_unsatisfiable,
    normalise_filters,
    payload_matches,
    spec_for,
    validate_collection_name,
    validate_payload,
)

if TYPE_CHECKING:
    from paa.config import Settings, StorageSettings

__all__ = ["NumpyVectorStore"]

log = structlog.get_logger(__name__)

_SUBSTRATE = "numpy"

#: Sidecar format version. Bumped if the on-disk layout ever changes shape, so
#: a future reader can refuse a file it does not understand instead of guessing.
_SIDECAR_VERSION = 1


@dataclass(slots=True)
class _Collection:
    """One collection's resident state.

    ``matrix`` rows are already L2-normalised, so ``matrix @ q`` is the cosine
    vector directly — no per-query normalisation pass over n rows.
    """

    spec: CollectionSpec
    ids: list[str]
    payloads: list[dict[str, Any]]
    matrix: FloatArray
    index: dict[str, int] = field(default_factory=dict)

    def rebuild_index(self) -> None:
        self.index = {point_id: row for row, point_id in enumerate(self.ids)}

    def __len__(self) -> int:
        return len(self.ids)


class NumpyVectorStore(VectorStore):
    """Exact cosine search over a persisted numpy matrix.

    ``autoflush`` rewrites both files after every mutation, which is O(n) in the
    collection size. That is the right default — it makes a crash between writes
    a non-event, and the runtime's write pattern is batched (the memory creator
    upserts during consolidation, not per keystroke). A caller doing sustained
    single-point writes should pass ``autoflush=False`` and call :meth:`flush`
    at a natural boundary.
    """

    substrate: ClassVar[str] = _SUBSTRATE

    def __init__(self, path: Path | str, *, autoflush: bool = True) -> None:
        self._path = Path(path)
        self._autoflush = autoflush
        self._collections: dict[str, _Collection] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    # -- lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            dirty = list(self._collections.values())
        if dirty:
            await asyncio.to_thread(self._flush_all, dirty)
        log.info("vector.numpy_closed", path=str(self._path), collections=len(dirty))

    async def flush(self) -> None:
        """Persist every loaded collection. Safe to call at any time."""
        async with self._lock:
            loaded = list(self._collections.values())
            await asyncio.to_thread(self._flush_all, loaded)

    # -- schema ------------------------------------------------------------

    async def ensure_collection(
        self,
        name: str,
        dim: int,
        hnsw_m: int = 16,
        hnsw_ef_construct: int = 100,
    ) -> None:
        validate_collection_name(name, substrate=_SUBSTRATE)
        async with self._lock:
            self._guard_open()
            existing = await asyncio.to_thread(self._load_if_present, name)
            if existing is not None:
                if existing.spec.dimensions != dim:
                    raise StorageError(
                        "collection already exists with a different vector width; "
                        "recreate it or point at a different collection",
                        substrate=_SUBSTRATE,
                        collection=name,
                        existing_dimensions=existing.spec.dimensions,
                        requested_dimensions=dim,
                    )
                return  # idempotent: existing data is left untouched

            # Explicit arguments always win; the RFC spec is consulted only for
            # the payload schema, which the ABC signature has no way to carry.
            known = spec_for(name)
            spec = CollectionSpec(
                name=name,
                dimensions=dim,
                distance="cosine",
                hnsw_m=hnsw_m,
                hnsw_ef_construct=hnsw_ef_construct,
                keyword_payload_fields=(
                    known.keyword_payload_fields
                    if known is not None and known.dimensions == dim
                    else ()
                ),
            )
            collection = _Collection(
                spec=spec,
                ids=[],
                payloads=[],
                matrix=np.zeros((0, dim), dtype=np.float32),
            )
            self._collections[name] = collection
            await asyncio.to_thread(self._flush_one, collection)

        log.info(
            "vector.collection_created",
            collection=name,
            dimensions=dim,
            distance="cosine",
            backend=_SUBSTRATE,
        )

    async def collection_exists(self, name: str) -> bool:
        async with self._lock:
            if name in self._collections:
                return True
            return await asyncio.to_thread(self._files_exist, name)

    # -- data --------------------------------------------------------------

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> int:
        if not points:
            return 0
        async with self._lock:
            self._guard_open()
            target = await self._require_collection(collection)

            # Validate the whole batch before mutating anything: a batch that
            # fails halfway would leave the collection in a state no caller
            # asked for, and the vectors are cheap to check.
            prepared: list[tuple[str, FloatArray, dict[str, Any]]] = []
            for point in points:
                payload = validate_payload(point.payload, substrate=_SUBSTRATE, point_id=point.id)
                vector = as_unit_vector(
                    point.as_array(),
                    target.spec.dimensions,
                    substrate=_SUBSTRATE,
                    context=f"upsert {collection}/{point.id}",
                )
                prepared.append((point.id, vector, payload))

            appended: list[FloatArray] = []
            for point_id, vector, payload in prepared:
                row = target.index.get(point_id)
                if row is None:
                    target.index[point_id] = len(target.ids)
                    target.ids.append(point_id)
                    target.payloads.append(payload)
                    appended.append(vector)
                else:
                    target.matrix[row] = vector
                    target.payloads[row] = payload

            if appended:
                target.matrix = np.vstack([target.matrix, np.asarray(appended, dtype=np.float32)])

            if self._autoflush:
                await asyncio.to_thread(self._flush_one, target)

        return len(points)

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

        async with self._lock:
            self._guard_open()
            target = await self._require_collection(collection)
            query = as_unit_vector(
                query_vector,
                target.spec.dimensions,
                substrate=_SUBSTRATE,
                context=f"search {collection}",
            )
            if len(target) == 0:
                return []
            return self._rank(target, query, limit, score_threshold, normalised)

    def _rank(
        self,
        target: _Collection,
        query: FloatArray,
        limit: int,
        score_threshold: float | None,
        normalised: NormalisedFilters | None,
    ) -> list[SearchHit]:
        # Both operands are unit vectors, so this dot product is the cosine.
        scores = np.asarray(target.matrix @ query, dtype=np.float32)
        np.clip(scores, -1.0, 1.0, out=scores)

        candidates = np.arange(len(target))
        if normalised:
            keep = np.fromiter(
                (payload_matches(p, normalised) for p in target.payloads),
                dtype=bool,
                count=len(target),
            )
            candidates = candidates[keep]
        if score_threshold is not None:
            candidates = candidates[scores[candidates] >= score_threshold]
        if candidates.size == 0:
            return []

        subset = scores[candidates]
        if subset.size > limit:
            # argpartition is O(n) against argsort's O(n log n); only the top
            # slice is then sorted. Matters once a collection gets large.
            top = np.argpartition(-subset, limit - 1)[:limit]
            order = top[np.argsort(-subset[top], kind="stable")]
        else:
            order = np.argsort(-subset, kind="stable")

        return [
            SearchHit(
                id=target.ids[int(candidates[i])],
                score=float(subset[int(i)]),
                payload=dict(target.payloads[int(candidates[i])]),
            )
            for i in order
        ]

    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        async with self._lock:
            self._guard_open()
            target = await self._require_collection(collection)

            rows = sorted({target.index[i] for i in ids if i in target.index})
            if not rows:
                return 0

            keep = np.ones(len(target), dtype=bool)
            keep[rows] = False
            target.matrix = np.ascontiguousarray(target.matrix[keep])
            target.ids = [v for i, v in enumerate(target.ids) if keep[i]]
            target.payloads = [v for i, v in enumerate(target.payloads) if keep[i]]
            target.rebuild_index()

            if self._autoflush:
                await asyncio.to_thread(self._flush_one, target)

        return len(rows)

    async def count(self, collection: str) -> int:
        async with self._lock:
            self._guard_open()
            target = await self._require_collection(collection)
            return len(target)

    # -- internals ---------------------------------------------------------

    def _guard_open(self) -> None:
        if self._closed:
            raise StorageError("vector store is closed", substrate=_SUBSTRATE)

    async def _require_collection(self, name: str) -> _Collection:
        """Resolve a collection, loading it from disk on first touch.

        Callers must already hold ``self._lock``.
        """
        if (cached := self._collections.get(name)) is not None:
            return cached
        loaded = await asyncio.to_thread(self._load_if_present, name)
        if loaded is None:
            raise StorageError(
                "collection does not exist; call ensure_collection first",
                substrate=_SUBSTRATE,
                collection=name,
            )
        self._collections[name] = loaded
        return loaded

    def _paths(self, name: str) -> tuple[Path, Path]:
        return self._path / f"{name}.npy", self._path / f"{name}.json"

    def _files_exist(self, name: str) -> bool:
        vectors, sidecar = self._paths(name)
        return vectors.exists() and sidecar.exists()

    # -- blocking section; only ever runs in a worker thread ----------------

    def _load_if_present(self, name: str) -> _Collection | None:
        vectors_path, sidecar_path = self._paths(name)
        if not (vectors_path.exists() and sidecar_path.exists()):
            return None

        try:
            with sidecar_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
            with vectors_path.open("rb") as handle:
                matrix = np.load(handle, allow_pickle=False)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"failed to read collection from disk: {exc}",
                substrate=_SUBSTRATE,
                collection=name,
                path=str(self._path),
            ) from exc

        version = int(meta.get("version", 0))
        if version != _SIDECAR_VERSION:
            raise StorageError(
                "unsupported vector sidecar version",
                substrate=_SUBSTRATE,
                collection=name,
                found_version=version,
                expected_version=_SIDECAR_VERSION,
            )

        spec = CollectionSpec(
            name=name,
            dimensions=int(meta["dimensions"]),
            distance=str(meta.get("distance", "cosine")),
            hnsw_m=int(meta.get("hnsw_m", 16)),
            hnsw_ef_construct=int(meta.get("hnsw_ef_construct", 100)),
            keyword_payload_fields=tuple(meta.get("keyword_payload_fields", ())),
        )
        entries = meta.get("points", [])
        matrix = np.ascontiguousarray(matrix, dtype=np.float32).reshape(-1, spec.dimensions)

        if matrix.shape[0] != len(entries):
            raise StorageError(
                "vector matrix and payload sidecar disagree on row count; "
                "the collection is corrupt",
                substrate=_SUBSTRATE,
                collection=name,
                matrix_rows=int(matrix.shape[0]),
                sidecar_rows=len(entries),
            )

        collection = _Collection(
            spec=spec,
            ids=[str(e["id"]) for e in entries],
            payloads=[dict(e.get("payload") or {}) for e in entries],
            matrix=matrix,
        )
        collection.rebuild_index()
        log.debug(
            "vector.collection_loaded",
            collection=name,
            points=len(collection),
            dimensions=spec.dimensions,
        )
        return collection

    def _flush_all(self, collections: Sequence[_Collection]) -> None:
        for collection in collections:
            self._flush_one(collection)

    def _flush_one(self, collection: _Collection) -> None:
        """Atomically persist one collection: temp file, fsync-free, os.replace.

        ``os.replace`` is atomic on both POSIX and Windows, so a crash mid-write
        leaves the previous complete version in place rather than a truncated
        file. The two files are replaced in sequence, so a crash *between* them
        is still possible — which is why :meth:`_load_if_present` treats a row
        count mismatch as corruption instead of trusting it.
        """
        name = collection.spec.name
        vectors_path, sidecar_path = self._paths(name)
        self._path.mkdir(parents=True, exist_ok=True)

        meta = {
            "version": _SIDECAR_VERSION,
            "name": name,
            "dimensions": collection.spec.dimensions,
            "distance": collection.spec.distance,
            # Inert for brute force, but recorded so a later migration to Qdrant
            # rebuilds the index with the RFC's parameters rather than defaults.
            "hnsw_m": collection.spec.hnsw_m,
            "hnsw_ef_construct": collection.spec.hnsw_ef_construct,
            "keyword_payload_fields": list(collection.spec.keyword_payload_fields),
            "points": [
                {"id": point_id, "payload": payload}
                for point_id, payload in zip(collection.ids, collection.payloads, strict=True)
            ],
        }

        try:
            vectors_tmp = vectors_path.with_suffix(".npy.tmp")
            with vectors_tmp.open("wb") as handle:
                np.save(handle, collection.matrix, allow_pickle=False)
            os.replace(vectors_tmp, vectors_path)

            sidecar_tmp = sidecar_path.with_suffix(".json.tmp")
            with sidecar_tmp.open("w", encoding="utf-8") as handle:
                json.dump(meta, handle, separators=(",", ":"), default=str)
            os.replace(sidecar_tmp, sidecar_path)
        except OSError as exc:
            raise StorageError(
                f"failed to persist collection: {exc}",
                substrate=_SUBSTRATE,
                collection=name,
                path=str(self._path),
            ) from exc

    @classmethod
    def from_settings(cls, settings: Settings | StorageSettings) -> NumpyVectorStore:
        """Build from config.

        The path is a sibling of ``qdrant_path`` (``<home>/state/vectors``)
        rather than a new configuration field, so that switching backends never
        risks one engine reading the other's directory.
        """
        storage = getattr(settings, "storage", settings)
        return cls(Path(storage.qdrant_path).with_name("vectors"))
