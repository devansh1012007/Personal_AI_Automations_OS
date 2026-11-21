"""The P1.2 extraction pins: the built-in pipeline, its facade's refusals,
and P1.5's slot swap.

What these tests pin that tests/agents/test_orchestrator.py does not:

* the orchestrator's strategy really is the
  :class:`~paa.pipelines.builtin.BuiltinPlanExecuteCritique` pipeline, structurally
  satisfying the frozen :class:`~paa.pipelines.base.Pipeline` protocol;
* the facade the host mints refuses the capabilities the built-in does not
  ask for (ADR-0051's only door), rather than erroring some other way;
* the failure path still records the pre-run ``attempts`` count the old
  inline code read when a stage raised — the subtlety the extraction
  preserves through the per-run task's projection writes;
* P1.5's exit criterion: swapping a stage slot (the critic) changes
  behaviour, with no code change — the layer-2 overlay, proven before the
  overlay loader exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult, MessageType
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.core.types import AgentRole, EventType, PermissionMode, new_correlation_id
from paa.ledger.replay import TaskPhase
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    Pipeline,
)
from paa.pipelines.builtin import (
    BuiltinPipelineFacade,
    BuiltinPlanExecuteCritique,
    StageAgents,
)


class _FakeAgent(Agent):
    """An agent that returns a canned value (mirrors the orchestrator suite's)."""

    def __init__(self, role: AgentRole, value: Any) -> None:
        super().__init__(name=role.value)
        self.role = role
        self._value = value

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        return AgentResult.success(self._value)


def _orchestrator(ledger: LedgerStore, **agent_overrides: Any) -> ChiefOrchestrator:
    agents: dict[str, Agent] = {
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
    agents.update(agent_overrides)
    return ChiefOrchestrator(
        ledger,
        context_builder=agents.get("context_builder"),
        planner=agents.get("planner"),
        policy=agents.get("policy"),
        worker=agents.get("worker"),
        critic=agents.get("critic"),
        permission_mode=PermissionMode.AUTO,
    )


class TestTheExtraction:
    async def test_the_orchestrators_strategy_is_the_builtin_pipeline(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        pipeline: Pipeline = orch._pipeline
        assert isinstance(pipeline, BuiltinPlanExecuteCritique)
        assert pipeline.name == "builtin.plan-execute-critique"
        assert pipeline.version == "4.1.0"
        assert pipeline.requires_runtime == ">=4.1,<5"
        assert pipeline.required_permissions == frozenset()
        assert pipeline.capabilities == frozenset(
            {
                Capability.CALL_AGENT,
                Capability.GATE,
                Capability.PLAN_SUBMIT,
                Capability.POLICY_CHECK,
                Capability.COMMIT,
            }
        )

    async def test_the_builtin_pipeline_drives_a_task_to_committed(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="do a thing"))
        outcome = await orch.run(cid)
        assert outcome.phase is TaskPhase.COMMITTED
        events = [e.event_type for e in await ledger.read_correlation(cid)]
        assert events == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.CRITIQUE_CONCLUDED,
            EventType.EXECUTION_COMPLETED,
            EventType.MUTATION_COMMITTED,
        ]

    async def test_the_failure_path_records_the_pre_run_attempt_count(
        self, ledger: LedgerStore
    ) -> None:
        """The stale-``attempts`` subtlety the extraction preserves.

        The old inline ``run()`` read its local ``state.attempts`` — the
        projection as of the last *completed* stage, not a fresh re-projection
        — so a step exhausting its retry budget fails with the attempt count
        from before any ``EXECUTION_STARTED`` event landed. The pipeline
        writes its live projection onto the per-run task at exactly the old
        assignment points, so the host reads the same number.
        """
        orch = _orchestrator(
            ledger,
            critic=_FakeAgent(AgentRole.CRITIC, {"verdict": "FAIL_REJECT_RETRY", "findings": []}),
        )
        cid = await orch.submit(TaskRequest(goal="critic says no"))
        outcome = await orch.run(cid)
        assert outcome.phase is TaskPhase.FAILED
        events = await ledger.read_correlation(cid)
        failed = [e for e in events if e.event_type is EventType.EXECUTION_FAILED]
        assert len(failed) == 1
        assert failed[0].attempt == 0


class TestTheFacade:
    async def test_the_host_mints_the_builtin_facade(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        ctx = AgentContext(
            correlation_id=new_correlation_id(), permission_mode=PermissionMode.AUTO
        )
        facade = orch._facade(ctx)
        assert isinstance(facade, BuiltinPipelineFacade)
        assert facade.correlation_id == ctx.correlation_id
        assert facade.agent_ctx is ctx

    async def test_ungranted_capabilities_are_refused_not_broken(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        ctx = AgentContext(
            correlation_id=new_correlation_id(), permission_mode=PermissionMode.AUTO
        )
        facade = orch._facade(ctx)
        with pytest.raises(CapabilityNotGrantedError) as raised:
            await facade.think("what next?")
        assert raised.value.capability == "think"
        with pytest.raises(CapabilityNotGrantedError):
            await facade.use_skill("read_file", {"path": "x"})
        with pytest.raises(CapabilityNotGrantedError):
            await facade.sub_run("coder.tool-loop", {})
        # The granted door still opens: the policy stage agent answers.
        message = AgentMessage(
            task_id=new_correlation_id(),
            correlation_id=facade.correlation_id,
            sender="pipeline",
            recipient="policy_risk",
            intent=MessageType.POLICY_CHECK,
        )
        result = await facade.call_agent(AgentRole.POLICY_RISK, message, facade.agent_ctx)
        assert result.ok and result.value == {
            "decision": "STATUS_APPROVED",
            "requires_human_gate": False,
        }

    async def test_a_runs_random_sequence_is_seeded_by_its_lineage(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        ctx = AgentContext(
            correlation_id=new_correlation_id(), permission_mode=PermissionMode.AUTO
        )
        facade = orch._facade(ctx)
        first = [await facade.random() for _ in range(4)]
        assert len(set(first)) == 4  # a sequence, not one repeated value
        # The seed is the correlation id, so a facade minted for the same
        # lineage replays the same sequence (ADR-0050's determinism aid).
        replay_facade = orch._facade(ctx)
        replayed = [await replay_facade.random() for _ in range(4)]
        assert replayed == first


class TestSlotSwap:
    async def test_swapping_the_critic_slot_changes_behaviour(self, ledger: LedgerStore) -> None:
        """P1.5's exit criterion: a stage slot swap is a behaviour change.

        A strict critic fails every step; swapping the ``critic`` slot for
        ``None`` (the built-in's "skip critique" stance) walks the very same
        plan to ``MUTATION_COMMITTED`` with no ``CRITIQUE_CONCLUDED`` event —
        a layer-2 overlay in miniature, before the overlay loader exists.
        """
        orch = _orchestrator(
            ledger,
            critic=_FakeAgent(AgentRole.CRITIC, {"verdict": "FAIL_REJECT_RETRY", "findings": []}),
        )
        first = await orch.submit(TaskRequest(goal="strict critic"))
        outcome = await orch.run(first)
        assert outcome.phase is TaskPhase.FAILED

        orch._pipeline.stages.critic = None
        second = await orch.submit(TaskRequest(goal="no critic"))
        outcome = await orch.run(second)
        assert outcome.phase is TaskPhase.COMMITTED
        events = [e.event_type for e in await ledger.read_correlation(second)]
        assert EventType.CRITIQUE_CONCLUDED not in events
        assert EventType.MUTATION_COMMITTED in events

        # The same slot also *replaces*: a PASS critic on the orchestrator
        # whose strict critic just failed the first task walks it to commit —
        # the overlay reading of P1.5, not just the disable reading.
        orch._pipeline.stages.critic = _FakeAgent(
            AgentRole.CRITIC, {"verdict": "PASS", "findings": []}
        )
        third = await orch.submit(TaskRequest(goal="a better critic"))
        outcome = await orch.run(third)
        assert outcome.phase is TaskPhase.COMMITTED
        events = [e.event_type for e in await ledger.read_correlation(third)]
        assert EventType.CRITIQUE_CONCLUDED in events


class TestStageAgents:
    def test_every_slot_is_none_by_default(self) -> None:
        stages = StageAgents()
        assert stages.context_builder is None
        assert stages.planner is None
        assert stages.policy is None
        assert stages.worker is None
        assert stages.critic is None
