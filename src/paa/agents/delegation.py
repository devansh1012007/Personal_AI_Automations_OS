"""The delegation graph and its safety invariants.

RFC §11.1(3) states the cycle-detection requirement as a formula:

.. math::
    \\text{Cycle} \\iff \\exists v \\in V \\text{ reachable from } v

but the RFC never says where the graph lives or who maintains it. Without an
owner it is prose, not a guard. This module makes it real: the orchestrator
holds one :class:`DelegationGraph` per correlation, and every delegation edge
is proposed to it *before* the callee runs.

Three bounds are enforced together, because each catches a failure the others
miss:

* **Depth** stops a linear chain a→b→c→d… from running forever.
* **Cycle** stops a→b→a, which has bounded depth per hop but never terminates.
* **Node count** stops a wide fan-out a→{b₁…b₅₀}, which is neither deep nor
  cyclic but still exhausts the machine.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from paa.core.errors import RecursionGuardError

__all__ = ["DelegationEdge", "DelegationGraph", "DelegationRegistry"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DelegationEdge:
    """One agent asking another to act."""

    sender: str
    target: str
    task_id: uuid.UUID
    depth: int


@dataclass(slots=True)
class DelegationGraph:
    """Active delegations for a single correlation.

    Nodes are agent *instance* keys, not class names: two concurrent workers
    are distinct nodes, so `worker#1 → critic` and `worker#2 → critic` do not
    look like a cycle through a shared `critic` node.
    """

    correlation_id: uuid.UUID
    max_depth: int
    max_nodes: int
    _edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _node_count: int = 0

    def would_cycle(self, sender: str, target: str) -> list[str] | None:
        """Return the cycle path if adding ``sender → target`` closes one.

        A cycle exists iff ``sender`` is already reachable *from* ``target``:
        adding the edge would then let control return to ``target`` via
        ``sender``. Depth-first search, iterative to avoid blowing the Python
        stack on a deep chain.
        """
        if sender == target:
            return [sender, target]

        stack: list[tuple[str, list[str]]] = [(target, [target])]
        seen: set[str] = set()

        while stack:
            node, path = stack.pop()
            if node == sender:
                return [*path, target]
            if node in seen:
                continue
            seen.add(node)
            for successor in self._edges.get(node, ()):
                stack.append((successor, [*path, successor]))
        return None

    def propose(self, edge: DelegationEdge) -> None:
        """Validate and record a delegation. Raises on any breach."""
        if edge.depth > self.max_depth:
            raise RecursionGuardError(
                "delegation would exceed the recursion ceiling",
                depth=edge.depth,
                ceiling=self.max_depth,
            )

        if (cycle := self.would_cycle(edge.sender, edge.target)) is not None:
            raise RecursionGuardError(
                "delegation would close a cycle in the agent graph",
                depth=edge.depth,
                ceiling=self.max_depth,
                cycle=cycle,
            )

        projected = self._node_count + 1
        if projected > self.max_nodes:
            raise RecursionGuardError(
                "delegation would exceed the expanded-node ceiling",
                depth=edge.depth,
                ceiling=self.max_nodes,
            )

        self._edges[edge.sender].add(edge.target)
        self._node_count = projected
        log.debug(
            "delegation.accepted",
            correlation_id=str(self.correlation_id),
            sender=edge.sender,
            target=edge.target,
            depth=edge.depth,
            nodes=self._node_count,
        )

    def release(self, edge: DelegationEdge) -> None:
        """Remove a completed delegation.

        Without this a sequential fan-out (a→b, b finishes, a→c, c→a) would
        keep stale edges and report false cycles.
        """
        self._edges.get(edge.sender, set()).discard(edge.target)

    @property
    def node_count(self) -> int:
        return self._node_count

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self._edges.values())


class DelegationRegistry:
    """All live delegation graphs, keyed by correlation.

    Graphs are per-correlation so two unrelated tasks cannot appear to form a
    cycle through a shared agent name.
    """

    def __init__(self, *, max_depth: int = 2, max_nodes: int = 32) -> None:
        self._graphs: dict[uuid.UUID, DelegationGraph] = {}
        self._max_depth = max_depth
        self._max_nodes = max_nodes

    def graph_for(
        self,
        correlation_id: uuid.UUID,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> DelegationGraph:
        if correlation_id not in self._graphs:
            self._graphs[correlation_id] = DelegationGraph(
                correlation_id=correlation_id,
                max_depth=self._max_depth if max_depth is None else max_depth,
                max_nodes=self._max_nodes if max_nodes is None else max_nodes,
            )
        return self._graphs[correlation_id]

    def discard(self, correlation_id: uuid.UUID) -> None:
        """Drop a finished correlation's graph. Called on task completion."""
        self._graphs.pop(correlation_id, None)

    def active_count(self) -> int:
        return len(self._graphs)
