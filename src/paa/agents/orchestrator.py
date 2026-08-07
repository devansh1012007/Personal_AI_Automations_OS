"""The Chief Orchestrator.

RFC §2.1 agent 1: the central hub. It owns the task lifecycle, writes every
state transition to the ledger, allocates budgets, mediates delegation, and
enforces backpressure.

Two properties are non-negotiable and shape the whole design:

**No generative reasoning in the control path.** Routing decisions are made by
Python, never by a model. A model deciding "should this task proceed?" is a
model that can be prompt-injected into proceeding. The orchestrator uses LLMs
for *content* (planning, critique) and never for *control*.

**Ledger before side effect.** Every phase writes its event before the work it
describes. A crash between the event and the work is recoverable — replay
knows the work was intended. A crash between the work and the event is not —
the work happened and nothing records it. Ordering is chosen so the survivable
failure is the only possible one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult, MessageType
from paa.agents.delegation import DelegationEdge, DelegationRegistry
from paa.core.errors import PaaError, RecursionGuardError
from paa.core.types import (
    MODALITY_PROFILES,
    AgentRole,
    ComplexityModality,
    CorrelationId,
    EventType,
    PermissionMode,
    SessionId,
    new_correlation_id,
)
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskPhase, TaskProjection, project

if TYPE_CHECKING:
    from paa.ledger.store import LedgerStore

__all__ = ["ChiefOrchestrator", "TaskOutcome", "TaskRequest"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TaskRequest:
    """What a caller asks the runtime to do."""

    goal: str
    modality: ComplexityModality | None = None
    """``None`` means classify automatically."""

    session_id: SessionId | None = None
    workspace_path: str | None = None
    target_agent: str | None = None
    """Naming an agent bypasses the router entirely — see ADR-0011."""

    required_slots: list[str] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class TaskOutcome:
    """Terminal result of driving one task."""

    correlation_id: uuid.UUID
    phase: TaskPhase
    projection: TaskProjection
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.phase is TaskPhase.COMMITTED


class ChiefOrchestrator:
    """Drives tasks through the lifecycle and owns all shared state.

    Collaborators are injected rather than constructed so the orchestrator can
    be exercised end-to-end with fakes — the alternative is an integration test
    that needs a model server to assert a state machine.
    """

    def __init__(
        self,
        ledger: LedgerStore,
        *,
        context_builder: Agent | None = None,
        worker_context_builder: Agent | None = None,
        planner: Agent | None = None,
        policy: Agent | None = None,
        worker: Agent | None = None,
        critic: Agent | None = None,
        router: Agent | None = None,
        queue: Any = None,
        backpressure: Any = None,
        recovery: Any = None,
        metrics: Any = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        max_step_retries: int = 2,
    ) -> None:
        self._ledger = ledger
        self._queue = queue
        self._backpressure = backpressure
        self._recovery = recovery
        self._metrics = metrics
        self._mode = permission_mode
        self._max_step_retries = max_step_retries

        self._agents: dict[str, Agent] = {}
        for agent in (
            context_builder,
            worker_context_builder,
            planner,
            policy,
            worker,
            critic,
            router,
        ):
            if agent is not None:
                self.register(agent)

        self._context_builder = context_builder
        self._worker_context_builder = worker_context_builder
        self._planner = planner
        self._policy = policy
        self._worker = worker
        self._critic = critic
        self._router = router

        self._delegations = DelegationRegistry()

    # -- registry ----------------------------------------------------------

    def register(self, agent: Agent) -> None:
        """Add an agent and give it a back-reference for delegation."""
        self._agents[agent.name] = agent
        agent._orchestrator = self
        if agent._ledger is None:
            agent._ledger = self._ledger

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    @property
    def agent_names(self) -> list[str]:
        return sorted(self._agents)

    # -- ledger helper -----------------------------------------------------

    async def _emit(
        self,
        correlation_id: uuid.UUID,
        event_type: EventType,
        *,
        session_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        role: AgentRole | None = None,
        attempt: int = 0,
        discriminator: str | None = None,
        causation_id: uuid.UUID | None = None,
    ) -> LedgerEvent:
        return await self._ledger.append(
            LedgerEvent(
                correlation_id=correlation_id,
                session_id=session_id,
                causation_id=causation_id,
                event_type=event_type,
                execution_mode=modality,
                agent_role=role.value if role else None,
                payload=payload or {},
                attempt=attempt,
                discriminator=discriminator,
            )
        )

    # -- submission --------------------------------------------------------

    async def submit(self, request: TaskRequest) -> CorrelationId:
        """Record a new task and return its lineage key.

        Deliberately does not execute. Submission must be cheap and always
        succeed so the caller's request is durable before any work is
        attempted — that is what lets a crash mid-execution be recovered
        rather than losing the request entirely.
        """
        correlation_id = new_correlation_id()
        modality = request.modality or self._classify(request)

        payload: dict[str, Any] = {
            "request": {
                "goal": request.goal,
                "target_agent": request.target_agent,
                "required_slots": request.required_slots or [],
                **(request.metadata or {}),
            }
        }
        if request.workspace_path:
            payload["workspace_path"] = request.workspace_path

        await self._emit(
            correlation_id,
            EventType.TASK_REQUESTED,
            session_id=request.session_id,
            payload=payload,
            modality=modality,
            role=AgentRole.ORCHESTRATOR,
        )

        modality = await self._apply_backpressure(correlation_id, modality)

        await self._emit(
            correlation_id,
            EventType.TASK_QUEUED,
            session_id=request.session_id,
            payload={"modality": modality.value},
            modality=modality,
            role=AgentRole.ORCHESTRATOR,
        )

        if self._queue is not None:
            from paa.storage.queue.base import StreamName

            await self._queue.enqueue(
                StreamName.ORCHESTRATOR_CORE,
                {"correlation_id": str(correlation_id), "goal": request.goal},
                correlation_id=str(correlation_id),
            )

        log.info(
            "orchestrator.submitted",
            correlation_id=str(correlation_id),
            modality=modality.value,
            goal=request.goal[:80],
        )
        return correlation_id

    def _classify(self, request: TaskRequest) -> ComplexityModality:
        """Pick a modality without calling a model.

        A deliberately crude heuristic. The RFC gives no classification rule,
        and a wrong guess is cheap to correct — the planner can escalate — so
        spending a model call here would be poor value. When a router agent is
        configured it can override this; see :meth:`_route`.
        """
        goal = request.goal.lower()
        if request.target_agent and len(goal) < 80:
            return ComplexityModality.SIMPLE
        if any(k in goal for k in ("migrate", "refactor everything", "redesign", "architect")):
            return ComplexityModality.MAX
        if any(k in goal for k in ("refactor", "implement", "build", "debug", "fix")):
            return ComplexityModality.COMPLEX
        return ComplexityModality.STANDARD

    async def _apply_backpressure(
        self, correlation_id: uuid.UUID, modality: ComplexityModality
    ) -> ComplexityModality:
        """Degrade modality when the queue is deep. RFC §6.2."""
        if self._backpressure is None or self._queue is None:
            return modality
        try:
            from paa.storage.queue.base import StreamName

            depth = await self._queue.depth(StreamName.ORCHESTRATOR_CORE)
            state = self._backpressure.assess(depth)
            degraded = self._backpressure.degrade_modality(modality, state)
            if degraded is not modality:
                log.warning(
                    "orchestrator.backpressure_degraded",
                    correlation_id=str(correlation_id),
                    depth=depth,
                    was=modality.value,
                    now=degraded.value,
                )
            return degraded
        except Exception as exc:
            # Backpressure is an optimisation. If it fails, run at the
            # requested modality rather than refusing the task.
            log.warning("orchestrator.backpressure_unavailable", error=str(exc))
            return modality

    # -- execution ---------------------------------------------------------

    async def run(self, correlation_id: CorrelationId | uuid.UUID) -> TaskOutcome:
        """Drive a submitted task to a terminal phase."""
        state = await project(self._ledger, correlation_id)
        if state.is_terminal:
            return TaskOutcome(uuid.UUID(str(correlation_id)), state.phase, state)

        ctx = self._context_for(state)

        try:
            state = await self._hydrate(state, ctx)
            state = await self._plan(state, ctx)
            state = await self._check_policy(state, ctx)
            if state.phase in (TaskPhase.BLOCKED, TaskPhase.AWAITING_HUMAN):
                return TaskOutcome(ctx.correlation_id, state.phase, state)
            state = await self._execute_steps(state, ctx)
            if state.phase is TaskPhase.AWAITING_HUMAN:
                return TaskOutcome(ctx.correlation_id, state.phase, state)
            state = await self._commit(state, ctx)
        except PaaError as exc:
            await self._emit(
                ctx.correlation_id,
                EventType.EXECUTION_FAILED,
                session_id=ctx.session_id,
                payload=exc.to_payload(),
                modality=ctx.modality,
                role=AgentRole.ORCHESTRATOR,
                attempt=state.attempts,
            )
            state = await project(self._ledger, correlation_id)
            return TaskOutcome(ctx.correlation_id, state.phase, state, exc.to_payload())
        finally:
            self._delegations.discard(ctx.correlation_id)

        return TaskOutcome(ctx.correlation_id, state.phase, state)

    def _context_for(self, state: TaskProjection) -> AgentContext:
        profile = MODALITY_PROFILES[state.modality]
        return AgentContext(
            correlation_id=uuid.UUID(state.correlation_id),
            session_id=uuid.UUID(state.session_id) if state.session_id else None,
            modality=state.modality,
            permission_mode=self._mode,
            tokens_budget=profile.token_ceiling,
        )

    def _message(
        self, state: TaskProjection, ctx: AgentContext, intent: MessageType, payload: dict
    ) -> AgentMessage:
        return AgentMessage(
            task_id=ctx.task_id,
            correlation_id=ctx.correlation_id,
            session_id=ctx.session_id,
            sender=self.name,
            recipient=intent.value,
            intent=intent,
            payload=payload,
            recursion_depth=ctx.recursion_depth,
        )

    name = AgentRole.ORCHESTRATOR.value

    async def _hydrate(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        if state.context_packet is not None or self._context_builder is None:
            return state

        await self._emit(
            ctx.correlation_id,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            session_id=ctx.session_id,
            modality=ctx.modality,
            role=AgentRole.CONTEXT_BUILDER_PLANNER,
        )

        message = self._message(
            state,
            ctx,
            MessageType.CONTEXT_REQUEST,
            {
                "goal": state.request.get("goal", ""),
                "required_slots": state.request.get("required_slots", []),
            },
        )
        result = await self._context_builder.run(message, ctx)
        if not result.ok:
            raise PaaError("context hydration failed", cause=result.error or {})

        packet = result.value if isinstance(result.value, dict) else {}
        if packet.get("routing_directive") == "HARD_STOP_ESCALATE_TO_USER":
            await self._emit(
                ctx.correlation_id,
                EventType.AWAITING_HUMAN_ATTESTATION,
                session_id=ctx.session_id,
                payload={
                    "reason": "insufficient context density",
                    "vacant_slots": packet.get("vacant_slots", []),
                },
                modality=ctx.modality,
                role=AgentRole.CONTEXT_BUILDER_PLANNER,
            )
            return await project(self._ledger, ctx.correlation_id)

        await self._emit(
            ctx.correlation_id,
            EventType.CONTEXT_HYDRATED,
            session_id=ctx.session_id,
            payload={"context_packet": packet},
            modality=ctx.modality,
            role=AgentRole.CONTEXT_BUILDER_PLANNER,
        )
        return await project(self._ledger, ctx.correlation_id)

    async def _plan(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        if state.plan_steps or self._planner is None:
            return state

        # SIMPLE bypasses the model entirely (RFC §9.2: "0 tokens").
        if ctx.modality is ComplexityModality.SIMPLE:
            await self._emit(
                ctx.correlation_id,
                EventType.PLAN_COMPILED,
                session_id=ctx.session_id,
                payload={
                    "execution_steps": [
                        {
                            "index": 0,
                            "action": state.request.get("goal", ""),
                            "agent": state.request.get("target_agent") or "worker",
                        }
                    ],
                    "bypassed_planner": True,
                },
                modality=ctx.modality,
                role=AgentRole.STRATEGIC_PLANNER,
            )
            return await project(self._ledger, ctx.correlation_id)

        message = self._message(
            state,
            ctx,
            MessageType.PLAN_PROPOSAL,
            {"goal": state.request.get("goal", ""), "context": state.context_packet or {}},
        )
        result = await self._planner.run(message, ctx)
        if not result.ok:
            raise PaaError("planning failed", cause=result.error or {})

        plan = result.value if isinstance(result.value, dict) else {}
        steps = plan.get("execution_steps", [])
        profile = MODALITY_PROFILES[ctx.modality]
        if len(steps) > profile.max_plan_nodes():
            raise RecursionGuardError(
                "plan exceeds the expanded-node ceiling for this modality",
                depth=len(steps),
                ceiling=profile.max_plan_nodes(),
            )

        await self._emit(
            ctx.correlation_id,
            EventType.PLAN_COMPILED,
            session_id=ctx.session_id,
            payload={
                "execution_steps": steps,
                "step_requirements": plan.get("step_requirements", {}),
            },
            modality=ctx.modality,
            role=AgentRole.STRATEGIC_PLANNER,
        )
        return await project(self._ledger, ctx.correlation_id)

    async def _check_policy(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        if state.policy_decision is not None or self._policy is None:
            return state

        message = self._message(
            state,
            ctx,
            MessageType.POLICY_CHECK,
            {"steps": state.plan_steps, "goal": state.request.get("goal", "")},
        )
        result = await self._policy.run(message, ctx)
        verdict = result.value if isinstance(result.value, dict) else {}

        if not result.ok or verdict.get("decision") == "STATUS_BLOCKED":
            event = (
                EventType.SECURITY_VIOLATION
                if verdict.get("anti_goal_match")
                else EventType.POLICY_BLOCKED
            )
            await self._emit(
                ctx.correlation_id,
                event,
                session_id=ctx.session_id,
                payload={
                    "decision": "STATUS_BLOCKED",
                    "reason": verdict.get("reason") or "policy refused the plan",
                    **(result.error or {}),
                },
                modality=ctx.modality,
                role=AgentRole.POLICY_RISK,
            )
            return await project(self._ledger, ctx.correlation_id)

        if verdict.get("requires_human_gate"):
            await self._emit(
                ctx.correlation_id,
                EventType.AWAITING_HUMAN_ATTESTATION,
                session_id=ctx.session_id,
                payload={"reason": verdict.get("reason", "human attestation required")},
                modality=ctx.modality,
                role=AgentRole.POLICY_RISK,
            )
            return await project(self._ledger, ctx.correlation_id)

        await self._emit(
            ctx.correlation_id,
            EventType.POLICY_CLEARED,
            session_id=ctx.session_id,
            payload={"decision": "STATUS_APPROVED", "risk_score": verdict.get("risk_score", 0.0)},
            modality=ctx.modality,
            role=AgentRole.POLICY_RISK,
        )
        return await project(self._ledger, ctx.correlation_id)

    async def _execute_steps(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        if self._worker is None:
            return state

        for index, step in enumerate(state.plan_steps):
            if index in state.completed_steps:
                continue  # resumed task: this step already landed

            for attempt in range(self._max_step_retries + 1):
                await self._emit(
                    ctx.correlation_id,
                    EventType.EXECUTION_STARTED,
                    session_id=ctx.session_id,
                    payload={"step_index": index, "step": step},
                    modality=ctx.modality,
                    role=AgentRole.WORKER,
                    attempt=attempt,
                    discriminator=f"step-{index}",
                )

                message = self._message(
                    state, ctx, MessageType.EXECUTION_REQUEST, {"step": step, "index": index}
                )
                result = await self._worker.run(message, ctx.child())

                if result.ok and await self._critique(state, ctx, index, result):
                    await self._emit(
                        ctx.correlation_id,
                        EventType.EXECUTION_COMPLETED,
                        session_id=ctx.session_id,
                        payload={
                            "step_index": index,
                            "tokens_consumed": result.tokens_consumed,
                        },
                        modality=ctx.modality,
                        role=AgentRole.WORKER,
                        attempt=attempt,
                        discriminator=f"step-{index}",
                    )
                    break

                await self._emit(
                    ctx.correlation_id,
                    EventType.VALIDATION_FAILED,
                    session_id=ctx.session_id,
                    payload={
                        "step_index": index,
                        "attempt": attempt,
                        **(result.error or {"verdict": "FAIL_REJECT_RETRY"}),
                    },
                    modality=ctx.modality,
                    role=AgentRole.CRITIC,
                    attempt=attempt,
                    discriminator=f"step-{index}",
                )
            else:
                raise PaaError(
                    "step exhausted its retry budget",
                    step_index=index,
                    retries=self._max_step_retries,
                )

            state = await project(self._ledger, ctx.correlation_id)

        return state

    async def _critique(
        self, state: TaskProjection, ctx: AgentContext, index: int, result: AgentResult[Any]
    ) -> bool:
        """Run the critic over a step's output. Returns whether it passed."""
        if self._critic is None:
            return True

        message = self._message(
            state,
            ctx,
            MessageType.REVIEW_RESULT,
            {"step_index": index, "output": result.value},
        )
        review = await self._critic.run(message, ctx.child())
        verdict = (review.value or {}).get("verdict") if isinstance(review.value, dict) else None
        passed = bool(review.ok and verdict == "PASS")

        await self._emit(
            ctx.correlation_id,
            EventType.CRITIQUE_CONCLUDED,
            session_id=ctx.session_id,
            payload={"step_index": index, "verdict": verdict or "FAIL_REJECT_RETRY"},
            modality=ctx.modality,
            role=AgentRole.CRITIC,
            discriminator=f"step-{index}",
        )
        return passed

    async def _commit(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        await self._emit(
            ctx.correlation_id,
            EventType.MUTATION_COMMITTED,
            session_id=ctx.session_id,
            payload={
                "steps_completed": len(state.completed_steps),
                "tokens_consumed": state.tokens_consumed,
            },
            modality=ctx.modality,
            role=AgentRole.ORCHESTRATOR,
        )
        log.info(
            "orchestrator.committed",
            correlation_id=state.correlation_id,
            steps=len(state.completed_steps),
            tokens=state.tokens_consumed,
        )
        return await project(self._ledger, ctx.correlation_id)

    # -- delegation --------------------------------------------------------

    async def mediate_delegation(
        self,
        *,
        sender: str,
        target: str,
        intent: MessageType,
        payload: dict[str, Any],
        ctx: AgentContext,
    ) -> AgentResult[Any]:
        """Run one agent on another's behalf, under the graph invariants.

        This is the only path by which one agent can cause another to run, so
        it is the only place the depth, cycle and node bounds need enforcing.
        """
        agent = self._agents.get(target)
        if agent is None:
            return AgentResult.failure(f"no agent registered under {target!r}")

        profile = MODALITY_PROFILES[ctx.modality]
        graph = self._delegations.graph_for(
            ctx.correlation_id,
            max_depth=profile.recursion_ceiling,
            max_nodes=profile.max_plan_nodes(),
        )
        child_ctx = ctx.child()
        edge = DelegationEdge(sender, target, child_ctx.task_id, child_ctx.recursion_depth)

        try:
            graph.propose(edge)
        except RecursionGuardError as exc:
            log.warning(
                "delegation.refused",
                sender=sender,
                target=target,
                reason=exc.message,
                cycle=exc.cycle,
            )
            return AgentResult.failure(exc)

        message = AgentMessage(
            task_id=child_ctx.task_id,
            parent_task_id=ctx.task_id,
            correlation_id=ctx.correlation_id,
            session_id=ctx.session_id,
            sender=sender,
            recipient=target,
            intent=intent,
            payload=payload,
            recursion_depth=child_ctx.recursion_depth,
        )
        try:
            return await agent.run(message, child_ctx)
        finally:
            graph.release(edge)

    # -- human gates -------------------------------------------------------

    async def clear_human_gate(
        self,
        correlation_id: CorrelationId | uuid.UUID,
        *,
        approved: bool,
        resume_phase: TaskPhase = TaskPhase.EXECUTING,
        note: str | None = None,
    ) -> TaskProjection:
        """Record a human's decision on a parked task.

        Only a real caller reaches this. Nothing in the automated path can
        clear its own gate, which is what makes ``AWAITING_HUMAN_ATTESTATION``
        meaningful.
        """
        state = await project(self._ledger, correlation_id)
        if state.phase is not TaskPhase.AWAITING_HUMAN:
            raise PaaError(
                "task is not awaiting human attestation",
                correlation_id=str(correlation_id),
                phase=state.phase.value,
            )

        await self._emit(
            uuid.UUID(str(correlation_id)),
            EventType.HUMAN_GATE_CLEARED if approved else EventType.HUMAN_GATE_REJECTED,
            session_id=uuid.UUID(state.session_id) if state.session_id else None,
            payload={"resume_phase": resume_phase.value, "note": note},
            modality=state.modality,
            role=AgentRole.ORCHESTRATOR,
            attempt=state.attempts,
        )
        return await project(self._ledger, correlation_id)

    async def record_correction(
        self, correlation_id: CorrelationId | uuid.UUID, correction: str
    ) -> None:
        """Log a user correction — the primary signal the reflection engine reads."""
        state = await project(self._ledger, correlation_id)
        await self._emit(
            uuid.UUID(str(correlation_id)),
            EventType.USER_CORRECTION,
            payload={"correction": correction},
            modality=state.modality,
            role=AgentRole.ORCHESTRATOR,
            attempt=state.user_corrections,
        )

    # -- boot --------------------------------------------------------------

    async def boot(self) -> Any:
        """Run crash recovery before accepting new work.

        Called once at startup. Deliberately ignores any queue state: the
        ledger is authoritative and the queue may have lost messages
        (RFC §17.4).
        """
        if self._recovery is None:
            return None
        report = await self._recovery.boot_sweep()
        log.info("orchestrator.boot_recovery", **report.summary())
        return report
