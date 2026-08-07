"""Planner, critic, router — the model-using agents and their fallbacks.

Two properties here are load-bearing for safety and were regressions caught by
the integration suite: the planner must preserve a step's ``command`` through
normalisation (or the policy gate scans nothing), and the critic's deterministic
verdict must be un-overridable by the model.
"""

from __future__ import annotations

import uuid
from typing import Any

from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.agents.reasoning import CriticReviewer, StrategicPlanner, TaskRouter, WorkerCell
from paa.core.types import ComplexityModality, Permission, PermissionMode


def _ctx(modality: ComplexityModality = ComplexityModality.COMPLEX) -> AgentContext:
    return AgentContext(
        correlation_id=uuid.uuid4(),
        modality=modality,
        permission_mode=PermissionMode.AUTO,
        tokens_budget=modality_ceiling(modality),
    )


def modality_ceiling(m: ComplexityModality) -> int:
    from paa.core.types import MODALITY_PROFILES

    return MODALITY_PROFILES[m].token_ceiling


def _msg(intent: MessageType, payload: dict[str, Any]) -> AgentMessage:
    return AgentMessage(
        task_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        sender="test",
        recipient="agent",
        intent=intent,
        payload=payload,
    )


class ScriptedModel:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls = 0

    async def complete_structured(self, prompt: str, schema: dict, **kw: Any) -> dict:
        self.calls += 1
        return self.response


class TestPlannerNormalisation:
    """The command-preservation fix: unknown execution keys must survive."""

    async def test_command_field_is_preserved(self) -> None:
        model = ScriptedModel(
            {
                "execution_steps": [
                    {"index": 0, "action": "run", "command": ["rm", "-rf", "/"], "mutates": True}
                ],
                "step_requirements": {},
            }
        )
        planner = StrategicPlanner(model=model)
        result = await planner.run(_msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx())

        assert result.ok
        step = result.value["execution_steps"][0]
        assert step["command"] == ["rm", "-rf", "/"], "command was stripped in normalisation"

    async def test_required_permissions_survive_normalisation(self) -> None:
        model = ScriptedModel(
            {
                "execution_steps": [
                    {
                        "index": 0,
                        "action": "call api",
                        "required_permissions": ["PERM_NET_EGRESS"],
                    }
                ]
            }
        )
        planner = StrategicPlanner(model=model)
        result = await planner.run(_msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx())
        assert result.value["execution_steps"][0]["required_permissions"] == ["PERM_NET_EGRESS"]

    async def test_known_fields_are_coerced(self) -> None:
        model = ScriptedModel(
            {"execution_steps": [{"index": "0", "action": "go", "mutates": 1, "risk_profile": "0.5"}]}
        )
        planner = StrategicPlanner(model=model)
        step = (await planner.run(_msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx())).value[
            "execution_steps"
        ][0]
        assert step["index"] == 0 and step["mutates"] is True and step["risk_profile"] == 0.5


class TestPlannerBounds:
    async def test_plan_exceeding_node_ceiling_fails(self) -> None:
        # COMPLEX allows max_plan_nodes() == 7; return 20 steps.
        steps = [{"index": i, "action": f"s{i}"} for i in range(20)]
        planner = StrategicPlanner(model=ScriptedModel({"execution_steps": steps}))
        result = await planner.run(_msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx())
        assert not result.ok

    async def test_empty_plan_fails(self) -> None:
        planner = StrategicPlanner(model=ScriptedModel({"execution_steps": []}))
        result = await planner.run(_msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx())
        assert not result.ok

    async def test_simple_modality_is_refused(self) -> None:
        """SIMPLE bypasses the planner entirely; running it there is a bug."""
        planner = StrategicPlanner(model=ScriptedModel({"execution_steps": [{"action": "a"}]}))
        result = await planner.run(
            _msg(MessageType.PLAN_PROPOSAL, {"goal": "x"}), _ctx(ComplexityModality.SIMPLE)
        )
        assert not result.ok

    async def test_no_model_falls_back_deterministically(self) -> None:
        planner = StrategicPlanner(model=None)
        result = await planner.run(
            _msg(MessageType.PLAN_PROPOSAL, {"goal": "do A then do B"}), _ctx()
        )
        assert result.ok
        assert len(result.value["execution_steps"]) == 2  # split on "then"
        assert result.confidence < 0.5  # low confidence flags the weak fallback


class TestCriticOverride:
    """The deterministic verdict is authoritative and can only downgrade."""

    async def test_deterministic_fail_beats_model_pass(self) -> None:
        class FailValidator:
            async def validate(self, output: dict) -> Any:
                class R:
                    passed = False
                    findings = [{"rule": "ast", "message": "os.system"}]

                return R()

        critic = CriticReviewer(model=ScriptedModel({"verdict": "PASS"}), validation_engine=FailValidator())
        result = await critic.run(
            _msg(MessageType.REVIEW_RESULT, {"output": {"source_files": {"x.py": "bad"}}}), _ctx()
        )
        assert result.value["verdict"] == "FAIL_REJECT_RETRY"
        assert result.value["source"] == "deterministic"

    async def test_model_is_not_even_consulted_on_deterministic_fail(self) -> None:
        class FailValidator:
            async def validate(self, output: dict) -> Any:
                class R:
                    passed = False
                    findings: list = []

                return R()

        model = ScriptedModel({"verdict": "PASS"})
        critic = CriticReviewer(model=model, validation_engine=FailValidator())
        await critic.run(_msg(MessageType.REVIEW_RESULT, {"output": {}}), _ctx())
        assert model.calls == 0, "model was consulted despite a deterministic failure"

    async def test_pass_when_deterministic_ok_and_no_model(self) -> None:
        critic = CriticReviewer(model=None, validation_engine=None)
        result = await critic.run(_msg(MessageType.REVIEW_RESULT, {"output": {}}), _ctx())
        assert result.value["verdict"] == "PASS"

    async def test_unusable_validator_fails_closed(self) -> None:
        class Broken:
            async def validate(self, output: dict) -> Any:
                raise RuntimeError("validator crashed")

        critic = CriticReviewer(model=None, validation_engine=Broken())
        result = await critic.run(_msg(MessageType.REVIEW_RESULT, {"output": {}}), _ctx())
        assert result.value["verdict"] == "FAIL_REJECT_RETRY"


class TestRouterOptionality:
    """The router is optional by design (ADR-0011)."""

    def test_named_target_bypasses_routing(self) -> None:
        router = TaskRouter(model=None, min_agents=3)
        assert router.should_route(target_agent="coding_agent", eligible_agents=10) is False

    def test_too_few_agents_bypasses_routing(self) -> None:
        router = TaskRouter(model=None, min_agents=3)
        assert router.should_route(target_agent=None, eligible_agents=2) is False

    def test_many_agents_and_no_target_enables_routing(self) -> None:
        router = TaskRouter(model=None, min_agents=3)
        assert router.should_route(target_agent=None, eligible_agents=5) is True

    def test_decompose_splits_a_compound_goal(self) -> None:
        parts = TaskRouter.decompose_deterministic("first do X then do Y and then Z")
        assert len(parts) == 3


class TestWorkerPermissions:
    async def test_worker_requires_sandbox_run_permission(self) -> None:
        worker = WorkerCell(sandbox=None)
        assert Permission.SANDBOX_RUN in worker.required_permissions

    async def test_worker_without_sandbox_returns_dry_run(self) -> None:
        worker = WorkerCell(sandbox=None)
        result = await worker.run(
            _msg(MessageType.EXECUTION_REQUEST, {"step": {"action": "x"}, "index": 0}), _ctx()
        )
        assert result.ok
        assert result.value["dry_run"] is True
