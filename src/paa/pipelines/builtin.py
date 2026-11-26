"""The built-in plan-execute-critique pipeline, extracted from the orchestrator.

P1.2 of docs/plans/2026-11-21-pipeline-platform-spec.md: the strategy that
used to live inline in :meth:`paa.agents.orchestrator.ChiefOrchestrator.run`
— hydrate, plan, policy, execute steps with critique and retry, commit —
moves here, verbatim, behind the facade P1.1 froze. The orchestrator remains
the *host*: it owns submission, backpressure, recovery, gates, delegation and
failure recording, and it mints the facade this pipeline runs on.

The behaviour-preservation gate is the full suite passing unchanged: the
events, their order, their payloads, the idempotency keys of the gate and
critique requests, and the stale-``attempts`` semantics of the failure path
are all pinned by tests, and this module reproduces them by carrying the old
stage bodies across untouched — only the *reach* changes: agent calls,
plan submission, clearance requests, gates and the commit now travel through
:class:`BuiltinPipelineFacade`, which is the in-process form of ADR-0051's
typed-calls-only door.

Two shapes in this module are deliberately in-process-only (the RPC
transport of P3 replaces them): the :class:`PipelineHost` protocol — the
host services the facade rides on — and the facade's transport reads
(``agent_ctx``, ``stage_question``, ``project``). The wire transport
re-derives projections host-side between calls, which is also why
:meth:`BuiltinPlanExecuteCritique.run` writes its live projection back onto
the per-run task at exactly the points the old inline ``run()`` reassigned
its local: the host's failure path must read the same (deliberately stale)
``attempts`` count the old code read at the moment a stage raised.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult, MessageType
from paa.core.errors import PaaError, RecursionGuardError
from paa.core.types import (
    MODALITY_PROFILES,
    AgentRole,
    ComplexityModality,
    EventType,
    Permission,
    PermissionMode,
)
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskPhase, TaskProjection
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    GateQuestion,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.base import CapabilityFacade as _FacadeProtocol

__all__ = [
    "BuiltinPipelineFacade",
    "BuiltinPlanExecuteCritique",
    "PipelineHost",
    "StageAgents",
    "SubRunExecutor",
    "risk_score_of",
]

log = structlog.get_logger(__name__)

#: What the built-in pipeline asks for. ``think``/``use_skill``/``recall``/
#: ``remember``/``sub_run`` are absent on purpose: its agents own their models,
#: its memory writes are the agents' own, and it never composes — every ask
#: here is one the facade grants and the stages exercise. The manager
#: (``builtin.manager``, C8.2) mints its facade with a *different* grant set
#: — ``sub_run`` and a host-provided executor — through the same class's
#: ``capabilities``/``sub_runs`` parameters, so composition reuses this door
#: instead of growing a second one.
_BUILTIN_CAPABILITIES = frozenset(
    {
        Capability.CALL_AGENT,
        Capability.GATE,
        Capability.PLAN_SUBMIT,
        Capability.POLICY_CHECK,
        Capability.COMMIT,
    }
)

#: The roles that dispatch to a stage agent. Everything else is a refusal, so
#: a mis-stated role cannot silently reach an arbitrary registered agent.
_STAGE_ROLES: dict[AgentRole, str] = {
    AgentRole.CONTEXT_BUILDER_PLANNER: "context_builder",
    AgentRole.STRATEGIC_PLANNER: "planner",
    AgentRole.POLICY_RISK: "policy",
    AgentRole.WORKER: "worker",
    AgentRole.CRITIC: "critic",
}


def risk_score_of(payload: dict[str, Any]) -> float | None:
    """The risk score a payload recorded, if it recorded one.

    ``None`` means **not recorded**, which is not the same as zero. A verdict
    that never computed a score, and a lineage written before the score was
    carried, both say "the number is not in the record" — and writing ``0.0``
    for them would put a number there that nobody measured. A decision
    without the number it decided about is half a record; a record with an
    invented number in it is worse.
    """
    value = payload.get("risk_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class PipelineHost(Protocol):
    """The host services the built-in pipeline's facade rides on.

    :class:`~paa.agents.orchestrator.ChiefOrchestrator` implements this
    surface today — every method it names already exists there — and the
    protocol exists for the same reason ``BackpressureLike`` does: the
    pipeline package must not import the orchestrator (the orchestrator
    imports the pipeline), and the contract the pipeline depends on is
    stated where it is depended on.

    The RPC transport (P3) replaces exactly this protocol: emit, message,
    project, gates become host-side operations behind the wire, and the
    pipeline stops being able to reach them directly.
    """

    @property
    def name(self) -> str: ...

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
    ) -> LedgerEvent: ...

    def message(
        self,
        state: TaskProjection,
        ctx: AgentContext,
        intent: MessageType,
        payload: dict[str, Any],
    ) -> AgentMessage: ...

    async def project(self, correlation_id: uuid.UUID) -> TaskProjection: ...

    async def open_gate(
        self, ctx: AgentContext, role: AgentRole, payload: dict[str, Any]
    ) -> TaskProjection: ...

    async def gate_history(self, correlation_id: uuid.UUID) -> list[GateQuestion]: ...

    def stage_question(
        self, questions: list[GateQuestion], role: AgentRole
    ) -> GateQuestion | None: ...


class SubRunExecutor(Protocol):
    """What a host provides to execute one composed child run (C8.2, layer 4).

    The composition engine lives in :mod:`paa.pipelines.compose` — the
    budget halving, the recursion guard, the grants-from-the-child's-own-
    declaration, the call-journal rows — and this protocol is the seam it
    rides on: the facade types its ``sub_runs`` parameter against it, the
    same way ``PipelineHost`` types the host services, so the pipeline
    package's dependency stays one-way (builtin does not import compose;
    compose imports builtin). A host that mints a facade granting
    ``SUB_RUN`` provides the executor; every other facade leaves it unset
    and ``sub_run`` refuses with the grant error, which is the discipline.
    """

    async def run_child(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        """Run one named pipeline to a terminal result (the composer's contract)."""
        ...


@dataclass(slots=True)
class StageAgents:
    """The built-in's stage slots: who plays each stage.

    P1.5's slot-addressable stages: an overlay swaps a stage by replacing a
    field here (see ``tests/pipelines/test_builtin.py``'s overlay test), with
    the same trust as the component it replaces. A ``None`` stage is how the
    orchestrator has always meant "skip this stage" — the guards read it
    directly, exactly as the old inline guards read ``self._planner``.
    """

    context_builder: Agent | None = None
    planner: Agent | None = None
    policy: Agent | None = None
    worker: Agent | None = None
    critic: Agent | None = None


class BuiltinPipelineFacade:
    """The facade the host mints for one built-in run — ADR-0051's door.

    Every capability method is the old orchestrator operation with the host
    owning the ledger write: ``call_agent`` is the old direct agent call,
    ``submit_plan`` is the old ``PLAN_COMPILED`` emission (with the node
    ceiling check the host is supposed to own), ``check_policy`` is the whole
    old policy stage, ``gate`` is ``open_gate``, ``commit`` is the old
    ``MUTATION_COMMITTED`` emission. Ungranted capabilities refuse with
    :class:`CapabilityNotGrantedError` — the built-in asks for exactly the
    five it uses, so a stage that grew a new reach would fail loudly rather
    than silently widening.

    In-process, the grant is correlation, not containment (ADR-0051): the
    built-in is first-party trusted code, which is also why its
    stage-transition events (``CONTEXT_HYDRATED``, ``EXECUTION_STARTED``, …)
    are emitted by the host's ``emit`` on the pipeline's behalf — an
    out-of-process pipeline gets no such method.
    """

    def __init__(
        self,
        host: PipelineHost,
        stages: StageAgents,
        agent_ctx: AgentContext,
        *,
        capabilities: frozenset[Capability] | None = None,
        sub_runs: SubRunExecutor | None = None,
    ) -> None:
        self._host = host
        self._stages = stages
        self._agent_ctx = agent_ctx
        # The default ask is the built-in's five; a composing run (the
        # manager, C8.2) passes its own set — ``sub_run`` plus whatever
        # else its declaration asked for — because the grant set is the
        # pipeline's declaration, never the host's generosity.
        self._grants = CapabilitySet.mint(
            capabilities if capabilities is not None else _BUILTIN_CAPABILITIES
        )
        # The layer-4 executor: present exactly when the host minted a
        # composing facade. Absent, sub_run refuses by grant first (the
        # default path) — the door and the key are separate disciplines.
        self._sub_runs = sub_runs
        # Seeded from the run's correlation id (not a fresh uuid4, which
        # would make every minted facade — including the shim's — a different
        # "run"): a re-run of the same lineage replays the same values, which
        # is what ADR-0050's determinism contract asks of it. A tiny LCG
        # rather than the stdlib PRNG, because determinism-per-run is the
        # opposite of what ``random`` is vetted for.
        self._rng_state = int(agent_ctx.correlation_id.hex[:16], 16)

    # -- run scope ---------------------------------------------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._agent_ctx.correlation_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return self._agent_ctx.session_id

    @property
    def modality(self) -> ComplexityModality:
        return self._agent_ctx.modality

    @property
    def permission_mode(self) -> PermissionMode:
        return self._agent_ctx.permission_mode

    @property
    def tokens_budget(self) -> int:
        return self._agent_ctx.tokens_budget

    @property
    def agent_ctx(self) -> AgentContext:
        return self._agent_ctx

    # -- capabilities --------------------------------------------------------

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        """The built-in asks for no model access: its agents own their models."""
        self._grants.require(Capability.THINK)
        raise CapabilityNotGrantedError(Capability.THINK.value)  # pragma: no cover

    async def call_agent(
        self, role: AgentRole, message: AgentMessage, agent_ctx: AgentContext
    ) -> AgentResult[Any]:
        """Reuse a built-in agent role — the old direct stage call."""
        self._grants.require(Capability.CALL_AGENT)
        agent: Agent | None = getattr(self._stages, _STAGE_ROLES.get(role, ""), None)
        if agent is None:
            return AgentResult.failure(f"no {role.value} stage agent is configured")
        return await agent.run(message, agent_ctx)

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> str:
        """The built-in asks for no skills: its worker's tools are the worker's own."""
        self._grants.require(Capability.USE_SKILL)
        raise CapabilityNotGrantedError(Capability.USE_SKILL.value)  # pragma: no cover

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        self._grants.require(Capability.RECALL)
        raise CapabilityNotGrantedError(Capability.RECALL.value)  # pragma: no cover

    async def remember(self, fact: Mapping[str, Any]) -> None:
        self._grants.require(Capability.REMEMBER)
        raise CapabilityNotGrantedError(Capability.REMEMBER.value)  # pragma: no cover

    async def gate(self, role: AgentRole, payload: Mapping[str, Any]) -> TaskProjection:
        """Ask a human; parks the run and returns the parked projection."""
        self._grants.require(Capability.GATE)
        return await self._host.open_gate(self._agent_ctx, role, dict(payload))

    async def submit_plan(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        requirements: Mapping[str, Any] | None = None,
        bypassed_planner: bool = False,
    ) -> None:
        """Offer a plan; the host validates size and emits ``PLAN_COMPILED``.

        The node ceiling is checked on the planner's output only, because
        that is where the old stage checked it — the SIMPLE bypass emits a
        single synthesised step that cannot exceed any modality's ceiling,
        and checking it there would be a behaviour change wearing a
        validation costume.
        """
        self._grants.require(Capability.PLAN_SUBMIT)
        if not bypassed_planner:
            profile = MODALITY_PROFILES[self._agent_ctx.modality]
            if len(steps) > profile.max_plan_nodes():
                raise RecursionGuardError(
                    "plan exceeds the expanded-node ceiling for this modality",
                    depth=len(steps),
                    ceiling=profile.max_plan_nodes(),
                )
        payload: dict[str, Any] = {"execution_steps": list(steps)}
        if bypassed_planner:
            payload["bypassed_planner"] = True
        else:
            # Passed through as-is (None included), exactly as the old stage
            # put ``plan.get("step_requirements", {})`` into the payload:
            # a planner that explicitly returned ``None`` recorded ``null``
            # then and records ``null`` now.
            payload["step_requirements"] = requirements
        await self._host.emit(
            self._agent_ctx.correlation_id,
            EventType.PLAN_COMPILED,
            session_id=self._agent_ctx.session_id,
            payload=payload,
            modality=self._agent_ctx.modality,
            role=AgentRole.STRATEGIC_PLANNER,
        )

    async def check_policy(self, state: TaskProjection) -> TaskProjection:
        """Request clearance for a plan — the whole old policy stage.

        The policy agent is host-side and model-free by design ("no
        generative reasoning in the control path"); a pipeline only *asks*.
        The body is the old ``ChiefOrchestrator._check_policy`` moved here
        with its reach swapped for facade calls, gate consults included.
        """
        if state.policy_decision is not None or self._stages.policy is None:
            return state

        questions = await self._host.gate_history(self._agent_ctx.correlation_id)
        gate = self._host.stage_question(questions, AgentRole.POLICY_RISK)
        if gate is not None:
            if gate.answered:
                # A human has ruled on this stage's question, and that ruling
                # *is* the policy decision: the plan is approved. The policy
                # agent is not asked again, because its second opinion is
                # not what the human approved — a policy agent that answered
                # differently on the second consult would either block a task
                # a human had approved, or (worse) wave through a plan the
                # human never saw. It would also ask the human the same
                # question twice, which is the loop ADR-0034 records.
                #
                # The decision is recorded with the same event the agent's own
                # approval uses, so the projection carries it and the stage is
                # skipped from then on. The payload says who decided: the
                # human, not the policy agent.
                #
                # It also carries **the number the policy computed**, when the
                # policy computed one. The human's answer is about a plan the
                # policy judged risky enough to gate, and a clearance that
                # records only "answered" leaves the audit trail unable to
                # answer "how risky was the plan a human approved?". The score
                # is read out of the *question* — the policy agent is not
                # consulted again, by design — so it is the lineage's own
                # number and not a number invented here.
                clearance: dict[str, Any] = {
                    "decision": "STATUS_APPROVED",
                    "reason": "human attestation granted",
                    "released_by": "human",
                    "gate": gate.discriminator,
                }
                if (score := risk_score_of(gate.payload)) is not None:
                    clearance["risk_score"] = score
                await self._host.emit(
                    self._agent_ctx.correlation_id,
                    EventType.POLICY_CLEARED,
                    session_id=self._agent_ctx.session_id,
                    payload=clearance,
                    modality=self._agent_ctx.modality,
                    role=AgentRole.POLICY_RISK,
                    discriminator=gate.discriminator,
                )
                return await self._host.project(self._agent_ctx.correlation_id)
            # The question is still open: nobody has answered it. The policy
            # agent is **not** asked again — a gate that is open is a gate
            # that is waiting, and a second opinion that disagreed with the
            # first would replace a question for a human with a block (or
            # worse, a clearance) that no human ever saw.
            log.info(
                "orchestrator.policy_gate_still_open",
                correlation_id=str(self._agent_ctx.correlation_id),
                reason=state.awaiting_reason,
            )
            return state

        message = self._host.message(
            state,
            self._agent_ctx,
            MessageType.POLICY_CHECK,
            {"steps": state.plan_steps, "goal": state.request.get("goal", "")},
        )
        result = await self.call_agent(AgentRole.POLICY_RISK, message, self._agent_ctx)
        verdict = result.value if isinstance(result.value, dict) else {}

        if not result.ok or verdict.get("decision") == "STATUS_BLOCKED":
            event = (
                EventType.SECURITY_VIOLATION
                if verdict.get("anti_goal_match")
                else EventType.POLICY_BLOCKED
            )
            await self._host.emit(
                self._agent_ctx.correlation_id,
                event,
                session_id=self._agent_ctx.session_id,
                payload={
                    "decision": "STATUS_BLOCKED",
                    "reason": verdict.get("reason") or "policy refused the plan",
                    **(result.error or {}),
                },
                modality=self._agent_ctx.modality,
                role=AgentRole.POLICY_RISK,
            )
            return await self._host.project(self._agent_ctx.correlation_id)

        if verdict.get("requires_human_gate"):
            # The gate's payload is the *question*, and a question the human
            # will answer has to say what it is about: the reason the plan
            # was gated and the score the policy gave it. The score is
            # carried here because the policy agent is not consulted again
            # after the gate opens — this is the only place the lineage can
            # learn it.
            gate_payload: dict[str, Any] = {
                "reason": verdict.get("reason", "human attestation required")
            }
            if (score := risk_score_of(verdict)) is not None:
                gate_payload["risk_score"] = score
            return await self.gate(AgentRole.POLICY_RISK, gate_payload)

        await self._host.emit(
            self._agent_ctx.correlation_id,
            EventType.POLICY_CLEARED,
            session_id=self._agent_ctx.session_id,
            payload={
                "decision": "STATUS_APPROVED",
                "risk_score": verdict.get("risk_score", 0.0),
            },
            modality=self._agent_ctx.modality,
            role=AgentRole.POLICY_RISK,
        )
        return await self._host.project(self._agent_ctx.correlation_id)

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        """Commit validated changes — the old ``MUTATION_COMMITTED`` emission.

        The clearance gating (ADR-0051: commit requires a ``POLICY_CLEARED``
        covering the plan hash) is enforceable against code the host did not
        write only once the journal lands (P2/P3); for the built-in, the
        policy stage having run is the same fact the old code relied on.
        """
        self._grants.require(Capability.COMMIT)
        await self._host.emit(
            self._agent_ctx.correlation_id,
            EventType.MUTATION_COMMITTED,
            session_id=self._agent_ctx.session_id,
            payload={
                "steps_completed": steps_completed,
                "tokens_consumed": tokens_consumed,
            },
            modality=self._agent_ctx.modality,
            role=AgentRole.ORCHESTRATOR,
        )
        log.info(
            "orchestrator.committed",
            correlation_id=str(self._agent_ctx.correlation_id),
            steps=steps_completed,
            tokens=tokens_consumed,
        )

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        """Run another pipeline — composition, layer 4 (C8.2).

        The built-in plan-execute-critique pipeline never composes (it is
        a worker), so the facade it runs on carries no ``SUB_RUN`` grant
        and this method refuses. The manager's facade — minted by the
        composition driver with ``sub_run`` granted and the host's
        executor wired in — delegates: the executor (see
        :mod:`paa.pipelines.compose`) resolves the child in the registry,
        halves the budget, guards the recursion, journals the call, runs
        the child to completion, and returns its result. A child's own
        failure comes back as the child's failed result; the composition
        contract's errors (guards, budgets, replay halts) raise.
        """
        self._grants.require(Capability.SUB_RUN)
        if self._sub_runs is None:
            raise CapabilityNotGrantedError(Capability.SUB_RUN.value)  # pragma: no cover
        return await self._sub_runs.run_child(name, task_input)

    async def now(self) -> datetime:
        """Recorded time — journalled when the journal lands (ADR-0050)."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """Seeded per run (the run id seeds it), so a re-run replays the same values."""
        self._rng_state = (self._rng_state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return self._rng_state / (1 << 64)

    async def progress(self, status: str) -> None:
        """Heartbeat; feeds the watchdog when one exists, never journalled."""
        log.debug(
            "pipeline.progress", correlation_id=str(self._agent_ctx.correlation_id), status=status
        )

    # -- in-process transport reads ------------------------------------------

    async def stage_question(self, role: AgentRole) -> GateQuestion | None:
        questions = await self._host.gate_history(self._agent_ctx.correlation_id)
        return self._host.stage_question(questions, role)

    async def project(self) -> TaskProjection:
        return await self._host.project(self._agent_ctx.correlation_id)


class BuiltinPlanExecuteCritique:
    """The built-in pipeline: hydrate → plan → policy → execute → commit.

    The stage bodies are the old ``ChiefOrchestrator`` stages moved verbatim
    (P1.2's gate: the suite passes unchanged); what changed is their reach —
    agents, plans, clearances, gates and the commit go through the facade
    the host minted, and the park checks that ADR-0034 pins live here, in
    the pipeline, between the stages that can park.
    """

    name = "builtin.plan-execute-critique"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = _BUILTIN_CAPABILITIES
    limits = RunLimits()

    def __init__(
        self,
        host: PipelineHost,
        *,
        stages: StageAgents,
        max_step_retries: int = 2,
    ) -> None:
        self._host = host
        self.stages = stages
        self._max_step_retries = max_step_retries

    async def run(self, task: PipelineTask, ctx: _FacadeProtocol) -> PipelineResult:
        """Drive one task to a terminal result, updating ``task.projection``.

        The projection writes happen at exactly the points the old inline
        ``run()`` reassigned its local ``state`` — after each stage returns —
        because the host's failure path reads ``task.projection.attempts``
        and must see the same (deliberately stale) count the old code saw
        when a stage raised. Per-step re-projection inside
        ``_execute_steps`` stays stage-local, as it was before.
        """
        state = task.projection if task.projection is not None else await ctx.project()

        state = await self._hydrate(state, ctx)
        task.projection = state
        # A stage may park the task for a human — the context builder
        # escalates when the context is too thin to plan from. The park
        # has to be honoured *here*, before the next stage runs: the
        # check that used to sit after ``_check_policy`` was the only
        # one, so an escalation raised earlier in the pipeline was
        # overwritten by the phase of the very next event and the task
        # carried on to ``MUTATION_COMMITTED`` (ADR-0034). Each stage
        # that can park is therefore followed by its own check.
        if state.phase is TaskPhase.AWAITING_HUMAN:
            return PipelineResult(
                status=PipelineStatus.NEEDS_HUMAN,
                summary=state.awaiting_reason or "parked awaiting human attestation",
            )
        state = await self._plan(state, ctx)
        task.projection = state
        state = await ctx.check_policy(state)
        task.projection = state
        if state.phase in (TaskPhase.BLOCKED, TaskPhase.AWAITING_HUMAN):
            if state.phase is TaskPhase.AWAITING_HUMAN:
                return PipelineResult(
                    status=PipelineStatus.NEEDS_HUMAN,
                    summary=state.awaiting_reason or "parked awaiting human attestation",
                )
            return PipelineResult(
                status=PipelineStatus.FAILED,
                summary=state.policy_reason or "policy refused the plan",
            )
        state = await self._execute_steps(state, ctx)
        task.projection = state
        if state.phase is TaskPhase.AWAITING_HUMAN:
            return PipelineResult(
                status=PipelineStatus.NEEDS_HUMAN,
                summary=state.awaiting_reason or "parked awaiting human attestation",
            )
        await ctx.commit(
            steps_completed=len(state.completed_steps),
            tokens_consumed=state.tokens_consumed,
        )
        state = await ctx.project()
        task.projection = state
        return PipelineResult(
            status=PipelineStatus.COMPLETED,
            summary=f"committed {len(state.completed_steps)} step(s)",
            metrics={
                "steps_completed": len(state.completed_steps),
                "tokens_consumed": state.tokens_consumed,
            },
        )

    async def _hydrate(self, state: TaskProjection, ctx: _FacadeProtocol) -> TaskProjection:
        agent_ctx = ctx.agent_ctx
        if state.context_packet is not None or self.stages.context_builder is None:
            return state

        gate = await ctx.stage_question(AgentRole.CONTEXT_BUILDER_PLANNER)
        if gate is not None:
            if gate.answered:
                # A human has already released this stage's question. Asking
                # the context builder again would raise the same directive,
                # and the task would park a second time on the answer a human
                # has already given — the escalation is a question, and the
                # human answered it (ADR-0034).
                return state
            # The question is still open: nobody has answered it. The task
            # stays parked, and the builder is **not** asked again — a
            # builder that answered differently this time (the memory store
            # may have filled a slot since) would otherwise emit a fresh
            # CONTEXT_HYDRATED and walk the task straight past a gate no
            # human has released.
            log.info(
                "orchestrator.escalation_still_open",
                correlation_id=str(agent_ctx.correlation_id),
                reason=state.awaiting_reason,
            )
            return state

        await self._host.emit(
            agent_ctx.correlation_id,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            session_id=agent_ctx.session_id,
            modality=agent_ctx.modality,
            role=AgentRole.CONTEXT_BUILDER_PLANNER,
        )

        message = self._host.message(
            state,
            agent_ctx,
            MessageType.CONTEXT_REQUEST,
            {
                "goal": state.request.get("goal", ""),
                "required_slots": state.request.get("required_slots", []),
            },
        )
        result = await ctx.call_agent(
            AgentRole.CONTEXT_BUILDER_PLANNER, message, agent_ctx
        )
        if not result.ok:
            raise PaaError("context hydration failed", cause=result.error or {})

        packet = result.value if isinstance(result.value, dict) else {}
        if packet.get("routing_directive") == "HARD_STOP_ESCALATE_TO_USER":
            # The context builder is asking for a human, and the request is
            # a *stop*: the task waits here until a human rules. The event
            # records the directive verbatim so the ledger says which
            # question was asked, not merely that some gate opened
            # (ADR-0034).
            return await ctx.gate(
                AgentRole.CONTEXT_BUILDER_PLANNER,
                {
                    "reason": "insufficient context density",
                    "directive": "HARD_STOP_ESCALATE_TO_USER",
                    "vacant_slots": packet.get("vacant_slots", []),
                },
            )

        await self._host.emit(
            agent_ctx.correlation_id,
            EventType.CONTEXT_HYDRATED,
            session_id=agent_ctx.session_id,
            payload={"context_packet": packet},
            modality=agent_ctx.modality,
            role=AgentRole.CONTEXT_BUILDER_PLANNER,
        )
        return await self._host.project(agent_ctx.correlation_id)

    async def _plan(self, state: TaskProjection, ctx: _FacadeProtocol) -> TaskProjection:
        agent_ctx = ctx.agent_ctx
        if state.plan_steps or self.stages.planner is None:
            return state

        # SIMPLE bypasses the model entirely (RFC §9.2: "0 tokens").
        if agent_ctx.modality is ComplexityModality.SIMPLE:
            await ctx.submit_plan(
                [
                    {
                        "index": 0,
                        "action": state.request.get("goal", ""),
                        "agent": state.request.get("target_agent") or "worker",
                    }
                ],
                bypassed_planner=True,
            )
            return await self._host.project(agent_ctx.correlation_id)

        message = self._host.message(
            state,
            agent_ctx,
            MessageType.PLAN_PROPOSAL,
            {"goal": state.request.get("goal", ""), "context": state.context_packet or {}},
        )
        result = await ctx.call_agent(AgentRole.STRATEGIC_PLANNER, message, agent_ctx)
        if not result.ok:
            raise PaaError("planning failed", cause=result.error or {})

        plan = result.value if isinstance(result.value, dict) else {}
        steps = plan.get("execution_steps", [])
        await ctx.submit_plan(steps, requirements=plan.get("step_requirements", {}))
        return await self._host.project(agent_ctx.correlation_id)

    async def _execute_steps(
        self, state: TaskProjection, ctx: _FacadeProtocol
    ) -> TaskProjection:
        agent_ctx = ctx.agent_ctx
        if self.stages.worker is None:
            return state

        for index, step in enumerate(state.plan_steps):
            if index in state.completed_steps:
                continue  # resumed task: this step already landed

            for attempt in range(self._max_step_retries + 1):
                await self._host.emit(
                    agent_ctx.correlation_id,
                    EventType.EXECUTION_STARTED,
                    session_id=agent_ctx.session_id,
                    payload={"step_index": index, "step": step},
                    modality=agent_ctx.modality,
                    role=AgentRole.WORKER,
                    attempt=attempt,
                    discriminator=f"step-{index}",
                )

                message = self._host.message(
                    state, agent_ctx, MessageType.EXECUTION_REQUEST, {"step": step, "index": index}
                )
                result = await ctx.call_agent(AgentRole.WORKER, message, agent_ctx.child())

                if result.ok and await self._critique(state, ctx, index, result):
                    await self._host.emit(
                        agent_ctx.correlation_id,
                        EventType.EXECUTION_COMPLETED,
                        session_id=agent_ctx.session_id,
                        payload={
                            "step_index": index,
                            "tokens_consumed": result.tokens_consumed,
                        },
                        modality=agent_ctx.modality,
                        role=AgentRole.WORKER,
                        attempt=attempt,
                        discriminator=f"step-{index}",
                    )
                    break

                await self._host.emit(
                    agent_ctx.correlation_id,
                    EventType.VALIDATION_FAILED,
                    session_id=agent_ctx.session_id,
                    payload={
                        "step_index": index,
                        "attempt": attempt,
                        **(result.error or {"verdict": "FAIL_REJECT_RETRY"}),
                    },
                    modality=agent_ctx.modality,
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

            state = await self._host.project(agent_ctx.correlation_id)

        return state

    async def _critique(
        self, state: TaskProjection, ctx: _FacadeProtocol, index: int, result: AgentResult[Any]
    ) -> bool:
        """Run the critic over a step's output. Returns whether it passed."""
        agent_ctx = ctx.agent_ctx
        if self.stages.critic is None:
            return True

        message = self._host.message(
            state,
            agent_ctx,
            MessageType.REVIEW_RESULT,
            {"step_index": index, "output": result.value},
        )
        review = await ctx.call_agent(AgentRole.CRITIC, message, agent_ctx.child())
        verdict = (review.value or {}).get("verdict") if isinstance(review.value, dict) else None
        passed = bool(review.ok and verdict == "PASS")

        await self._host.emit(
            agent_ctx.correlation_id,
            EventType.CRITIQUE_CONCLUDED,
            session_id=agent_ctx.session_id,
            payload={"step_index": index, "verdict": verdict or "FAIL_REJECT_RETRY"},
            modality=agent_ctx.modality,
            role=AgentRole.CRITIC,
            discriminator=f"step-{index}",
        )
        return passed
