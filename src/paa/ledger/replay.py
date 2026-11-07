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
from paa.memory.domains import TierPolicy

__all__ = ["TaskPhase", "TaskProjection", "apply_event", "apply_tier_event", "project", "replay"]

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


#: The default tier for any fact whose history predates the tier layer.
#: PLAN:integration-fix:P009 — the v5 migration backfills existing rows to T2
#: (ADR-0037), and any event lacking tier fields defaults the fact it names
#: to T2, so old lineages replay identically.
DEFAULT_TIER: str = "T2"


def apply_tier_event(state: dict[str, str], event: LedgerEvent) -> dict[str, str]:
    """Fold one event into the tier-dict projection, returning a new dict.

    PLAN:integration-fix:P009 — ADR-0037's decided shape: tier state is fact-
    scoped and keyed by ``fact_id``, so it lives in this dict, never in
    ``TaskProjection`` (task-lineage state folds the wrong entity; ADR-0037
    rejects that explicitly).

    Pure: ``state`` is not mutated; the caller must assign the return value
    (mirrors :func:`apply_event`). Tier writes come only from tier events:

    - ``TIER_DEMOTED`` / ``TIER_ARCHIVED`` set ``state[fact_id]`` to the
      payload ``to_tier`` validated against the canonical tier vocabulary
      (:meth:`~paa.memory.domains.TierPolicy.resolve` — sharing that one
      validator is what keeps the rebuilt tier identical to the live column,
      whose CHECK forbids any tier outside ``T0``-``T4``; violations of the
      ``{fact_id, from_tier, to_tier, reason}`` contract in
      :class:`~paa.core.types.EventType` fall back to :data:`DEFAULT_TIER`,
      never a raise);
    - any other event naming a ``fact_id`` defaults that fact to
      :data:`DEFAULT_TIER` *only if the fact has no tier yet* — the
      old-events path (lineages written before the tier existed never emitted
      tier fields). A non-tier event never *overwrites* an existing tier, so
      e.g. an archived fact is never resurrected to T2 by an unrelated event
      that happens to carry its ``fact_id``.

    Events without a usable ``fact_id`` (all task-lineage events, plus a
    falsy ``fact_id`` — ``None``, ``""``, ``0``, ``False`` — or a
    non-scalar one such as a list or dict) return an unchanged copy, so a
    malformed payload never becomes a junk dict key. The fold is total: it
    never raises, so replay over any stream — hostile, malformed, or
    future — completes. In particular
    :func:`replay` over a stream containing ``TIER_*`` never records them in
    ``TaskProjection.unknown_events`` (they are members of
    ``_NO_PHASE_CHANGE``) and never raises.

    Union precedence (ADR-0037: "retention decides survival, ``prune_floor``
    decides hot-set membership") is *sweeper policy* — the decision of whether
    a tier TTL expiry may move a fact, and how it composes with the curator's
    DECAY phase. It is deliberately NOT implemented here: this helper only
    records where a fact's tier is; the W3 sweeper is the sole writer of tier
    moves and the sole place the composition must live.
    """
    updated = dict(state)
    payload = event.payload
    fact_id = payload.get("fact_id")
    # Falsy fact_ids are treated as absent, and a non-scalar fact_id (a
    # list, dict, ...) is malformed input — both are a no-op rather than a
    # junk dict key. ``bool`` is excluded explicitly because it subclasses
    # ``int`` and ``str(True)`` would be a junk key. Scalar ids (strings,
    # and integer ids coerced for compatibility with older payloads) still
    # fold normally. Totality holds either way: this never raises.
    if (
        not fact_id
        or isinstance(fact_id, bool)
        or not isinstance(fact_id, (str, int, float))
    ):
        return updated
    key = str(fact_id)
    to_tier = payload.get("to_tier")
    if event.event_type in (EventType.TIER_DEMOTED, EventType.TIER_ARCHIVED):
        # Validate against the canonical vocabulary — a `to_tier` outside
        # T0-T4 (`"T9"`, `"not-a-tier"`, `""`, a non-string) falls back to
        # T2, exactly as TierPolicy resolves it, so the rebuilt tier can
        # never hold a value the DB CHECK forbids. A fresh instance per
        # call: resolve() only reads the tier set, and TierPolicy's table
        # is mutable (R-W2-006, do-later), so no shared instance is kept.
        updated[key] = TierPolicy().resolve(to_tier if isinstance(to_tier, str) else None)
    elif key not in updated:
        # Any other event naming a fact defaults an unseen fact to T2.
        updated[key] = DEFAULT_TIER
    return updated


#: Events that carry information but do not move the lifecycle phase.
#:
#: PLAN:integration-fix:P009 — TIER_DEMOTED / TIER_ARCHIVED are fact-scoped,
#: not task phases (ADR-0037). They live here, never in `_PHASE_MAP`, and
#: never in TERMINAL_EVENTS: a tier move neither closes a task lineage nor
#: advances its phase. The tier state itself is folded by `apply_tier_event`
#: above, keyed by fact_id — TaskProjection gains no tier fields.
_NO_PHASE_CHANGE = frozenset(
    {
        EventType.HUMAN_GATE_CLEARED,
        EventType.VALIDATION_FAILED,
        EventType.USER_CORRECTION,
        EventType.MODEL_ESCALATED,
        EventType.RECOVERY_REPLAY_COMPLETED,
        EventType.TIER_DEMOTED,
        EventType.TIER_ARCHIVED,
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
