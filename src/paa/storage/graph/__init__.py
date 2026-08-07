"""Graph substrate — multi-hop relationship traversal (RFC §1.3, §5).

Two interchangeable backends behind one ABC. See
:mod:`paa.storage.graph.base` for the semantics they are both held to, and
:mod:`paa.storage.graph.kuzu_backend` for the spec deviations kuzu required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from paa.storage.graph.base import (
    DEFAULT_MAX_HOPS,
    EDGE_TYPES,
    MAX_HOPS_CEILING,
    Direction,
    GraphEdge,
    GraphEntity,
    GraphPath,
    GraphStore,
    Neighbor,
)
from paa.storage.graph.sqlite_backend import SqliteGraphStore

if TYPE_CHECKING:
    from paa.config import StorageSettings
    from paa.storage.relational.database import Database

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
    "SqliteGraphStore",
    "get_graph_store",
    "kuzu_available",
]

log = structlog.get_logger(__name__)


def kuzu_available() -> bool:
    """Whether the ``graph`` extra is installed and loadable.

    Import is attempted rather than checked against a version list: a kuzu that
    is present but whose native extension fails to load on this platform must
    count as absent, and only an import proves that either way.
    """
    try:
        import kuzu  # noqa: F401
    except Exception:
        return False
    return True


def get_graph_store(settings: StorageSettings, db: Database) -> GraphStore:
    """Build the configured graph store.

    ``backend_graph``:

    ``"auto"``
        Kuzu when importable, SQLite otherwise. Degrading rather than failing is
        the point of the fallback — a missing optional extra must never stop the
        runtime booting.
    ``"kuzu"``
        Explicit request. Raises if the extra is absent, because a caller who
        named the backend wants to know it is missing rather than be quietly
        downgraded and left wondering why traversal got slower.
    ``"sqlite"``
        Recursive CTEs over ``hot_serving_relationships``.

    ``db`` is required regardless of backend so the caller need not know which
    one it will get; the kuzu store simply ignores it.

    Note for the integrator: these are two *separate* stores, not a mirrored
    pair. ``hot_serving_relationships`` is the durable record (the schema says
    so) and the kuzu graph is a derived projection of it. Whichever component
    owns edge writes — the memory creator, per RFC §4 — is responsible for
    writing both, or for rebuilding the projection from relational truth.
    """
    choice = settings.backend_graph

    if choice == "sqlite":
        return SqliteGraphStore(db)

    if choice in ("auto", "kuzu"):
        if kuzu_available():
            from paa.storage.graph.kuzu_backend import KuzuGraphStore

            return KuzuGraphStore(settings.kuzu_path)
        if choice == "kuzu":
            raise ImportError(
                "backend_graph='kuzu' but the extra is not installed: "
                "uv pip install -e '.[graph]' (or set backend_graph='auto')"
            )
        log.info("graph.backend_fallback", requested="auto", chosen="sqlite", reason="kuzu absent")
        return SqliteGraphStore(db)

    raise ValueError(f"unknown backend_graph {choice!r}")  # pragma: no cover - Literal-typed
