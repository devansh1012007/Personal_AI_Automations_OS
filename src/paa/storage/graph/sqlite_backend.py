"""Graph traversal over the relational record, using recursive CTEs.

This backend has no dependency beyond the SQLite that is already open, and it
reads ``hot_serving_relationships`` directly — the table the schema calls "the
durable record so the graph can always be rebuilt from relational truth". That
gives it two jobs:

1. the zero-extra-dependency fallback when the ``graph`` extra is not installed
   (``backend_graph = "auto"`` on a machine without kuzu), and
2. the reference semantics. Where kuzu and this backend could disagree, this
   one is right and the kuzu backend is bent to match, because this one cannot
   drift from the durable table it queries.

Why a recursive CTE rather than repeated single-hop queries
-----------------------------------------------------------
A Python-side BFS issuing one query per frontier node turns a 3-hop traversal
into hundreds of round trips through the connection pool. The CTE expands the
whole frontier inside one query plan, and — critically — lets the cycle guard
run *during* expansion rather than after, so a cyclic graph never materialises
the paths that would have to be discarded.

Termination has two independent guarantees, deliberately belt-and-braces:
``w.depth < :max_hops`` bounds length, and the ``instr(...) = 0`` test refuses
to step onto a node already on the path. Either alone would terminate; a bug in
one still leaves the other standing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
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
from paa.storage.relational.database import Database, from_iso, to_iso, utc_now

__all__ = ["SqliteGraphStore"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "sqlite_graph"

#: ASCII Unit Separator. Used to delimit ids inside the CTE's path accumulator.
#: A printable delimiter such as ``/`` or ``,`` would risk a false positive in
#: the cycle test against an id that happened to contain it; 0x1F cannot appear
#: in a UUID hex or a canonical entity id.
_SEP: Final = "\x1f"

#: Conservative chunk for ``IN (...)`` rehydration. SQLite's compiled variable
#: ceiling is 999 on older builds; 400 stays clear of it on every build.
_IN_CHUNK: Final = 400

_EDGE_COLUMNS: Final = """
    id, from_entity_id, to_entity_id, rel_type, weight, confidence_decay,
    evidence_count, contradiction_score, source_memory_id, origin_signal_id,
    created_by_agent, valid_from, valid_to
"""

_UPSERT_ENTITY_SQL: Final = """
INSERT INTO hot_serving_entity_index (id, class, canonical_name, created_at, updated_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    class          = excluded.class,
    canonical_name = excluded.canonical_name,
    updated_at     = excluded.updated_at
"""

_UPSERT_EDGE_SQL: Final = f"""
INSERT INTO hot_serving_relationships ({_EDGE_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(from_entity_id, to_entity_id, rel_type, valid_from) DO UPDATE SET
    weight              = excluded.weight,
    confidence_decay    = excluded.confidence_decay,
    evidence_count      = excluded.evidence_count,
    contradiction_score = excluded.contradiction_score,
    source_memory_id    = excluded.source_memory_id,
    origin_signal_id    = excluded.origin_signal_id,
    created_by_agent    = excluded.created_by_agent,
    valid_to            = excluded.valid_to
"""

# The ``id`` column is pointedly absent from the DO UPDATE list: on conflict the
# incumbent row keeps its primary key, so anything that already recorded a
# reference to that edge id still resolves. The ABC documents this.


class SqliteGraphStore(GraphStore):
    """Property-graph semantics over ``hot_serving_relationships``.

    Does not own the :class:`Database` it is handed; :meth:`close` is a no-op.
    Closing a shared connection pool because one of its consumers went away
    would take the ledger down with it.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- schema ------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Verify the relational schema is present.

        There is nothing to create — ``schema_sqlite.sql`` owns these tables and
        :meth:`Database.connect` has already applied it. What this *can* catch is
        a graph store pointed at a database that was never migrated, which
        otherwise fails much later with a confusing "no such table" mid-traversal.
        """
        for table in ("hot_serving_entity_index", "hot_serving_relationships"):
            found = await self._db.fetch_value(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            if found is None:
                raise StorageError(
                    f"required table {table!r} is missing; the relational schema "
                    "has not been applied to this database",
                    substrate=_SUBSTRATE,
                    table=table,
                )

    # -- writes ------------------------------------------------------------

    async def upsert_entity(self, entity_id: str, class_: str, canonical_name: str) -> GraphEntity:
        now = to_iso(utc_now())
        try:
            await self._db.execute(
                _UPSERT_ENTITY_SQL, (entity_id, class_, canonical_name, now, now)
            )
        except sqlite3.IntegrityError as exc:
            # Almost always the UNIQUE on canonical_name: two ids claiming one
            # name. That is a resolution failure upstream in entity linking, and
            # guessing which one wins here would corrupt provenance silently.
            raise StorageError(
                f"entity upsert rejected: {exc}",
                substrate=_SUBSTRATE,
                entity_id=entity_id,
                canonical_name=canonical_name,
            ) from exc
        return GraphEntity(id=entity_id, class_=class_, canonical_name=canonical_name)

    async def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        if edge.rel_type not in EDGE_TYPES:
            raise StorageError(
                f"unknown relationship type {edge.rel_type!r}",
                substrate=_SUBSTRATE,
                rel_type=edge.rel_type,
            )
        params = (
            edge.id,
            edge.from_entity_id,
            edge.to_entity_id,
            edge.rel_type,
            edge.weight,
            edge.confidence_decay,
            edge.evidence_count,
            edge.contradiction_score,
            edge.source_memory_id,
            edge.origin_signal_id,
            edge.created_by_agent,
            to_iso(edge.valid_from),
            to_iso(edge.valid_to) if edge.valid_to else None,
        )
        try:
            await self._db.execute(_UPSERT_EDGE_SQL, params)
        except sqlite3.IntegrityError as exc:
            # FOREIGN KEY failure means an endpoint was never registered. See the
            # ABC: we refuse rather than conjuring a placeholder entity.
            raise StorageError(
                f"edge upsert rejected: {exc}",
                substrate=_SUBSTRATE,
                from_entity_id=edge.from_entity_id,
                to_entity_id=edge.to_entity_id,
                rel_type=edge.rel_type,
            ) from exc

        stored = await self._db.fetch_one(
            f"SELECT {_EDGE_COLUMNS} FROM hot_serving_relationships "
            "WHERE from_entity_id = ? AND to_entity_id = ? AND rel_type = ? AND valid_from = ?",
            (edge.from_entity_id, edge.to_entity_id, edge.rel_type, to_iso(edge.valid_from)),
        )
        if stored is None:  # pragma: no cover - implies the insert vanished
            raise StorageError(
                "edge disappeared immediately after upsert",
                substrate=_SUBSTRATE,
                rel_type=edge.rel_type,
            )
        return _edge_from_row(stored)

    async def delete_edge(self, from_id: str, to_id: str, rel_type: str) -> int:
        """Hard-delete live edges of one type between two entities.

        Retracted (``valid_to``-stamped) rows are left alone: they are history,
        and history is not adjacency. Deleting them would also break equivalence
        with the kuzu projection, which never held them in the first place.
        """
        if rel_type not in EDGE_TYPES:
            raise StorageError(
                f"unknown relationship type {rel_type!r}",
                substrate=_SUBSTRATE,
                rel_type=rel_type,
            )
        return await self._db.execute(
            "DELETE FROM hot_serving_relationships "
            "WHERE from_entity_id = ? AND to_entity_id = ? AND rel_type = ? AND valid_to IS NULL",
            (from_id, to_id, rel_type),
        )

    async def prune_edges(self, min_weight: float) -> int:
        """RFC §4.2 curation. Strictly below the floor: an edge sitting exactly on
        ``relationship_prune_floor`` survives, so raising the floor to a weight
        does not silently sever every edge that was tuned to it.

        Only live edges are prunable — a retracted edge is history, and history
        is not curated away by a weight sweep.
        """
        removed = await self._db.execute(
            "DELETE FROM hot_serving_relationships WHERE weight < ? AND valid_to IS NULL",
            (min_weight,),
        )
        if removed:
            log.info("graph.pruned", backend="sqlite", min_weight=min_weight, removed=removed)
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
        types = normalise_rel_types(rel_types)
        found: list[Neighbor] = []
        if direction in ("out", "both"):
            found.extend(await self._one_hop(entity_id, types, outbound=True))
        if direction in ("in", "both"):
            found.extend(await self._one_hop(entity_id, types, outbound=False))
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out', 'in' or 'both', got {direction!r}")
        return sort_neighbors(found, limit=limit)

    async def _one_hop(
        self, entity_id: str, types: tuple[str, ...] | None, *, outbound: bool
    ) -> list[Neighbor]:
        near, far = ("from_entity_id", "to_entity_id") if outbound else ("to_entity_id", "from_entity_id")
        clause, params = _rel_type_clause(types, prefix="r.")
        rows = await self._db.fetch_all(
            f"""
            SELECT {_prefixed(_EDGE_COLUMNS, "r.")},
                   e.id AS n_id, e.class AS n_class, e.canonical_name AS n_name
            FROM hot_serving_relationships r
            JOIN hot_serving_entity_index e ON e.id = r.{far}
            WHERE r.{near} = ? AND r.valid_to IS NULL{clause}
            """,
            (entity_id, *params),
        )
        direction = "out" if outbound else "in"
        return [
            Neighbor(
                entity=GraphEntity(
                    id=row["n_id"], class_=row["n_class"], canonical_name=row["n_name"]
                ),
                edge=_edge_from_row(row),
                direction=direction,
            )
            for row in rows
        ]

    async def traverse(
        self,
        start_id: str,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        rel_types: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[GraphPath]:
        hops = validate_max_hops(max_hops)
        types = normalise_rel_types(rel_types)
        clause, type_params = _rel_type_clause(types, prefix="r.")

        rows = await self._db.fetch_all(
            f"""
            WITH RECURSIVE walk(node_id, depth, node_path, edge_path) AS (
                SELECT ?, 0, ? || ? || ?, ''
                UNION ALL
                SELECT
                    r.to_entity_id,
                    w.depth + 1,
                    w.node_path || r.to_entity_id || ?,
                    CASE WHEN w.edge_path = '' THEN r.id ELSE w.edge_path || ? || r.id END
                FROM walk w
                JOIN hot_serving_relationships r ON r.from_entity_id = w.node_id
                WHERE w.depth < ?
                  AND r.valid_to IS NULL
                  AND instr(w.node_path, ? || r.to_entity_id || ?) = 0{clause}
            )
            SELECT node_path, edge_path FROM walk WHERE depth > 0
            """,
            (start_id, _SEP, start_id, _SEP, _SEP, _SEP, hops, _SEP, _SEP, *type_params),
        )
        paths = await self._rehydrate(rows)
        ordered = sort_paths(paths)
        return ordered[:limit] if limit is not None else ordered

    async def shortest_path(
        self, from_id: str, to_id: str, *, max_hops: int = DEFAULT_MAX_HOPS
    ) -> GraphPath | None:
        hops = validate_max_hops(max_hops)

        if from_id == to_id:
            entity = await self._entity(from_id)
            return GraphPath(nodes=(entity,), edges=()) if entity else None

        # ``w.node_id <> ?`` stops expansion the moment a walk reaches the
        # target: any continuation is by definition a longer route to the same
        # place, so exploring it is pure waste on a dense graph.
        rows = await self._db.fetch_all(
            """
            WITH RECURSIVE walk(node_id, depth, node_path, edge_path) AS (
                SELECT ?, 0, ? || ? || ?, ''
                UNION ALL
                SELECT
                    r.to_entity_id,
                    w.depth + 1,
                    w.node_path || r.to_entity_id || ?,
                    CASE WHEN w.edge_path = '' THEN r.id ELSE w.edge_path || ? || r.id END
                FROM walk w
                JOIN hot_serving_relationships r ON r.from_entity_id = w.node_id
                WHERE w.depth < ?
                  AND w.node_id <> ?
                  AND r.valid_to IS NULL
                  AND instr(w.node_path, ? || r.to_entity_id || ?) = 0
            )
            SELECT node_path, edge_path FROM walk WHERE depth > 0 AND node_id = ?
            """,
            (from_id, _SEP, from_id, _SEP, _SEP, _SEP, hops, to_id, _SEP, _SEP, to_id),
        )
        candidates = await self._rehydrate(rows)
        if not candidates:
            return None
        fewest = min(p.hop_count for p in candidates)
        return pick_canonical_path(p for p in candidates if p.hop_count == fewest)

    async def close(self) -> None:
        """No-op: the :class:`Database` belongs to whoever constructed it."""
        return None

    # -- internals ---------------------------------------------------------

    async def _entity(self, entity_id: str) -> GraphEntity | None:
        row = await self._db.fetch_one(
            "SELECT id, class, canonical_name FROM hot_serving_entity_index WHERE id = ?",
            (entity_id,),
        )
        if row is None:
            return None
        return GraphEntity(id=row["id"], class_=row["class"], canonical_name=row["canonical_name"])

    async def _rehydrate(self, rows: Sequence[Any]) -> list[GraphPath]:
        """Turn ``(node_path, edge_path)`` accumulator strings into full paths.

        The CTE carries only ids, because dragging thirteen edge columns through
        every recursion step would multiply the intermediate result size by the
        path count. Ids come back, then two bulk lookups rehydrate them — a
        fixed two extra queries regardless of how many paths matched.
        """
        if not rows:
            return []

        decoded: list[tuple[list[str], list[str]]] = []
        node_ids: set[str] = set()
        edge_ids: set[str] = set()
        for row in rows:
            path_nodes = [p for p in row["node_path"].split(_SEP) if p]
            path_edges = [e for e in row["edge_path"].split(_SEP) if e]
            # Defensive: the CTE already refuses to revisit a node, so a failure
            # here would mean the guard regressed rather than that data is odd.
            if not is_simple_path(path_nodes):  # pragma: no cover - guarded in SQL
                continue
            decoded.append((path_nodes, path_edges))
            node_ids.update(path_nodes)
            edge_ids.update(path_edges)

        entities = await self._fetch_entities(node_ids)
        edges = await self._fetch_edges(edge_ids)

        paths: list[GraphPath] = []
        for path_nodes, path_edges in decoded:
            try:
                paths.append(
                    GraphPath(
                        nodes=tuple(entities[n] for n in path_nodes),
                        edges=tuple(edges[e] for e in path_edges),
                    )
                )
            except KeyError as exc:  # pragma: no cover - implies a concurrent delete
                raise StorageError(
                    "path references a row that vanished mid-traversal",
                    substrate=_SUBSTRATE,
                    missing=str(exc),
                ) from exc
        return paths

    async def _fetch_entities(self, ids: set[str]) -> dict[str, GraphEntity]:
        out: dict[str, GraphEntity] = {}
        for chunk in _chunked(sorted(ids)):
            placeholders = ",".join("?" * len(chunk))
            rows = await self._db.fetch_all(
                "SELECT id, class, canonical_name FROM hot_serving_entity_index "
                f"WHERE id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                out[row["id"]] = GraphEntity(
                    id=row["id"], class_=row["class"], canonical_name=row["canonical_name"]
                )
        return out

    async def _fetch_edges(self, ids: set[str]) -> dict[str, GraphEdge]:
        out: dict[str, GraphEdge] = {}
        for chunk in _chunked(sorted(ids)):
            placeholders = ",".join("?" * len(chunk))
            rows = await self._db.fetch_all(
                f"SELECT {_EDGE_COLUMNS} FROM hot_serving_relationships "
                f"WHERE id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                out[row["id"]] = _edge_from_row(row)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunked(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), _IN_CHUNK):
        yield items[start : start + _IN_CHUNK]


def _prefixed(columns: str, prefix: str) -> str:
    """Qualify a column list for a join, so ``id`` cannot collide with the peer table."""
    return ", ".join(f"{prefix}{c.strip()}" for c in columns.split(",") if c.strip())


def _rel_type_clause(
    types: tuple[str, ...] | None, *, prefix: str = ""
) -> tuple[str, tuple[str, ...]]:
    """Build ``AND rel_type IN (?, ?)`` plus its bound values.

    Only the placeholder count is interpolated; the type strings themselves are
    bound parameters even though :func:`normalise_rel_types` has already vetted
    them against :data:`EDGE_TYPES`.
    """
    if not types:
        return "", ()
    placeholders = ",".join("?" * len(types))
    return f" AND {prefix}rel_type IN ({placeholders})", types


def _edge_from_row(row: Any) -> GraphEdge:
    return GraphEdge(
        id=row["id"],
        from_entity_id=row["from_entity_id"],
        to_entity_id=row["to_entity_id"],
        rel_type=row["rel_type"],
        weight=row["weight"],
        confidence_decay=row["confidence_decay"],
        evidence_count=row["evidence_count"],
        contradiction_score=row["contradiction_score"],
        source_memory_id=row["source_memory_id"],
        origin_signal_id=row["origin_signal_id"],
        created_by_agent=row["created_by_agent"],
        valid_from=from_iso(row["valid_from"]),
        valid_to=from_iso(row["valid_to"]) if row["valid_to"] else None,
    )
