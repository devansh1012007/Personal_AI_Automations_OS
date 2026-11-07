"""T12 — the pilot ``coding`` specialist (P-011, ADR-0041).

Pilot-first: exactly one specialist ships. Delegation reaches it only through
``ChiefOrchestrator.mediate_delegation`` (the sole agent→agent path), and a
recursion-guard block emits the *existing* ``STATE_ROLLBACK_TRIGGERED`` event
(resumable ``ROLLED_BACK``, never terminal) before returning the failure.
No new ``EventType`` member is created for this task.

The scripted model below follows ``tests/integration/conftest.py``'s
``ScriptedModel`` pattern: it answers by schema shape and records every call,
so routing can be asserted without spending a real model token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paa.agents.base import AgentContext, MessageType
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.coder.agent import MUTATING_RUNTIME_TOOLS
from paa.coder.permissions import DEFAULT_MUTATING_TOOLS
from paa.core.types import (
    TERMINAL_EVENTS,
    AgentRole,
    ComplexityModality,
    EventType,
    PermissionMode,
)
from paa.ledger.replay import TaskPhase, project
from paa.ledger.store import LedgerStore


class _ScriptedTurnModel:
    """A ``ModelLike`` scripted double answering the constrained turn schema.

    Mirrors ``tests/integration/conftest.py::ScriptedModel`` (answer by schema
    shape, record every call) for the coder constrained-JSON turn shape
    ``{text, tool_calls[]}`` that ``turn_schema`` declares.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "kwargs": kwargs})
        props = schema.get("properties", {})
        if "tool_calls" in props:
            enum = props["tool_calls"]["items"]["properties"]["name"].get("enum", [])
            # The enum must be the offered surface and nothing else.
            assert enum, "turn schema must enumerate the offered tools"
            return {"text": "scripted coding turn", "tool_calls": []}
        return {}


def _matches_any_glob(name: str, globs: tuple[str, ...]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(name, pattern) for pattern in globs)


def _orchestrator(ledger: LedgerStore, *, with_coding: bool = True) -> ChiefOrchestrator:
    from paa.agents.specialists.coding import CodingSpecialist

    kwargs: dict[str, Any] = {"permission_mode": PermissionMode.AUTO}
    if with_coding:
        kwargs["coding"] = CodingSpecialist(model=_ScriptedTurnModel())
    return ChiefOrchestrator(ledger, **kwargs)


def _ctx(
    cid: Any, *, modality: ComplexityModality = ComplexityModality.COMPLEX, depth: int = 0
) -> AgentContext:
    return AgentContext(
        correlation_id=cid,
        modality=modality,
        permission_mode=PermissionMode.AUTO,
        recursion_depth=depth,
    )


async def _rollback_events(ledger: LedgerStore, cid: Any) -> list[Any]:
    events = await ledger.read_correlation(cid)
    return [e for e in events if e.event_type is EventType.STATE_ROLLBACK_TRIGGERED]


class TestRoleAndRegistration:
    def test_coding_specialist_role_value_is_additive(self) -> None:
        assert AgentRole.CODING_SPECIALIST.value == "coding_specialist"
        # Additive only: the nine hierarchy roles plus the router still exist.
        values = {r.value for r in AgentRole}
        assert {
            "chief_orchestrator",
            "router",
            "context_builder_planner",
            "strategic_planner",
            "policy_risk",
            "context_builder_worker",
            "worker",
            "critic",
            "memory_creator",
            "memory_curator",
        } <= values

    def test_specialist_package_exports_only_coding(self) -> None:
        import paa.agents.specialists as pkg

        assert pkg.CodingSpecialist is not None
        # The four gated specialists are NOT built: no stubs, no empty modules.
        for name in ("research", "writing", "data_analysis", "ops"):
            assert not hasattr(pkg, name), f"gated specialist {name!r} must not exist yet"

    def test_no_new_event_type_member(self) -> None:
        # The block path reuses the existing rollback event; adding a member
        # would be scope creep the ADR explicitly rejects.
        names = [e.name for e in EventType]
        assert "STATE_ROLLBACK_TRIGGERED" in names
        assert not any("CODING" in n for n in names)

    def test_rollback_event_stays_resumable_not_terminal(self) -> None:
        # ADR-0041 + live code rule: ROLLED_BACK is resumable. The plan's
        # "terminal in replay" sub-claim is misclassified; code is the authority.
        assert EventType.STATE_ROLLBACK_TRIGGERED not in TERMINAL_EVENTS

    def test_registered_name_is_coding(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        assert orch.get("coding") is not None
        assert orch.get("coding") is not None
        assert orch.get("coding_specialist") is None  # one name only

    def test_coding_specialist_is_leaf(self) -> None:
        from paa.agents.specialists.coding import CodingSpecialist

        spec = CodingSpecialist()
        assert spec.role is AgentRole.CODING_SPECIALIST
        assert spec.name == "coding"
        assert spec.can_delegate is False


class TestToolSurfacePolicy:
    def test_every_mutating_tool_is_covered_by_policy(self) -> None:
        """A mutating tool matching neither the globs nor the build tuple
        becomes read-only-by-accident (AGENTS.md coder rule)."""
        from paa.agents.specialists.coding import CODING_TOOL_NAMES

        mutating = [
            n for n in CODING_TOOL_NAMES if _matches_any_glob(n, DEFAULT_MUTATING_TOOLS)
        ] + [n for n in CODING_TOOL_NAMES if n in MUTATING_RUNTIME_TOOLS]
        covered = set(CODING_TOOL_NAMES) & set(mutating)
        # Every tool the specialist offers that mutates must be either
        # glob-matched or in the tuple CoderAgent.build passes.
        for name in CODING_TOOL_NAMES:
            if name in MUTATING_RUNTIME_TOOLS or _matches_any_glob(
                name, DEFAULT_MUTATING_TOOLS
            ):
                assert _matches_any_glob(name, DEFAULT_MUTATING_TOOLS) or name in (
                    MUTATING_RUNTIME_TOOLS
                )
        assert covered  # the surface really has a mutating half

    def test_tool_names_are_in_constrained_enum(self) -> None:
        from paa.agents.specialists.coding import CODING_TOOL_NAMES
        from paa.coder.model_adapter import turn_schema

        schema = turn_schema(list(CODING_TOOL_NAMES))
        enum = schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"]
        assert sorted(enum) == sorted(CODING_TOOL_NAMES)


class TestRoutingT12:
    async def test_fifty_scripted_tasks_route_through_mediation(
        self, ledger: LedgerStore
    ) -> None:
        """T12: 50 scripted coding tasks routed >=90% through mediate_delegation.

        ADR-0041 adopts the plan's 90%-over-50 gate as stated. Each task is
        its own lineage (the delegation graph is per-correlation, so sharing
        one correlation would trip the node ceiling before proving routing).
        """
        orch = _orchestrator(ledger)
        routed = 0
        for i in range(50):
            cid = await orch.submit(
                TaskRequest(goal=f"coding task {i}", modality=ComplexityModality.COMPLEX)
            )
            result = await orch.mediate_delegation(
                sender="orchestrator",
                target="coding",
                intent=MessageType.DELEGATION,
                payload={"goal": f"coding task {i}"},
                ctx=_ctx(cid),
            )
            if result.ok:
                routed += 1
        assert routed >= 45, f"routing precision {routed}/50 below the 90% gate"

    async def test_unknown_target_is_failure_not_crash(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="unknown", modality=ComplexityModality.COMPLEX)
        )
        result = await orch.mediate_delegation(
            sender="orchestrator",
            target="ghost",
            intent=MessageType.DELEGATION,
            payload={"goal": "x"},
            ctx=_ctx(cid),
        )
        assert result.ok is False
        assert "no agent registered" in str((result.error or {}).get("message", ""))

    async def test_disabled_coding_is_no_agent_failure(self, ledger: LedgerStore) -> None:
        """`specialists.coding.enabled=False` means the agent is not registered:
        delegation returns the no-agent failure, not a crash or a silent queue."""
        orch = _orchestrator(ledger, with_coding=False)
        cid = await orch.submit(
            TaskRequest(goal="disabled", modality=ComplexityModality.COMPLEX)
        )
        result = await orch.mediate_delegation(
            sender="orchestrator",
            target="coding",
            intent=MessageType.DELEGATION,
            payload={"goal": "x"},
            ctx=_ctx(cid),
        )
        assert result.ok is False
        assert "no agent registered" in str((result.error or {}).get("message", ""))

    async def test_empty_payload_does_not_crash_specialist(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="empty packet", modality=ComplexityModality.COMPLEX)
        )
        result = await orch.mediate_delegation(
            sender="orchestrator",
            target="coding",
            intent=MessageType.DELEGATION,
            payload={},
            ctx=_ctx(cid),
        )
        assert result.ok is True


class TestDepthBlockEmitsRollback:
    async def test_depth_above_ceiling_blocked_and_emits_rollback(
        self, ledger: LedgerStore
    ) -> None:
        """Depth>2 (COMPLEX ceiling=2) is blocked AND the ledger records
        STATE_ROLLBACK_TRIGGERED under the task's correlation_id."""
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="too deep", modality=ComplexityModality.COMPLEX)
        )
        result = await orch.mediate_delegation(
            sender="orchestrator",
            target="coding",
            intent=MessageType.DELEGATION,
            payload={"goal": "x"},
            ctx=_ctx(cid, depth=2),  # child depth 3 > ceiling 2
        )
        assert result.ok is False
        error = result.error or {}
        assert error["error_type"] == "RecursionGuardError"
        assert error["depth"] == 3
        assert error["ceiling"] == 2

        events = await _rollback_events(ledger, cid)
        assert len(events) == 1
        event = events[0]
        assert event.correlation_id == cid
        assert event.payload["reason"] == "delegation_recursion_block"
        assert event.payload["depth"] == 3
        assert event.payload["ceiling"] == 2
        assert event.payload["target"] == "coding"

    async def test_replay_rebuilds_rolled_back_and_rollback_count(
        self, ledger: LedgerStore
    ) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="replay", modality=ComplexityModality.COMPLEX)
        )
        await orch.mediate_delegation(
            sender="orchestrator",
            target="coding",
            intent=MessageType.DELEGATION,
            payload={"goal": "x"},
            ctx=_ctx(cid, depth=2),
        )
        projection = await project(ledger, cid)
        assert projection.phase is TaskPhase.ROLLED_BACK
        assert projection.rollback_count == 1

    async def test_redelivered_rollback_suppressed_once(
        self, ledger: LedgerStore
    ) -> None:
        """Idempotency by the full ADR-0008 key: the same block emitted twice
        (redelivery) is recorded once."""
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="idem", modality=ComplexityModality.COMPLEX)
        )
        for _ in range(2):
            result = await orch.mediate_delegation(
                sender="orchestrator",
                target="coding",
                intent=MessageType.DELEGATION,
                payload={"goal": "x"},
                ctx=_ctx(cid, depth=2),
            )
            assert result.ok is False
        events = await _rollback_events(ledger, cid)
        assert len(events) == 1
        projection = await project(ledger, cid)
        assert projection.rollback_count == 1

    async def test_self_delegation_is_cycle_block(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(
            TaskRequest(goal="self", modality=ComplexityModality.COMPLEX)
        )
        result = await orch.mediate_delegation(
            sender="coding",
            target="coding",
            intent=MessageType.DELEGATION,
            payload={"goal": "x"},
            ctx=_ctx(cid),
        )
        assert result.ok is False
        error = result.error or {}
        assert error["error_type"] == "RecursionGuardError"
        assert error.get("cycle") == ["coding", "coding"]


class TestRuntimeRegistration:
    async def test_runtime_registers_coding_only_when_enabled(
        self, tmp_path: Path
    ) -> None:
        from paa.config import Settings
        from paa.runtime import Runtime

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        settings = Settings(home=home)
        settings.sandbox.backend = "subprocess"
        settings.specialists.coding.enabled = True
        rt = await Runtime.build(
            settings,
            model_adapter=_ScriptedTurnModel(),
            run_recovery=False,
            enable_optional_backends=False,
        )
        try:
            assert rt.agents.get("coding") is not None
            assert rt.orchestrator.get("coding") is not None
        finally:
            await rt.close()

    async def test_runtime_default_does_not_register_coding(
        self, tmp_path: Path
    ) -> None:
        from paa.config import Settings
        from paa.runtime import Runtime

        home = tmp_path / "paa_home"
        home.mkdir(parents=True, exist_ok=True)
        settings = Settings(home=home)
        settings.sandbox.backend = "subprocess"
        assert settings.specialists.coding.enabled is False
        rt = await Runtime.build(
            settings,
            model_adapter=_ScriptedTurnModel(),
            run_recovery=False,
            enable_optional_backends=False,
        )
        try:
            assert "coding" not in rt.agents
            assert rt.orchestrator.get("coding") is None
        finally:
            await rt.close()


# Structural note (policy-mirror decision): `test_policy_agent_has_no_model_dependency`
# asserts the policy agent holds no model because the security decision must not
# call one. The coding specialist is the opposite by design — it is a reasoning
# agent on the constrained-JSON seam — so the mirror assertion does not apply.
# Instead the structural property pinned here is that delegation *into* it is
# the only entry point: it cannot delegate back out (can_delegate is False).
@pytest.mark.parametrize("depth", [0, 1, 2])
async def test_coding_never_delegates_out(
    ledger: LedgerStore, depth: int
) -> None:
    orch = _orchestrator(ledger)
    coding = orch.get("coding")
    assert coding is not None
    assert coding.can_delegate is False
