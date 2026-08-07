"""Agent base classes and the structured inter-agent message protocol.

RFC §13 specifies that agents exchange typed messages rather than prose. This
module defines that envelope and the base class every agent implements.

Resolving the RFC's internal contradiction on peer calls
--------------------------------------------------------

RFC §2 states that "direct agent-to-agent peer calls are hardcoded out of the
runtime layers" and mandates a strict hub-and-spoke topology, justified by
deadlock avoidance: if agents never block on peer callbacks, cyclic waiting
chains cannot form.

The project brief asks for the opposite — "add multi agent and agent calling
and interacting abilities", with specialists able to extend a cycle mid-run.

Both goals are satisfiable, because the RFC's justification is about *blocking*
call graphs, not about interaction per se. The deadlock risk comes from
synchronous mutual waiting, not from one agent causing another to run. So:

* Agents may **delegate** to one another (:meth:`Agent.delegate`).
* Delegation is **mediated** by the orchestrator, which owns the delegation
  graph, enforces the depth ceiling, and refuses any edge that would close a
  cycle (RFC §11.1's cycle-detection invariant, now actually load-bearing).
* Delegation is **non-blocking at the topology level**: a parent awaits a
  child's result, but the guard guarantees the wait graph is a DAG, so no
  cycle of waiters can exist.

SPEC DEVIATION (docs/adr/0018). The net effect is real multi-agent interaction
with the RFC's safety property preserved by construction rather than by
prohibition.
"""

from __future__ import annotations

import abc
import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.core.errors import BudgetExceededError, PaaError
from paa.core.types import (
    MODALITY_PROFILES,
    AgentRole,
    ComplexityModality,
    ModalityProfile,
    Permission,
    PermissionMode,
)

if TYPE_CHECKING:
    from paa.ledger.store import LedgerStore

__all__ = [
    "Agent",
    "AgentContext",
    "AgentMessage",
    "AgentResult",
    "MessageType",
    "RiskLevel",
]

log = structlog.get_logger(__name__)


class MessageType(str, enum.Enum):
    """RFC §13 message vocabulary."""

    TASK_REQUEST = "TASK_REQUEST"
    CONTEXT_REQUEST = "CONTEXT_REQUEST"
    CONTEXT_RESPONSE = "CONTEXT_RESPONSE"
    PLAN_PROPOSAL = "PLAN_PROPOSAL"
    POLICY_CHECK = "POLICY_CHECK"
    EXECUTION_REQUEST = "EXECUTION_REQUEST"
    EXECUTION_RESULT = "EXECUTION_RESULT"
    REVIEW_RESULT = "REVIEW_RESULT"
    MEMORY_UPDATE = "MEMORY_UPDATE"
    ESCALATION = "ESCALATION"
    REFUSAL = "REFUSAL"
    COMPLETION = "COMPLETION"
    DELEGATION = "DELEGATION"


class RiskLevel(str, enum.Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def score(self) -> float:
        return {"none": 0.0, "low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}[
            self.value
        ]

    @classmethod
    def from_score(cls, value: float) -> RiskLevel:
        for level in (cls.CRITICAL, cls.HIGH, cls.MEDIUM, cls.LOW):
            if value >= level.score:
                return level
        return cls.NONE


class AgentMessage(BaseModel):
    """A typed message between agents. RFC §13's required-fields list.

    Frozen: a message that could be mutated after dispatch would make the
    ledger's causation chain a lie.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    task_id: uuid.UUID
    parent_task_id: uuid.UUID | None = None
    correlation_id: uuid.UUID
    session_id: uuid.UUID | None = None
    trace_id: str | None = None

    sender: str
    recipient: str
    intent: MessageType

    payload: dict[str, Any] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    risk_level: RiskLevel = RiskLevel.NONE
    required_permissions: list[Permission] = Field(default_factory=list)

    status: str = "pending"
    deadline: datetime | None = None
    recursion_depth: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def reply(
        self,
        intent: MessageType,
        payload: dict[str, Any],
        *,
        sender: str | None = None,
        **kwargs: Any,
    ) -> AgentMessage:
        """Build a response that preserves lineage."""
        return AgentMessage(
            task_id=self.task_id,
            parent_task_id=self.parent_task_id,
            correlation_id=self.correlation_id,
            session_id=self.session_id,
            trace_id=self.trace_id,
            sender=sender or self.recipient,
            recipient=self.sender,
            intent=intent,
            payload=payload,
            recursion_depth=self.recursion_depth,
            **kwargs,
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        return (now or datetime.now(UTC)) > self.deadline


T = TypeVar("T")


class AgentResult(BaseModel, Generic[T]):
    """What an agent hands back.

    Carries the accounting the orchestrator needs (tokens, latency, model) so
    budget enforcement does not depend on agents self-reporting honestly in
    free-form text.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: T | None = None
    error: dict[str, Any] | None = None

    tokens_consumed: int = 0
    latency_ms: float = 0.0
    model_used: str | None = None
    escalated: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    telemetry: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, value: T, **kwargs: Any) -> AgentResult[T]:
        return cls(ok=True, value=value, **kwargs)

    @classmethod
    def failure(cls, error: PaaError | str, **kwargs: Any) -> AgentResult[T]:
        payload = (
            error.to_payload() if isinstance(error, PaaError) else {"message": str(error)}
        )
        return cls(ok=False, error=payload, **kwargs)


class AgentContext(BaseModel):
    """Per-invocation execution envelope.

    Bundles identity, budget and permissions so an agent never has to reach
    into global state to find out what it is allowed to do — which is what
    makes the permission checks auditable.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    correlation_id: uuid.UUID
    session_id: uuid.UUID | None = None
    task_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    parent_task_id: uuid.UUID | None = None
    trace_id: str | None = None

    modality: ComplexityModality = ComplexityModality.STANDARD
    permission_mode: PermissionMode = PermissionMode.ASK
    recursion_depth: int = Field(default=0, ge=0)

    tokens_budget: int = 0
    tokens_spent: int = 0
    deadline: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def profile(self) -> ModalityProfile:
        return MODALITY_PROFILES[self.modality]

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.tokens_budget - self.tokens_spent)

    def spend(self, tokens: int) -> None:
        """Charge tokens, raising once the budget is gone."""
        self.tokens_spent += tokens
        if self.tokens_budget and self.tokens_spent > self.tokens_budget:
            raise BudgetExceededError("tokens", self.tokens_budget, self.tokens_spent)

    def child(self, **overrides: Any) -> AgentContext:
        """Derive a sub-agent context one level deeper.

        The token budget halves per level (RFC §15.7), which is what keeps a
        depth-3 delegation tree from costing 8x its parent.
        """
        depth = self.recursion_depth + 1
        base: dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "parent_task_id": self.task_id,
            "trace_id": self.trace_id,
            "modality": self.modality,
            "permission_mode": self.permission_mode,
            "recursion_depth": depth,
            "tokens_budget": self.profile.token_quota_at_depth(depth),
            "deadline": self.deadline,
        }
        return AgentContext(**{**base, **overrides})

    def grants(self, permission: Permission) -> bool:
        return self.permission_mode.grants(permission)


class Agent(abc.ABC):
    """Base class for every agent in the hierarchy.

    Subclasses implement :meth:`handle`. The base class owns the cross-cutting
    concerns — permission checks, budget accounting, timing, ledger emission —
    so that no agent can accidentally skip them by forgetting boilerplate.
    """

    role: AgentRole
    #: Permissions this agent needs before it may run at all.
    required_permissions: tuple[Permission, ...] = ()
    #: Whether this agent may delegate. Most may not.
    can_delegate: bool = False

    def __init__(
        self,
        *,
        name: str | None = None,
        ledger: LedgerStore | None = None,
        orchestrator: Any = None,
    ) -> None:
        self.name = name or self.role.value
        self._ledger = ledger
        self._orchestrator = orchestrator

    @abc.abstractmethod
    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        """Do the agent's work. Implemented by subclasses."""

    async def run(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        """Invoke the agent with all cross-cutting guarantees applied."""
        import time

        from paa.core.errors import PermissionDeniedError

        missing = [p for p in self.required_permissions if not ctx.grants(p)]
        if missing:
            error = PermissionDeniedError(
                [p.value for p in missing], ctx.permission_mode.value
            )
            log.warning(
                "agent.permission_denied",
                agent=self.name,
                missing=[p.value for p in missing],
                mode=ctx.permission_mode.value,
            )
            return AgentResult.failure(error)

        if message.is_expired():
            return AgentResult.failure(f"{self.name}: message deadline already passed")

        started = time.perf_counter()
        try:
            result = await self.handle(message, ctx)
        except PaaError as exc:
            log.warning("agent.failed", agent=self.name, error=str(exc))
            return AgentResult.failure(
                exc, latency_ms=(time.perf_counter() - started) * 1000
            )
        except Exception as exc:
            # An unexpected error must still produce a structured result — the
            # orchestrator records outcomes in the ledger and cannot do that
            # with a bare traceback.
            log.exception("agent.unhandled_error", agent=self.name)
            return AgentResult.failure(
                f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        if not result.latency_ms:
            result = result.model_copy(
                update={"latency_ms": (time.perf_counter() - started) * 1000}
            )
        if result.tokens_consumed:
            ctx.spend(result.tokens_consumed)
        return result

    async def delegate(
        self,
        target: str,
        intent: MessageType,
        payload: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult[Any]:
        """Ask another agent to do something, via the orchestrator.

        Never calls the peer directly: the orchestrator owns the delegation
        graph and is the only component that can see whether a new edge would
        close a cycle.
        """
        from paa.core.errors import RecursionGuardError

        if not self.can_delegate:
            raise RecursionGuardError(f"{self.name} is not permitted to delegate")
        if self._orchestrator is None:
            raise RecursionGuardError(f"{self.name} has no orchestrator to delegate through")

        return await self._orchestrator.mediate_delegation(
            sender=self.name, target=target, intent=intent, payload=payload, ctx=ctx
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} role={self.role.value}>"
