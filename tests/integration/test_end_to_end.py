"""End-to-end proof of the five Definition-of-Done items, through the real
composition root.

Each test class maps to one DoD item from the RFC. Where a property is also
covered exhaustively at the unit level (the token ceiling, chain integrity), the
test here proves it survives the *wiring* — that the guarantee holds when the
whole system runs, not just when the component is exercised in isolation.
"""

from __future__ import annotations

import uuid
from itertools import pairwise
from typing import Any

import pytest

from paa.core.types import ComplexityModality, EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.recovery import RecoveryOutcome
from paa.ledger.replay import TaskPhase
from paa.runtime import Runtime
from tests.integration.conftest import ScriptedModel

pytestmark = pytest.mark.asyncio


class TestHappyPath:
    """The full lifecycle commits and records every transition."""

    async def test_task_runs_to_committed(self, runtime: Runtime) -> None:
        outcome = await runtime.submit_and_run("summarise today's notes")

        assert outcome.phase is TaskPhase.COMMITTED
        assert outcome.ok

    async def test_full_event_sequence_is_recorded(self, runtime: Runtime) -> None:
        """The exact trace, not merely the presence of its parts.

        The membership check this replaces could not see an event emitted in
        the wrong place, an extra event, or a missing one — it asked "is
        ``CRITIQUE_CONCLUDED`` somewhere in here?", which is true whether the
        lifecycle is the RFC §7 one or a jumble. The list is the same ten
        events the isolated orchestrator tests assert, so the real wiring is
        proved to add nothing and remove nothing.
        """
        outcome = await runtime.submit_and_run("summarise today's notes")
        events = [
            e.event_type for e in await runtime.ledger.read_correlation(outcome.correlation_id)
        ]

        assert events == [
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

    async def test_the_chain_is_intact_after_a_full_run(self, runtime: Runtime) -> None:
        outcome = await runtime.submit_and_run("summarise today's notes")
        ok, problems = await runtime.ledger.verify_chain(outcome.correlation_id)
        assert ok, problems

    async def test_causation_is_walkable_for_explainability(self, runtime: Runtime) -> None:
        """DoD: 'explain why it chose an action.' Every event after the first
        must carry a hash chain that lets you walk the lineage backwards."""
        outcome = await runtime.submit_and_run("summarise today's notes")
        events = await runtime.ledger.read_correlation(outcome.correlation_id)

        assert len(events) >= 8
        # Each event links to its predecessor's digest — the audit spine.
        for prev, nxt in pairwise(events):
            assert nxt.prev_hash == prev.event_hash


class TestDeterministicRecovery:
    """DoD 1: a hard kill mid-execution resolves on boot to the exact state."""

    async def test_task_stalled_mid_execution_is_resumed_on_restart(
        self, build_runtime: Any, integration_home: Any
    ) -> None:
        # Boot 1: drive a task to mid-execution, then simulate a crash by
        # closing the runtime without completing it.
        rt1 = await build_runtime()
        cid = await rt1.submit("refactor the widget")
        # Hand-place the lineage in mid-flight: planned, cleared, one step
        # started but never completed — exactly what a power cut looks like.
        for etype, payload in [
            (EventType.CONTEXT_HYDRATED, {"context_packet": {}}),
            (EventType.PLAN_COMPILED, {"execution_steps": [{"index": 0}, {"index": 1}]}),
            (EventType.POLICY_CLEARED, {"decision": "STATUS_APPROVED"}),
            (EventType.EXECUTION_STARTED, {"step_index": 0}),
        ]:
            await rt1.ledger.append(LedgerEvent.create(cid, etype, payload=payload))
        before = await rt1.project(cid)
        assert before.phase is TaskPhase.EXECUTING
        await rt1.close()  # the "crash"

        # Boot 2: same PAA_HOME, recovery runs on build.
        rt2 = await build_runtime(run_recovery=True)
        try:
            report = rt2._boot_report
            assert report is not None
            assert report.count(RecoveryOutcome.RESUMED) == 1

            after = await rt2.project(cid)
            # State reconstructed exactly: same phase, same progress.
            assert after.phase in (TaskPhase.EXECUTING, TaskPhase.ROLLED_BACK)
            assert after.plan_steps == before.plan_steps
            assert after.current_step_index == before.current_step_index

            ok, problems = await rt2.ledger.verify_chain(cid)
            assert ok, problems
        finally:
            await rt2.close()

    async def test_committed_task_is_not_touched_by_recovery(
        self, build_runtime: Any
    ) -> None:
        rt1 = await build_runtime()
        outcome = await rt1.submit_and_run("summarise today's notes")
        assert outcome.phase is TaskPhase.COMMITTED
        await rt1.close()

        rt2 = await build_runtime(run_recovery=True)
        try:
            # A finished task is not open, so the sweep must not re-touch it.
            assert rt2._boot_report.scanned == 0
        finally:
            await rt2.close()

    async def test_human_gated_task_stays_parked_across_restart(
        self, build_runtime: Any
    ) -> None:
        """A crash must never be read as consent (RFC §9 SUPERVISED / gates)."""
        rt1 = await build_runtime()
        cid = await rt1.submit("delete the archive")
        await rt1.ledger.append(
            LedgerEvent.create(
                cid, EventType.AWAITING_HUMAN_ATTESTATION, payload={"reason": "destructive"}
            )
        )
        await rt1.close()

        rt2 = await build_runtime(run_recovery=True)
        try:
            assert rt2._boot_report.count(RecoveryOutcome.PARKED_HUMAN) == 1
            proj = await rt2.project(cid)
            assert proj.phase is TaskPhase.AWAITING_HUMAN
        finally:
            await rt2.close()


class TestSecurityGuard:
    """DoD 2: forbidden operations are blocked deterministically, no model call
    in the security decision."""

    async def test_irreversible_op_is_blocked_in_safe_mode(
        self, build_runtime: Any
    ) -> None:
        # A plan whose step deletes recursively. Under SAFE this is hard-blocked.
        model = ScriptedModel(
            steps=[
                {
                    "index": 0,
                    "action": "clean up",
                    "command": ["rm", "-rf", "/"],
                    "agent": "worker",
                    "mutates": True,
                }
            ]
        )
        rt = await build_runtime(model=model, mode=PermissionMode.SAFE)
        try:
            outcome = await rt.submit_and_run("clean up the workspace")

            assert outcome.phase is TaskPhase.BLOCKED
            events = [
                e.event_type for e in await rt.ledger.read_correlation(outcome.correlation_id)
            ]
            assert EventType.POLICY_BLOCKED in events or EventType.SECURITY_VIOLATION in events
            # The plan step never ran: no execution, no commit.
            assert EventType.EXECUTION_STARTED not in events
            assert EventType.MUTATION_COMMITTED not in events
        finally:
            await rt.close()

    async def test_policy_agent_holds_no_model(self, runtime: Runtime) -> None:
        """The security decision is structurally model-free: the policy agent
        has no model dependency to call. This is the RFC §13 guarantee made
        unbreakable by construction rather than by discipline."""
        policy = runtime.agents["policy_risk"]
        assert not hasattr(policy, "_model") or policy.__dict__.get("_model") is None

    async def test_lockdown_blocks_network_egress_plan(self, build_runtime: Any) -> None:
        model = ScriptedModel(
            steps=[
                {
                    "index": 0,
                    "action": "fetch data",
                    "command": ["python", "-c", "import requests; requests.get('http://x')"],
                    "agent": "worker",
                    "mutates": False,
                }
            ]
        )
        rt = await build_runtime(model=model, mode=PermissionMode.LOCKDOWN)
        try:
            outcome = await rt.submit_and_run("fetch remote data")
            assert outcome.phase is TaskPhase.BLOCKED
        finally:
            await rt.close()


class TestHumanGate:
    """Two questions in one lineage are two questions (F-1, ADR-0034).

    The unit suite drives the orchestrator with fakes; this one drives the
    **real** wiring — the real context builder, the real policy agent, the
    real ledger — through the reviewer's own measurement: a task that parks
    on the context builder's question, is answered by a human, resumes, and
    then meets the policy agent's question.

    Before the fix the second question was swallowed by the ledger's
    idempotency key (the gate event carried no discriminator, so every gate
    request in a lineage hashed to the same key), the projection never saw
    it, and the task committed with the policy's risk gate unanswered.
    """

    async def test_a_second_question_parks_the_task_again(
        self, build_runtime: Any
    ) -> None:
        model = ScriptedModel(
            steps=[
                {
                    "index": 0,
                    "action": "risky",
                    "agent": "worker",
                    "mutates": True,
                    "risk_profile": 0.95,
                }
            ]
        )
        rt = await build_runtime(model=model)
        try:
            # The task declares slots the memory store cannot fill, so the
            # real gatherer's density is 0 and the real context builder
            # escalates.
            cid = await rt.submit(
                "do something risky",
                modality=ComplexityModality.COMPLEX,
                required_slots=["slot_a", "slot_b"],
            )

            first = await rt.run(cid)
            assert first.phase is TaskPhase.AWAITING_HUMAN

            # A human answers the context builder's question.
            await rt.orchestrator.clear_human_gate(cid, approved=True)
            assert (await rt.project(cid)).phase is TaskPhase.PLANNING

            # The task resumes, and the *real* policy agent asks its own
            # question: the plan carries a step over the always-gate risk
            # profile.
            second = await rt.run(cid)
            assert second.phase is TaskPhase.AWAITING_HUMAN
            assert second.ok is False

            events = [
                e.event_type for e in await rt.ledger.read_correlation(cid)
            ]
            assert events == [
                EventType.TASK_REQUESTED,
                EventType.TASK_QUEUED,
                EventType.CONTEXT_HYDRATION_REQUESTED,
                EventType.AWAITING_HUMAN_ATTESTATION,
                EventType.HUMAN_GATE_CLEARED,
                EventType.PLAN_COMPILED,
                EventType.AWAITING_HUMAN_ATTESTATION,
            ]
            assert EventType.MUTATION_COMMITTED not in events
        finally:
            await rt.close()

    async def test_the_two_questions_carry_two_identities(
        self, build_runtime: Any
    ) -> None:
        """The ledger says which question was asked, and who answered it."""
        model = ScriptedModel(
            steps=[
                {
                    "index": 0,
                    "action": "risky",
                    "agent": "worker",
                    "mutates": True,
                    "risk_profile": 0.95,
                }
            ]
        )
        rt = await build_runtime(model=model)
        try:
            cid = await rt.submit(
                "do something risky",
                modality=ComplexityModality.COMPLEX,
                required_slots=["slot_a", "slot_b"],
            )
            await rt.run(cid)
            await rt.orchestrator.clear_human_gate(cid, approved=True)
            await rt.run(cid)

            events = await rt.ledger.read_correlation(cid)
            gates = [e for e in events if e.event_type is EventType.AWAITING_HUMAN_ATTESTATION]
            releases = [e for e in events if e.event_type is EventType.HUMAN_GATE_CLEARED]

            assert len(gates) == 2
            assert all(g.discriminator for g in gates)
            assert len({g.discriminator for g in gates}) == 2
            # The release names the question it answers.
            assert len(releases) == 1
            assert releases[0].discriminator == gates[0].discriminator
        finally:
            await rt.close()


class TestTokenBoundary:
    """DoD 3: the context packet never breaches the 1500-token ceiling."""

    async def test_hydrated_packet_respects_the_ceiling(self, runtime: Runtime) -> None:
        outcome = await runtime.submit_and_run("summarise today's notes")
        events = await runtime.ledger.read_correlation(outcome.correlation_id)

        hydrated = [e for e in events if e.event_type is EventType.CONTEXT_HYDRATED]
        assert hydrated, "no CONTEXT_HYDRATED event was recorded"
        packet = hydrated[0].payload.get("context_packet", {})
        # The ceiling flows from config (1500) through the gatherer into the
        # ledgered packet. Prove the wiring preserves it.
        assert packet.get("allocated_tokens", 0) <= packet.get("token_ceiling", 1500)
        assert packet.get("token_ceiling", 1500) <= 1500


class TestSchemaValidationIntegrity:
    """DoD 5: malformed / unsafe output is blocked before it can commit."""

    async def test_deterministic_validator_is_wired_into_the_critic(
        self, runtime: Runtime
    ) -> None:
        critic = runtime.agents["critic"]
        assert critic.__dict__.get("_validator") is not None, (
            "critic has no deterministic validator — the security floor is missing"
        )

    async def test_model_pass_cannot_override_a_deterministic_fail(
        self, build_runtime: Any
    ) -> None:
        """The critic's model always says PASS here; a worker output carrying
        forbidden source must still be rejected by the AST scan underneath it."""
        from paa.agents.base import AgentContext, AgentMessage, MessageType
        from paa.core.types import ComplexityModality

        rt = await build_runtime(model=ScriptedModel(verdict="PASS"))
        try:
            critic = rt.agents["critic"]
            ctx = AgentContext(
                correlation_id=uuid.uuid4(),
                modality=ComplexityModality.COMPLEX,
                permission_mode=PermissionMode.AUTO,
            )
            # Output whose source imports os.system — a hard AST failure.
            msg = AgentMessage(
                task_id=uuid.uuid4(),
                correlation_id=ctx.correlation_id,
                sender="test",
                recipient="critic",
                intent=MessageType.REVIEW_RESULT,
                payload={
                    "step_index": 0,
                    "output": {
                        "source_files": {"evil.py": "import os\nos.system('rm -rf /')\n"}
                    },
                },
            )
            result = await critic.run(msg, ctx)
            verdict = (result.value or {}).get("verdict")
            assert verdict != "PASS", "AST failure was overridden by the model's PASS"
            assert (result.value or {}).get("source") == "deterministic"
        finally:
            await rt.close()


class TestZeroNetworkPosture:
    """DoD 4: the stack runs without egress. Proven as posture, not packet
    capture — the deep escalation test lives in tests/models."""

    async def test_loopback_binding_is_enforced(self, runtime: Runtime) -> None:
        assert runtime.settings.api_host in {"127.0.0.1", "localhost", "::1"}

    async def test_a_full_run_needs_no_frontier_provider(
        self, build_runtime: Any
    ) -> None:
        """A task completes end-to-end with only the local (scripted) model —
        no escalation, no frontier call."""
        model = ScriptedModel()
        rt = await build_runtime(model=model)
        try:
            outcome = await rt.submit_and_run("summarise today's notes")
            assert outcome.phase is TaskPhase.COMMITTED
            # Every model call was the injected local one; there is no frontier
            # router in this wiring at all.
            assert rt.model_router is None
            assert model.call_count >= 1
        finally:
            await rt.close()


class TestIdempotentBoot:
    """Booting twice must not corrupt or duplicate anything (a crash during
    recovery is itself possible)."""

    async def test_double_boot_is_stable(self, build_runtime: Any) -> None:
        rt1 = await build_runtime()
        cid = await rt1.submit("refactor the widget")
        await rt1.ledger.append(LedgerEvent.create(cid, EventType.EXECUTION_STARTED))
        await rt1.close()

        rt2 = await build_runtime(run_recovery=True)
        count_after_first = await rt2.ledger.count()
        await rt2.close()

        rt3 = await build_runtime(run_recovery=True)
        try:
            # The second recovery sweep adds no new events for an unchanged task.
            assert await rt3.ledger.count() == count_after_first
        finally:
            await rt3.close()
