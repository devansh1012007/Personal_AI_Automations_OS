"""The pilot ``coding`` specialist. PLAN:integration-fix:P011, ADR-0041.

Pilot-first: exactly one specialist ships (``coding``). The other RFC §11.8
examples (research/writing/data-analysis/ops, plus five further deferred
examples) are gated behind this pilot's routing-precision gate and are
deliberately *not* built here — no stubs, no empty modules.

The specialist is a leaf: it is only ever reached through
``ChiefOrchestrator.mediate_delegation`` (the sole agent→agent path), and it
never delegates back out (``can_delegate = False``). The delegation graph's
depth/cycle/node guards therefore always see it as a terminal node.

**Tool surface — constrained JSON, not native tool calls.** The specialist
speaks the same narrow ``ModelLike`` seam as every reasoning agent, and its
turn schema is :func:`paa.coder.model_adapter.turn_schema` over
:data:`CODING_TOOL_NAMES`: tool names sit in a JSON-schema ``enum``, so the
model physically cannot emit a tool outside the offered surface. Since N1.5
the pilot offers the full catalogue the coder agent drives (runtime
ledger/memory tools plus the filesystem-read, filesystem-write, shell and
git work tools), so the enum can propose every step the tools dict can
execute. Its mutating half is nine names: the runtime triple
``MUTATING_RUNTIME_TOOLS`` (``submit_task`` / ``run_task`` /
``remember_fact``) plus the six work-tool mutating names (``write_file`` /
``edit_file`` / ``apply_patch`` / ``bash`` / ``git_stage`` / ``git_commit``),
the latter matching ``DEFAULT_MUTATING_TOOLS`` globs (including the
``git_stage*`` glob N1.5 added — that name matches nothing without it).
Offering the mutating names changes no execution path: the specialist
*proposes* tool calls for a later executor, it never runs them. A mutating
tool outside both sets would become read-only-by-accident;
:mod:`tests.agents.test_coding_specialist_p011` pins the coverage so a
future tool cannot slip in uncovered.

**What the pilot does not do.** The specialist's ``handle`` returns the
model's constrained turn (text + proposed tool calls); executing those calls
is a later task, gated by the same precision gate that gates the other four
specialists. What this task proves is routing, guards and the rollback emit
— the ADR's own cost list is one enum member, one registration, one package,
one emit site, and the 50-task routing test.
"""

from __future__ import annotations

from typing import Any

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.agents.reasoning import ModelLike
from paa.coder.model_adapter import turn_schema
from paa.core.types import AgentRole

__all__ = ["CODING_TOOL_NAMES", "CodingSpecialist"]

log = structlog.get_logger(__name__)

#: The pilot's offered tool surface. The read-only half is exactly
#: ``READ_ONLY_TOOLS`` in ``coder/runtime_tools.py`` (eleven names: the six
#: runtime reads plus the filesystem-read and git-read halves N1.5
#: aggregated there); the mutating half is ``MUTATING_RUNTIME_TOOLS`` in
#: ``coder/agent.py`` (the runtime triple) plus the six work-tool mutating
#: names, each matching a ``DEFAULT_MUTATING_TOOLS`` glob. Kept as a literal
#: tuple (not imported from either module) so the enum is fixed at import
#: time and the policy-coverage test reads exactly what the model can emit.
#: Deliberate pin growth at N1.5: if the specialist enum stayed narrower
#: than the agent's tools dict it could never propose a work-tool step, and
#: the constrained-JSON enum can only emit names that are in it.
CODING_TOOL_NAMES: tuple[str, ...] = (
    "check_runtime",
    "list_tasks",
    "task_status",
    "read_ledger",
    "verify_ledger",
    "recall_facts",
    "submit_task",
    "run_task",
    "remember_fact",
    "read_file",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "apply_patch",
    "bash",
    "git_status",
    "git_diff",
    "git_stage",
    "git_commit",
)


class CodingSpecialist(Agent):
    """A leaf coding specialist reached only through ``mediate_delegation``.

    ``model`` is optional: with no model the specialist still answers
    deterministically from the delegation payload (an empty packet — no
    ``goal`` key — is a valid input and returns an empty-goal result rather
    than raising). With a model, one constrained turn is taken and its
    ``{text, tool_calls[]}`` is returned verbatim for the caller to act on;
    ``ModelTurn.tokens`` is always 0 on this seam, so runs are bounded by
    callers with ``max_steps``, never ``max_tokens``.
    """

    role: AgentRole = AgentRole.CODING_SPECIALIST
    can_delegate: bool = False

    def __init__(
        self,
        *,
        name: str | None = None,
        model: ModelLike | None = None,
        ledger: Any = None,
        orchestrator: Any = None,
    ) -> None:
        super().__init__(name=name or "coding", ledger=ledger, orchestrator=orchestrator)
        self._model = model

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        goal = str(message.payload.get("goal", ""))
        text = f"coding specialist accepted delegation (goal={goal!r})"
        tool_calls: list[dict[str, Any]] = []
        if self._model is not None:
            raw = await self._model.complete_structured(
                f"coding delegation: {goal}", turn_schema(list(CODING_TOOL_NAMES))
            )
            text = str(raw.get("text", text))
            calls = raw.get("tool_calls", [])
            if isinstance(calls, list):
                tool_calls = [c for c in calls if isinstance(c, dict)]
        return AgentResult.success(
            {
                "agent": self.name,
                "role": self.role.value,
                "goal": goal,
                "text": text,
                "tool_calls": tool_calls,
            }
        )
