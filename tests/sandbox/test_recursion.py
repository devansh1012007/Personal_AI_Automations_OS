"""RFC §11 delegation limits: depth, node budget, and cycle detection."""

from __future__ import annotations

import pytest

from paa.core.errors import RecursionGuardError
from paa.core.types import MODALITY_PROFILES, ComplexityModality
from paa.sandbox.recursion import DelegationGraph, RecursionGuard


class TestDepthCeiling:
    def test_delegation_within_the_ceiling_is_allowed(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.COMPLEX)
        assert guard.depth_ceiling == 2

        guard.register("orchestrator", "planner", depth=1)
        guard.register("planner", "worker", depth=2)

        assert guard.nodes_expanded == 2

    def test_exceeding_the_depth_ceiling_raises(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.COMPLEX)

        with pytest.raises(RecursionGuardError) as exc_info:
            guard.register("worker", "sub_worker", depth=3)

        error = exc_info.value
        assert error.depth == 3
        assert error.ceiling == 2
        assert "depth ceiling" in str(error)

    def test_absolute_ceiling_can_only_lower_the_profile_ceiling(self) -> None:
        """A MAX profile must not talk its way past the operator's bound."""
        guard = RecursionGuard(modality=ComplexityModality.MAX, absolute_ceiling=1)
        assert MODALITY_PROFILES[ComplexityModality.MAX].recursion_ceiling == 4
        assert guard.depth_ceiling == 1

        with pytest.raises(RecursionGuardError):
            guard.register("a", "b", depth=2)

    def test_a_refused_delegation_leaves_the_graph_untouched(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.COMPLEX)

        with pytest.raises(RecursionGuardError):
            guard.register("a", "b", depth=99)

        assert guard.nodes_expanded == 0
        assert guard.graph.edge_count() == 0


class TestNodeCeiling:
    def test_simple_modality_permits_exactly_one_node(self) -> None:
        """SIMPLE has branch_factor 1 and ceiling 0, so ``max_plan_nodes`` is 1."""
        guard = RecursionGuard(modality=ComplexityModality.SIMPLE)
        assert guard.node_ceiling == 1

        guard.register("orchestrator", "handler", depth=0)

        with pytest.raises(RecursionGuardError, match="node ceiling"):
            guard.register("orchestrator", "another", depth=0)

    def test_node_ceiling_uses_the_geometric_series(self) -> None:
        """RFC §11.1: ``(B^(D+1) - 1)/(B - 1)``. MAX is B=2, D=4 -> 31."""
        guard = RecursionGuard(modality=ComplexityModality.MAX)
        assert guard.node_ceiling == 31

    def test_breadth_is_bounded_independently_of_depth(self) -> None:
        """A shallow but very wide plan must still be stopped."""
        guard = RecursionGuard(modality=ComplexityModality.STANDARD)
        assert guard.node_ceiling == 3  # B=2, D=1 -> (4-1)/1

        guard.register("root", "child_0", depth=1)
        guard.register("root", "child_1", depth=1)
        guard.register("root", "child_2", depth=1)

        with pytest.raises(RecursionGuardError, match="node ceiling"):
            guard.register("root", "child_3", depth=1)


class TestCycleDetection:
    def test_a_genuine_three_node_cycle_is_refused(self) -> None:
        """a -> b -> c, then c -> a must be refused. RFC §11.1(3).

        The three-node case is the one a naive "is the target my direct
        parent?" check misses entirely.
        """
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        guard.register("agent_a", "agent_b", depth=1)
        guard.register("agent_b", "agent_c", depth=2)

        with pytest.raises(RecursionGuardError) as exc_info:
            guard.register("agent_c", "agent_a", depth=3)

        error = exc_info.value
        assert "cycle" in str(error).lower()
        # The reported cycle must name every participant so an operator can
        # actually debug it.
        assert set(error.cycle) == {"agent_a", "agent_b", "agent_c"}
        assert error.cycle[0] == "agent_c"
        assert error.cycle[-1] == "agent_c"

    def test_a_two_node_cycle_is_refused(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.MAX)
        guard.register("a", "b", depth=1)

        with pytest.raises(RecursionGuardError, match="cycle"):
            guard.register("b", "a", depth=2)

    def test_self_delegation_is_refused(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        with pytest.raises(RecursionGuardError) as exc_info:
            guard.register("a", "a", depth=1)
        assert exc_info.value.cycle == ["a", "a"]

    def test_a_five_node_cycle_is_refused(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.MAX)
        chain = ["n0", "n1", "n2", "n3", "n4"]
        for depth, (source, target) in enumerate(zip(chain, chain[1:], strict=False), start=1):
            guard.register(source, target, depth=depth)

        with pytest.raises(RecursionGuardError, match="cycle"):
            guard.register("n4", "n0", depth=4)

    def test_fan_in_is_allowed_and_is_not_a_cycle(self) -> None:
        """Two agents delegating to the same worker is fan-in, not recursion.

        A visited-set implementation would wrongly reject this — which is why
        the guard does a real path search instead.
        """
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        guard.register("planner", "shared_worker", depth=1)
        guard.register("critic", "shared_worker", depth=1)

        assert guard.nodes_expanded == 2

    def test_diamond_topology_is_allowed(self) -> None:
        """a->b, a->c, b->d, c->d has a repeated node but no cycle."""
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        guard.register("a", "b", depth=1)
        guard.register("a", "c", depth=1)
        guard.register("b", "d", depth=2)
        guard.register("c", "d", depth=2)

        assert guard.graph.edge_count() == 4

    def test_releasing_an_edge_permits_a_previously_cyclic_delegation(self) -> None:
        """Only *active* delegations count — history must not accumulate."""
        guard = RecursionGuard(modality=ComplexityModality.MAX)
        guard.register("a", "b", depth=1)

        with pytest.raises(RecursionGuardError):
            guard.register("b", "a", depth=2)

        guard.release("a", "b")
        guard.register("b", "a", depth=2)  # no longer a cycle

        assert guard.graph.edge_count() == 1


class TestDelegateContextManager:
    def test_the_edge_is_released_on_normal_exit(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        with guard.delegate("a", "b", depth=1):
            assert guard.graph.edge_count() == 1

        assert guard.graph.edge_count() == 0

    def test_the_edge_is_released_even_when_the_body_raises(self) -> None:
        """A failed sub-agent must not leave a permanent edge behind."""
        guard = RecursionGuard(modality=ComplexityModality.MAX)

        with pytest.raises(RuntimeError), guard.delegate("a", "b", depth=1):
            raise RuntimeError("sub-agent exploded")

        assert guard.graph.edge_count() == 0
        # Budget is consumed regardless — the tokens were spent.
        assert guard.nodes_expanded == 1


class TestDelegationGraph:
    def test_find_path_walks_transitively(self) -> None:
        graph = DelegationGraph()
        graph.add_edge("a", "b")
        graph.add_edge("b", "c")
        graph.add_edge("c", "d")

        assert graph.find_path("a", "d") == ["a", "b", "c", "d"]
        assert graph.find_path("d", "a") is None

    def test_find_path_terminates_on_a_cyclic_graph(self) -> None:
        """The search must not loop forever on a graph that already cycles."""
        graph = DelegationGraph()
        graph.add_edge("a", "b")
        graph.add_edge("b", "a")

        assert graph.find_path("a", "zzz") is None

    def test_deep_chain_does_not_overflow_the_stack(self) -> None:
        """Iterative DFS: a guard that blows the stack while checking for
        runaway recursion would be self-defeating."""
        graph = DelegationGraph()
        for i in range(5000):
            graph.add_edge(f"n{i}", f"n{i + 1}")

        assert graph.would_cycle("n5000", "n0") is not None

    def test_snapshot_reports_guard_state(self) -> None:
        guard = RecursionGuard(modality=ComplexityModality.COMPLEX, correlation_id="corr-1")
        guard.register("a", "b", depth=1)

        snapshot = guard.snapshot()
        assert snapshot["modality"] == "COMPLEX"
        assert snapshot["depth_ceiling"] == 2
        assert snapshot["nodes_expanded"] == 1
        assert snapshot["active_edges"] == 1
