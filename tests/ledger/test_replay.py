"""Left-associative replay: determinism, purity, and phase derivation.

Replay is the mathematical core of recovery (RFC §1.5). If the fold is not
deterministic and total, every DoD guarantee that rests on it is void.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.core.types import ComplexityModality, EventType, new_correlation_id, new_session_id
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskPhase, TaskProjection, apply_event, project, replay
from paa.ledger.store import LedgerStore


def make(cid, etype: EventType, version: int = 1, **payload) -> LedgerEvent:
    """Build a sealed-looking event without touching the store."""
    return LedgerEvent(
        correlation_id=cid,
        event_type=etype,
        state_version=version,
        payload=payload,
    )


HAPPY_PATH = [
    (EventType.TASK_REQUESTED, {"request": {"goal": "refactor auth"}}),
    (EventType.TASK_QUEUED, {}),
    (EventType.CONTEXT_HYDRATED, {"context_packet": {"density": 0.92}}),
    (EventType.PLAN_COMPILED, {"execution_steps": [{"id": 0}, {"id": 1}]}),
    (EventType.POLICY_CLEARED, {"decision": "STATUS_APPROVED"}),
    (EventType.EXECUTION_STARTED, {"step_index": 0}),
    (EventType.EXECUTION_COMPLETED, {"step_index": 0, "tokens_consumed": 400}),
    (EventType.EXECUTION_STARTED, {"step_index": 1}),
    (EventType.EXECUTION_COMPLETED, {"step_index": 1, "tokens_consumed": 350}),
    (EventType.CRITIQUE_CONCLUDED, {"verdict": "PASS"}),
    (EventType.MUTATION_COMMITTED, {"applied_patch_sha256": "a" * 64}),
]


def build(cid, spec) -> list[LedgerEvent]:
    return [make(cid, et, i + 1, **pl) for i, (et, pl) in enumerate(spec)]


class TestPurity:
    def test_apply_event_does_not_mutate_input(self) -> None:
        cid = new_correlation_id()
        state = TaskProjection(correlation_id=str(cid))
        before = state.model_dump()

        apply_event(state, make(cid, EventType.PLAN_COMPILED, 1, execution_steps=[{"id": 0}]))

        assert state.model_dump() == before

    def test_replay_is_deterministic(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH)
        assert replay(events).model_dump() == replay(events).model_dump()

    def test_replay_is_associative_over_prefixes(self) -> None:
        """Folding all at once equals folding a prefix then the remainder.

        This is what makes snapshotting sound: a snapshot at version k plus
        events k+1..n must equal a full replay.
        """
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH)
        full = replay(events)
        staged = replay(events[6:], initial=replay(events[:6]))
        assert staged.model_dump() == full.model_dump()

    def test_unknown_event_does_not_abort(self) -> None:
        """Forward compatibility: an unrecognised type is recorded, not fatal."""
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:3])
        # RECOVERY_REPLAY_COMPLETED carries no phase mapping by design.
        events.append(make(cid, EventType.RECOVERY_REPLAY_COMPLETED, 4))
        state = replay(events)
        assert state.phase is TaskPhase.PLANNING  # unchanged by the extra event

    def test_empty_history_requires_initial(self) -> None:
        with pytest.raises(ValueError, match="empty history"):
            replay([])


class TestPhaseDerivation:
    def test_happy_path_reaches_committed(self) -> None:
        state = replay(build(new_correlation_id(), HAPPY_PATH))
        assert state.phase is TaskPhase.COMMITTED
        assert state.is_terminal
        assert state.completed_steps == [0, 1]
        assert state.tokens_consumed == 750
        assert state.applied_patch_sha256 == "a" * 64

    def test_partial_plan_stays_executing(self) -> None:
        """One of two steps done must NOT read as finished.

        Getting this wrong would make recovery skip the remaining work.
        """
        cid = new_correlation_id()
        state = replay(build(cid, HAPPY_PATH[:7]))
        assert state.phase is TaskPhase.EXECUTING
        assert state.completed_steps == [0]
        assert state.current_step_index == 1
        assert state.needs_recovery

    def test_policy_block_is_terminal_and_not_resumable(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:4])
        events.append(make(cid, EventType.POLICY_BLOCKED, 5, reason="anti-goal match"))
        state = replay(events)

        assert state.phase is TaskPhase.BLOCKED
        assert state.is_terminal
        assert not state.needs_recovery
        assert state.policy_reason == "anti-goal match"

    def test_awaiting_human_is_not_resumable(self) -> None:
        """A crash must never be interpretable as consent."""
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:5])
        events.append(make(cid, EventType.AWAITING_HUMAN_ATTESTATION, 6, reason="deletes files"))
        state = replay(events)

        assert state.phase is TaskPhase.AWAITING_HUMAN
        assert not state.phase.is_resumable
        assert state.awaiting_reason == "deletes files"

    def test_human_gate_clearing_resumes(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:5])
        events += [
            make(cid, EventType.AWAITING_HUMAN_ATTESTATION, 6, reason="x"),
            make(cid, EventType.HUMAN_GATE_CLEARED, 7),
        ]
        state = replay(events)
        assert state.phase is TaskPhase.EXECUTING
        assert state.awaiting_reason is None

    def test_validation_failure_is_retryable_not_fatal(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:7])
        events.append(make(cid, EventType.VALIDATION_FAILED, 8, verdict="FAIL_REJECT_RETRY"))
        state = replay(events)

        assert state.phase is TaskPhase.EXECUTING
        assert not state.is_terminal
        assert state.validation_verdict == "FAIL_REJECT_RETRY"
        assert len(state.errors) == 1


class TestRollbackSemantics:
    def test_rollback_rewinds_completed_steps(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:9])  # both steps complete
        events.append(
            make(
                cid,
                EventType.STATE_ROLLBACK_TRIGGERED,
                10,
                restored_step_index=1,
                restored_manifest_hash="b" * 64,
            )
        )
        state = replay(events)

        assert state.phase is TaskPhase.ROLLED_BACK
        assert state.completed_steps == [0]  # step 1 undone
        assert state.current_step_index == 1
        assert state.rollback_count == 1
        assert state.checkpoint_manifest_hash == "b" * 64

    def test_rollback_is_counted(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:6])
        for v in (7, 8):
            events.append(make(cid, EventType.STATE_ROLLBACK_TRIGGERED, v, restored_step_index=0))
        assert replay(events).rollback_count == 2


class TestCounters:
    def test_attempts_increment_per_execution_start(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:5])
        for v in (6, 7, 8):
            events.append(make(cid, EventType.EXECUTION_STARTED, v, step_index=0))
        assert replay(events).attempts == 3

    def test_user_corrections_and_escalations_counted(self) -> None:
        cid = new_correlation_id()
        events = build(cid, HAPPY_PATH[:4])
        events += [
            make(cid, EventType.USER_CORRECTION, 5),
            make(cid, EventType.USER_CORRECTION, 6),
            make(cid, EventType.MODEL_ESCALATED, 7, to="claude-sonnet-5"),
        ]
        state = replay(events)
        assert state.user_corrections == 2
        assert state.escalation_count == 1

    def test_session_captured_from_first_event_carrying_one(self) -> None:
        cid, sid = new_correlation_id(), new_session_id()
        events = [
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.TASK_REQUESTED,
                state_version=1,
                session_id=sid,
            )
        ]
        assert replay(events).session_id == str(sid)


class TestSnapshotIntegration:
    async def test_project_uses_snapshot(self, ledger: LedgerStore) -> None:
        cid = new_correlation_id()
        for etype, payload in HAPPY_PATH:
            await ledger.append(LedgerEvent.create(cid, etype, payload=payload))

        full = await project(ledger, cid, use_snapshot=False)
        await ledger.save_snapshot(cid, full.state_version, full.model_dump(mode="json"))

        from_snapshot = await project(ledger, cid, use_snapshot=True)
        assert from_snapshot.phase is full.phase
        assert from_snapshot.completed_steps == full.completed_steps

    async def test_corrupt_snapshot_falls_back_to_full_replay(self, ledger: LedgerStore) -> None:
        """A snapshot is a cache. An unreadable one must not break recovery."""
        cid = new_correlation_id()
        for etype, payload in HAPPY_PATH:
            await ledger.append(LedgerEvent.create(cid, etype, payload=payload))

        await ledger.save_snapshot(cid, 5, {"not_a": "valid projection", "phase": 12345})

        state = await project(ledger, cid, use_snapshot=True)
        assert state.phase is TaskPhase.COMMITTED  # full replay still correct


class TestProperties:
    @given(
        seq=st.lists(
            st.sampled_from(
                [
                    EventType.TASK_REQUESTED,
                    EventType.TASK_QUEUED,
                    EventType.CONTEXT_HYDRATED,
                    EventType.PLAN_COMPILED,
                    EventType.POLICY_CLEARED,
                    EventType.EXECUTION_STARTED,
                    EventType.EXECUTION_COMPLETED,
                    EventType.VALIDATION_FAILED,
                    EventType.USER_CORRECTION,
                    EventType.STATE_ROLLBACK_TRIGGERED,
                    EventType.MUTATION_COMMITTED,
                ]
            ),
            min_size=1,
            max_size=25,
        )
    )
    @hyp_settings(max_examples=150, deadline=None)
    def test_replay_never_raises_on_any_ordering(self, seq: list[EventType]) -> None:
        """Total function: recovery meets malformed histories after crashes."""
        cid = uuid.uuid4()
        events = [make(cid, et, i + 1) for i, et in enumerate(seq)]
        state = replay(events)
        assert isinstance(state.phase, TaskPhase)
        assert state.attempts >= 0
        assert state.tokens_consumed >= 0

    @given(st.integers(min_value=1, max_value=30))
    @hyp_settings(max_examples=40, deadline=None)
    def test_state_version_tracks_last_event(self, n: int) -> None:
        cid = uuid.uuid4()
        events = [make(cid, EventType.EXECUTION_STARTED, i + 1) for i in range(n)]
        assert replay(events).state_version == n

    def test_modality_is_carried_from_events(self) -> None:
        cid = new_correlation_id()
        ev = LedgerEvent(
            correlation_id=cid,
            event_type=EventType.TASK_REQUESTED,
            state_version=1,
            execution_mode=ComplexityModality.MAX,
        )
        assert replay([ev]).modality is ComplexityModality.MAX
