"""The policy/risk gate — the deterministic security layer.

Every test asserts a decision the gate must make with **no model in the loop**.
The gate has no model dependency to call, so that property is structural; these
tests prove the decisions themselves are right across the permission modes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.agents.policy import PolicyRiskAgent, _flatten
from paa.core.types import ComplexityModality, PermissionMode
from paa.storage.relational.connection import TransactionConnection
from paa.storage.relational.dialect import Dialect

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
        verdict = await _decide(
            agent, [{"action": "danger", "command": command}], PermissionMode.SAFE
        )
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


class _FakeRuleDB:
    """A relational backend that serves operator rules and nothing else.

    ``_load_rules`` reads ``hot_serving_policy_rules`` with a single
    ``fetch_all`` and turns each row into a plain dict, so one table's worth
    of rows is the whole surface this fake has to offer. The rest of the
    backend is present but inert, because the policy gate is read-only by
    construction: it never writes, never opens a transaction, and never asks
    for a dialect.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    @property
    def dialect(self) -> Dialect:
        raise NotImplementedError("the rule loader never asks for a dialect")

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        self.queries.append(sql)
        return list(self._rows)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any | None:
        return None

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise NotImplementedError("the policy gate never writes")

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        raise NotImplementedError("the policy gate never writes")

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        raise NotImplementedError("the policy gate never opens a transaction")

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def table_names(self) -> list[str]:
        return ["hot_serving_policy_rules"]


def _rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_name": "no-docs",
        "rule_kind": "regex",
        "pattern": "documentation",
        "severity": "block",
        "threshold": 0.0,
        "is_active": 1,
    }
    rule.update(overrides)
    return rule


class TestOperatorRules:
    """The operator-authored rule path (``hot_serving_policy_rules``).

    A matching pattern is only a block when the rule says so: the severity
    is part of the decision, not an afterthought. These tests pin that, and
    pin that a malformed operator rule is skipped rather than being allowed
    to take the gate down with it.
    """

    async def test_matching_block_rule_blocks_the_plan(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule()]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"
        assert "no-docs" in str(verdict["reason"])

    async def test_non_blocking_severity_does_not_block(self) -> None:
        """The pattern matched; the severity says warn. Nothing is blocked."""
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(severity="warn")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_inactive_rule_is_ignored(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(is_active=0)]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_non_regex_rule_kind_is_ignored(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(rule_kind="keyword")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_malformed_pattern_is_skipped_not_fatal(self) -> None:
        """A broken operator regex must not take the security gate down."""
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(pattern="[unclosed")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"
