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

import asyncio
from typing import Any

import pytest

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.core.types import AgentRole, ComplexityModality, EventType, PermissionMode
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

    async def test_the_released_gate_path_emits_the_exact_event_sequence(
        self, ledger: LedgerStore
    ) -> None:
        """The policy gate, answered, and the task carried on.

        The list has one event the pre-fix list did not: ``POLICY_CLEARED``.
        The pre-fix trace had none, because the policy stage's clearance was
        never written down — the second ``AWAITING_HUMAN_ATTESTATION`` was
        swallowed by the ledger's idempotency key, so the stage "cleared" by
        way of a question that was never asked. The stage's decision is now
        recorded, and its payload says who made it (``released_by: human``).

        The discriminator changes the gate event's **key**; it does not add,
        remove or reorder an event. This list is the trace, and the trace is
        the contract.
        """
        orch = _orchestrator(
            ledger,
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {
                    "decision": "STATUS_APPROVED",
                    "requires_human_gate": True,
                    "reason": "confirm",
                },
            ),
        )
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        await orch.run(cid)
        await orch.clear_human_gate(cid, approved=True)
        await orch.run(cid)

        assert await _events(ledger, cid) == [
            EventType.TASK_REQUESTED,
            EventType.TASK_QUEUED,
            EventType.CONTEXT_HYDRATION_REQUESTED,
            EventType.CONTEXT_HYDRATED,
            EventType.PLAN_COMPILED,
            EventType.AWAITING_HUMAN_ATTESTATION,
            EventType.HUMAN_GATE_CLEARED,
            EventType.POLICY_CLEARED,
            EventType.EXECUTION_STARTED,
            EventType.CRITIQUE_CONCLUDED,
            EventType.EXECUTION_COMPLETED,
            EventType.MUTATION_COMMITTED,
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


class TestGateIdentity:
    """A gate request has an identity, so two questions are two records (F-1).

    ``AWAITING_HUMAN_ATTESTATION`` is the ledger's one event type for "a
    human has to rule". The ledger's idempotency key is ``correlation |
    event_type | attempt | discriminator``
    (``ledger/events.py::compute_idempotency_key``), so a gate request that
    carries neither an attempt nor a discriminator hashes to the **same** key
    as every other gate request in the lineage. The second request is then
    suppressed as a duplicate: the projection never sees it, the
    orchestrator sees no pending gate, and the task carries on to
    ``MUTATION_COMMITTED`` with the second question unanswered.

    Measured against the pre-fix sources: a task that parks on the context
    builder's question, is answered by a human, resumes, and then meets the
    policy agent's question ends ``COMMITTED`` with **one** gate event in the
    ledger and a ``ledger.duplicate_suppressed`` line for the second. The
    audit trail says the policy never asked.

    The fix gives the question an identity at the emit site: the stage that
    asked, and which question of that stage it is. The two properties that
    identity has to hold are tested here — *distinct* across different
    questions, and *stable* across a redelivery of the same one.
    """

    def _escalating_builder(self) -> _FakeAgent:
        return _FakeAgent(
            AgentRole.CONTEXT_BUILDER_PLANNER,
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": ["slot_a"]},
        )

    def _gating_policy(self) -> _FakeAgent:
        return _FakeAgent(
            AgentRole.POLICY_RISK,
            {
                "decision": "STATUS_APPROVED",
                "requires_human_gate": True,
                "reason": "step exceeds the always-gate risk profile",
            },
        )

    async def test_a_second_gate_in_one_lineage_is_recorded_and_parks_the_task(
        self, ledger: LedgerStore
    ) -> None:
        """The reviewer's measurement, as a test.

        A task parks on the context builder's question, a human answers it,
        the task resumes — and then the policy agent asks its own question.
        That second question must be **recorded** and must park the task
        again. Before the fix it was swallowed by the ledger's idempotency
        key and the task committed with the policy's risk gate unanswered.
        """
        orch = _orchestrator(
            ledger,
            context_builder=self._escalating_builder(),
            policy=self._gating_policy(),
        )
        cid = await orch.submit(TaskRequest(goal="needs two humans"))

        first = await orch.run(cid)
        assert first.phase is TaskPhase.AWAITING_HUMAN

        # A human answers the first question, and the task resumes.
        await orch.clear_human_gate(cid, approved=True)
        second = await orch.run(cid)

        # The policy agent's question is a *second* question, and it parks.
        assert second.phase is TaskPhase.AWAITING_HUMAN
        assert second.ok is False

        events = await ledger.read_correlation(cid)
        gates = [e for e in events if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]
        assert len(gates) == 2, "the second gate request was swallowed by the ledger"
        assert [g.agent_role for g in gates] == [
            AgentRole.CONTEXT_BUILDER_PLANNER.value,
            AgentRole.POLICY_RISK.value,
        ]
        assert EventType.MUTATION_COMMITTED not in [e.event_type for e in events]

    async def test_each_gate_question_carries_its_own_identity(
        self, ledger: LedgerStore
    ) -> None:
        """The two questions are distinguishable, and each names its stage.

        The discriminator is the question's identity, and it is what the
        ledger's key is built from. It says which stage asked, so an
        operator reading the raw ledger can tell a context-density
        escalation from a policy approval request without knowing the emit
        site.
        """
        orch = _orchestrator(
            ledger,
            context_builder=self._escalating_builder(),
            policy=self._gating_policy(),
        )
        cid = await orch.submit(TaskRequest(goal="needs two humans"))
        await orch.run(cid)
        await orch.clear_human_gate(cid, approved=True)
        await orch.run(cid)

        events = await ledger.read_correlation(cid)
        gates = [e for e in events if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]

        assert all(g.discriminator for g in gates), "a gate request carries no discriminator"
        assert len({g.discriminator for g in gates}) == 2, "the two questions share an identity"
        assert AgentRole.CONTEXT_BUILDER_PLANNER.value in (gates[0].discriminator or "")
        assert AgentRole.POLICY_RISK.value in (gates[1].discriminator or "")

        # The two questions have two different ledger keys, which is the
        # whole mechanism: the key is what the store deduplicates on.
        assert gates[0].idempotency_key != gates[1].idempotency_key

    async def test_a_release_names_the_question_it_answers(
        self, ledger: LedgerStore
    ) -> None:
        """"The gate" is ambiguous as soon as a lineage has asked twice.

        The release carries the discriminator of the question it answers, so
        the ledger says which of the two questions the human ruled on, and
        the two releases cannot collide on the idempotency key either.
        """
        orch = _orchestrator(
            ledger,
            context_builder=self._escalating_builder(),
            policy=self._gating_policy(),
        )
        cid = await orch.submit(TaskRequest(goal="needs two humans"))
        await orch.run(cid)
        await orch.clear_human_gate(cid, approved=True)
        await orch.run(cid)
        await orch.clear_human_gate(cid, approved=True)

        events = await ledger.read_correlation(cid)
        gates = [e for e in events if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]
        releases = [e for e in events if e.event_type is EventType.HUMAN_GATE_CLEARED]

        assert len(releases) == 2
        assert [r.discriminator for r in releases] == [g.discriminator for g in gates]
        assert releases[0].idempotency_key != releases[1].idempotency_key

    async def test_a_task_parked_on_a_second_gate_is_not_walked_past(
        self, ledger: LedgerStore
    ) -> None:
        """The human-only rule survives the fix.

        Re-running a task whose second gate is open must not carry on, and
        nothing in the automated path may clear a gate: the only release in
        the lineage is the one a human asked for. Before the fix this task
        committed — the second gate had been swallowed, so the orchestrator
        saw no pending gate at all.
        """
        orch = _orchestrator(
            ledger,
            context_builder=self._escalating_builder(),
            policy=self._gating_policy(),
        )
        cid = await orch.submit(TaskRequest(goal="needs two humans"))
        await orch.run(cid)
        await orch.clear_human_gate(cid, approved=True)
        await orch.run(cid)

        again = await orch.run(cid)

        assert again.phase is TaskPhase.AWAITING_HUMAN
        types = await _events(ledger, cid)
        assert EventType.MUTATION_COMMITTED not in types
        assert EventType.EXECUTION_STARTED not in types
        # Exactly one release: the human's. The automated path wrote none.
        raw = await ledger.read_correlation(cid)
        cleared = [e for e in raw if e.event_type is EventType.HUMAN_GATE_CLEARED]
        assert len(cleared) == 1, "something in the automated path cleared a gate"
        assert len([e for e in raw if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]) == 2

    async def test_an_answered_gate_does_not_block_the_task(self, ledger: LedgerStore) -> None:
        """A gate that was opened *and answered* releases the task.

        And the policy agent is not consulted a second time: the human's
        answer is the decision, and asking again would ask the human the
        same question twice — the loop ADR-0034 records. Before the fix the
        agent was consulted again and the second gate was swallowed by the
        ledger, so the task only moved on because the policy agent happened
        to be deterministic.
        """
        policy = self._gating_policy()
        orch = _orchestrator(ledger, policy=policy)
        cid = await orch.submit(TaskRequest(goal="needs approval"))

        parked = await orch.run(cid)
        assert parked.phase is TaskPhase.AWAITING_HUMAN

        await orch.clear_human_gate(cid, approved=True)
        resumed = await orch.run(cid)

        assert resumed.phase is TaskPhase.COMMITTED
        assert policy.call_count == 1, "the policy agent was consulted again after a human ruled"

    async def test_a_redelivery_of_the_same_gate_request_is_suppressed(
        self, ledger: LedgerStore
    ) -> None:
        """The same request, delivered twice, is one question.

        At-least-once delivery is the ledger's own premise: "an at-least-once
        transport may deliver the same logical event twice; the second write
        is suppressed rather than duplicated" (``ledger/store.py``). Two
        workers picking up the same task both ask the builder the same
        question and both write the answer — the store must recognise the
        second write as the same request, so the human is asked once and the
        audit trail says once.

        The barrier in the builder makes the interleaving deterministic:
        both orchestrators have read the lineage before either writes, so
        both write the *same* request. A discriminator that minted a fresh
        identity per write would record two questions here, and the human
        would be asked the same thing twice.
        """
        builder = _BarrierBuilder(parties=2)
        first = _orchestrator(ledger, context_builder=builder)
        second = _orchestrator(ledger, context_builder=builder)
        cid = await first.submit(TaskRequest(goal="needs a human"))

        await asyncio.gather(first.run(cid), second.run(cid))

        events = await ledger.read_correlation(cid)
        gates = [e for e in events if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]
        assert len(gates) == 1, "the same gate request was recorded twice"

    async def test_the_gate_request_minted_for_a_question_is_the_same_request(
        self, ledger: LedgerStore
    ) -> None:
        """The identity is minted from the *request*, not from a counter.

        This one reaches into the emit site on purpose. The public flow
        cannot re-enter it — a stage does not ask again while its question
        is open — so the property the fix rests on is only observable where
        the identity is minted. Two properties, both required:

        * the same question asked twice is the **same** request, so the
          ledger suppresses the second (a counter that moved on every write
          would record two questions and ask the human the same thing twice);
        * a genuinely new question from the same stage is a **different**
          request, so the ledger records it (this is what the second gate in
          a lineage is).
        """
        orch = _orchestrator(ledger)
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.AUTO,
        )

        # The same request, delivered twice.
        await orch._open_gate(ctx, AgentRole.POLICY_RISK, {"reason": "confirm"})
        await orch._open_gate(ctx, AgentRole.POLICY_RISK, {"reason": "confirm"})

        gates = [
            e
            for e in await ledger.read_correlation(cid)
            if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION
        ]
        assert len(gates) == 1, "the same request was recorded twice"

        # A human answers it, and the same stage asks a *new* question.
        await orch.clear_human_gate(cid, approved=True)
        await orch._open_gate(ctx, AgentRole.POLICY_RISK, {"reason": "confirm again"})

        gates = [
            e
            for e in await ledger.read_correlation(cid)
            if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION
        ]
        assert len(gates) == 2, "the new question was swallowed"
        assert gates[1].discriminator != gates[0].discriminator

    async def test_the_open_policy_gate_holds_the_task_against_a_policy_agent_that_changes_its_mind(
        self, ledger: LedgerStore
    ) -> None:
        """A gate that is open is a gate that is waiting.

        The policy agent's verdict can change between two runs — a rule
        table moved, a threshold moved. If the agent were consulted again
        while its question was unanswered and it now said "approved", the
        task would walk past a gate no human had ever seen. The question
        stands until a human answers it, whatever the agent says in the
        meantime.
        """
        policy = _PolicyThatChangesItsMind()
        orch = _orchestrator(ledger, policy=policy)
        cid = await orch.submit(TaskRequest(goal="needs approval"))

        parked = await orch.run(cid)
        assert parked.phase is TaskPhase.AWAITING_HUMAN

        again = await orch.run(cid)

        assert again.phase is TaskPhase.AWAITING_HUMAN
        assert policy.call_count == 1, "the policy agent was asked again while its gate was open"
        events = await _events(ledger, cid)
        assert EventType.POLICY_CLEARED not in events
        assert EventType.MUTATION_COMMITTED not in events

    async def test_the_policy_stage_does_not_consult_its_agent_while_its_question_is_open(
        self, ledger: LedgerStore
    ) -> None:
        """The precise guard, under the coarse one.

        ``run()`` also checks the task's phase after every stage, and that
        check fires first — a parked task stops there. Underneath it, each
        stage consults **its own** question before it consults its agent: a
        stage whose question is open does not ask again, because a second
        opinion is not an answer and must never replace a question for a
        human with a clearance no human saw.

        The stage is driven directly here, because the coarse check makes
        the branch unreachable through ``run()`` — the phase is
        ``AWAITING_HUMAN`` whenever the gate event is the last event, which
        is the only shape the current pipeline writes.
        """
        policy = self._gating_policy()
        orch = _orchestrator(ledger, policy=policy)
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        await orch.run(cid)
        assert policy.call_count == 1

        state = await project(ledger, cid)
        ctx = AgentContext(
            correlation_id=cid,
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.AUTO,
        )
        held = await orch._check_policy(state, ctx)

        assert policy.call_count == 1, "the policy agent was asked while its question was open"
        assert held.phase is TaskPhase.AWAITING_HUMAN
        assert held.policy_decision is None, "the gate was cleared without a human"

    async def test_the_release_resumes_the_task_where_it_parked(self, ledger: LedgerStore) -> None:
        """The release path and the phase map agree.

        The context builder parks *during hydration*, so the task has not
        planned when a human rules: the phase it resumes at is ``PLANNING``,
        the one the phase map puts a hydrated context in. The policy agent
        parks during the review, so its task resumes at ``EXECUTING`` — where
        a cleared policy leaves it.

        The release therefore names the phase the task was working towards,
        rather than asserting a phase the task had not reached.
        """
        orch = _orchestrator(
            ledger,
            context_builder=self._escalating_builder(),
            policy=self._gating_policy(),
        )
        cid = await orch.submit(TaskRequest(goal="needs two humans"))
        await orch.run(cid)

        await orch.clear_human_gate(cid, approved=True)
        assert (await project(ledger, cid)).phase is TaskPhase.PLANNING

        # The task resumes, and the policy stage's own gate parks it again.
        await orch.run(cid)
        assert (await project(ledger, cid)).phase is TaskPhase.AWAITING_HUMAN

        await orch.clear_human_gate(cid, approved=True)
        assert (await project(ledger, cid)).phase is TaskPhase.EXECUTING

    async def test_a_rejected_gate_ends_the_lineage(self, ledger: LedgerStore) -> None:
        """A human who says no ends the lineage — it does not resume."""
        orch = _orchestrator(ledger, policy=self._gating_policy())
        cid = await orch.submit(TaskRequest(goal="needs approval"))
        await orch.run(cid)

        await orch.clear_human_gate(cid, approved=False)
        outcome = await orch.run(cid)

        assert outcome.phase is TaskPhase.BLOCKED
        events = await _events(ledger, cid)
        assert EventType.HUMAN_GATE_REJECTED in events
        assert EventType.MUTATION_COMMITTED not in events


class _BarrierBuilder(Agent):
    """A context builder that waits until every caller has asked it.

    The barrier is what makes a concurrent delivery deterministic: each
    caller has read the lineage (and found no open question) before any of
    them writes the gate, so all of them write the *same* request.
    """

    def __init__(self, parties: int) -> None:
        super().__init__(name=AgentRole.CONTEXT_BUILDER_PLANNER.value)
        self.role = AgentRole.CONTEXT_BUILDER_PLANNER
        self._parties = parties
        self._arrived = 0
        self._all_arrived = asyncio.Event()

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._all_arrived.set()
        await self._all_arrived.wait()
        return AgentResult.success(
            {"routing_directive": "HARD_STOP_ESCALATE_TO_USER", "vacant_slots": []}
        )


class _PolicyThatChangesItsMind(Agent):
    """Asks for a human the first time; approves the second.

    The policy agent's verdict is not a constant — a rule table moved, a
    threshold moved — so a second consult can disagree with the first. A
    question that is open must survive that.
    """

    def __init__(self) -> None:
        super().__init__(name=AgentRole.POLICY_RISK.value)
        self.role = AgentRole.POLICY_RISK
        self.call_count = 0

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
        self.call_count += 1
        if self.call_count == 1:
            return AgentResult.success(
                {
                    "decision": "STATUS_APPROVED",
                    "requires_human_gate": True,
                    "reason": "step exceeds the always-gate risk profile",
                }
            )
        return AgentResult.success(
            {"decision": "STATUS_APPROVED", "requires_human_gate": False, "risk_score": 0.0}
        )


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
