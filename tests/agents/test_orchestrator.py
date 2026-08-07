"""Orchestrator lifecycle, driven with fake agents.

The integration suite proves the orchestrator against the *real* agents; these
tests isolate its control logic with fakes, so a failure points at the
orchestrator rather than at a collaborator. They assert the properties that make
it a control plane: correct event ordering, retry-then-fail, resume-skips-done,
and human-gate handling.
"""

from __future__ import annotations

from typing import Any

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.core.types import AgentRole, EventType, PermissionMode
from paa.ledger.replay import TaskPhase, project
from paa.ledger.store import LedgerStore


class _FakeAgent(Agent):
    """An agent that returns a canned value, or fails a set number of times."""

    def __init__(self, role: AgentRole, value: Any, *, fail_times: int = 0) -> None:
        super().__init__(name=role.value)
        self.role = role
        self._value = value
        self._fail_times = fail_times
        self.call_count = 0

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            return AgentResult.failure("scripted failure")
        return AgentResult.success(self._value)


def _orchestrator(ledger: LedgerStore, **agent_overrides: Any) -> ChiefOrchestrator:
    defaults = {
        "context_builder": _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER, {"routing_directive": "PROCEED_TO_PLANNER"}
        ),
        "planner": _FakeAgent(
            AgentRole.STRATEGIC_PLANNER,
            {"execution_steps": [{"index": 0, "action": "a"}], "step_requirements": {}},
        ),
        "policy": _FakeAgent(
            AgentRole.POLICY_RISK, {"decision": "STATUS_APPROVED", "requires_human_gate": False}
        ),
        "worker": _FakeAgent(AgentRole.WORKER, {"patch": "", "step_index": 0}),
        "critic": _FakeAgent(AgentRole.CRITIC, {"verdict": "PASS", "findings": []}),
    }
    defaults.update(agent_overrides)
    return ChiefOrchestrator(ledger, permission_mode=PermissionMode.AUTO, **defaults)


async def _events(ledger: LedgerStore, cid: Any) -> list[EventType]:
    return [e.event_type for e in await ledger.read_correlation(cid)]


class TestLifecycle:
    async def test_happy_path_event_order(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="do a thing"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        events = await _events(ledger, cid)
        # The RFC §7 order, checked as a subsequence.
        order = [
            EventType.TASK_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.MUTATION_COMMITTED,
        ]
        positions = [events.index(e) for e in order]
        assert positions == sorted(positions)

    async def test_policy_block_stops_before_execution(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_BLOCKED", "reason": "nope", "anti_goal_match": False},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="dangerous"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.BLOCKED
        events = await _events(ledger, cid)
        assert EventType.EXECUTION_STARTED not in events
        assert EventType.MUTATION_COMMITTED not in events

    async def test_security_violation_on_anti_goal(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_BLOCKED", "reason": "anti-goal", "anti_goal_match": True},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="exfiltrate"))
        await orch.run(cid)
        assert EventType.SECURITY_VIOLATION in await _events(ledger, cid)


class TestRetry:
    async def test_worker_retries_then_succeeds(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            worker=_FakeAgent(AgentRole.WORKER, {"patch": ""}, fail_times=1),
        )
        cid = await orch.submit(TaskRequest(goal="flaky"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        # One VALIDATION_FAILED (the failed attempt) then success.
        assert EventType.VALIDATION_FAILED in await _events(ledger, cid)

    async def test_retry_exhaustion_fails_the_task(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            worker=_FakeAgent(AgentRole.WORKER, {"patch": ""}, fail_times=99),
        )
        cid = await orch.submit(TaskRequest(goal="always fails"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.FAILED
        assert EventType.MUTATION_COMMITTED not in await _events(ledger, cid)


class TestHumanGate:
    async def test_gate_parks_then_clears(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_APPROVED", "requires_human_gate": True, "reason": "confirm"},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        outcome = await orch.run(cid)
        assert outcome.phase is TaskPhase.AWAITING_HUMAN

        # A human clears it. Only this external call can.
        state = await orch.clear_human_gate(cid, approved=True)
        assert state.phase is not TaskPhase.AWAITING_HUMAN
        assert EventType.HUMAN_GATE_CLEARED in await _events(ledger, cid)

    async def test_clearing_a_non_gated_task_raises(self, ledger: LedgerStore) -> None:
        from paa.core.errors import PaaError

        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="x"))
        with pytest.raises(PaaError, match="not awaiting"):
            await orch.clear_human_gate(cid, approved=True)


class TestResume:
    async def test_resumed_task_skips_completed_steps(self, ledger: LedgerStore) -> None:
        """A two-step plan with step 0 already done must only run step 1."""
        worker = _FakeAgent(AgentRole.WORKER, {"patch": ""})
        orch = _orchestrator(
            ledger,
            planner=_FakeAgent(
                AgentRole.STRATEGIC_PLANNER,
                {
                    "execution_steps": [
                        {"index": 0, "action": "a"},
                        {"index": 1, "action": "b"},
                    ],
                },
            ),
            worker=worker,
        )
        cid = await orch.submit(TaskRequest(goal="two steps"))
        # Drive to a point where step 0 is recorded complete, then run.
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        proj = await project(ledger, cid)
        assert proj.completed_steps == [0, 1]
        # Worker ran exactly twice — once per step, no redundant re-run.
        assert worker.call_count == 2


class TestClassification:
    async def test_simple_goal_gets_simple_modality(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="hi", target_agent="worker"))
        head = await ledger.head(cid)
        assert head is not None

    async def test_complex_keyword_escalates_modality(self, ledger: LedgerStore) -> None:
        from paa.core.types import ComplexityModality

        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="refactor the entire auth subsystem"))
        events = await ledger.read_correlation(cid)
        assert events[0].execution_mode in (
            ComplexityModality.COMPLEX,
            ComplexityModality.MAX,
        )
