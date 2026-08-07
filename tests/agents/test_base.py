"""Agent base class: the cross-cutting guarantees every agent inherits.

Budget accounting, the permission gate, and the child-context derivation live
here so no agent can skip them by forgetting boilerplate. These test that they
actually fire.
"""

from __future__ import annotations

import uuid

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult, MessageType, RiskLevel
from paa.core.errors import BudgetExceededError
from paa.core.types import AgentRole, ComplexityModality, Permission, PermissionMode


class _RecordingAgent(Agent):
    role = AgentRole.WORKER

    def __init__(self, *, tokens: int = 0, raises: Exception | None = None, **kw: object) -> None:
        super().__init__(**kw)
        self.ran = False
        self._tokens = tokens
        self._raises = raises

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult:
        self.ran = True
        if self._raises is not None:
            raise self._raises
        return AgentResult.success({"ok": True}, tokens_consumed=self._tokens)


def _msg() -> AgentMessage:
    return AgentMessage(
        task_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        sender="t",
        recipient="a",
        intent=MessageType.TASK_REQUEST,
    )


def _ctx(**kw: object) -> AgentContext:
    base = {
        "correlation_id": uuid.uuid4(),
        "modality": ComplexityModality.COMPLEX,
        "permission_mode": PermissionMode.AUTO,
    }
    base.update(kw)
    return AgentContext(**base)  # type: ignore[arg-type]


class TestContextBudget:
    def test_spend_tracks_remaining(self) -> None:
        ctx = _ctx(tokens_budget=1000)
        ctx.spend(300)
        assert ctx.tokens_remaining == 700

    def test_overspend_raises(self) -> None:
        ctx = _ctx(tokens_budget=100)
        with pytest.raises(BudgetExceededError):
            ctx.spend(200)

    def test_child_halves_budget_by_depth(self) -> None:
        ctx = _ctx()  # COMPLEX ceiling is 4096
        child = ctx.child()
        grandchild = child.child()
        assert child.recursion_depth == 1
        assert grandchild.recursion_depth == 2
        # RFC §15.7: ceiling >> depth.
        assert child.tokens_budget == 4096 >> 1
        assert grandchild.tokens_budget == 4096 >> 2

    def test_child_inherits_correlation_and_mode(self) -> None:
        ctx = _ctx()
        child = ctx.child()
        assert child.correlation_id == ctx.correlation_id
        assert child.permission_mode == ctx.permission_mode
        assert child.parent_task_id == ctx.task_id


class TestPermissionGate:
    async def test_missing_permission_blocks_before_handle(self) -> None:
        class _NeedsDelete(_RecordingAgent):
            required_permissions = (Permission.FILE_DELETE,)

        agent = _NeedsDelete()
        # SAFE does not grant FILE_DELETE.
        result = await agent.run(_msg(), _ctx(permission_mode=PermissionMode.SAFE))
        assert not result.ok
        assert agent.ran is False, "handle ran despite a missing permission"

    async def test_granted_permission_allows_handle(self) -> None:
        class _NeedsSandbox(_RecordingAgent):
            required_permissions = (Permission.SANDBOX_RUN,)

        agent = _NeedsSandbox()
        result = await agent.run(_msg(), _ctx(permission_mode=PermissionMode.AUTO))
        assert result.ok and agent.ran


class TestRunHardening:
    async def test_unexpected_error_becomes_a_failure_result(self) -> None:
        """A bare exception must become a structured result — the orchestrator
        records outcomes in the ledger and cannot ledger a traceback."""
        agent = _RecordingAgent(raises=RuntimeError("boom"))
        result = await agent.run(_msg(), _ctx())
        assert not result.ok
        assert "boom" in str(result.error)

    async def test_budget_is_charged_from_result(self) -> None:
        agent = _RecordingAgent(tokens=500)
        ctx = _ctx(tokens_budget=1000)
        await agent.run(_msg(), ctx)
        assert ctx.tokens_spent == 500

    async def test_latency_is_recorded(self) -> None:
        agent = _RecordingAgent()
        result = await agent.run(_msg(), _ctx())
        assert result.latency_ms >= 0.0


class TestRiskLevel:
    def test_from_score_maps_to_bands(self) -> None:
        assert RiskLevel.from_score(0.0) is RiskLevel.NONE
        assert RiskLevel.from_score(0.3) is RiskLevel.LOW
        assert RiskLevel.from_score(0.6) is RiskLevel.MEDIUM
        assert RiskLevel.from_score(0.8) is RiskLevel.HIGH
        assert RiskLevel.from_score(1.0) is RiskLevel.CRITICAL

    def test_score_round_trips_through_bands(self) -> None:
        for level in RiskLevel:
            assert RiskLevel.from_score(level.score).score <= level.score + 1e-9


class TestMessageReply:
    def test_reply_preserves_lineage(self) -> None:
        original = AgentMessage(
            task_id=uuid.uuid4(),
            correlation_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            sender="planner",
            recipient="critic",
            intent=MessageType.REVIEW_RESULT,
        )
        reply = original.reply(MessageType.COMPLETION, {"done": True})
        assert reply.correlation_id == original.correlation_id
        assert reply.session_id == original.session_id
        assert reply.sender == original.recipient
        assert reply.recipient == original.sender
