"""Delegation safety: depth, node budget, and cycle detection (RFC §11).

Three independent ways an agent hierarchy runs away, each needing its own
ceiling because none of them implies the others:

1. **Depth.** A chain ``A -> B -> C -> D -> ...`` that never bottoms out.
   Bounded by ``ModalityProfile.recursion_ceiling``.
2. **Breadth.** A *shallow* plan that fans out enormously — depth 2 with a
   branch factor of 50 is 2551 nodes and blows the token budget without ever
   tripping the depth ceiling. Bounded by ``ModalityProfile.max_plan_nodes()``,
   the closed-form geometric series from RFC §11.1.
3. **Cycles.** ``A`` delegates to ``B``, ``B`` to ``C``, ``C`` back to ``A``.
   Each hop increments depth, so a depth ceiling *eventually* stops it — but
   only after burning the entire budget on work that was structurally doomed,
   and the ledger is left showing a plausible-looking chain rather than the
   loop that actually happened. RFC §11.1(3) requires detecting the cycle
   itself.

The cycle check is a real graph search (DFS over the live delegation edges),
not a "have I seen this id" set. Those are different questions: an agent may
legitimately appear twice in a plan on *sibling* branches — ``A`` delegating to
``C`` and ``B`` also delegating to ``C`` is fan-in, not recursion. Only a path
that returns to an *ancestor* is a cycle. A visited-set would reject the first
case and a naive parent-only check would miss the three-node version of the
second.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog

from paa.core.errors import RecursionGuardError
from paa.core.types import MODALITY_PROFILES, ComplexityModality, ModalityProfile

__all__ = ["DelegationGraph", "RecursionGuard"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class DelegationGraph:
    """Directed graph of *currently active* agent-to-agent delegations.

    "Currently active" is the important qualifier. Edges are removed when a
    delegation completes, so a sequential plan that calls ``C`` from ``A`` and
    later from ``B`` does not accumulate phantom structure. A graph of all
    *historical* delegations would report cycles that never coexisted.
    """

    edges: dict[str, set[str]] = field(default_factory=dict)

    def add_edge(self, source: str, target: str) -> None:
        self.edges.setdefault(source, set()).add(target)
        self.edges.setdefault(target, set())

    def remove_edge(self, source: str, target: str) -> None:
        if (targets := self.edges.get(source)) is not None:
            targets.discard(target)

    def successors(self, node: str) -> set[str]:
        return self.edges.get(node, set())

    def find_path(self, start: str, goal: str) -> list[str] | None:
        """Return a path ``start -> ... -> goal``, or ``None``.

        Iterative DFS with an explicit stack rather than recursion — a
        recursion guard that overflows the Python stack while checking for
        runaway recursion would be a genuinely embarrassing failure mode, and
        the graphs are adversarial by construction.
        """
        if start == goal:
            return [start]
        stack: list[tuple[str, list[str]]] = [(start, [start])]
        visited: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for successor in sorted(self.successors(node)):
                if successor == goal:
                    return [*path, successor]
                if successor not in visited:
                    stack.append((successor, [*path, successor]))
        return None

    def would_cycle(self, source: str, target: str) -> list[str] | None:
        """The cycle that adding ``source -> target`` would close, or ``None``.

        Adding an edge closes a cycle exactly when ``target`` can already reach
        ``source``. So we search *backwards* along the proposed edge — from
        target to source — and if a path exists, the new edge completes it.

        Returns the full cycle for the ledger: an operator debugging a refused
        delegation needs to see *which* loop, not merely that one existed.
        """
        if source == target:
            return [source, target]
        if (path := self.find_path(target, source)) is not None:
            return [source, *path]
        return None

    def node_count(self) -> int:
        return len(self.edges)

    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.edges.values())

    def clear(self) -> None:
        self.edges.clear()


class RecursionGuard:
    """Enforces RFC §11 delegation limits for one task lineage.

    Thread-safe: an ``RLock`` guards the graph and the counter, because the
    orchestrator may fan out sibling delegations across threads and two
    concurrent checks against an unsynchronised graph could both pass while
    together closing a cycle — a TOCTOU race that produces exactly the runaway
    this class exists to prevent.
    """

    def __init__(
        self,
        *,
        modality: ComplexityModality | ModalityProfile = ComplexityModality.STANDARD,
        absolute_ceiling: int = 4,
        correlation_id: str | None = None,
    ) -> None:
        profile = (
            MODALITY_PROFILES[modality] if isinstance(modality, ComplexityModality) else modality
        )
        self._profile = profile
        # The modality profile may only ever *lower* the absolute ceiling.
        # A MAX-modality profile must not be able to talk its way past the
        # system-wide bound configured by the operator.
        self._depth_ceiling = min(profile.recursion_ceiling, absolute_ceiling)
        self._node_ceiling = profile.max_plan_nodes()
        self._correlation_id = correlation_id

        self.graph = DelegationGraph()
        self._nodes_expanded = 0
        self._lock = threading.RLock()

    # -- introspection -----------------------------------------------------

    @property
    def depth_ceiling(self) -> int:
        return self._depth_ceiling

    @property
    def node_ceiling(self) -> int:
        return self._node_ceiling

    @property
    def nodes_expanded(self) -> int:
        return self._nodes_expanded

    @property
    def profile(self) -> ModalityProfile:
        return self._profile

    # -- individual checks -------------------------------------------------

    def check_depth(self, depth: int) -> None:
        """Refuse a delegation at or beyond the depth ceiling.

        ``depth`` is the depth the *child* would occupy. A ceiling of 2 permits
        depths 0, 1 and 2 — the ceiling is inclusive, matching RFC §11.1's
        ``D`` in ``B^(D+1)``, where a ceiling of 0 still allows one node.
        """
        if depth < 0:
            raise ValueError(f"depth must be non-negative, got {depth}")
        if depth > self._depth_ceiling:
            raise RecursionGuardError(
                "delegation would exceed the recursion depth ceiling",
                depth=depth,
                ceiling=self._depth_ceiling,
            )

    def check_nodes(self, additional: int = 1) -> None:
        """Refuse an expansion that would breach the node budget."""
        if self._nodes_expanded + additional > self._node_ceiling:
            raise RecursionGuardError(
                "plan expansion would exceed the node ceiling",
                depth=None,
                ceiling=self._node_ceiling,
            )

    def check_cycle(self, source: str, target: str) -> None:
        """Refuse an edge that would close a cycle in the delegation graph."""
        with self._lock:
            if (cycle := self.graph.would_cycle(source, target)) is not None:
                log.warning(
                    "sandbox.recursion.cycle_refused",
                    correlation_id=self._correlation_id,
                    cycle=cycle,
                )
                raise RecursionGuardError(
                    "delegation would close a cycle in the delegation graph",
                    cycle=cycle,
                )

    # -- the combined entry point -----------------------------------------

    def register(self, source: str, target: str, *, depth: int) -> None:
        """Run every check, then record the edge. Atomic.

        Checks run *before* any mutation, so a refused delegation leaves the
        graph exactly as it found it. A guard that half-applied a rejected edge
        would poison every subsequent check on that lineage.
        """
        with self._lock:
            self.check_depth(depth)
            self.check_nodes(1)
            self.check_cycle(source, target)

            self.graph.add_edge(source, target)
            self._nodes_expanded += 1
            log.debug(
                "sandbox.recursion.delegation_registered",
                correlation_id=self._correlation_id,
                source=source,
                target=target,
                depth=depth,
                nodes_expanded=self._nodes_expanded,
            )

    def release(self, source: str, target: str) -> None:
        """Drop a completed delegation edge.

        ``_nodes_expanded`` is deliberately **not** decremented: it is a budget
        consumed over the task's life, not a gauge of what is in flight. A
        planner that expanded and abandoned a thousand nodes has spent the
        tokens regardless of how many survive.
        """
        with self._lock:
            self.graph.remove_edge(source, target)

    @contextmanager
    def delegate(self, source: str, target: str, *, depth: int) -> Iterator[RecursionGuard]:
        """Scope one delegation, releasing the edge on exit.

        The recommended entry point: ``release`` in a ``finally`` is what keeps
        a *failed* sub-agent from leaving a permanent edge behind, which would
        make a later legitimate delegation look like a cycle.
        """
        self.register(source, target, depth=depth)
        try:
            yield self
        finally:
            self.release(source, target)

    def snapshot(self) -> dict[str, Any]:
        """Guard state for the ledger payload."""
        with self._lock:
            return {
                "modality": self._profile.modality.value,
                "depth_ceiling": self._depth_ceiling,
                "node_ceiling": self._node_ceiling,
                "nodes_expanded": self._nodes_expanded,
                "active_edges": self.graph.edge_count(),
                "known_agents": self.graph.node_count(),
            }

    def reset(self) -> None:
        """Clear all state. For reuse across lineages in a long-lived process."""
        with self._lock:
            self.graph.clear()
            self._nodes_expanded = 0
