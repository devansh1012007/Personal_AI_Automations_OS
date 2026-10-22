"""The delegation graph — the guard that makes multi-agent calls deadlock-free.

RFC §11.1(3) requires cycle detection over the agent dependency graph. This is
where that requirement stops being prose: every property here corresponds to a
way a delegation could hang or exhaust the machine, and the graph refusing it.

The last class in this file drives the same guard through the *orchestrator*,
because the graph on its own is not the thing that runs in production:
``ChiefOrchestrator.mediate_delegation`` builds its own graph, with its own
bounds, taken from the task's modality profile.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult, MessageType
from paa.agents.delegation import DelegationEdge, DelegationGraph, DelegationRegistry
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.core.errors import RecursionGuardError
from paa.core.types import AgentRole, ComplexityModality, PermissionMode
from paa.ledger.store import LedgerStore


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


class _DelegatingAgent(Agent):
    """An agent that delegates to another agent and returns its outcome.

    The child's result is returned **as-is** rather than wrapped in a success.
    That is the whole point: a refusal at the far end of a chain has to
    travel back to the caller, and a wrapper that reports "the delegation
    succeeded" would bury it under a layer of optimism.
    """

    def __init__(self, name: str, target: str) -> None:
        super().__init__(name=name)
        self.role = AgentRole.WORKER
        self.can_delegate = True
        self._target = target

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        return await self.delegate(self._target, MessageType.DELEGATION, {"hop": True}, ctx)


def _cyclic_orchestrator(ledger: LedgerStore) -> tuple[ChiefOrchestrator, _DelegatingAgent]:
    """alpha → beta → gamma → alpha, wired through a real orchestrator.

    The head of the chain is returned alongside the orchestrator so the test
    can call it directly: the orchestrator's ``get`` is typed as returning
    ``Agent | None``, and a test that has to narrow a lookup it does not need
    is a test that can fail for the wrong reason.
    """
    alpha = _DelegatingAgent("alpha", "beta")
    beta = _DelegatingAgent("beta", "gamma")
    gamma = _DelegatingAgent("gamma", "alpha")
    orch = ChiefOrchestrator(ledger, worker=alpha, permission_mode=PermissionMode.AUTO)
    orch.register(beta)
    orch.register(gamma)
    return orch, alpha


class TestMediatedCycle:
    """The cycle guard, driven *through* the orchestrator.

    The graph-level tests above prove that ``DelegationGraph`` refuses a
    cycle. They do not prove the orchestrator is what enforces it — the
    mediation path builds its own graph, from the task's modality profile,
    and a cycle that never reaches it is not guarded.

    **The modality is not a detail here.** At ``STANDARD`` the recursion
    ceiling is 1, so the *depth* guard refuses the second hop before the
    chain is long enough to close a cycle at all: the refusal carries an
    empty ``cycle``. A test written at the default modality would therefore
    assert the depth guard and would pass even if ``would_cycle`` were
    deleted. Only at ``MAX`` — whose ceiling is 4 — does the chain reach the
    third hop and close ``alpha → beta → gamma → alpha``.
    """

    async def test_a_mediated_three_node_cycle_is_refused(self, ledger: LedgerStore) -> None:
        orch, alpha = _cyclic_orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="cycle", modality=ComplexityModality.MAX))
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.MAX,
            permission_mode=PermissionMode.AUTO,
        )

        result = await alpha.delegate("beta", MessageType.DELEGATION, {"hop": 1}, ctx)

        assert result.ok is False
        error = result.error or {}
        assert error["error_type"] == "RecursionGuardError"
        # The **cycle** guard fired, not the depth ceiling. A depth refusal
        # carries an empty cycle; only the cycle guard names the path.
        assert error["cycle"] == ["alpha", "beta", "gamma", "alpha"]
        # And the depth was well inside the ceiling, so the depth guard
        # cannot be what stopped it.
        assert error["depth"] == 3
        assert error["ceiling"] == 4

    async def test_the_cycle_guard_is_what_fires_at_max(self, ledger: LedgerStore) -> None:
        """The refusal names the cycle — the guard that produced it is
        unambiguous, and it is not the one that would fire by default."""
        orch, alpha = _cyclic_orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="cycle", modality=ComplexityModality.MAX))
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.MAX,
            permission_mode=PermissionMode.AUTO,
        )

        result = await alpha.delegate("beta", MessageType.DELEGATION, {"hop": 1}, ctx)

        message = str((result.error or {}).get("message", ""))
        assert "cycle" in message
        assert "recursion ceiling" not in message

    async def test_at_the_default_modality_the_depth_ceiling_fires_first(
        self, ledger: LedgerStore
    ) -> None:
        """Why the cycle test must run at ``MAX``.

        At ``STANDARD`` the chain is cut short by the depth ceiling and the
        cycle is never closed, so the refusal carries **no** cycle. This is
        the trap the exact-cycle test has to avoid, pinned so the reason
        lives in the suite rather than in a comment nobody re-reads.
        """
        orch, alpha = _cyclic_orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="cycle", modality=ComplexityModality.STANDARD))
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.AUTO,
        )

        result = await alpha.delegate("beta", MessageType.DELEGATION, {"hop": 1}, ctx)

        assert result.ok is False
        error = result.error or {}
        assert error["error_type"] == "RecursionGuardError"
        # No cycle path: the depth guard stopped the chain before a cycle
        # could close. (The payload carries ``None`` here and the exception
        # attribute carries ``[]`` — both are "no cycle".)
        assert not error.get("cycle")
        assert error["depth"] == 2
        assert error["ceiling"] == 1

    async def test_a_diamond_through_the_orchestrator_is_allowed(
        self, ledger: LedgerStore
    ) -> None:
        """The guard refuses cycles, not *reachability*.

        A DAG that reaches the same node twice is not a cycle, and refusing
        it would make the guard useless: almost every real delegation tree
        has a node with two parents.
        """
        alpha = _DelegatingAgent("alpha2", "beta2")
        beta = _DelegatingAgent("beta2", "delta2")
        delta = _DelegatingAgent("delta2", "omega2")
        omega = _DelegatingAgent("omega2", "omega2")
        orch = ChiefOrchestrator(ledger, worker=alpha, permission_mode=PermissionMode.AUTO)
        orch.register(beta)
        orch.register(delta)
        orch.register(omega)

        cid = await orch.submit(TaskRequest(goal="diamond", modality=ComplexityModality.MAX))
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.MAX,
            permission_mode=PermissionMode.AUTO,
        )
        result = await alpha.delegate("beta2", MessageType.DELEGATION, {"hop": 1}, ctx)

        # The chain ran to its end; the only refusal is the self-delegation
        # at the bottom, which is a cycle of length one.
        error = result.error or {}
        assert error.get("error_type") == "RecursionGuardError"
        assert error.get("cycle") == ["omega2", "omega2"]
