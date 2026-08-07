"""Embedded property-graph backend on KuzuDB (RFC §1.3).

Kuzu is a columnar, embedded graph engine — no server, no Docker, a single
directory on disk. It is the fast path for multi-hop provenance traversal; the
SQLite backend remains the durable record and the reference semantics.

SPEC DEVIATION (docs/adr/0003) — one REL TABLE per relationship type
--------------------------------------------------------------------
RFC §1.3 declares exactly two REL TABLEs, ``MUTATES`` and ``DEPENDS_ON``, while
RFC §7.3 goes on to list eleven relationship types the memory creator is
expected to emit. Those two sections cannot both be satisfied: kuzu resolves a
relationship label to a physical table, so an edge of a type with no REL TABLE
has nowhere to be written. Rather than silently dropping nine of the eleven, the
schema is generalised — one REL TABLE per type in the ``ck_rel_type`` CHECK
constraint of ``schema_sqlite.sql`` (twelve in total), every one carrying the
identical property set. :data:`paa.storage.graph.base.EDGE_TYPES` is the single
list all three places are generated from, so they cannot drift.

SPEC DEVIATION (docs/adr/0003) — live-edge projection
------------------------------------------------------
``hot_serving_relationships`` is bitemporal: an edge is retracted by stamping
``valid_to``, never by deleting the row. Kuzu holds only the *live* slice —
upserting an edge that carries a ``valid_to`` removes it from the graph instead
of storing it. This keeps the two backends answering identically without
threading a temporal predicate through every recursive Cypher pattern (kuzu's
filtered-recursive-join syntax would have to be correct in twelve places for
that to hold, and a wrong filter there fails silently by returning fewer paths).
History stays queryable in SQLite, which is where history belongs.

CONCURRENCY — read this before adding a call site
--------------------------------------------------
The kuzu Python bindings are **not thread-safe for concurrent writes**, and
RFC §16 lists exactly this as a known unknown. A second writer entering the
engine while the first is mid-transaction does not raise a tidy exception; it
corrupts or aborts the process. So:

* **Every** access — read or write, DDL or query — is serialised through one
  :class:`asyncio.Lock`. Not just writes: a read concurrent with a write is the
  same violation.
* Every kuzu call is blocking C++, so it runs via :func:`asyncio.to_thread`. Left
  on the event loop, a multi-hop traversal would stall every other coroutine in
  the runtime, including the heartbeats that prove workers are alive.
* Operations needing more than one statement to be atomic (count-then-delete)
  take the lock **once** and run the whole batch inside a single thread hop.
  :class:`asyncio.Lock` is not reentrant — a helper that re-acquires it while
  held would deadlock the runtime, which is why there is no public method here
  that calls another public method.

The lock plus a single connection is a deliberate throughput ceiling. This is a
single-user runtime; correctness under an engine that documents no concurrency
guarantee is worth far more than parallel reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final

import structlog

from paa.core.errors import StorageError
from paa.storage.graph.base import (
    DEFAULT_MAX_HOPS,
    EDGE_TYPES,
    Direction,
    GraphEdge,
    GraphEntity,
    GraphPath,
    GraphStore,
    Neighbor,
    is_simple_path,
    normalise_rel_types,
    pick_canonical_path,
    sort_neighbors,
    sort_paths,
    validate_max_hops,
)
from paa.storage.relational.database import from_iso, to_iso

try:
    import kuzu
except ImportError as exc:  # pragma: no cover - exercised by the factory's fallback
    raise ImportError(
        "KuzuGraphStore requires the 'graph' extra: uv pip install -e '.[graph]'"
    ) from exc

__all__ = ["KuzuGraphStore"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "kuzu"

#: Node label. Singular and generic: the relational entity index is a single
#: table with a ``class`` discriminator, and mirroring that as one kuzu node
#: table keeps traversal free of per-class label unions.
_NODE_TABLE: Final = "Entity"

#: Shared property set for all twelve REL TABLEs, mirroring the columns of
#: ``hot_serving_relationships``. ``valid_to`` is present for parity and is
#: always NULL by the live-edge projection invariant above.
_REL_PROPERTIES: Final = """
    edge_id STRING,
    weight DOUBLE,
    confidence_decay DOUBLE,
    evidence_count INT64,
    contradiction_score DOUBLE,
    source_memory_id STRING,
    origin_signal_id STRING,
    created_by_agent STRING,
    valid_from STRING,
    valid_to STRING
"""

#: Edge properties in the fixed order every RETURN clause below projects them.
_EDGE_RETURN: Final = (
    "e.edge_id, e.weight, e.confidence_decay, e.evidence_count, e.contradiction_score, "
    "e.source_memory_id, e.origin_signal_id, e.created_by_agent, e.valid_from, e.valid_to"
)

#: Cap the buffer pool explicitly. Kuzu otherwise sizes it from total system
#: memory (80% by default), which on the ~3.5 GB target machine would evict the
#: model runtime and the SQLite page cache to serve a graph of a few thousand
#: edges. See ADR-0001 for the memory budget this runtime lives inside.
_DEFAULT_BUFFER_POOL_BYTES: Final = 256 * 1024 * 1024


class KuzuGraphStore(GraphStore):
    """Kuzu-backed :class:`GraphStore`. See the module docstring for concurrency."""

    def __init__(
        self,
        path: Path | str,
        *,
        buffer_pool_bytes: int = _DEFAULT_BUFFER_POOL_BYTES,
        read_only: bool = False,
    ) -> None:
        self._path = Path(path)
        self._buffer_pool_bytes = buffer_pool_bytes
        self._read_only = read_only

        #: The one lock. See the module docstring — this guards reads too.
        self._lock = asyncio.Lock()
        self._db: Any = None
        self._conn: Any = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Open the database and create the node and REL TABLEs. Idempotent.

        Connecting also applies the DDL, so no query can reach a half-built
        schema; calling this at boot simply pays that cost up front rather than
        on the first traversal.
        """
        await self._batch([])

    async def close(self) -> None:
        if self._closed:
            return
        async with self._lock:
            self._closed = True
            await asyncio.to_thread(self._close_sync)
        log.info("graph.closed", backend=_SUBSTRATE, path=str(self._path))

    def _close_sync(self) -> None:
        for handle_name in ("_conn", "_db"):
            handle = getattr(self, handle_name)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception as exc:
                log.warning("graph.close_error", backend=_SUBSTRATE, error=str(exc))
            setattr(self, handle_name, None)

    def _connect_sync(self) -> None:
        """Open the engine and apply DDL. Called only from inside the lock."""
        if self._conn is not None:
            return
        if self._closed:
            raise StorageError("graph store is closed", substrate=_SUBSTRATE)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = kuzu.Database(
                str(self._path),
                buffer_pool_size=self._buffer_pool_bytes,
                # One engine thread. The lock already serialises us, so extra
                # worker threads would only add contention and memory.
                max_num_threads=1,
                read_only=self._read_only,
            )
            self._conn = kuzu.Connection(self._db)
        except Exception as exc:
            raise StorageError(
                f"could not open the kuzu database: {exc}",
                substrate=_SUBSTRATE,
                path=str(self._path),
            ) from exc

        if not self._read_only:
            self._apply_ddl_sync()
        log.info(
            "graph.connected",
            backend=_SUBSTRATE,
            path=str(self._path),
            rel_tables=len(EDGE_TYPES),
            buffer_pool_mb=self._buffer_pool_bytes // (1024 * 1024),
        )

    def _apply_ddl_sync(self) -> None:
        self._run_sync(
            f"CREATE NODE TABLE IF NOT EXISTS {_NODE_TABLE}("
            "id STRING, class STRING, canonical_name STRING, PRIMARY KEY(id))",
            None,
        )
        # sorted() so the table creation order is deterministic across runs; a
        # set's iteration order is not stable enough to debug against.
        for rel_type in sorted(EDGE_TYPES):
            self._run_sync(
                f"CREATE REL TABLE IF NOT EXISTS {rel_type}("
                f"FROM {_NODE_TABLE} TO {_NODE_TABLE},{_REL_PROPERTIES})",
                None,
            )

    # -- engine plumbing ---------------------------------------------------

    def _run_sync(self, query: str, params: dict[str, Any] | None) -> list[list[Any]]:
        """One blocking kuzu call. Only ever reached inside the lock, in a thread."""
        if self._conn is None:  # pragma: no cover - _connect_sync runs first
            raise StorageError("graph store is not connected", substrate=_SUBSTRATE)
        try:
            result = self._conn.execute(query, params or {})
        except Exception as exc:
            raise StorageError(
                f"kuzu rejected a query: {exc}", substrate=_SUBSTRATE, query=query[:200]
            ) from exc
        # execute() returns a list when handed a multi-statement string. We only
        # ever send one, but be explicit rather than trusting that.
        results = result if isinstance(result, list) else [result]
        try:
            return list(results[-1].get_all())
        finally:
            # Explicit close: a QueryResult finalised after its Database has been
            # closed raises from __del__ during interpreter shutdown.
            for res in results:
                res.close()

    def _run_batch_sync(
        self, batch: Sequence[tuple[str, dict[str, Any] | None]]
    ) -> list[list[list[Any]]]:
        self._connect_sync()
        return [self._run_sync(query, params) for query, params in batch]

    async def _batch(
        self, batch: Sequence[tuple[str, dict[str, Any] | None]]
    ) -> list[list[list[Any]]]:
        """Run statements under one lock acquisition and one thread hop.

        Batching is what makes count-then-delete atomic: nothing else can enter
        the engine between the two statements.
        """
        if self._closed:
            raise StorageError("graph store is closed", substrate=_SUBSTRATE)
        async with self._lock:
            return await asyncio.to_thread(self._run_batch_sync, batch)

    async def _one(self, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        return (await self._batch([(query, params)]))[0]

    # -- writes ------------------------------------------------------------

    async def upsert_entity(self, entity_id: str, class_: str, canonical_name: str) -> GraphEntity:
        await self._one(
            f"MERGE (n:{_NODE_TABLE} {{id: $id}}) "
            "SET n.class = $class_, n.canonical_name = $canonical_name",
            {"id": entity_id, "class_": class_, "canonical_name": canonical_name},
        )
        return GraphEntity(id=entity_id, class_=class_, canonical_name=canonical_name)

    async def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        rel_type = _checked_rel_type(edge.rel_type)

        # A retracted edge leaves the live projection entirely. See the
        # live-edge deviation in the module docstring.
        if edge.valid_to is not None:
            await self._delete_matching(edge.from_entity_id, edge.to_entity_id, rel_type)
            return edge

        endpoints_query = (
            f"MATCH (n:{_NODE_TABLE}) WHERE n.id = $from_id OR n.id = $to_id RETURN n.id"
        )
        merge_query = f"""
            MATCH (a:{_NODE_TABLE} {{id: $from_id}}), (b:{_NODE_TABLE} {{id: $to_id}})
            MERGE (a)-[e:{rel_type}]->(b)
            SET e.edge_id = coalesce(e.edge_id, $edge_id),
                e.weight = $weight,
                e.confidence_decay = $confidence_decay,
                e.evidence_count = $evidence_count,
                e.contradiction_score = $contradiction_score,
                e.source_memory_id = $source_memory_id,
                e.origin_signal_id = $origin_signal_id,
                e.created_by_agent = $created_by_agent,
                e.valid_from = $valid_from,
                e.valid_to = NULL
        """
        readback_query = f"""
            MATCH (a:{_NODE_TABLE} {{id: $from_id}})-[e:{rel_type}]->(b:{_NODE_TABLE} {{id: $to_id}})
            RETURN {_EDGE_RETURN}
        """
        # Kuzu rejects a parameter the query does not reference, so each
        # statement is handed exactly the bindings it names.
        endpoint_params = {"from_id": edge.from_entity_id, "to_id": edge.to_entity_id}
        merge_params = {
            **endpoint_params,
            "edge_id": edge.id,
            "weight": edge.weight,
            "confidence_decay": edge.confidence_decay,
            "evidence_count": edge.evidence_count,
            "contradiction_score": edge.contradiction_score,
            "source_memory_id": edge.source_memory_id,
            "origin_signal_id": edge.origin_signal_id,
            "created_by_agent": edge.created_by_agent,
            "valid_from": to_iso(edge.valid_from),
        }
        endpoints, _, readback = await self._batch(
            [
                (endpoints_query, endpoint_params),
                (merge_query, merge_params),
                (readback_query, endpoint_params),
            ]
        )

        # The MERGE is a no-op when an endpoint is missing (its MATCH binds
        # nothing), so checking afterwards is safe and keeps the batch atomic.
        # SQLite raises a FOREIGN KEY error on the same input; both refuse.
        present = {row[0] for row in endpoints}
        missing = sorted({edge.from_entity_id, edge.to_entity_id} - present)
        if missing:
            raise StorageError(
                "edge endpoints are not registered entities",
                substrate=_SUBSTRATE,
                missing=missing,
                rel_type=rel_type,
            )
        if not readback:  # pragma: no cover - implies MERGE silently did nothing
            raise StorageError(
                "edge disappeared immediately after upsert",
                substrate=_SUBSTRATE,
                rel_type=rel_type,
            )
        return _edge_from_row(
            readback[0], rel_type, edge.from_entity_id, edge.to_entity_id
        )

    async def delete_edge(self, from_id: str, to_id: str, rel_type: str) -> int:
        """Delete and report the count.

        Kuzu's ``QueryResult`` reports no affected-row count for a DELETE, so the
        matching edges are counted in the same batch — under the same lock — and
        that count is authoritative.
        """
        return await self._delete_matching(from_id, to_id, rel_type)

    async def _delete_matching(self, from_id: str, to_id: str, rel_type: str) -> int:
        """Shared by :meth:`delete_edge` and edge retraction in :meth:`upsert_edge`.

        Private so that neither public method ever calls the other — the lock is
        not reentrant, and one public method awaiting another is how that
        invariant gets broken by accident later.
        """
        checked = _checked_rel_type(rel_type)
        pattern = (
            f"MATCH (a:{_NODE_TABLE} {{id: $from_id}})-[e:{checked}]->"
            f"(b:{_NODE_TABLE} {{id: $to_id}})"
        )
        params = {"from_id": from_id, "to_id": to_id}
        counted, _ = await self._batch(
            [(f"{pattern} RETURN count(e)", params), (f"{pattern} DELETE e", params)]
        )
        return int(counted[0][0]) if counted else 0

    async def prune_edges(self, min_weight: float) -> int:
        """Sever weak edges across all twelve REL TABLEs (RFC §4.2).

        The untyped ``-[e]->`` pattern spans every REL TABLE, so curation does
        not have to know which types exist. Strictly below the floor, matching
        the SQLite backend: an edge exactly on the floor survives.
        """
        pattern = f"MATCH (a:{_NODE_TABLE})-[e]->(b:{_NODE_TABLE}) WHERE e.weight < $min_weight"
        params = {"min_weight": min_weight}
        counted, _ = await self._batch(
            [(f"{pattern} RETURN count(e)", params), (f"{pattern} DELETE e", params)]
        )
        removed = int(counted[0][0]) if counted else 0
        if removed:
            log.info("graph.pruned", backend=_SUBSTRATE, min_weight=min_weight, removed=removed)
        return removed

    # -- reads -------------------------------------------------------------

    async def neighbors(
        self,
        entity_id: str,
        *,
        rel_types: Iterable[str] | None = None,
        direction: Direction = "out",
        limit: int | None = 50,
    ) -> list[Neighbor]:
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out', 'in' or 'both', got {direction!r}")
        types = normalise_rel_types(rel_types)
        label = _label_clause(types)

        # "both" is two directed queries rather than kuzu's undirected `-[e]-`
        # pattern: the undirected form loses which way each edge points, and the
        # SQLite backend answers this as a union of two directed reads. Same
        # shape on both backends means the same answer.
        wanted: list[tuple[str, str]] = []
        if direction in ("out", "both"):
            wanted.append(("out", f"-[e{label}]->"))
        if direction in ("in", "both"):
            wanted.append(("in", f"<-[e{label}]-"))

        batch = [
            (
                f"MATCH (a:{_NODE_TABLE} {{id: $id}}){pattern}(b:{_NODE_TABLE}) "
                f"RETURN b.id, b.class, b.canonical_name, label(e), {_EDGE_RETURN}",
                {"id": entity_id},
            )
            for _, pattern in wanted
        ]
        results = await self._batch(batch)

        found: list[Neighbor] = []
        for (heading, _), rows in zip(wanted, results, strict=True):
            for row in rows:
                peer_id, peer_class, peer_name, rel_type = row[0], row[1], row[2], row[3]
                from_id, to_id = (
                    (entity_id, peer_id) if heading == "out" else (peer_id, entity_id)
                )
                found.append(
                    Neighbor(
                        entity=GraphEntity(
                            id=peer_id, class_=peer_class, canonical_name=peer_name
                        ),
                        edge=_edge_from_row(row[4:], rel_type, from_id, to_id),
                        direction=heading,
                    )
                )
        return sort_neighbors(found, limit=limit)

    async def traverse(
        self,
        start_id: str,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        rel_types: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[GraphPath]:
        hops = validate_max_hops(max_hops)
        label = _label_clause(normalise_rel_types(rel_types))

        # ``hops`` is interpolated because kuzu cannot bind a parameter inside a
        # recursive bound (``*1..$n`` is a parse error). validate_max_hops has
        # already proved it is an int within the ceiling, and the label clause
        # is built only from EDGE_TYPES members — neither can carry caller text.
        rows = await self._one(
            f"MATCH p = (a:{_NODE_TABLE} {{id: $id}})-[e{label}*1..{hops}]->(b:{_NODE_TABLE}) "
            "RETURN nodes(p), rels(p)",
            {"id": start_id},
        )
        paths = _paths_from_rows(rows)
        ordered = sort_paths(paths)
        return ordered[:limit] if limit is not None else ordered

    async def shortest_path(
        self, from_id: str, to_id: str, *, max_hops: int = DEFAULT_MAX_HOPS
    ) -> GraphPath | None:
        hops = validate_max_hops(max_hops)

        if from_id == to_id:
            rows = await self._one(
                f"MATCH (n:{_NODE_TABLE} {{id: $id}}) RETURN n.id, n.class, n.canonical_name",
                {"id": from_id},
            )
            if not rows:
                return None
            node = GraphEntity(id=rows[0][0], class_=rows[0][1], canonical_name=rows[0][2])
            return GraphPath(nodes=(node,), edges=())

        # ALL SHORTEST rather than SHORTEST: equal-length routes tie constantly,
        # kuzu picks one by physical layout, and SQLite would pick another.
        # Taking every tied route and applying the shared tie-break makes the
        # two backends agree.
        rows = await self._one(
            f"MATCH p = (a:{_NODE_TABLE} {{id: $from_id}})"
            f"-[e* ALL SHORTEST 1..{hops}]->(b:{_NODE_TABLE} {{id: $to_id}}) "
            "RETURN nodes(p), rels(p)",
            {"from_id": from_id, "to_id": to_id},
        )
        return pick_canonical_path(_paths_from_rows(rows))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checked_rel_type(rel_type: str) -> str:
    """Gate every relationship label before it reaches a Cypher string.

    Kuzu resolves labels at parse time and cannot bind them as parameters, so
    they must be interpolated. This is the only door they come through.
    """
    if rel_type not in EDGE_TYPES:
        raise StorageError(
            f"unknown relationship type {rel_type!r}", substrate=_SUBSTRATE, rel_type=rel_type
        )
    return rel_type


def _label_clause(types: tuple[str, ...] | None) -> str:
    """Render ``:A|B`` for a type filter, or ``""`` to span every REL TABLE."""
    if not types:
        return ""
    return ":" + "|".join(_checked_rel_type(t) for t in types)


def _paths_from_rows(rows: Sequence[Sequence[Any]]) -> list[GraphPath]:
    """Build paths from ``(nodes(p), rels(p))`` rows, dropping non-simple walks.

    Kuzu 0.11 accepts the ``ACYCLIC`` and ``TRAIL`` keywords in a recursive
    pattern but still returns ``a -> b -> c -> a`` for a cyclic graph, so the
    cycle guard has to be applied here. The bounded hop count is what makes that
    safe: the engine can only ever hand back a finite set of walks to filter.

    Edge endpoints come from the node sequence rather than from the relationship
    payload, whose ``_src``/``_dst`` are internal storage offsets, not entity ids.
    """
    paths: list[GraphPath] = []
    for row in rows:
        node_dicts, rel_dicts = row[0], row[1]
        node_ids = [n["id"] for n in node_dicts]
        if not is_simple_path(node_ids):
            continue
        nodes = tuple(
            GraphEntity(id=n["id"], class_=n["class"], canonical_name=n["canonical_name"])
            for n in node_dicts
        )
        edges = tuple(
            _edge_from_mapping(rel, rel["_label"], node_ids[i], node_ids[i + 1])
            for i, rel in enumerate(rel_dicts)
        )
        paths.append(GraphPath(nodes=nodes, edges=edges))
    return paths


def _edge_from_mapping(
    values: dict[str, Any], rel_type: str, from_id: str, to_id: str
) -> GraphEdge:
    return GraphEdge(
        id=values["edge_id"],
        from_entity_id=from_id,
        to_entity_id=to_id,
        rel_type=rel_type,
        weight=values["weight"],
        confidence_decay=values["confidence_decay"],
        evidence_count=values["evidence_count"],
        contradiction_score=values["contradiction_score"],
        source_memory_id=values["source_memory_id"],
        origin_signal_id=values["origin_signal_id"],
        created_by_agent=values["created_by_agent"],
        valid_from=from_iso(values["valid_from"]),
        valid_to=from_iso(values["valid_to"]) if values["valid_to"] else None,
    )


def _edge_from_row(
    values: Sequence[Any], rel_type: str, from_id: str, to_id: str
) -> GraphEdge:
    """Positional counterpart of :func:`_edge_from_mapping` for ``_EDGE_RETURN`` rows."""
    keys = (
        "edge_id",
        "weight",
        "confidence_decay",
        "evidence_count",
        "contradiction_score",
        "source_memory_id",
        "origin_signal_id",
        "created_by_agent",
        "valid_from",
        "valid_to",
    )
    return _edge_from_mapping(dict(zip(keys, values, strict=True)), rel_type, from_id, to_id)
