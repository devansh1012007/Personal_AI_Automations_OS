"""The P1.1 freeze: capability vocabulary, grant computation, the two protocols.

What these tests pin is the contract ADR-0051 fixes, not an implementation:
the closed capability vocabulary, the intersection semantics of grants
(tighten-only), the run-scoped token refusal, the error hierarchy the wire
table renders, and the facade surface a pipeline can actually call. The
``_FakeFacade`` here implements every method of the frozen protocol, so a
method added to :class:`paa.pipelines.base.CapabilityFacade` without
updating this file fails the structural-consumption tests — the freeze is
enforced by a consumer, not by hope.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from paa.agents.base import AgentContext, AgentMessage, AgentResult, MessageType
from paa.core.errors import PaaError
from paa.core.types import AgentRole, ComplexityModality, Permission, PermissionMode
from paa.ledger.replay import TaskPhase, TaskProjection
from paa.pipelines.base import (
    FACADE_RUNTIME_VERSION,
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    GateQuestion,
    GateRejectedError,
    OutcomeUnknownError,
    Pipeline,
    PipelineCancelledError,
    PipelineDivergedError,
    PipelineError,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
    compute_grants,
)
from paa.pipelines.base import CapabilityFacade as _FacadeProtocol

MANIFEST_REQUESTABLE = frozenset(
    {
        Capability.THINK,
        Capability.USE_SKILL,
        Capability.RECALL,
        Capability.REMEMBER,
        Capability.CALL_AGENT,
        Capability.SUB_RUN,
        Capability.COMMIT,
    }
)


class TestCapabilityVocabulary:
    def test_the_vocabulary_is_closed_and_named_for_the_facade(self) -> None:
        assert {c.value for c in Capability} == {
            "think",
            "call_agent",
            "use_skill",
            "recall",
            "remember",
            "gate",
            "plan_submit",
            "policy_check",
            "commit",
            "sub_run",
        }

    def test_the_manifest_requestable_subset_matches_adr_0052(self) -> None:
        # The seven keys [capabilities] may carry (ADR-0052); gate,
        # plan_submit and policy_check are host-side operations, not asks.
        assert set(Capability) >= MANIFEST_REQUESTABLE

    def test_the_determinism_aids_are_not_gated_capabilities(self) -> None:
        # now/random/progress are always served: gating them would push a
        # compliant pipeline back to reading the clock directly (ADR-0050).
        assert not {"now", "random", "progress"} <= {c.value for c in Capability}


class TestGrantComputation:
    def test_a_grant_is_the_request_intersected_with_every_constraint(self) -> None:
        granted = compute_grants(
            (Capability.THINK, Capability.USE_SKILL, Capability.COMMIT),
            (Capability.THINK, Capability.COMMIT, Capability.RECALL),
            (Capability.COMMIT, Capability.THINK),
        )
        assert granted == frozenset({Capability.THINK, Capability.COMMIT})

    def test_none_means_that_source_imposes_no_constraint(self) -> None:
        requested = frozenset({Capability.THINK, Capability.COMMIT})
        assert compute_grants(requested, None, None) == requested

    def test_a_grant_can_only_shrink_the_request(self) -> None:
        requested = frozenset(Capability)
        granted = compute_grants(requested, frozenset({Capability.COMMIT}))
        assert granted <= requested

    def test_an_empty_constraint_grants_nothing(self) -> None:
        assert compute_grants(frozenset(Capability), frozenset()) == frozenset()


class TestCapabilitySet:
    def test_a_token_exists_for_every_granted_capability_only(self) -> None:
        grants = CapabilitySet.mint((Capability.THINK, Capability.COMMIT))
        assert Capability.THINK in grants
        assert Capability.USE_SKILL not in grants
        assert grants.token_for(Capability.USE_SKILL) is None

    def test_an_ungranted_call_is_refused_with_the_capability_named(self) -> None:
        grants = CapabilitySet.mint((Capability.THINK,))
        with pytest.raises(CapabilityNotGrantedError) as raised:
            grants.require(Capability.COMMIT)
        assert raised.value.capability == "commit"
        assert raised.value.details["granted"] == ["think"]

    def test_tokens_are_run_scoped(self) -> None:
        run_a = CapabilitySet.mint((Capability.THINK,))
        run_b = CapabilitySet.mint((Capability.THINK,))
        assert not run_a.verify(run_b.token_for(Capability.THINK) or _bad_token())

    def test_mints_are_distinct_for_the_same_run_and_capability(self) -> None:
        one = CapabilitySet.mint((Capability.THINK,), run_id=uuid.uuid4())
        two = CapabilitySet.mint((Capability.THINK,), run_id=one.run_id)
        token_one = one.token_for(Capability.THINK)
        token_two = two.token_for(Capability.THINK)
        assert token_one is not None and token_two is not None
        assert token_one.nonce != token_two.nonce


def _bad_token() -> Any:  # pragma: no cover - the assertion above never runs on success
    raise AssertionError("token_for must not return None for a granted capability")


class TestErrorVocabulary:
    @pytest.mark.parametrize(
        "exc",
        [
            CapabilityNotGrantedError("commit"),
            PipelineDivergedError(seq=3, expected_fingerprint="a", actual_fingerprint="b"),
            PipelineCancelledError("cancelled"),
            OutcomeUnknownError("outcome unknown", seq=2, tool="bash"),
        ],
    )
    def test_every_pipeline_error_is_a_paa_error(self, exc: PipelineError) -> None:
        assert isinstance(exc, PaaError)
        assert exc.to_payload()["error_type"] == type(exc).__name__

    def test_the_wire_codes_are_distinct_and_match_the_table(self) -> None:
        codes = {
            GateRejectedError,
            PipelineDivergedError,
            PipelineCancelledError,
            CapabilityNotGrantedError,
            OutcomeUnknownError,
        }
        assert {c.wire_code for c in codes} == {-32003, -32004, -32005, -32006, -32008}

    def test_the_divergence_refusal_carries_its_evidence(self) -> None:
        exc = PipelineDivergedError(
            seq=7, expected_fingerprint="deadbeef", actual_fingerprint="cafe"
        )
        assert exc.details["seq"] == 7
        assert exc.details["expected_fingerprint"] == "deadbeef"
        assert exc.details["actual_fingerprint"] == "cafe"


class TestRunShapes:
    def test_a_result_defaults_to_empty_metrics_and_artifacts(self) -> None:
        result = PipelineResult(status=PipelineStatus.COMPLETED, summary="done")
        assert result.metrics == {}
        assert result.artifacts == []

    def test_a_task_carries_its_in_process_projection(self) -> None:
        task = PipelineTask(
            correlation_id=uuid.uuid4(),
            session_id=None,
            goal="fix the login flow",
        )
        assert task.projection is None
        assert task.modality is ComplexityModality.STANDARD
        assert task.permission_mode is PermissionMode.ASK

    def test_limits_default_to_unbounded(self) -> None:
        limits = RunLimits()
        assert limits.max_tokens is None
        assert limits.max_calls is None

    def test_the_facade_has_a_version_pipelines_declare_against(self) -> None:
        assert FACADE_RUNTIME_VERSION == "4.1"


# ---------------------------------------------------------------------------
# Structural satisfaction: the freeze enforced by consumers
# ---------------------------------------------------------------------------


class _FakePipeline:
    """A minimal pipeline, shaped exactly like the frozen protocol."""

    name = "fake.echo"
    version = "0.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions = frozenset({Permission.SANDBOX_RUN})
    capabilities = frozenset({Capability.THINK, Capability.COMMIT})

    async def run(self, task: PipelineTask, ctx: _FacadeProtocol) -> PipelineResult:
        await ctx.progress("thinking")
        await ctx.think("what next?")
        return PipelineResult(status=PipelineStatus.COMPLETED, summary=task.goal)

class _FakeFacade:
    """Every method of the frozen facade, minimally implemented.

    Updating the protocol without updating this class fails the structural
    tests below — this file is the freeze's tripwire, in the same spirit as
    the ToolJournal protocol pins in tests/coder/test_journal.py.
    """

    def __init__(self) -> None:
        self.ctx = AgentContext(
            correlation_id=uuid.uuid4(), permission_mode=PermissionMode.AUTO
        )

    @property
    def correlation_id(self) -> uuid.UUID:
        return self.ctx.correlation_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return None

    @property
    def modality(self) -> ComplexityModality:
        return ComplexityModality.STANDARD

    @property
    def permission_mode(self) -> PermissionMode:
        return PermissionMode.AUTO

    @property
    def tokens_budget(self) -> int:
        return 1_000

    async def think(self, prompt: str, **_: Any) -> dict[str, Any]:
        return {"thought": prompt}

    async def call_agent(self, role: AgentRole, message: AgentMessage, *_: Any) -> AgentResult[Any]:
        return AgentResult.success(value={"role": role.value})

    async def use_skill(self, name: str, args: Any) -> str:
        return f"{name}({args})"

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        return [{"query": query, "k": k}]

    async def remember(self, fact: Any) -> None:
        return None

    async def gate(self, role: AgentRole, payload: Any) -> TaskProjection:
        return TaskProjection(correlation_id=str(uuid.uuid4()), phase=TaskPhase.AWAITING_HUMAN)

    async def submit_plan(self, steps: Any, **_: Any) -> None:
        return None

    async def check_policy(self, state: TaskProjection) -> TaskProjection:
        return state

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        return None

    async def sub_run(self, name: str, task_input: Any) -> PipelineResult:
        return PipelineResult(status=PipelineStatus.COMPLETED, summary=name)

    async def now(self) -> Any:
        from datetime import UTC, datetime

        return datetime.now(UTC)

    async def random(self) -> float:
        return 0.5

    async def progress(self, status: str) -> None:
        return None

    @property
    def agent_ctx(self) -> AgentContext:
        return self.ctx

    async def stage_question(self, role: AgentRole) -> GateQuestion | None:
        return None

    async def project(self) -> TaskProjection:
        return TaskProjection(correlation_id=str(self.ctx.correlation_id))


class TestStructuralSatisfaction:
    async def test_a_minimal_pipeline_satisfies_and_runs_the_protocol(self) -> None:
        pipeline: Pipeline = _FakePipeline()
        facade: _FacadeProtocol = _FakeFacade()
        task = PipelineTask(correlation_id=uuid.uuid4(), session_id=None, goal="hello")
        result = await pipeline.run(task, facade)
        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "hello"

    async def test_every_frozen_facade_method_is_callable_through_the_protocol(self) -> None:
        facade: _FacadeProtocol = _FakeFacade()
        assert await facade.think("what next?") == {"thought": "what next?"}
        message = AgentMessage(
            task_id=uuid.uuid4(),
            correlation_id=facade.correlation_id,
            sender="pipeline",
            recipient="worker",
            intent=MessageType.EXECUTION_REQUEST,
        )
        review = await facade.call_agent(AgentRole.CRITIC, message, facade.agent_ctx)
        assert review.ok and review.value == {"role": "critic"}
        assert await facade.use_skill("read_file", {"path": "x"}) == "read_file({'path': 'x'})"
        assert await facade.recall("login", k=3) == [{"query": "login", "k": 3}]
        assert await facade.remember({"fact": "one"}) is None
        parked = await facade.gate(AgentRole.POLICY_RISK, {"reason": "risky"})
        assert parked.phase is TaskPhase.AWAITING_HUMAN
        assert await facade.submit_plan([{"index": 0, "action": "go"}]) is None
        assert await facade.check_policy(parked) is parked
        assert await facade.commit(steps_completed=1, tokens_consumed=0) is None
        child = await facade.sub_run("builtin.plan-execute-critique", {})
        assert child.status is PipelineStatus.COMPLETED
        assert await facade.now() is not None
        assert 0.0 <= await facade.random() <= 1.0
        assert await facade.progress("still working") is None
        assert await facade.stage_question(AgentRole.POLICY_RISK) is None
        assert (await facade.project()).correlation_id == str(facade.correlation_id)
