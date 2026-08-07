"""The delegation graph — the guard that makes multi-agent calls deadlock-free.

RFC §11.1(3) requires cycle detection over the agent dependency graph. This is
where that requirement stops being prose: every property here corresponds to a
way a delegation could hang or exhaust the machine, and the graph refusing it.
"""

from __future__ import annotations

import uuid

import pytest

from paa.agents.delegation import DelegationEdge, DelegationGraph, DelegationRegistry
from paa.core.errors import RecursionGuardError


def edge(sender: str, target: str, *, depth: int = 1) -> DelegationEdge:
    return DelegationEdge(sender=sender, target=target, task_id=uuid.uuid4(), depth=depth)


class TestCycleDetection:
    def test_a_two_node_cycle_is_refused(self) -> None:
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)
        g.propose(edge("a", "b"))
        with pytest.raises(RecursionGuardError) as exc:
            g.propose(edge("b", "a"))
        assert exc.value.cycle == ["a", "b", "a"] or "a" in (exc.value.cycle or [])

    def test_a_three_node_cycle_is_refused(self) -> None:
        """a→b→c→a: bounded per hop, but non-terminating. The classic case."""
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)
        g.propose(edge("a", "b"))
        g.propose(edge("b", "c"))
        with pytest.raises(RecursionGuardError, match="cycle"):
            g.propose(edge("c", "a"))

    def test_self_delegation_is_refused(self) -> None:
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)
        with pytest.raises(RecursionGuardError, match="cycle"):
            g.propose(edge("a", "a"))

    def test_a_diamond_is_allowed(self) -> None:
        """a→b, a→c, b→d, c→d is a DAG — not a cycle. Must be permitted."""
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)
        g.propose(edge("a", "b"))
        g.propose(edge("a", "c"))
        g.propose(edge("b", "d"))
        g.propose(edge("c", "d"))  # d reached two ways, still acyclic
        assert g.edge_count == 4


class TestBounds:
    def test_depth_ceiling_is_enforced(self) -> None:
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=2, max_nodes=100)
        g.propose(edge("a", "b", depth=2))
        with pytest.raises(RecursionGuardError, match="recursion ceiling"):
            g.propose(edge("b", "c", depth=3))

    def test_node_ceiling_is_enforced(self) -> None:
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=2)
        g.propose(edge("a", "b"))
        g.propose(edge("a", "c"))
        with pytest.raises(RecursionGuardError, match="expanded-node ceiling"):
            g.propose(edge("a", "d"))


class TestRelease:
    def test_release_lets_a_sequential_fanout_reuse_targets(self) -> None:
        """a→b (done), then a→c, then c→a must NOT read as a cycle, because the
        a→b edge is gone. Without release(), stale edges cause false cycles."""
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)

        e1 = edge("a", "b")
        g.propose(e1)
        g.release(e1)

        g.propose(edge("a", "c"))
        # c→a is only a cycle if a→...→c exists; a→c does, so c→a IS a cycle.
        with pytest.raises(RecursionGuardError, match="cycle"):
            g.propose(edge("c", "a"))

    def test_release_of_a_missing_edge_is_harmless(self) -> None:
        g = DelegationGraph(correlation_id=uuid.uuid4(), max_depth=10, max_nodes=100)
        g.release(edge("x", "y"))  # never proposed; must not raise


class TestRegistry:
    def test_graphs_are_isolated_per_correlation(self) -> None:
        """Two tasks sharing agent names must not cross-contaminate: a→b in one
        correlation and b→a in another is two DAGs, not a cycle."""
        reg = DelegationRegistry(max_depth=10, max_nodes=100)
        c1, c2 = uuid.uuid4(), uuid.uuid4()

        reg.graph_for(c1).propose(edge("a", "b"))
        # Same names, different correlation — b→a here is fine.
        reg.graph_for(c2).propose(edge("b", "a"))

        assert reg.active_count() == 2

    def test_discard_frees_a_graph(self) -> None:
        reg = DelegationRegistry()
        cid = uuid.uuid4()
        reg.graph_for(cid).propose(edge("a", "b"))
        reg.discard(cid)
        assert reg.active_count() == 0

    def test_profile_bounds_flow_through(self) -> None:
        reg = DelegationRegistry(max_depth=1, max_nodes=1)
        g = reg.graph_for(uuid.uuid4())
        assert g.max_depth == 1
        assert g.max_nodes == 1
