"""Graph substrate contract — entities, edges, paths, and the store ABC.

The graph answers one question the vector and relational layers cannot: *how is
this entity connected to that one, and through what evidence?* RFC §1.3 gives it
a property-graph engine (KuzuDB); RFC §5 spends those hops building bounded
context; RFC §4.2 prunes it during curation.

Two backends implement this contract — :mod:`paa.storage.graph.kuzu_backend`
and :mod:`paa.storage.graph.sqlite_backend`. They are required to return
*identical* results for every operation here, because the runtime is free to
swap between them at boot (``backend_graph = "auto"``). Equivalence is not left
to chance: every ordering, cycle and tie-break rule lives in this module as a
shared helper that both backends call, so the two cannot drift apart in the
places where graph engines usually disagree.

Traversal semantics (the contract both backends implement)
----------------------------------------------------------
``simple paths``
    A path never revisits a node. This is what makes traversal terminate on a
    cyclic graph, and it is also the semantically correct rule for provenance:
    re-entering an entity you have already passed through adds no evidence.
``live edges only``
    Edges with a non-null ``valid_to`` are tombstoned history. They are visible
    in the relational record but never traversed.
``directed``
    :meth:`GraphStore.traverse` and :meth:`GraphStore.shortest_path` follow edge
    direction. Only :meth:`GraphStore.neighbors` takes a ``direction``.
``bounded``
    ``max_hops`` is mandatory and ceiling-checked. See :data:`MAX_HOPS_CEILING`.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from paa.storage.relational.database import utc_now

__all__ = [
    "DEFAULT_MAX_HOPS",
    "EDGE_TYPES",
    "MAX_HOPS_CEILING",
    "Direction",
    "GraphEdge",
    "GraphEntity",
    "GraphPath",
    "GraphStore",
    "Neighbor",
    "is_simple_path",
    "normalise_rel_types",
    "pick_canonical_path",
    "sort_neighbors",
    "sort_paths",
    "validate_max_hops",
]

#: Every relationship type the runtime may store.
#:
#: This set is the single source of truth shared by three places that must agree:
#: the ``ck_rel_type`` CHECK constraint in ``schema_sqlite.sql``, the REL TABLEs
#: created by the kuzu backend, and validation here. RFC §7.3 lists these 11;
#: ``MUTATES`` is the 12th, declared as a REL TABLE in RFC §1.3.
EDGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "DEPENDS_ON",
        "PART_OF",
        "DERIVED_FROM",
        "INVOLVES",
        "BLOCKS",
        "CAUSES",
        "SUPPORTS",
        "CONTRADICTS",
        "REFERS_TO",
        "TRIGGERED_BY",
        "SIMILAR_TO",
        "MUTATES",
    }
)

#: Hard ceiling on traversal depth, regardless of what a caller asks for.
#:
#: An unbounded (or merely generous) traversal on a dense graph does not fail —
#: it hangs, holding the kuzu lock or a SQLite reader while the path count grows
#: combinatorially. The RFC's own §16 flags graph blow-up as a known unknown, and
#: the deepest modality in :data:`paa.core.types.MODALITY_PROFILES` asks for 3
#: hops, so 8 is already generous headroom over every legitimate caller.
MAX_HOPS_CEILING: Final[int] = 8

#: Used when a caller expresses no opinion. Matches the COMPLEX modality's
#: ``graph_hops``; deeper work must ask for it explicitly.
DEFAULT_MAX_HOPS: Final[int] = 2

Direction = Literal["out", "in", "both"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GraphEntity(BaseModel):
    """A node. Mirrors ``hot_serving_entity_index``.

    The graph deliberately carries only the identity columns. Importance,
    confidence and attributes live in the relational row; duplicating them here
    would create two mutable copies of the same number and no way to say which
    is right.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    #: Maps to the ``class`` column. Trailing underscore because ``class`` is a
    #: Python keyword; the alias keeps ``GraphEntity(**row)`` working.
    class_: str = Field(alias="class")
    canonical_name: str


class GraphEdge(BaseModel):
    """A relationship. Fields mirror ``hot_serving_relationships`` one-for-one.

    Bitemporality lives in ``valid_from``/``valid_to``: an edge is retracted by
    stamping ``valid_to`` rather than deleting the row, so the relational table
    keeps the full history. Only live edges are traversable — see the module
    docstring.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    from_entity_id: str
    to_entity_id: str
    rel_type: str
    weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: λ for this edge's confidence decay. Defaults to the RFC §4.1
    #: ``relationship`` domain coefficient, which is what the schema defaults to.
    confidence_decay: float = 0.004
    evidence_count: int = 1
    contradiction_score: float = 0.0
    source_memory_id: str | None = None
    origin_signal_id: str | None = None
    created_by_agent: str = "memory_creator"
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None

    @property
    def is_live(self) -> bool:
        """Whether this edge is still in force and therefore traversable."""
        return self.valid_to is None


class Neighbor(BaseModel):
    """One adjacent entity, the edge that reaches it, and which way it points.

    Returning the edge alongside the entity means callers ranking by weight or
    filtering by provenance do not need a second round trip, which is the whole
    point of a graph hop being cheap.
    """

    model_config = ConfigDict(frozen=True)

    entity: GraphEntity
    edge: GraphEdge
    #: ``"out"`` when the edge points away from the queried entity.
    direction: Literal["out", "in"]


class GraphPath(BaseModel):
    """A walk through the graph: ``len(nodes) == len(edges) + 1``.

    ``accumulated_weight`` is the *product* of the edge weights, not their sum.
    Weights are confidences in [0, 1], and the confidence that a chain of
    inferences holds end-to-end is the product of its links — so a long chain of
    weak edges ranks below a short strong one, which is the ordering a context
    builder wants. A sum would reward length, which is exactly backwards.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[GraphEntity, ...]
    edges: tuple[GraphEdge, ...]

    @property
    def hop_count(self) -> int:
        return len(self.edges)

    @property
    def accumulated_weight(self) -> float:
        weight = 1.0
        for edge in self.edges:
            weight *= edge.weight
        return weight

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes)

    @property
    def terminal_id(self) -> str:
        return self.nodes[-1].id

    def __repr__(self) -> str:
        arrow = " -> ".join(self.node_ids)
        return f"GraphPath({arrow} w={self.accumulated_weight:.4f})"


# ---------------------------------------------------------------------------
# Shared semantics
#
# Both backends call these. Anything that decides *which* results come back or
# *in what order* belongs here rather than in a backend, because a difference in
# ordering between two engines is indistinguishable from a difference in
# results once a caller applies a limit.
# ---------------------------------------------------------------------------


def validate_max_hops(max_hops: int) -> int:
    """Reject unbounded or absurd traversals before they reach an engine.

    Raises rather than silently clamping: a caller asking for 40 hops has a bug,
    and quietly giving them 8 would hide it behind mysteriously truncated
    results.
    """
    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise TypeError(f"max_hops must be an int, got {type(max_hops).__name__}")
    if max_hops < 1:
        raise ValueError(f"max_hops must be at least 1, got {max_hops}")
    if max_hops > MAX_HOPS_CEILING:
        raise ValueError(
            f"max_hops {max_hops} exceeds the hard ceiling of {MAX_HOPS_CEILING}; "
            "unbounded traversal on a dense graph is a hang, not a slow query"
        )
    return max_hops


def normalise_rel_types(rel_types: Iterable[str] | None) -> tuple[str, ...] | None:
    """Validate and canonicalise a relationship-type filter.

    ``None`` means "every type". An empty iterable is a caller error rather than
    a synonym for ``None`` — silently widening a filter the caller narrowed to
    nothing would be the wrong guess.

    Also the injection boundary: these strings are interpolated into Cypher
    (kuzu cannot parameterise a relationship label), so nothing unvetted may
    pass. Membership of :data:`EDGE_TYPES` is checked here and only here.
    """
    if rel_types is None:
        return None
    ordered = tuple(dict.fromkeys(rel_types))
    if not ordered:
        raise ValueError("rel_types must be None (meaning 'all') or a non-empty selection")
    unknown = sorted(set(ordered) - EDGE_TYPES)
    if unknown:
        raise ValueError(f"unknown relationship type(s): {unknown}; expected a subset of EDGE_TYPES")
    return ordered


def is_simple_path(node_ids: Sequence[str]) -> bool:
    """Whether a walk visits every node at most once.

    This is the cycle guard. The SQLite backend enforces it inside the recursive
    CTE; the kuzu backend applies it to returned walks, because kuzu 0.11 accepts
    the ``ACYCLIC``/``TRAIL`` keywords but still returns ``a -> b -> c -> a``.
    """
    return len(set(node_ids)) == len(node_ids)


def sort_paths(paths: Iterable[GraphPath]) -> list[GraphPath]:
    """Deterministic path ordering: shortest first, then strongest, then by id.

    The final ``node_ids`` term is not cosmetic. Without a total order, two
    engines can legitimately return the same set of equally-ranked paths in
    different sequences, and any caller applying a limit would then see
    different results depending on which backend booted.
    """
    return sorted(paths, key=lambda p: (p.hop_count, -p.accumulated_weight, p.node_ids))


def sort_neighbors(neighbors: Iterable[Neighbor], *, limit: int | None = None) -> list[Neighbor]:
    """Rank adjacency by weight, breaking ties on a total order, then truncate."""
    ranked = sorted(
        neighbors,
        key=lambda n: (-n.edge.weight, n.entity.id, n.edge.rel_type, n.direction),
    )
    return ranked[:limit] if limit is not None else ranked


def pick_canonical_path(paths: Iterable[GraphPath]) -> GraphPath | None:
    """Choose one path from a set of candidates, identically on every backend.

    Used by ``shortest_path``. Several routes of equal length routinely tie, and
    both engines happily return a different one; :func:`sort_paths` breaks the
    tie the same way for both.
    """
    ordered = sort_paths(paths)
    return ordered[0] if ordered else None


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class GraphStore(ABC):
    """Async property-graph contract.

    Implementations must be safe to call concurrently from the runtime's task
    group. That is a real constraint rather than a formality — see the kuzu
    backend, whose driver is not thread-safe for concurrent writes.
    """

    @abstractmethod
    async def ensure_schema(self) -> None:
        """Create whatever the backend needs. Idempotent; safe to call at boot."""

    @abstractmethod
    async def upsert_entity(self, entity_id: str, class_: str, canonical_name: str) -> GraphEntity:
        """Insert or update a node, keyed on ``entity_id``."""

    @abstractmethod
    async def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        """Insert or update an edge, keyed on ``(from, to, rel_type, valid_from)``.

        Both endpoints must already exist; implementations raise
        :class:`paa.core.errors.StorageError` rather than silently creating
        placeholder nodes, because an edge to an entity nobody has named is
        almost always an ordering bug in the caller.

        Returns the *stored* edge, which may differ from the argument: on
        conflict the incumbent row's ``id`` is retained.
        """

    @abstractmethod
    async def neighbors(
        self,
        entity_id: str,
        *,
        rel_types: Iterable[str] | None = None,
        direction: Direction = "out",
        limit: int | None = 50,
    ) -> list[Neighbor]:
        """One hop out of ``entity_id``, ranked by edge weight descending."""

    @abstractmethod
    async def traverse(
        self,
        start_id: str,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        rel_types: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[GraphPath]:
        """Every simple outbound path from ``start_id`` up to ``max_hops``.

        Terminates on cyclic graphs by construction: a path may not revisit a
        node, and depth is ceiling-checked besides.
        """

    @abstractmethod
    async def shortest_path(
        self, from_id: str, to_id: str, *, max_hops: int = DEFAULT_MAX_HOPS
    ) -> GraphPath | None:
        """Fewest-hop directed route, or ``None`` if none exists within budget.

        ``from_id == to_id`` yields a zero-hop path when the entity exists, which
        is the mathematically consistent answer and spares callers a special
        case. It is *not* the same as a cycle back to the start: that would be a
        non-simple path and is never returned.
        """

    @abstractmethod
    async def delete_edge(self, from_id: str, to_id: str, rel_type: str) -> int:
        """Hard-delete matching edges. Returns how many were removed."""

    @abstractmethod
    async def prune_edges(self, min_weight: float) -> int:
        """Sever edges below ``min_weight`` (RFC §4.2 curation). Returns the count."""

    @abstractmethod
    async def close(self) -> None:
        """Release engine resources. Idempotent."""
