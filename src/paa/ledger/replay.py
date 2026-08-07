"""Deterministic state projection by left-associative event replay.

Implements RFC §1.5:

.. math:: S(C_k)_t = \\bigoplus_{i=1}^{N} \\Delta s_i

where :math:`\\bigoplus` is :func:`apply_event` and :math:`\\Delta s_i` is the
*i*-th event of the lineage.

The fold must be **pure and total**: given the same events it must always
produce the same projection, and it must never raise on an event it does not
recognise. Recovery runs this against histories written by older code, so an
unknown event type is recorded and skipped rather than treated as fatal.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.core.types import ComplexityModality, EventType
from paa.ledger.events import LedgerEvent

__all__ = ["TaskPhase", "TaskProjection", "apply_event", "project", "replay"]

log = structlog.get_logger(__name__)


class TaskPhase(str, enum.Enum):
    """Coarse lifecycle position derived from the event history.

    Distinct from :class:`~paa.core.types.EventType`: many event types map to
    the same phase, and the phase is what schedulers and the dashboard care
    about.
    """

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    HYDRATING = "HYDRATING"
    PLANNING = "PLANNING"
    POLICY_REVIEW = "POLICY_REVIEW"
    EXECUTING = "EXECUTING"
    VALIDATING = "VALIDATING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ROLLED_BACK = "ROLLED_BACK"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PHASES

    @property
    def is_resumable(self) -> bool:
        """Whether recovery may safely re-queue a lineage in this phase.

        ``AWAITING_HUMAN`` is deliberately excluded: a task parked on a human
        gate must stay parked across a restart, or a crash would silently
        become consent.
        """
        return self in _RESUMABLE_PHASES


_TERMINAL_PHASES = frozenset(
    {TaskPhase.COMMITTED, TaskPhase.FAILED, TaskPhase.BLOCKED, TaskPhase.ABANDONED}
)
_RESUMABLE_PHASES = frozenset(
    {
        TaskPhase.CREATED,
        TaskPhase.QUEUED,
        TaskPhase.HYDRATING,
        TaskPhase.PLANNING,
        TaskPhase.POLICY_REVIEW,
        TaskPhase.EXECUTING,
        TaskPhase.VALIDATING,
        TaskPhase.ROLLED_BACK,
    }
)


class TaskProjection(BaseModel):
    """Computed current state of one task lineage.

    Never persisted as truth — always derived. Snapshots of this exist purely
    as a replay optimisation and are safe to delete.
    """

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    session_id: str | None = None
    phase: TaskPhase = TaskPhase.CREATED
    modality: ComplexityModality = ComplexityModality.STANDARD
    state_version: int = 0

    request: dict[str, Any] = Field(default_factory=dict)
    context_packet: dict[str, Any] | None = None
    plan_steps: list[dict[str, Any]] = Field(default_factory=list)
    current_step_index: int = 0
    completed_steps: list[int] = Field(default_factory=list)

    policy_decision: str | None = None
    policy_reason: str | None = None

    workspace_path: str | None = None
    checkpoint_manifest_hash: str | None = None
    """Digest of the workspace manifest at the last verified checkpoint. The
    recovery engine compares this against the live filesystem to detect drift."""

    applied_patch_sha256: str | None = None
    validation_verdict: str | None = None

    attempts: int = 0
    rollback_count: int = 0
    escalation_count: int = 0
    tokens_consumed: int = 0
    user_corrections: int = 0

    errors: list[dict[str, Any]] = Field(default_factory=list)
    awaiting_reason: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None
    unknown_events: list[str] = Field(default_factory=list)
    """Event types this build could not interpret. Non-fatal; surfaced so a
    forward-incompatible replay is visible rather than silently lossy."""

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    @property
    def needs_recovery(self) -> bool:
        return not self.is_terminal and self.phase.is_resumable


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------

#: Event types that map cleanly onto a phase with no other side effect.
_PHASE_MAP: dict[EventType, TaskPhase] = {
    EventType.TASK_REQUESTED: TaskPhase.CREATED,
    EventType.TASK_QUEUED: TaskPhase.QUEUED,
    EventType.CONTEXT_HYDRATION_REQUESTED: TaskPhase.HYDRATING,
    EventType.CONTEXT_HYDRATED: TaskPhase.PLANNING,
    EventType.PLAN_COMPILED: TaskPhase.POLICY_REVIEW,
    EventType.POLICY_CLEARED: TaskPhase.EXECUTING,
    EventType.EXECUTION_STARTED: TaskPhase.EXECUTING,
    EventType.CRITIQUE_CONCLUDED: TaskPhase.VALIDATING,
    EventType.EXECUTION_COMPLETED: TaskPhase.VALIDATING,
    EventType.MUTATION_COMMITTED: TaskPhase.COMMITTED,
    EventType.EXECUTION_FAILED: TaskPhase.FAILED,
    EventType.POLICY_BLOCKED: TaskPhase.BLOCKED,
    EventType.SECURITY_VIOLATION: TaskPhase.BLOCKED,
    EventType.HUMAN_GATE_REJECTED: TaskPhase.BLOCKED,
    EventType.TASK_ABANDONED: TaskPhase.ABANDONED,
    EventType.AWAITING_HUMAN_ATTESTATION: TaskPhase.AWAITING_HUMAN,
    EventType.STATE_ROLLBACK_TRIGGERED: TaskPhase.ROLLED_BACK,
}


def apply_event(state: TaskProjection, event: LedgerEvent) -> TaskProjection:
    """Apply one event to a projection, returning a new projection.

    Pure: ``state`` is not mutated. This is the :math:`\\bigoplus` operator.
    """
    updates: dict[str, Any] = {
        "state_version": event.state_version or state.state_version,
        "updated_at": event.recorded_at,
        "modality": event.execution_mode,
    }
    if state.created_at is None:
        updates["created_at"] = event.recorded_at
    if event.session_id and not state.session_id:
        updates["session_id"] = str(event.session_id)

    payload = event.payload
    etype = event.event_type

    if (phase := _PHASE_MAP.get(etype)) is not None:
        updates["phase"] = phase
    elif etype not in _NO_PHASE_CHANGE:
        # Forward compatibility: an unrecognised event must not abort replay.
        seen = list(state.unknown_events)
        if etype.value not in seen:
            seen.append(etype.value)
            updates["unknown_events"] = seen
        log.warning("replay.unknown_event", event_type=etype.value)

    # -- event-specific state -------------------------------------------
    match etype:
        case EventType.TASK_REQUESTED:
            updates["request"] = payload.get("request", payload)
            if wp := payload.get("workspace_path"):
                updates["workspace_path"] = wp

        case EventType.CONTEXT_HYDRATED:
            updates["context_packet"] = payload.get("context_packet")

        case EventType.PLAN_COMPILED:
            steps = payload.get("execution_steps", [])
            updates["plan_steps"] = steps
            updates["current_step_index"] = 0
            updates["completed_steps"] = []

        case EventType.POLICY_CLEARED | EventType.POLICY_BLOCKED | EventType.SECURITY_VIOLATION:
            updates["policy_decision"] = payload.get("decision", etype.value)
            updates["policy_reason"] = payload.get("reason")

        case EventType.EXECUTION_STARTED:
            updates["attempts"] = state.attempts + 1
            if (idx := payload.get("step_index")) is not None:
                updates["current_step_index"] = int(idx)
            if wp := payload.get("workspace_path"):
                updates["workspace_path"] = wp
            if mh := payload.get("checkpoint_manifest_hash"):
                updates["checkpoint_manifest_hash"] = mh

        case EventType.EXECUTION_COMPLETED:
            done = list(state.completed_steps)
            idx = int(payload.get("step_index", state.current_step_index))
            if idx not in done:
                done.append(idx)
            updates["completed_steps"] = done
            updates["current_step_index"] = idx + 1
            updates["tokens_consumed"] = state.tokens_consumed + int(
                payload.get("tokens_consumed", 0)
            )
            # A plan is only really finished once every step is done; otherwise
            # stay in EXECUTING so recovery resumes at the next step.
            if len(done) < len(state.plan_steps):
                updates["phase"] = TaskPhase.EXECUTING

        case EventType.EXECUTION_FAILED | EventType.VALIDATION_FAILED:
            errs = [*state.errors, {"event": etype.value, **payload}]
            updates["errors"] = errs
            if etype is EventType.VALIDATION_FAILED:
                updates["phase"] = TaskPhase.EXECUTING  # retryable, not fatal
                updates["validation_verdict"] = payload.get("verdict", "FAIL_REJECT_RETRY")

        case EventType.CRITIQUE_CONCLUDED:
            updates["validation_verdict"] = payload.get("verdict")

        case EventType.MUTATION_COMMITTED:
            updates["applied_patch_sha256"] = payload.get("applied_patch_sha256")
            updates["checkpoint_manifest_hash"] = payload.get(
                "checkpoint_manifest_hash", state.checkpoint_manifest_hash
            )

        case EventType.AWAITING_HUMAN_ATTESTATION:
            updates["awaiting_reason"] = payload.get("reason")

        case EventType.HUMAN_GATE_CLEARED:
            updates["awaiting_reason"] = None
            # Resume where the gate interrupted, defaulting to execution.
            updates["phase"] = TaskPhase(payload.get("resume_phase", TaskPhase.EXECUTING.value))

        case EventType.STATE_ROLLBACK_TRIGGERED:
            updates["rollback_count"] = state.rollback_count + 1
            updates["checkpoint_manifest_hash"] = payload.get(
                "restored_manifest_hash", state.checkpoint_manifest_hash
            )
            # Steps after the restore point are no longer done.
            restored_to = payload.get("restored_step_index")
            if restored_to is not None:
                keep = [s for s in state.completed_steps if s < int(restored_to)]
                updates["completed_steps"] = keep
                updates["current_step_index"] = int(restored_to)

        case EventType.USER_CORRECTION:
            updates["user_corrections"] = state.user_corrections + 1

        case EventType.MODEL_ESCALATED:
            updates["escalation_count"] = state.escalation_count + 1

        case _:
            pass

    return state.model_copy(update=updates)


#: Events that carry information but do not move the lifecycle phase.
_NO_PHASE_CHANGE = frozenset(
    {
        EventType.HUMAN_GATE_CLEARED,
        EventType.VALIDATION_FAILED,
        EventType.USER_CORRECTION,
        EventType.MODEL_ESCALATED,
        EventType.RECOVERY_REPLAY_COMPLETED,
    }
)


def replay(
    events: Iterable[LedgerEvent],
    *,
    initial: TaskProjection | None = None,
) -> TaskProjection:
    """Fold an event sequence into a projection.

    Events must already be ordered by ``state_version``; the store's
    ``read_correlation`` guarantees that.
    """
    events = list(events)
    if not events and initial is None:
        raise ValueError("cannot replay an empty history without an initial projection")

    state = initial or TaskProjection(correlation_id=str(events[0].correlation_id))
    for event in events:
        state = apply_event(state, event)
    return state


async def project(
    store: Any,
    correlation_id: Any,
    *,
    use_snapshot: bool = True,
) -> TaskProjection:
    """Load and replay a lineage from the store.

    ``store`` is a :class:`~paa.ledger.store.LedgerStore`; typed loosely to
    avoid a circular import between the store and the projector.
    """
    from_version = 0
    initial: TaskProjection | None = None

    if use_snapshot and (snap := await store.load_snapshot(correlation_id)):
        version, projection_data = snap
        try:
            initial = TaskProjection.model_validate(projection_data)
            from_version = version
        except Exception as exc:
            # A snapshot written by an older schema is a cache miss, not a
            # failure — fall back to replaying from genesis.
            log.warning("replay.snapshot_incompatible", error=str(exc))
            initial, from_version = None, 0

    events = await store.read_correlation(correlation_id, from_version=from_version)
    if not events and initial is not None:
        return initial
    return replay(events, initial=initial)
