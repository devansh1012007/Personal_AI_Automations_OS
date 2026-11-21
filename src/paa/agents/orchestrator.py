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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

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
from paa.ledger.mandate import Mandate
from paa.ledger.replay import TaskPhase, TaskProjection, project
from paa.pipelines.base import GateQuestion, PipelineTask
from paa.pipelines.builtin import (
    BuiltinPipelineFacade,
    BuiltinPlanExecuteCritique,
    StageAgents,
)

if TYPE_CHECKING:
    from paa.context.allocator import AllocationResult
    from paa.ledger.store import LedgerStore

__all__ = ["BackpressureLike", "ChiefOrchestrator", "TaskOutcome", "TaskRequest"]

log = structlog.get_logger(__name__)

#: The prefix of a human-gate question's discriminator. A gate request is a
#: *question*, and the discriminator is that question's identity: the stage
#: that asked, and which question of that stage it is. See
#: :meth:`ChiefOrchestrator._gate_discriminator` for why the ledger's
#: idempotency key needs it, and ADR-0034 for the mechanism.
_GATE_DISCRIMINATOR_PREFIX = "gate"

#: Where a task resumes once a human releases the gate a stage opened, keyed
#: by the role that asked. The context builder parks *during hydration*, so
#: the task resumes where the phase map puts a hydrated context
#: (``PLANNING``); the policy agent parks *during the review*, so the task
#: resumes where a cleared policy leaves it (``EXECUTING``). A role that is
#: not listed resumes at ``EXECUTING``, which is the phase map's own default
#: for a cleared gate.
_GATE_RESUME_PHASES: dict[str, TaskPhase] = {
    AgentRole.CONTEXT_BUILDER_PLANNER.value: TaskPhase.PLANNING,
    AgentRole.POLICY_RISK.value: TaskPhase.EXECUTING,
}


def _released_question(
    questions: list[GateQuestion], discriminator: str | None
) -> int | None:
    """The index of the question a release answers, if it answers one.

    A release that names a question answers *that* question, and answers
    nothing if the lineage holds no such open question — a release nobody
    asked for must not release a gate. A release that names none (a lineage
    written before the discriminator existed) answers the question it
    follows, which is the only one that can be open.
    """
    if discriminator is not None:
        for index in range(len(questions) - 1, -1, -1):
            if questions[index].discriminator == discriminator:
                return None if questions[index].answered else index
        return None
    for index in range(len(questions) - 1, -1, -1):
        if not questions[index].answered:
            return index
    return None


class BackpressureLike(Protocol):
    """The one thing the orchestrator asks of a backpressure controller.

    A protocol rather than a direct import of
    :class:`paa.storage.queue.backpressure.BackpressureController`, so the
    orchestrator keeps no queue-backend import at module scope — the
    controller is optional and the queue may not even be configured — and so
    the contract the orchestrator depends on is stated: a modality goes in
    and a modality comes out. Returning anything else would silently
    corrupt the modality the rest of the lifecycle runs under.
    """

    def degrade_modality(self, modality: ComplexityModality) -> ComplexityModality: ...


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
    mandate: Mandate | None = None
    """The authority record for this task (N2.4, ADR-0046).

    ``None`` means no mandate was asserted for the submission; the runtime
    resolves the default (implicit) before the request reaches here, so a
    ``None`` here means the caller bypassed the runtime and the lineage
    carries no mandate event at all. When present, :meth:`submit` records
    it as the lineage's head event (``MANDATE_GRANTED``) before
    ``TASK_REQUESTED``, so ``paa ledger`` shows the authority first.
    """


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
        coding: Agent | None = None,
        queue: Any = None,
        backpressure: BackpressureLike | None = None,
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
            coding,
        ):
            if agent is not None:
                self.register(agent)

        self._delegations = DelegationRegistry()

        # P1.2: the strategy is a pipeline; the orchestrator is its host.
        # The stage agents are captured here, at construction, so a
        # registration that arrives later cannot change what a run reaches
        # (the registry serves delegation, not the stage slots). The direct
        # per-stage references the old code kept are deliberately gone: the
        # pipeline's StageAgents is the one holder now, and a second copy
        # here would be write-only state that could drift from it.
        self._pipeline = BuiltinPlanExecuteCritique(
            host=self,
            stages=StageAgents(
                context_builder=context_builder,
                planner=planner,
                policy=policy,
                worker=worker,
                critic=critic,
            ),
            max_step_retries=max_step_retries,
        )

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

    async def emit(
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

    async def project(self, correlation_id: uuid.UUID) -> TaskProjection:
        """Re-read a lineage's current projection — the host's R2 read."""
        return await project(self._ledger, correlation_id)

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

        # The mandate is the lineage's head event: the authority is
        # recorded before the request it authorises, so a task's first
        # state_version is the grant (N2.4). Ledger-before-side-effect
        # applies to authority too — a task that crashed before its grant
        # landed has no authority record and no work.
        if request.mandate is not None:
            await self.emit(
                correlation_id,
                EventType.MANDATE_GRANTED,
                session_id=request.session_id,
                payload=request.mandate.granted_payload(
                    session_id=str(request.session_id) if request.session_id else None
                ),
                modality=modality,
                role=AgentRole.ORCHESTRATOR,
                discriminator="mandate",
            )

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

        await self.emit(
            correlation_id,
            EventType.TASK_REQUESTED,
            session_id=request.session_id,
            payload=payload,
            modality=modality,
            role=AgentRole.ORCHESTRATOR,
        )

        modality = await self._apply_backpressure(correlation_id, modality)

        await self.emit(
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
        configured it can override this via delegation.
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
            degraded = self._backpressure.degrade_modality(modality)
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
        """Drive a submitted task to a terminal phase.

        P1.2: the orchestrator is the *host* — it owns submission,
        backpressure, recovery, human gates, delegation and failure
        recording — and the strategy is
        :class:`~paa.pipelines.builtin.BuiltinPlanExecuteCritique`, driving
        the same hydrate → plan → policy → execute → commit sequence this
        method itself used to drive inline. The events, their order and
        their payloads are unchanged; the migration gate is the full suite
        passing unchanged
        (docs/plans/2026-11-21-pipeline-platform-spec.md P1.2).

        The pipeline writes its live projection back onto the per-run task
        at exactly the points this method used to reassign its local
        ``state``, so the failure path below still reads the same
        ``attempts`` count the old code read when a stage raised — the
        subtlety tests/pipelines/test_builtin.py pins.
        """
        state = await project(self._ledger, correlation_id)
        if state.is_terminal:
            return TaskOutcome(uuid.UUID(str(correlation_id)), state.phase, state)

        agent_ctx = self._context_for(state)
        task = PipelineTask(
            correlation_id=agent_ctx.correlation_id,
            session_id=agent_ctx.session_id,
            goal=state.request.get("goal", ""),
            request=state.request,
            modality=state.modality,
            permission_mode=agent_ctx.permission_mode,
            projection=state,
        )

        try:
            await self._pipeline.run(task, self._facade(agent_ctx))
            # The pipeline's PipelineResult (status/summary/metrics) is
            # deliberately not surfaced yet: the TaskOutcome is derived from
            # the ledger, which is the truth; P4's selection surfaces it.
            state = await project(self._ledger, correlation_id)
        except PaaError as exc:
            await self.emit(
                agent_ctx.correlation_id,
                EventType.EXECUTION_FAILED,
                session_id=agent_ctx.session_id,
                payload=exc.to_payload(),
                modality=agent_ctx.modality,
                role=AgentRole.ORCHESTRATOR,
                attempt=task.projection.attempts
                if task.projection is not None
                else state.attempts,
            )
            state = await project(self._ledger, correlation_id)
            return TaskOutcome(agent_ctx.correlation_id, state.phase, state, exc.to_payload())
        finally:
            self._delegations.discard(agent_ctx.correlation_id)

        return TaskOutcome(agent_ctx.correlation_id, state.phase, state)

    def _facade(self, agent_ctx: AgentContext) -> BuiltinPipelineFacade:
        """Mint the capability facade for one run — the host grants (ADR-0051)."""
        return BuiltinPipelineFacade(self, self._pipeline.stages, agent_ctx)

    def _context_for(self, state: TaskProjection) -> AgentContext:
        profile = MODALITY_PROFILES[state.modality]
        return AgentContext(
            correlation_id=uuid.UUID(state.correlation_id),
            session_id=uuid.UUID(state.session_id) if state.session_id else None,
            modality=state.modality,
            permission_mode=self._mode,
            tokens_budget=profile.token_ceiling,
        )

    def message(
        self, state: TaskProjection, ctx: AgentContext, intent: MessageType, payload: dict[str, Any]
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

    async def _check_policy(self, state: TaskProjection, ctx: AgentContext) -> TaskProjection:
        """Request clearance for a plan, through the facade (P1.2).

        The stage's body lives on the facade the host mints — the policy
        agent is host-side by design, and a pipeline only asks (ADR-0051).
        The private name is kept because the gate-identity tests call it
        directly on a parked lineage to assert the policy agent is not
        consulted while its question is open.
        """
        return await self._facade(ctx).check_policy(state)

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
            # ADR-0041: the block emits the EXISTING STATE_ROLLBACK_TRIGGERED
            # (resumable ROLLED_BACK, never terminal) BEFORE returning the
            # failure — ledger-before-side-effect. Additive fields only
            # (reason/depth/ceiling alongside the recovery drift keys); the
            # full ADR-0008 key (attempt=0, discriminator naming sender/target/
            # depth) makes a redelivered block idempotent.
            await self.emit(
                ctx.correlation_id,
                EventType.STATE_ROLLBACK_TRIGGERED,
                session_id=ctx.session_id,
                payload={
                    "reason": "delegation_recursion_block",
                    "depth": exc.depth,
                    "ceiling": exc.ceiling,
                    "cycle": exc.cycle,
                    "sender": sender,
                    "target": target,
                },
                modality=ctx.modality,
                role=AgentRole.ORCHESTRATOR,
                attempt=0,
                discriminator=f"delegation-block:{sender}->{target}:depth{exc.depth}",
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

    async def open_gate(
        self, ctx: AgentContext, role: AgentRole, payload: dict[str, Any]
    ) -> TaskProjection:
        """Ask a human a question, and park the task on the answer.

        The one place a gate is opened. Both stages that can park — the
        context builder and the policy agent — come through here, so the
        identity of the question is minted in one place and the two sites
        cannot drift apart: a discriminator is what tells the ledger that
        two gate requests in one lineage are two *questions* and not one
        request delivered twice (ADR-0034).

        The event is written before the caller returns, and the projection it
        returns is the parked one — the caller does not have to re-read the
        lineage to learn that the task is now waiting for a human.
        """
        questions = await self.gate_history(ctx.correlation_id)
        await self.emit(
            ctx.correlation_id,
            EventType.AWAITING_HUMAN_ATTESTATION,
            session_id=ctx.session_id,
            payload=payload,
            modality=ctx.modality,
            role=role,
            discriminator=self._gate_discriminator(questions, role),
        )
        return await project(self._ledger, ctx.correlation_id)

    #: The private name the gate-identity tests call directly; the pipeline
    #: host protocol reaches for the public one (P1.2).
    _open_gate = open_gate

    async def gate_history(self, correlation_id: uuid.UUID) -> list[GateQuestion]:
        """Every human-gate question this lineage has asked, in order.

        A question is opened by an ``AWAITING_HUMAN_ATTESTATION`` event and
        answered by the ``HUMAN_GATE_CLEARED`` event that names it (the
        release carries the question's discriminator). A
        ``HUMAN_GATE_REJECTED`` event is a refusal: the lineage ends in
        ``BLOCKED``, which is terminal, so a rejected question never reaches
        a caller of this method.

        Read from the lineage rather than from the projection, because the
        projection cannot express the fact: ``apply_event`` clears
        ``awaiting_reason`` when a gate is cleared, and the phase is
        overwritten by whatever event comes next. The ledger is the truth and
        a projection is one view of it — and this question is about the
        *past* (has someone ruled?) rather than about the present state.

        Nothing in the automated path calls ``clear_human_gate``, so the
        answer can only be "a human has ruled" when a real caller did.
        """
        events = await self._ledger.read_correlation(correlation_id)
        questions: list[GateQuestion] = []
        for event in events:
            if event.event_type is EventType.AWAITING_HUMAN_ATTESTATION:
                questions.append(
                    GateQuestion(
                        role=event.agent_role or "",
                        discriminator=event.discriminator,
                        answered=False,
                        payload=event.payload,
                    )
                )
            elif event.event_type is EventType.HUMAN_GATE_CLEARED:
                index = _released_question(questions, event.discriminator)
                if index is not None:
                    questions[index] = replace(questions[index], answered=True)
        return questions

    @staticmethod
    def stage_question(
        questions: list[GateQuestion], role: AgentRole
    ) -> GateQuestion | None:
        """The question this stage asked most recently, if it asked one.

        Per *stage*, not per lineage: a stage's own question is the only one
        it may consult, and the answer to it is the only one that releases
        that stage. A lineage that asked two questions has one open at a
        time, and each stage must be able to tell which of them is its own.
        """
        for question in reversed(questions):
            if question.role == role.value:
                return question
        return None

    @staticmethod
    def _gate_discriminator(questions: list[GateQuestion], role: AgentRole) -> str:
        """The discriminator naming the question a stage is about to ask.

        The discriminator is the *request's identity*, and the ledger's
        idempotency key is ``correlation | event_type | attempt |
        discriminator`` (``ledger/events.py::compute_idempotency_key``). So
        the same request must always produce the same discriminator — or the
        ledger stops suppressing its redeliveries — and two different
        requests must never produce the same one — or the second is
        swallowed and the question is never asked.

        Without one, the two are the same key: a lineage that asked two
        questions had its second one dropped by the store, the projection
        never saw it, and the task carried on to commit with the second
        question unanswered. That is the defect ADR-0034 §6 records.

        The name is the stage that asks and the question's *ordinal* within
        that stage. The role is in the name because the role is the subject
        of the question: "is the context sufficient?" belongs to the context
        builder, "is this plan safe to run?" to the policy agent, and two
        stages asking at once are two different questions by construction.

        The ordinal counts the questions of that stage that have already been
        **answered**, not the ones that have been asked, and that is the
        whole trick:

        * a redelivery of a question nobody has answered yet is the *same*
          request — nothing has been answered, so the ordinal does not move,
          the key does not change, and the store suppresses the redelivery;
        * a genuinely new question can only be asked after the previous one
          was answered (a stage does not ask again while its question is
          open), so the ordinal has moved and the new question is a
          *different* request with a different key.

        Counting the questions *asked* instead would break the first rule: a
        redelivery would be recorded as a second, different question, and
        the audit trail would show one human being asked the same thing
        twice.
        """
        answered = sum(1 for q in questions if q.role == role.value and q.answered)
        return f"{_GATE_DISCRIMINATOR_PREFIX}:{role.value}:{answered + 1}"

    async def clear_human_gate(
        self,
        correlation_id: CorrelationId | uuid.UUID,
        *,
        approved: bool,
        resume_phase: TaskPhase | None = None,
        note: str | None = None,
    ) -> TaskProjection:
        """Record a human's decision on a parked task.

        Only a real caller reaches this. Nothing in the automated path can
        clear its own gate, which is what makes ``AWAITING_HUMAN_ATTESTATION``
        meaningful.

        The release names the question it answers — it carries that
        question's discriminator — so a lineage that asked two questions
        says which one the human ruled on. Without the name, "the gate" is
        ambiguous as soon as a second question is asked, and a release would
        answer whichever question happened to be last.

        A lineage holds at most one *open* question at a time — a stage does
        not ask again while its question is open — so the release answers the
        open one. If the projection says the task is waiting but the lineage
        holds no open question, the release is refused: there is no gate to
        clear, and inventing one would be a lie the ledger would record.

        The resume phase is the phase the task was working towards when it
        parked, unless the caller says otherwise: the context builder parks
        during hydration, so its task resumes at ``PLANNING``, and the
        policy agent parks during the review, so its task resumes at
        ``EXECUTING``. That is the release path agreeing with the phase map
        in ``ledger/replay.py`` instead of asserting a phase the task had
        not reached.
        """
        state = await project(self._ledger, correlation_id)
        if state.phase is not TaskPhase.AWAITING_HUMAN:
            raise PaaError(
                "task is not awaiting human attestation",
                correlation_id=str(correlation_id),
                phase=state.phase.value,
            )

        cid = uuid.UUID(str(correlation_id))
        questions = await self.gate_history(cid)
        question = next((q for q in reversed(questions) if not q.answered), None)
        if question is None:
            # The projection says the task is waiting, but the lineage holds
            # no open question. Refuse rather than release a gate nobody
            # asked for.
            raise PaaError(
                "task is not awaiting human attestation",
                correlation_id=str(correlation_id),
                phase=state.phase.value,
            )

        phase = resume_phase or _GATE_RESUME_PHASES.get(question.role, TaskPhase.EXECUTING)
        await self.emit(
            cid,
            EventType.HUMAN_GATE_CLEARED if approved else EventType.HUMAN_GATE_REJECTED,
            session_id=uuid.UUID(state.session_id) if state.session_id else None,
            payload={
                "resume_phase": phase.value,
                "note": note,
                "gate": question.discriminator,
                "gate_role": question.role,
            },
            modality=state.modality,
            role=AgentRole.ORCHESTRATOR,
            attempt=state.attempts,
            discriminator=question.discriminator,
        )
        return await project(self._ledger, correlation_id)

    async def record_correction(
        self, correlation_id: CorrelationId | uuid.UUID, correction: str
    ) -> None:
        """Log a user correction — the primary signal the reflection engine reads."""
        state = await project(self._ledger, correlation_id)
        await self.emit(
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

    async def run_allocation(
        self, *, limit: int = 25, settings: AllocatorSettings | None = None
    ) -> AllocationResult:
        """Run one attention-allocation pass and return its result.

        PLAN:integration-fix:P007 (ADR-0036) — the only orchestrator
        callsite for the allocator. The loop itself lives in
        :mod:`paa.context.allocator` (claim ``workers:alloc``, score via
        the ADR-0012 formula, ledger event before notify, ack after
        persist); this method is its wiring: the store is the
        orchestrator's ledger, the settings are the process
        `AllocatorSettings`, and the imports are lazy so the orchestrator
        keeps no queue/allocator/settings import at module scope (the queue
        may be absent entirely, which the allocator treats as an underfed
        no-op). Placed at the end of the class: a new method mid-file would
        shift every `orchestrator.py:<line>` citation below it.

        Never blocks the gatherer: it claims, scores, emits, and returns
        an `AllocationResult` (``outcome`` is ``"disabled"`` when the
        operator has not enabled the loop, ``"underfed"`` when there is
        nothing to decide, else ``"allocated"``). The gatherer compiles
        packets with or without this method ever running. The ``settings`` arg wins.
        """
        from paa.config import get_settings
        from paa.context.allocator import allocate_attention

        return await allocate_attention(
            self._queue,
            self._ledger,
            settings=settings if settings is not None else get_settings().allocator,
            limit=limit,
        )


# DECISION: this second TYPE_CHECKING block sits at the end of the file
# because docs/ cites orchestrator.py:<line> over a hundred times and the
# docs-references lock pins every cited line non-blank — adding an import
# to the block at the top would shift every citation below it.
if TYPE_CHECKING:
    from paa.config import AllocatorSettings
