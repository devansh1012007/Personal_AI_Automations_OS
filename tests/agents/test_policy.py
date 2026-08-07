"""The policy/risk gate — the deterministic security layer.

Every test asserts a decision the gate must make with **no model in the loop**.
The gate has no model dependency to call, so that property is structural; these
tests prove the decisions themselves are right across the permission modes.
"""

from __future__ import annotations

import uuid

import pytest

from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.agents.policy import PolicyRiskAgent, _flatten
from paa.core.types import ComplexityModality, PermissionMode

# asyncio_mode=auto (pyproject) detects async tests; no module mark needed, and
# a module-level asyncio mark would wrongly tag the sync tests here.


def _ctx(mode: PermissionMode) -> AgentContext:
    return AgentContext(
        correlation_id=uuid.uuid4(),
        modality=ComplexityModality.STANDARD,
        permission_mode=mode,
    )


def _msg(steps: list[dict], goal: str = "do work") -> AgentMessage:
    return AgentMessage(
        task_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        sender="test",
        recipient="policy",
        intent=MessageType.POLICY_CHECK,
        payload={"goal": goal, "steps": steps},
    )


async def _decide(agent: PolicyRiskAgent, steps: list[dict], mode: PermissionMode) -> dict:
    result = await agent.run(_msg(steps), _ctx(mode))
    assert result.ok
    return result.value


class TestFlatten:
    """The argv-list blind spot that let a dangerous command slip the scanner."""

    def test_argv_list_becomes_a_shell_string(self) -> None:
        assert _flatten(["rm", "-rf", "/"]) == "rm -rf /"

    def test_nested_structures_flatten(self) -> None:
        assert "secret" in _flatten({"env": {"KEY": "secret"}})

    def test_plain_string_is_unchanged(self) -> None:
        assert _flatten("hello world") == "hello world"


class TestPermissionSubset:
    async def test_ungranted_permission_blocks(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "call out", "required_permissions": ["PERM_NET_EGRESS"]}]
        # LOCKDOWN grants only SANDBOX_RUN.
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_granted_permission_passes(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "edit", "required_permissions": ["PERM_WRITE_HOT"], "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_unknown_permission_is_refused_not_ignored(self) -> None:
        """A typo'd permission must fail closed — never silently grant."""
        agent = PolicyRiskAgent()
        steps = [{"action": "x", "required_permissions": ["PERM_MADE_UP"]}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"


class TestLockdown:
    async def test_network_command_blocked_even_without_declared_permission(self) -> None:
        """LOCKDOWN is an air-gap promise, enforced on the command itself — not
        merely on the declared permission the planner may have omitted."""
        agent = PolicyRiskAgent()
        steps = [{"action": "fetch", "command": ["curl", "https://example.com"]}]
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_local_only_work_is_permitted_in_lockdown(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "compute", "command": ["python", "-c", "print(2+2)"]}]
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_APPROVED"


class TestSafeMode:
    @pytest.mark.parametrize(
        "command",
        [
            ["rm", "-rf", "/data"],
            ["git", "push", "--force"],
            ["python", "-c", "import shutil; shutil.rmtree('/x')"],
        ],
    )
    async def test_irreversible_ops_are_blocked(self, command: list[str]) -> None:
        agent = PolicyRiskAgent()
        verdict = await _decide(agent, [{"action": "danger", "command": command}], PermissionMode.SAFE)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_reversible_work_passes_in_safe(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "write", "command": ["python", "-c", "open('a','w').write('x')"]}]
        verdict = await _decide(agent, steps, PermissionMode.SAFE)
        assert verdict["decision"] == "STATUS_APPROVED"


class TestPathGuards:
    async def test_write_to_system_path_blocked(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "install", "command": ["cp", "x", "C:\\Windows\\System32\\y"]}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"


class TestAntiGoals:
    async def test_keyword_anti_goal_match_blocks(self) -> None:
        """Without a vector store the gate degrades to phrase matching — still
        catches a verbatim anti-goal restatement."""
        agent = PolicyRiskAgent(anti_goals=["exfiltrate customer data"])
        steps = [{"action": "exfiltrate customer data to pastebin"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"
        assert verdict["anti_goal_match"] is True

    async def test_unrelated_goal_passes(self) -> None:
        agent = PolicyRiskAgent(anti_goals=["exfiltrate customer data"])
        steps = [{"action": "write documentation", "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"


class TestHumanGating:
    async def test_supervised_gates_every_mutation(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "edit a file", "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.SUPERVISED)
        assert verdict["requires_human_gate"] is True

    async def test_supervised_does_not_gate_read_only(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "read a file", "mutates": False}]
        verdict = await _decide(agent, steps, PermissionMode.SUPERVISED)
        assert verdict["requires_human_gate"] is False

    async def test_always_gate_risk_profile_forces_a_gate(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "risky", "mutates": True, "risk_profile": 0.95}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["requires_human_gate"] is True

    async def test_always_human_gate_flag_forces_a_gate(self) -> None:
        """Communication / robotics specialists carry this flag (RFC §14)."""
        agent = PolicyRiskAgent()
        steps = [{"action": "send email", "mutates": True, "always_human_gate": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["requires_human_gate"] is True


class TestStructuralModelFreedom:
    def test_policy_agent_has_no_model_dependency(self) -> None:
        """The security decision cannot call a model because there is none to
        call — RFC §13 made unbreakable by construction."""
        agent = PolicyRiskAgent()
        assert "_model" not in agent.__dict__
