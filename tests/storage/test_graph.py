"""Graph substrate tests.

Every behavioural test is parametrised across both backends. That is the point:
the runtime picks a backend at boot, so a behaviour that holds only on SQLite is
a latent production bug on a machine that happens to have the graph extra
installed. Where a result could legitimately differ between two engines —
ordering, cycles, ties — the assertion pins the shared contract rather than
whatever the engine felt like returning.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from paa.core.errors import StorageError
from paa.storage.graph import (
    MAX_HOPS_CEILING,
    GraphEdge,
    GraphStore,
    SqliteGraphStore,
    get_graph_store,
    kuzu_available,
)
from paa.storage.relational.database import Database, utc_now

BACKENDS = [
    "sqlite",
    pytest.param(
        "kuzu",
        marks=[
            pytest.mark.requires_graph,
            pytest.mark.skipif(not kuzu_available(), reason="the 'graph' extra is not installed"),
        ],
    ),
]


async def _make_store(kind: str, db: Database, root: Path) -> GraphStore:
    if kind == "sqlite":
        store: GraphStore = SqliteGraphStore(db)
    else:
        from paa.storage.graph.kuzu_backend import KuzuGraphStore

        store = KuzuGraphStore(root / "graph")
    await store.ensure_schema()
    return store


@pytest.fixture(params=BACKENDS)
async def store(request: pytest.FixtureRequest, db: Database, tmp_path: Path) -> AsyncIterator[GraphStore]:
    graph = await _make_store(request.param, db, tmp_path)
    try:
        yield graph
    finally:
        await graph.close()


# ---------------------------------------------------------------------------
# Fixture graphs
# ---------------------------------------------------------------------------


async def _entities(store: GraphStore, *ids: str) -> None:
    for entity_id in ids:
        await store.upsert_entity(entity_id, "task", f"name-{entity_id}")


async def _edge(
    store: GraphStore, src: str, dst: str, rel_type: str = "DEPENDS_ON", weight: float = 0.5
) -> GraphEdge:
    return await store.upsert_edge(
        GraphEdge(from_entity_id=src, to_entity_id=dst, rel_type=rel_type, weight=weight)
    )


async def build_diamond(store: GraphStore) -> None:
    """Two equally-weighted 2-hop routes from ``a`` to ``c``, plus a cycle home.

    ``a -> b -> c`` and ``a -> d -> c`` have *identical* accumulated weight
    (0.9 x 0.8 == 0.8 x 0.9), so shortest_path cannot break the tie on length or
    weight and must fall through to the id ordering. That is deliberate: it is
    the only way to prove both engines resolve a genuine tie the same way.
    """
    await _entities(store, "a", "b", "c", "d", "e")
    await _edge(store, "a", "b", "DEPENDS_ON", 0.9)
    await _edge(store, "b", "c", "DEPENDS_ON", 0.8)
    await _edge(store, "c", "a", "DEPENDS_ON", 0.7)  # closes a cycle
    await _edge(store, "a", "d", "PART_OF", 0.8)
    await _edge(store, "d", "c", "DEPENDS_ON", 0.9)
    # "e" stays isolated so unreachability has something to prove.


async def build_cycle(store: GraphStore) -> None:
    """The minimal trap: a -> b -> c -> a and nothing else."""
    await _entities(store, "a", "b", "c")
    await _edge(store, "a", "b", "DEPENDS_ON", 0.9)
    await _edge(store, "b", "c", "DEPENDS_ON", 0.8)
    await _edge(store, "c", "a", "DEPENDS_ON", 0.7)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


async def test_upsert_entity_is_idempotent(store: GraphStore) -> None:
    first = await store.upsert_entity("a", "task", "alpha")
    second = await store.upsert_entity("a", "project", "alpha-renamed")
    assert first.id == second.id == "a"
    assert second.class_ == "project"
    assert second.canonical_name == "alpha-renamed"


async def test_upsert_edge_roundtrip(store: GraphStore) -> None:
    await _entities(store, "a", "b")
    stored = await store.upsert_edge(
        GraphEdge(
            from_entity_id="a",
            to_entity_id="b",
            rel_type="CAUSES",
            weight=0.42,
            evidence_count=7,
            created_by_agent="critic",
        )
    )
    assert stored.rel_type == "CAUSES"
    assert stored.weight == pytest.approx(0.42)
    assert stored.evidence_count == 7
    assert stored.created_by_agent == "critic"
    assert stored.is_live


async def test_upsert_edge_retains_incumbent_id(store: GraphStore) -> None:
    """A second upsert updates in place; anything holding the old edge id still resolves."""
    await _entities(store, "a", "b")
    valid_from = utc_now()
    first = await store.upsert_edge(
        GraphEdge(
            from_entity_id="a", to_entity_id="b", rel_type="DEPENDS_ON",
            weight=0.3, valid_from=valid_from,
        )
    )
    second = await store.upsert_edge(
        GraphEdge(
            from_entity_id="a", to_entity_id="b", rel_type="DEPENDS_ON",
            weight=0.95, valid_from=valid_from,
        )
    )
    assert second.id == first.id
    assert second.weight == pytest.approx(0.95)
    assert len(await store.neighbors("a")) == 1


async def test_upsert_edge_rejects_unknown_endpoint(store: GraphStore) -> None:
    """An edge to an entity nobody has named is an ordering bug, not a node to invent."""
    await _entities(store, "a")
    with pytest.raises(StorageError) as excinfo:
        await _edge(store, "a", "ghost")
    assert excinfo.value.substrate in {"sqlite_graph", "kuzu"}


async def test_upsert_edge_rejects_unknown_rel_type(store: GraphStore) -> None:
    await _entities(store, "a", "b")
    with pytest.raises(StorageError):
        await store.upsert_edge(
            GraphEdge(from_entity_id="a", to_entity_id="b", rel_type="ENTANGLES_WITH")
        )


# ---------------------------------------------------------------------------
# Neighbors
# ---------------------------------------------------------------------------


async def test_neighbors_outbound(store: GraphStore) -> None:
    await build_diamond(store)
    found = await store.neighbors("a", direction="out")
    assert [n.entity.id for n in found] == ["b", "d"]  # 0.9 outranks 0.8
    assert {n.direction for n in found} == {"out"}
    assert found[0].entity.canonical_name == "name-b"


async def test_neighbors_inbound(store: GraphStore) -> None:
    await build_diamond(store)
    found = await store.neighbors("c", direction="in")
    assert [n.entity.id for n in found] == ["d", "b"]  # d->c is 0.9, b->c is 0.8
    assert {n.direction for n in found} == {"in"}


async def test_neighbors_both_directions(store: GraphStore) -> None:
    await build_diamond(store)
    found = await store.neighbors("a", direction="both")
    assert sorted((n.entity.id, n.direction) for n in found) == [
        ("b", "out"),
        ("c", "in"),
        ("d", "out"),
    ]


async def test_neighbors_filters_rel_types(store: GraphStore) -> None:
    await build_diamond(store)
    found = await store.neighbors("a", rel_types=["PART_OF"])
    assert [n.entity.id for n in found] == ["d"]
    assert found[0].edge.rel_type == "PART_OF"


async def test_neighbors_limit_takes_the_strongest(store: GraphStore) -> None:
    await build_diamond(store)
    found = await store.neighbors("a", limit=1)
    assert [n.entity.id for n in found] == ["b"]


async def test_neighbors_of_isolated_and_unknown_entities(store: GraphStore) -> None:
    await build_diamond(store)
    assert await store.neighbors("e", direction="both") == []
    assert await store.neighbors("nonexistent", direction="both") == []


async def test_neighbors_rejects_bad_arguments(store: GraphStore) -> None:
    await build_diamond(store)
    with pytest.raises(ValueError, match="direction"):
        await store.neighbors("a", direction="sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown relationship type"):
        await store.neighbors("a", rel_types=["NOT_A_TYPE"])
    with pytest.raises(ValueError, match="non-empty"):
        await store.neighbors("a", rel_types=[])


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------


async def test_traverse_respects_max_hops(store: GraphStore) -> None:
    await _entities(store, "a", "b", "c", "d")
    await _edge(store, "a", "b")
    await _edge(store, "b", "c")
    await _edge(store, "c", "d")

    one = await store.traverse("a", max_hops=1)
    assert [p.node_ids for p in one] == [("a", "b")]

    two = await store.traverse("a", max_hops=2)
    assert [p.node_ids for p in two] == [("a", "b"), ("a", "b", "c")]

    three = await store.traverse("a", max_hops=3)
    assert [p.node_ids for p in three] == [
        ("a", "b"),
        ("a", "b", "c"),
        ("a", "b", "c", "d"),
    ]
    assert [p.hop_count for p in three] == [1, 2, 3]


async def test_traverse_terminates_on_a_cycle(store: GraphStore) -> None:
    """The one that matters: a -> b -> c -> a must not walk forever.

    Asking for the full ceiling means an engine looping the cycle would return
    paths of length 4..8 (or never return at all). The contract is simple paths,
    so the answer is the two that do not revisit ``a``.
    """
    await build_cycle(store)

    paths = await store.traverse("a", max_hops=MAX_HOPS_CEILING)

    assert [p.node_ids for p in paths] == [("a", "b"), ("a", "b", "c")]
    for path in paths:
        assert len(set(path.node_ids)) == len(path.node_ids), f"cycle leaked: {path.node_ids}"

    # Every node in the cycle sees the same bounded behaviour, not just the one
    # the walk happens to start from.
    for start in ("a", "b", "c"):
        assert len(await store.traverse(start, max_hops=MAX_HOPS_CEILING)) == 2


async def test_traverse_deeper_hops_add_nothing_once_the_cycle_closes(store: GraphStore) -> None:
    await build_diamond(store)
    at_two = await store.traverse("a", max_hops=2)
    at_five = await store.traverse("a", max_hops=5)
    assert [p.node_ids for p in at_two] == [p.node_ids for p in at_five]
    assert [p.node_ids for p in at_two] == [
        ("a", "b"),
        ("a", "d"),
        ("a", "b", "c"),
        ("a", "d", "c"),
    ]


async def test_traverse_filters_rel_types(store: GraphStore) -> None:
    await build_diamond(store)
    paths = await store.traverse("a", max_hops=3, rel_types=["DEPENDS_ON"])
    assert [p.node_ids for p in paths] == [("a", "b"), ("a", "b", "c")]


async def test_traverse_accumulates_weight_as_a_product(store: GraphStore) -> None:
    await build_diamond(store)
    paths = {p.node_ids: p for p in await store.traverse("a", max_hops=2)}
    assert paths[("a", "b")].accumulated_weight == pytest.approx(0.9)
    assert paths[("a", "b", "c")].accumulated_weight == pytest.approx(0.72)
    # A longer chain of good edges still ranks below a single strong hop.
    assert paths[("a", "b", "c")].accumulated_weight < paths[("a", "b")].accumulated_weight


async def test_traverse_carries_edge_payloads(store: GraphStore) -> None:
    await build_diamond(store)
    paths = {p.node_ids: p for p in await store.traverse("a", max_hops=2)}
    hop = paths[("a", "d")].edges[0]
    assert hop.rel_type == "PART_OF"
    assert hop.from_entity_id == "a"
    assert hop.to_entity_id == "d"
    assert hop.weight == pytest.approx(0.8)
    assert hop.created_by_agent == "memory_creator"


async def test_traverse_refuses_unbounded_depth(store: GraphStore) -> None:
    await build_diamond(store)
    with pytest.raises(ValueError, match="at least 1"):
        await store.traverse("a", max_hops=0)
    with pytest.raises(ValueError, match="ceiling"):
        await store.traverse("a", max_hops=MAX_HOPS_CEILING + 1)
    with pytest.raises(TypeError):
        await store.traverse("a", max_hops="lots")  # type: ignore[arg-type]


async def test_traverse_from_an_unknown_entity(store: GraphStore) -> None:
    await build_diamond(store)
    assert await store.traverse("nonexistent", max_hops=3) == []
    assert await store.traverse("e", max_hops=3) == []


# ---------------------------------------------------------------------------
# Shortest path
# ---------------------------------------------------------------------------


async def test_shortest_path_prefers_fewer_hops(store: GraphStore) -> None:
    await _entities(store, "a", "b", "c", "shortcut")
    await _edge(store, "a", "b", weight=0.9)
    await _edge(store, "b", "c", weight=0.9)
    await _edge(store, "a", "shortcut", weight=0.1)
    await _edge(store, "shortcut", "c", weight=0.1)
    await _edge(store, "a", "c", weight=0.2)

    path = await store.shortest_path("a", "c", max_hops=4)
    assert path is not None
    assert path.node_ids == ("a", "c")
    assert path.hop_count == 1  # one weak hop beats two strong ones


async def test_shortest_path_breaks_ties_identically(store: GraphStore) -> None:
    await build_diamond(store)
    path = await store.shortest_path("a", "c", max_hops=4)
    assert path is not None
    assert path.hop_count == 2
    # Both routes weigh exactly 0.72; the shared total order picks this one.
    assert path.node_ids == ("a", "b", "c")


async def test_shortest_path_honours_the_hop_budget(store: GraphStore) -> None:
    await build_diamond(store)
    assert await store.shortest_path("a", "c", max_hops=1) is None
    assert await store.shortest_path("a", "c", max_hops=2) is not None


async def test_shortest_path_when_unreachable(store: GraphStore) -> None:
    await build_diamond(store)
    assert await store.shortest_path("a", "e", max_hops=4) is None
    assert await store.shortest_path("a", "nonexistent", max_hops=4) is None
    assert await store.shortest_path("nonexistent", "a", max_hops=4) is None


async def test_shortest_path_to_self_is_zero_hops(store: GraphStore) -> None:
    await build_cycle(store)
    path = await store.shortest_path("a", "a", max_hops=4)
    assert path is not None
    assert path.hop_count == 0
    assert path.node_ids == ("a",)
    # Emphatically not the cycle back to itself.
    assert path.edges == ()
    assert await store.shortest_path("nonexistent", "nonexistent", max_hops=4) is None


# ---------------------------------------------------------------------------
# Deletion and curation
# ---------------------------------------------------------------------------


async def test_delete_edge(store: GraphStore) -> None:
    await build_diamond(store)
    assert await store.delete_edge("a", "b", "DEPENDS_ON") == 1
    assert [n.entity.id for n in await store.neighbors("a")] == ["d"]
    # Deleting again is a no-op, not an error.
    assert await store.delete_edge("a", "b", "DEPENDS_ON") == 0


async def test_delete_edge_rejects_unknown_rel_type(store: GraphStore) -> None:
    await build_diamond(store)
    with pytest.raises(StorageError):
        await store.delete_edge("a", "b", "TELEPORTS_TO")


async def test_prune_edges_severs_the_weak(store: GraphStore) -> None:
    await build_diamond(store)
    removed = await store.prune_edges(0.75)
    assert removed == 1  # only c->a at 0.7 falls below
    assert await store.neighbors("c", direction="out") == []
    # The cycle is gone, so nothing changes about termination — but the route
    # that survived is still intact.
    assert [p.node_ids for p in await store.traverse("a", max_hops=3)] == [
        ("a", "b"),
        ("a", "d"),
        ("a", "b", "c"),
        ("a", "d", "c"),
    ]


async def test_prune_edges_is_strictly_below_the_floor(store: GraphStore) -> None:
    """An edge tuned to sit exactly on the floor must survive a sweep at that floor."""
    await _entities(store, "a", "b")
    await _edge(store, "a", "b", weight=0.1)
    assert await store.prune_edges(0.1) == 0
    assert len(await store.neighbors("a")) == 1
    assert await store.prune_edges(0.11) == 1


# ---------------------------------------------------------------------------
# Bitemporality — retracted edges are history, not adjacency
# ---------------------------------------------------------------------------


async def test_retracted_edges_are_not_traversable(store: GraphStore) -> None:
    """Stamping ``valid_to`` removes an edge from adjacency without deleting history.

    The retraction names the same ``valid_from`` as the original: that 4-tuple is
    the edge's identity in the relational record, so retracting with a fresh
    timestamp would file a *second*, already-expired edge rather than closing the
    first.
    """
    await _entities(store, "a", "b", "c")
    opened = utc_now()
    await store.upsert_edge(
        GraphEdge(
            from_entity_id="a", to_entity_id="b", rel_type="DEPENDS_ON",
            weight=0.9, valid_from=opened,
        )
    )
    await _edge(store, "b", "c", weight=0.9)
    assert len(await store.neighbors("a")) == 1

    await store.upsert_edge(
        GraphEdge(
            from_entity_id="a",
            to_entity_id="b",
            rel_type="DEPENDS_ON",
            weight=0.9,
            valid_from=opened,
            valid_to=utc_now(),
        )
    )
    assert await store.neighbors("a") == []
    assert await store.traverse("a", max_hops=3) == []
    # b -> c is untouched; retraction is surgical, not a cascade.
    assert len(await store.neighbors("b")) == 1


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def test_factory_honours_explicit_sqlite(db: Database, tmp_path: Path) -> None:
    from paa.config import StorageSettings

    settings = StorageSettings(backend_graph="sqlite", kuzu_path=tmp_path / "graph")
    assert isinstance(get_graph_store(settings, db), SqliteGraphStore)


async def test_factory_auto_matches_availability(db: Database, tmp_path: Path) -> None:
    from paa.config import StorageSettings

    settings = StorageSettings(backend_graph="auto", kuzu_path=tmp_path / "graph")
    chosen = get_graph_store(settings, db)
    try:
        if kuzu_available():
            assert type(chosen).__name__ == "KuzuGraphStore"
        else:
            assert isinstance(chosen, SqliteGraphStore)
    finally:
        await chosen.close()


# ---------------------------------------------------------------------------
# Cross-backend equivalence
# ---------------------------------------------------------------------------


@pytest.mark.requires_graph
@pytest.mark.skipif(not kuzu_available(), reason="the 'graph' extra is not installed")
async def test_backends_return_identical_results(db: Database, tmp_path: Path) -> None:
    """Build the same graph in both engines and diff every answer.

    The parametrised tests above already hold each backend to the same
    assertions; this one is the direct proof, and it is the test that would
    catch an ordering difference nobody thought to assert on.
    """
    sqlite_store = await _make_store("sqlite", db, tmp_path)
    kuzu_store = await _make_store("kuzu", db, tmp_path)
    try:
        for graph in (sqlite_store, kuzu_store):
            await build_diamond(graph)

        for direction in ("out", "in", "both"):
            left = await sqlite_store.neighbors("a", direction=direction)  # type: ignore[arg-type]
            right = await kuzu_store.neighbors("a", direction=direction)  # type: ignore[arg-type]
            assert [(n.entity.id, n.direction, n.edge.rel_type) for n in left] == [
                (n.entity.id, n.direction, n.edge.rel_type) for n in right
            ], f"neighbors disagree for direction={direction}"

        for hops in range(1, 5):
            left_paths = [p.node_ids for p in await sqlite_store.traverse("a", max_hops=hops)]
            right_paths = [p.node_ids for p in await kuzu_store.traverse("a", max_hops=hops)]
            assert left_paths == right_paths, f"traverse disagrees at max_hops={hops}"

        for target in ("a", "b", "c", "d", "e", "nonexistent"):
            left_path = await sqlite_store.shortest_path("a", target, max_hops=4)
            right_path = await kuzu_store.shortest_path("a", target, max_hops=4)
            assert (left_path.node_ids if left_path else None) == (
                right_path.node_ids if right_path else None
            ), f"shortest_path disagrees for target={target}"

        assert await sqlite_store.prune_edges(0.75) == await kuzu_store.prune_edges(0.75)
        assert await sqlite_store.delete_edge("a", "b", "DEPENDS_ON") == await (
            kuzu_store.delete_edge("a", "b", "DEPENDS_ON")
        )
    finally:
        await sqlite_store.close()
        await kuzu_store.close()
