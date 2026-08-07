"""Vector store contract, shared semantics and the RFC's collection specs.

Two backends implement :class:`VectorStore`: an embedded Qdrant (real HNSW) and
a brute-force numpy matrix. They are meant to be *behaviourally identical* —
``tests/storage/test_vector.py`` runs one suite against both and fails if they
disagree. That is only achievable if the fiddly semantics live in exactly one
place, so everything a backend could plausibly get subtly wrong is implemented
here as a free function and called by both:

* :func:`normalise_filters` / :func:`payload_matches` — filter meaning
* :func:`as_unit_vector` — width, finiteness and zero-norm rules
* :func:`validate_payload` / :func:`validate_collection_name` — input rules

A backend that reimplements any of these is a bug waiting to happen.

Scores
------
Both collections are Cosine, and every vector is L2-normalised on the way in
(:func:`as_unit_vector`), so a stored dot product *is* a cosine in ``[-1, 1]``.
That is what makes ``context.relevance_floor`` and ``policy.anti_goal_threshold``
directly comparable against a :attr:`SearchHit.score` without rescaling.
``score_threshold`` is inclusive (``score >= threshold``) in both backends,
matching Qdrant's server-side behaviour.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, ClassVar, Final, Self

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from paa.config import Settings, StorageSettings
from paa.core.errors import StorageError
from paa.storage.vector.embeddings import FloatArray

__all__ = [
    "ABSOLUTE_FACTS_INDEX",
    "ACTIVE_FACTS",
    "RESERVED_PAYLOAD_KEYS",
    "RFC_COLLECTIONS",
    "CollectionSpec",
    "FilterValue",
    "Filters",
    "NormalisedFilters",
    "SearchHit",
    "VectorPoint",
    "VectorStore",
    "as_unit_vector",
    "is_unsatisfiable",
    "normalise_filters",
    "payload_matches",
    "resolve_storage_settings",
    "spec_for",
    "validate_collection_name",
    "validate_payload",
]

log = structlog.get_logger(__name__)

#: Scalar types a payload filter may compare against. Deliberately narrow — see
#: :func:`normalise_filters` for why floats and bools are refused.
FilterValue = str | int

#: What a caller passes: field -> scalar (equality) or sequence (membership).
Filters = Mapping[str, FilterValue | Sequence[FilterValue]]

#: What backends consume: field -> tuple of accepted values. Equality collapses
#: into a 1-tuple so there is a single code path.
NormalisedFilters = dict[str, tuple[FilterValue, ...]]

#: Payload keys the storage layer owns. The Qdrant backend needs one to carry
#: the caller's original point id (see :mod:`~paa.storage.vector.qdrant_backend`),
#: and a caller payload that collided with it would be silently corrupted. Both
#: backends refuse these keys so the failure is identical either way.
RESERVED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"__paa_point_id"})

#: Collection names double as filenames in the numpy backend, so they are held
#: to a filesystem-safe alphabet in *both* backends rather than only where it
#: matters. Divergent validation is divergent behaviour.
_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")

_DEFAULT_HNSW_M = 16
_DEFAULT_HNSW_EF_CONSTRUCT = 100


# ---------------------------------------------------------------------------
# Collection specifications (RFC §3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """One vector collection's physical definition.

    ``hnsw_m`` and ``hnsw_ef_construct`` are meaningless to the brute-force
    backend, which is exactly why they are recorded in the spec rather than
    passed ad hoc: the numpy backend persists them into its sidecar so a later
    migration to Qdrant can rebuild the index with the parameters the RFC
    specified, not whatever the default happened to be that day.
    """

    name: str
    dimensions: int
    distance: str
    hnsw_m: int
    hnsw_ef_construct: int
    keyword_payload_fields: tuple[str, ...]
    description: str = ""


#: Working memory: every live fact the runtime can recall. Higher ``m`` (16)
#: because this collection is large, churns constantly and is on the hot recall
#: path — graph degree buys recall at query time, which is where it is felt.
ACTIVE_FACTS: Final = CollectionSpec(
    name="active_facts",
    dimensions=384,
    distance="cosine",
    hnsw_m=16,
    hnsw_ef_construct=100,
    # Mirrors the queryable columns of hot_serving_active_facts. SPEC NOTE: the
    # RFC gives the collection's payload schema in prose only; the indexed set
    # is derived from the relational mirror, which is the schema of record in
    # this repo (see storage/relational/schema_sqlite.sql).
    keyword_payload_fields=(
        "entity_id",
        "predicate",
        "memory_domain",
        "memory_scope",
        "session_id",
    ),
    description="Decaying working memory. Point ids are hot_serving_active_facts.id, 1:1.",
)

#: Immutable strategic layer: doctrine, anti-goals, absolute directives (RFC §9).
#: Lower ``m`` (8) with a *higher* ``ef_construct`` (128) is not a typo — this
#: collection is small and written once, so a slower, more thorough build is
#: free, and a sparser graph keeps the resident index tiny. Recall here must be
#: near-exact: it is what the policy agent checks anti-goals against.
ABSOLUTE_FACTS_INDEX: Final = CollectionSpec(
    name="absolute_facts_index",
    dimensions=384,
    distance="cosine",
    hnsw_m=8,
    hnsw_ef_construct=128,
    # Mirrors hot_serving_policy_rules plus the entity link. Same SPEC NOTE as
    # ACTIVE_FACTS applies.
    keyword_payload_fields=("entity_id", "rule_name", "rule_kind", "severity", "source_file"),
    description="Immutable doctrine and anti-goal vectors. Never decays, never pruned.",
)

RFC_COLLECTIONS: Final[tuple[CollectionSpec, ...]] = (ACTIVE_FACTS, ABSOLUTE_FACTS_INDEX)

_SPEC_BY_NAME: Final[dict[str, CollectionSpec]] = {s.name: s for s in RFC_COLLECTIONS}


def spec_for(name: str) -> CollectionSpec | None:
    """The RFC spec for ``name``, or ``None`` for an ad-hoc collection."""
    return _SPEC_BY_NAME.get(name)


def resolve_storage_settings(settings: Settings | StorageSettings) -> StorageSettings:
    """Accept either the full :class:`~paa.config.Settings` or its storage half.

    Half the call sites hold one and half hold the other, and an ``isinstance``
    here beats a ``getattr`` because it actually narrows for the type checker.
    """
    return settings.storage if isinstance(settings, Settings) else settings


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class VectorPoint(BaseModel):
    """One embedded record on its way into a collection.

    ``vector`` accepts a list or an ndarray because both call sites are real:
    the embedder hands back an ndarray row, while anything deserialised from
    JSON or the ledger arrives as a list. It is normalised to float32 at write
    time by :func:`as_unit_vector`, not here — the store knows the collection
    width and this model does not.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(min_length=1, max_length=512)
    # Annotated with the bare ``np.ndarray`` rather than the parametrised
    # :data:`FloatArray` alias: pydantic cannot build a schema for a subscripted
    # numpy generic, and the dtype is enforced at write time anyway.
    vector: list[float] | np.ndarray
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("vector")
    @classmethod
    def _check_vector(cls, value: list[float] | np.ndarray) -> list[float] | np.ndarray:
        array = np.asarray(value)
        if array.ndim != 1:
            raise ValueError(f"vector must be 1-D, got shape {array.shape}")
        if array.size == 0:
            raise ValueError("vector must not be empty")
        return value

    def as_array(self) -> FloatArray:
        """The vector as a contiguous float32 array (not yet normalised)."""
        return np.ascontiguousarray(self.vector, dtype=np.float32)


class SearchHit(BaseModel):
    """One result. ``score`` is a cosine in ``[-1, 1]``; higher is nearer."""

    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Shared semantics — the single source of truth both backends call
# ---------------------------------------------------------------------------


def validate_collection_name(name: str, *, substrate: str) -> str:
    if not _COLLECTION_NAME_RE.match(name):
        raise StorageError(
            "invalid collection name; expected 1-64 chars of [A-Za-z0-9_-] "
            "starting with a letter, digit or underscore",
            substrate=substrate,
            collection=name,
        )
    return name


def validate_payload(
    payload: Mapping[str, Any], *, substrate: str, point_id: str
) -> dict[str, Any]:
    """Reject payloads that collide with storage-owned keys."""
    if collisions := RESERVED_PAYLOAD_KEYS.intersection(payload):
        raise StorageError(
            "payload uses a reserved key owned by the storage layer",
            substrate=substrate,
            point_id=point_id,
            reserved_keys=sorted(collisions),
        )
    return dict(payload)


def as_unit_vector(
    vector: Sequence[float] | FloatArray,
    dimensions: int,
    *,
    substrate: str,
    context: str,
) -> FloatArray:
    """Validate and L2-normalise a vector. The one place these rules live.

    Refuses three things, identically in both backends:

    * a width other than the collection's — the usual symptom of a swapped
      embedder, and the failure mode is silently wrong answers, not a crash;
    * NaN/inf — poisons every subsequent comparison in the matrix;
    * a zero vector. Qdrant accepts one and scores it 0.0 against everything;
      numpy would produce NaN normalising it. Rather than paper over the
      divergence, both refuse: a zero vector means "the encoder produced no
      signal", which is a bug upstream and should surface there.
    """
    array = np.ascontiguousarray(vector, dtype=np.float32).ravel()

    if array.size != dimensions:
        raise StorageError(
            "vector width does not match the collection",
            substrate=substrate,
            context=context,
            expected_dimensions=dimensions,
            actual_dimensions=int(array.size),
        )
    if not bool(np.isfinite(array).all()):
        raise StorageError(
            "vector contains NaN or infinity",
            substrate=substrate,
            context=context,
        )

    norm = float(np.linalg.norm(array))
    if norm == 0.0:
        raise StorageError(
            "refusing a zero vector; cosine similarity is undefined for it",
            substrate=substrate,
            context=context,
        )
    return (array / norm).astype(np.float32, copy=False)


def normalise_filters(filters: Filters | None, *, substrate: str) -> NormalisedFilters | None:
    """Collapse the caller's filter dict into ``field -> accepted values``.

    Semantics, which both backends honour exactly:

    * scalar value  -> equality  (``{"entity_id": "e1"}``)
    * sequence      -> membership (``{"memory_scope": ["a", "b"]}``)
    * multiple keys -> AND
    * a field absent from a payload never matches
    * an empty sequence matches nothing (see :func:`is_unsatisfiable`)

    SPEC DEVIATION (docs/adr/0003): filter values are restricted to ``str`` and
    ``int``. Everything excluded here is excluded because the backends could not
    otherwise be guaranteed to agree, and a filter that quietly means different
    things on different engines is worse than one that refuses to run:

    * **float** — numpy's ``==`` compares them happily; Qdrant's ``MatchValue``
      does not accept them at all (numeric payloads there want a range query,
      which is a separate feature and is not pretended to exist here). A caller
      filtering on ``0.1 + 0.2`` would get two different answers.
    * **bool** — the genuinely nasty one. Python says ``True == 1``, and
      embedded Qdrant, being pure Python, agrees. A real Qdrant *server* has a
      typed payload index and does not. So a bool filter would work locally and
      silently change meaning the day someone sets ``qdrant_url``. Encode flags
      as ``"true"``/``"false"`` strings, or as ints used consistently.
    * **None** — "field is null" and "field is absent" are distinguishable in
      Qdrant and indistinguishable in a plain dict comparison.
    """
    if not filters:
        return None

    normalised: NormalisedFilters = {}
    for field, raw in filters.items():
        if not isinstance(field, str) or not field:
            raise StorageError(
                "filter field names must be non-empty strings",
                substrate=substrate,
                field=repr(field),
            )
        values: tuple[FilterValue, ...] = (
            tuple(raw) if isinstance(raw, (list, tuple, set, frozenset)) else (raw,)
        )
        for value in values:
            _check_filter_value(value, field=field, substrate=substrate)
        normalised[field] = values

    return normalised


def _check_filter_value(value: Any, *, field: str, substrate: str) -> None:
    # bool is checked first because it is a subclass of int and would otherwise
    # slip through the isinstance below.
    if isinstance(value, bool):
        raise StorageError(
            "bool filter values are refused because embedded and server Qdrant "
            'disagree on whether True equals 1; use "true"/"false" strings',
            substrate=substrate,
            field=field,
            value_type="bool",
        )
    if isinstance(value, (str, int)):
        return
    raise StorageError(
        "unsupported filter value type; filters accept str and int only",
        substrate=substrate,
        field=field,
        value_type=type(value).__name__,
    )


def is_unsatisfiable(normalised: NormalisedFilters | None) -> bool:
    """Whether the filter can never match, so a backend can skip the query.

    An empty membership list is the case that matters: ``{"scope": []}`` means
    "in the empty set". Short-circuiting here rather than trusting each engine's
    edge-case handling is what keeps the two backends in agreement.
    """
    return normalised is not None and any(not values for values in normalised.values())


def payload_matches(payload: Mapping[str, Any], normalised: NormalisedFilters | None) -> bool:
    """Evaluate normalised filters against one payload."""
    if not normalised:
        return True
    for field, accepted in normalised.items():
        if field not in payload:
            return False
        actual = payload[field]
        if not any(_scalar_eq(actual, candidate) for candidate in accepted):
            return False
    return True


def _scalar_eq(actual: Any, expected: FilterValue) -> bool:
    """Plain Python equality — deliberately, not incidentally.

    This mirrors ``QdrantLocal``, which is also pure Python, so the two shipped
    backends match a stored ``True`` against a filter of ``1`` identically. The
    case where that would diverge from a real Qdrant server is a *bool filter
    value*, and :func:`normalise_filters` refuses those outright rather than
    letting the ambiguity through.
    """
    return bool(actual == expected)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class VectorStore(ABC):
    """Async vector recall substrate.

    Implementations are expected to be usable as async context managers and to
    tolerate :meth:`close` being called more than once — the runtime's shutdown
    path is best-effort and must not raise while unwinding.
    """

    #: Value passed as ``substrate`` on every StorageError this backend raises.
    substrate: ClassVar[str] = "vector"

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:  # noqa: B027 - a no-op default is correct here
        """Release handles and flush pending state. Idempotent.

        Deliberately concrete rather than abstract: a backend that holds no
        handles has nothing to release, and forcing it to write an empty
        override would be ceremony. Both shipped backends do override it.
        """

    # -- schema ------------------------------------------------------------

    @abstractmethod
    async def ensure_collection(
        self,
        name: str,
        dim: int,
        hnsw_m: int = _DEFAULT_HNSW_M,
        hnsw_ef_construct: int = _DEFAULT_HNSW_EF_CONSTRUCT,
    ) -> None:
        """Create the collection if absent. Idempotent.

        Raises :class:`~paa.core.errors.StorageError` if it exists at a
        different width: silently serving a 384-dim query against 768-dim data
        is worse than refusing to start.
        """

    @abstractmethod
    async def collection_exists(self, name: str) -> bool: ...

    async def ensure_spec(self, spec: CollectionSpec) -> None:
        """Create a collection from its :class:`CollectionSpec`."""
        await self.ensure_collection(
            spec.name, spec.dimensions, spec.hnsw_m, spec.hnsw_ef_construct
        )

    async def ensure_rfc_collections(self) -> None:
        """Create both RFC §3.2 collections with their specified HNSW params."""
        for spec in RFC_COLLECTIONS:
            await self.ensure_spec(spec)

    # -- data --------------------------------------------------------------

    @abstractmethod
    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> int:
        """Insert or replace points by id. Returns the number written."""

    @abstractmethod
    async def search(
        self,
        collection: str,
        query_vector: Sequence[float] | FloatArray,
        limit: int = 10,
        score_threshold: float | None = None,
        filters: Filters | None = None,
    ) -> list[SearchHit]:
        """Nearest neighbours, highest cosine first.

        ``score_threshold`` is inclusive. ``filters`` is documented on
        :func:`normalise_filters`.
        """

    @abstractmethod
    async def delete(self, collection: str, ids: Sequence[str]) -> int:
        """Delete by id. Returns how many of ``ids`` were actually present."""

    @abstractmethod
    async def count(self, collection: str) -> int:
        """Exact number of points in the collection."""

    # -- convenience -------------------------------------------------------

    async def upsert_one(self, collection: str, point: VectorPoint) -> int:
        return await self.upsert(collection, [point])

    def _validated_limit(self, limit: int) -> int:
        if limit < 1:
            raise StorageError(
                "search limit must be at least 1",
                substrate=self.substrate,
                limit=limit,
            )
        return limit
