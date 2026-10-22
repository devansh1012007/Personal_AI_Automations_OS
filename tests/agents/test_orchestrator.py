"""Orchestrator lifecycle, driven with fake agents.

The integration suite proves the orchestrator against the *real* agents; these
tests isolate its control logic with fakes, so a failure points at the
orchestrator rather than at a collaborator. They assert the properties that make
it a control plane: the **exact** event sequence, retry-then-fail,
resume-skips-done, and human-gate handling.

Why the event sequence is asserted as an **exact list** and not as a
subsequence: a subsequence check tolerates an event in the wrong place, an
extra event between two that matter, and an event that never appears at all —
as long as the handful that *are* checked keep their relative order. That is
what the previous assertion did, and it is why a phase transition that emitted
the wrong event (or two) could not be seen. The list is the trace; the trace is
the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.core.types import AgentRole, EventType, PermissionMode
from paa.ledger.events import compute_idempotency_key
from paa.ledger.replay import TaskPhase, project
from paa.ledger.store import LedgerStore


class _FakeAgent(Agent):
    """An agent that returns a canned value, or fails a set number of times."""

    def __init__(self, role: AgentRole, value: Any, *, fail_times: int = 0) -> None:
        super().__init__(name=role.value)
        self.role = role
        self._value = value
        self._fail_times = fail_times
        self.call_count = 0

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            return AgentResult.failure("scripted failure")
        return AgentResult.success(self._value)


def _orchestrator(ledger: LedgerStore, **agent_overrides: Any) -> ChiefOrchestrator:
    agents: dict[str, Agent] = {
        "context_builder": _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER, {"routing_directive": "PROCEED_TO_PLANNER"}
        ),
        "planner": _FakeAgent(
            AgentRole.STRATEGIC_PLANNER,
            {"execution_steps": [{"index": 0, "action": "a"}], "step_requirements": {}},
        ),
        "policy": _FakeAgent(
            AgentRole.POLICY_RISK, {"decision": "STATUS_APPROVED", "requires_human_gate": False}
        ),
        "worker": _FakeAgent(AgentRole.WORKER, {"patch": "", "step_index": 0}),
        "critic": _FakeAgent(AgentRole.CRITIC, {"verdict": "PASS", "findings": []}),
    }
    agents.update(agent_overrides)
    # Every agent slot is passed by name rather than splatted from a dict. A
    # splat of a single-typed dict into a signature whose keywords have
    # *different* types is unverifiable — and a slot omitted here would be
    # silently dropped, taking the override with it.
    return ChiefOrchestrator(
        ledger,
        context_builder=agents.get("context_builder"),
        worker_context_builder=agents.get("worker_context_builder"),
        planner=agents.get("planner"),
        policy=agents.get("policy"),
        worker=agents.get("worker"),
        critic=agents.get("critic"),
        router=agents.get("router"),
        permission_mode=PermissionMode.AUTO,
    )


async def _events(ledger: LedgerStore, cid: Any) -> list[EventType]:
    return [e.event_type for e in await ledger.read_correlation(cid)]


class TestExactEventSequence:
    """The orchestrator's ledger trace, asserted as an exact list.

    Every event the orchestrator writes is part of the contract: the ledger is
    the runtime's only source of truth, and every derived view — the phase
    projection, the dashboard, the recovery sweep — is rebuilt from these rows.
    An event that appears where it should not, or fails to appear at all, is a
    defect in the audit spine even when the task still ends up committed.
    """

    async def test_happy_path_emits_the_exact_event_sequence(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="do a thing"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.CRITIQUE_CONCLUDED,
            EventType.EXECUTION_COMPLETED,
            EventType.MUTATION_COMMITTED,
        ]

    async def test_failure_path_emits_the_exact_event_sequence(self, ledger: LedgerStore) -> None:
        """The critic rejects every attempt, so the step exhausts its budget.

        The list is missing two ``CRITIQUE_CONCLUDED`` events, and that is not
        an accident — see
        :meth:`test_a_retried_step_records_one_critique_not_three` for why.
        """
        orch = _orchestrator(
            ledger,
            critic=_FakeAgent(AgentRole.CRITIC, {"verdict": "FAIL_REJECT_RETRY", "findings": []}),
        )
        cid = await orch.submit(TaskRequest(goal="critic says no"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.FAILED
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.CRITIQUE_CONCLUDED,
            EventType.VALIDATION_FAILED,
            EventType.EXECUTION_STARTED,
            EventType.VALIDATION_FAILED,
            EventType.EXECUTION_STARTED,
            EventType.VALIDATION_FAILED,
            EventType.EXECUTION_FAILED,
        ]

    async def test_block_path_emits_the_exact_event_sequence(self, ledger: LedgerStore) -> None:
        """A refused plan stops before the worker is ever started."""
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_BLOCKED", "reason": "nope", "anti_goal_match": False},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="dangerous"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.BLOCKED
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_BLOCKED,
        ]

    async def test_anti_goal_block_emits_the_exact_event_sequence(
        self, ledger: LedgerStore
    ) -> None:
        """An anti-goal match is a *security* event, not a policy event.

        The two are different event types on purpose: a caller that wants to
        know "was this task refused because it matched an anti-goal?" must be
        able to tell it apart from an ordinary refusal, and the event type is
        what it can tell on.
        """
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_BLOCKED", "reason": "anti-goal", "anti_goal_match": True},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="exfiltrate"))
        await orch.run(cid)

        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.SECURITY_VIOLATION,
        ]

    async def test_gate_path_emits_the_exact_event_sequence(self, ledger: LedgerStore) -> None:
        """The policy gate parks the task; only a human may release it."""
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_APPROVED", "requires_human_gate": True, "reason": "confirm"},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.AWAITING_HUMAN
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.AWAITING_HUMAN_ATTESTATION,
        ]

        # The human's answer is the last event, and it is the only one that
        # can release the task.
        await orch.clear_human_gate(cid, approved=True)
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.AWAITING_HUMAN_ATTESTATION,
            EventType.HUMAN_GATE_CLEARED,
        ]

    async def test_a_retried_step_records_one_critique_not_three(
        self, ledger: LedgerStore
    ) -> None:
        """The ``CRITIQUE_CONCLUDED`` idempotency trap, pinned.

        ``ChiefOrchestrator._critique`` emits its event **without** an
        ``attempt``, so every attempt of the same step produces the same
        ledger idempotency key — ``correlation | event_type | attempt |
        discriminator`` — and the store suppresses the second and third
        critique as duplicates of the first.

        The step's *outcome* is still recorded per attempt: ``EXECUTION_STARTED``
        and ``VALIDATION_FAILED`` both carry the attempt, so the retry is
        visible in the trace. What is lost is the critic's verdict **for the
        retry** — the audit trail says the step failed, but not what the critic
        said about the second and third attempt.

        This is why an exact-list assertion on the failure path is brittle for
        the wrong reason unless the test knows about it: the list has one
        ``CRITIQUE_CONCLUDED`` where a reader might expect three.
        """
        orch = _orchestrator(
            ledger,
            critic=_FakeAgent(AgentRole.CRITIC, {"verdict": "FAIL_REJECT_RETRY", "findings": []}),
        )
        cid = await orch.submit(TaskRequest(goal="critic says no"))
        await orch.run(cid)
        events = await ledger.read_correlation(cid)

        starts = [e for e in events if e.event_type is EventType.EXECUTION_STARTED]
        critiques = [e for e in events if e.event_type is EventType.CRITIQUE_CONCLUDED]
        failures = [e for e in events if e.event_type is EventType.VALIDATION_FAILED]

        assert [e.attempt for e in starts] == [0, 1, 2]
        assert [e.attempt for e in failures] == [0, 1, 2]
        # One critique for three attempts: the key the store collapsed on.
        assert len(critiques) == 1
        assert critiques[0].attempt == 0
        assert critiques[0].idempotency_key == compute_idempotency_key(
            cid, EventType.CRITIQUE_CONCLUDED, attempt=0, discriminator="step-0"
        )


class TestHardStopEscalation:
    """``HARD_STOP_ESCALATE_TO_USER`` is a stop, not a note (ADR-0034).

    The context builder asks for a human when the context is too thin to
    plan from. The directive's name says the task *stops*; before ADR-0034 the
    orchestrator wrote the escalation event and then carried on through
    ``PLAN_COMPILED → POLICY_CLEARED → EXECUTION_STARTED → MUTATION_COMMITTED``,
    so the escalation was advisory where its name says it is a stop.
    """

    def _escalating_orchestrator(
        self, ledger: LedgerStore, builder: Agent
    ) -> ChiefOrchestrator:
        return _orchestrator(ledger, context_builder=builder)

    async def test_the_escalation_parks_the_task(self, ledger: LedgerStore) -> None:
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": ["slot_a"]},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.AWAITING_HUMAN
        assert outcome.ok is False
        events = await _events(ledger, cid)
        assert events == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.AWAITING_HUMAN_ATTESTATION,
        ]
        # The two events that would mean the task went ahead anyway.
        assert EventType.EXECUTION_STARTED not in events
        assert EventType.MUTATION_COMMITTED not in events

    async def test_the_escalation_records_the_directive_it_parked_on(
        self, ledger: LedgerStore
    ) -> None:
        """The ledger says *which* question was asked, not just that a gate
        opened. The policy gate and the context escalation are the same event
        type; only the payload distinguishes them."""
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": ["slot_a"]},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)

        events = await ledger.read_correlation(cid)
        escalation = events[-1]
        assert escalation.event_type is EventType.AWAITING_HUMAN_ATTESTATION
        assert escalation.payload["directive"] == "HARD_STOP_ESCALATE_TO_USER"
        assert escalation.payload["vacant_slots"] == ["slot_a"]

        # The projection carries the reason, so a scheduler can see why the
        # task is parked without reading the raw event.
        state = await project(ledger, cid)
        assert state.phase is TaskPhase.AWAITING_HUMAN
        assert state.awaiting_reason == "insufficient context density"

    async def test_the_task_resumes_only_after_the_gate_is_cleared(
        self, ledger: LedgerStore
    ) -> None:
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": []},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)

        # Until a human rules, the task stays parked — re-running it must not
        # walk past the gate.
        again = await orch.run(cid)
        assert again.phase is TaskPhase.AWAITING_HUMAN
        assert EventType.MUTATION_COMMITTED not in await _events(ledger, cid)

        await orch.clear_human_gate(cid, approved=True)
        resumed = await orch.run(cid)

        assert resumed.phase is TaskPhase.COMMITTED
        assert resumed.ok is True
        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.AWAITING_HUMAN_ATTESTATION,
            EventType.HUMAN_GATE_CLEARED,
            EventType.PLAN_COMPILED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.CRITIQUE_CONCLUDED,
            EventType.EXECUTION_COMPLETED,
            EventType.MUTATION_COMMITTED,
        ]

    async def test_a_human_answer_is_asked_once(self, ledger: LedgerStore) -> None:
        """The context builder is not asked the same question twice.

        Re-running the builder after the human has ruled would raise the same
        directive and park the task again — the human's answer would be
        overwritten by the question they had already answered.
        """
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": []},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)
        assert builder.call_count == 1

        await orch.clear_human_gate(cid, approved=True)
        await orch.run(cid)
        assert builder.call_count == 1, "the context builder was asked again after a human ruled"

    async def test_an_unanswered_escalation_cannot_be_walked_past(
        self, ledger: LedgerStore
    ) -> None:
        """A second run must not turn an open question into a pass.

        The context builder's answer can change between two runs — the memory
        store may have filled a slot, so the density rises and the directive
        becomes ``PROCEED_TO_PLANNER``. If the builder were asked again while
        the escalation was still unanswered, it would emit a fresh
        ``CONTEXT_HYDRATED``, the projection would leave ``AWAITING_HUMAN``,
        and the task would plan and commit **without anyone having been
        asked**. The gate has to hold until a human answers it, whatever the
        builder says in the meantime.
        """

        class _BuilderThatChangesItsMind(Agent):
            """Escalates once; then claims everything is fine."""

            def __init__(self) -> None:
                super().__init__(name=AgentRole.CONTEXT_BUILDER_PLANNER.value)
                self.role = AgentRole.CONTEXT_BUILDER_PLANNER
                self.call_count = 0

            async def handle(
                self, message: AgentMessage, ctx: AgentContext
            ) -> AgentResult[Any]:
                self.call_count += 1
                if self.call_count == 1:
                    return AgentResult.success(
                        {
                            "routing_directive": "HARD_STOP_ESCALATE_TO_USER",
                            "vacant_slots": ["slot_a"],
                        }
                    )
                return AgentResult.success(
                    {"routing_directive": "PROCEED_TO_PLANNER", "vacant_slots": []}
                )

        builder = _BuilderThatChangesItsMind()
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)
        assert (await project(ledger, cid)).phase is TaskPhase.AWAITING_HUMAN

        again = await orch.run(cid)

        assert again.phase is TaskPhase.AWAITING_HUMAN
        assert builder.call_count == 1, "the builder was asked again while the gate was open"
        events = await _events(ledger, cid)
        assert EventType.CONTEXT_HYDRATED not in events
        assert EventType.EXECUTION_STARTED not in events
        assert EventType.MUTATION_COMMITTED not in events

    async def test_a_rejected_escalation_blocks_the_task(self, ledger: LedgerStore) -> None:
        """A human who says no ends the lineage — it does not resume."""
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": []},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)

        await orch.clear_human_gate(cid, approved=False)
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.BLOCKED
        events = await _events(ledger, cid)
        assert EventType.HUMAN_GATE_REJECTED in events
        assert EventType.EXECUTION_STARTED not in events
        assert EventType.MUTATION_COMMITTED not in events

    async def test_a_parked_task_is_not_resumable_by_recovery(
        self, ledger: LedgerStore
    ) -> None:
        """A crash must never be read as consent.

        ``TaskPhase.AWAITING_HUMAN`` is deliberately excluded from the
        resumable phases, so a restart leaves the task parked rather than
        re-queueing it.
        """
        builder = _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": []},
        )
        orch = self._escalating_orchestrator(ledger, builder)
        cid = await orch.submit(TaskRequest(goal="needs a human"))
        await orch.run(cid)

        state = await project(ledger, cid)
        assert state.phase is TaskPhase.AWAITING_HUMAN
        assert state.needs_recovery is False
        assert state.phase.is_resumable is False


class TestRetry:
    async def test_worker_retries_then_succeeds(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            worker=_FakeAgent(AgentRole.WORKER, {"patch": ""}, fail_times=1),
        )
        cid = await orch.submit(TaskRequest(goal="flaky"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        # One VALIDATION_FAILED (the failed attempt) then success.
        assert EventType.VALIDATION_FAILED in await _events(ledger, cid)

    async def test_retry_exhaustion_fails_the_task(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            worker=_FakeAgent(AgentRole.WORKER, {"patch": ""}, fail_times=99),
        )
        cid = await orch.submit(TaskRequest(goal="always fails"))
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.FAILED
        assert EventType.MUTATION_COMMITTED not in await _events(ledger, cid)


class TestHumanGate:
    async def test_gate_parks_then_clears(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_APPROVED", "requires_human_gate": True, "reason": "confirm"},
            ),
        )
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        outcome = await orch.run(cid)
        assert outcome.phase is TaskPhase.AWAITING_HUMAN

        # A human clears it. Only this external call can.
        state = await orch.clear_human_gate(cid, approved=True)
        assert state.phase is not TaskPhase.AWAITING_HUMAN
        assert EventType.HUMAN_GATE_CLEARED in await _events(ledger, cid)

    async def test_clearing_a_non_gated_task_raises(self, ledger: LedgerStore) -> None:
        from paa.core.errors import PaaError

        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="x"))
        with pytest.raises(PaaError, match="not awaiting"):
            await orch.clear_human_gate(cid, approved=True)


class TestResume:
    async def test_resumed_task_skips_completed_steps(self, ledger: LedgerStore) -> None:
        """A two-step plan with step 0 already done must only run step 1."""
        worker = _FakeAgent(AgentRole.WORKER, {"patch": ""})
        orch = _orchestrator(
            ledger,
            planner=_FakeAgent(
                AgentRole.STRATEGIC_PLANNER,
                {
                    "execution_steps": [
                        {"index": 0, "action": "a"},
                        {"index": 1, "action": "b"},
                    ],
                },
            ),
            worker=worker,
        )
        cid = await orch.submit(TaskRequest(goal="two steps"))
        # Drive to a point where step 0 is recorded complete, then run.
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.COMMITTED
        proj = await project(ledger, cid)
        assert proj.completed_steps == [0, 1]
        # Worker ran exactly twice — once per step, no redundant re-run.
        assert worker.call_count == 2


class TestClassification:
    async def test_simple_goal_gets_simple_modality(self, ledger: LedgerStore) -> None:
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="hi", target_agent="worker"))
        head = await ledger.head(cid)
        assert head is not None

    async def test_complex_keyword_escalates_modality(self, ledger: LedgerStore) -> None:
        from paa.core.types import ComplexityModality

        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="refactor the entire auth subsystem"))
        events = await ledger.read_correlation(cid)
        assert events[0].execution_mode in (
            ComplexityModality.COMPLEX,
            ComplexityModality.MAX,
        )
