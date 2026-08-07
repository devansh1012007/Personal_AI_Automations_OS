"""Core domain types for the cognitive runtime.

This module is the vocabulary every other subsystem imports. It has no
dependencies beyond the standard library and pydantic so that it can be
imported from anywhere without creating cycles.

Deviations from the v4.0 RFC are recorded inline and in ``docs/adr/``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Final, NewType

__all__ = [
    "MODALITY_PROFILES",
    "TERMINAL_EVENTS",
    "AgentRole",
    "ComplexityModality",
    "CorrelationId",
    "EventType",
    "ModalityProfile",
    "Permission",
    "PermissionMode",
    "SessionId",
    "TaskId",
    "new_correlation_id",
    "new_session_id",
    "new_task_id",
]

# ---------------------------------------------------------------------------
# Identifiers
#
# These are NewTypes over UUID rather than bare strings. The runtime threads a
# lot of different ids through the same call signatures (correlation, session,
# task, event); making them distinct types means mypy catches transposed
# arguments, which is the single most common bug class in event-sourced code.
# ---------------------------------------------------------------------------

CorrelationId = NewType("CorrelationId", uuid.UUID)
SessionId = NewType("SessionId", uuid.UUID)
TaskId = NewType("TaskId", uuid.UUID)


def new_correlation_id() -> CorrelationId:
    """Mint a correlation id — the lineage key for one task from request to commit."""
    return CorrelationId(uuid.uuid4())


def new_session_id() -> SessionId:
    """Mint a session id — the isolation boundary for one workspace."""
    return SessionId(uuid.uuid4())


def new_task_id() -> TaskId:
    return TaskId(uuid.uuid4())


# ---------------------------------------------------------------------------
# Ledger event vocabulary
# ---------------------------------------------------------------------------


class EventType(str, enum.Enum):
    """Every state transition the runtime can record.

    SPEC DEVIATION (docs/adr/0008): the RFC's ``system_state.transition_event``
    ENUM declares 10 members, but the RFC's own "Main User Flow" and
    self-improvement sections reference ``POLICY_CLEARED``,
    ``CRITIQUE_CONCLUDED``, ``MUTATION_COMMITTED`` and ``USER_CORRECTION``,
    none of which are in that ENUM. A ledger that cannot record the events its
    own happy path emits is not a ledger, so the vocabulary is completed here.

    Members are ordered along the canonical task lifecycle.
    """

    # -- intake ------------------------------------------------------------
    TASK_REQUESTED = "TASK_REQUESTED"
    TASK_QUEUED = "TASK_QUEUED"

    # -- context + planning ------------------------------------------------
    CONTEXT_HYDRATION_REQUESTED = "CONTEXT_HYDRATION_REQUESTED"
    CONTEXT_HYDRATED = "CONTEXT_HYDRATED"
    PLAN_COMPILED = "PLAN_COMPILED"

    # -- policy ------------------------------------------------------------
    POLICY_CLEARED = "POLICY_CLEARED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"

    # -- execution ---------------------------------------------------------
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"

    # -- human gates -------------------------------------------------------
    AWAITING_HUMAN_ATTESTATION = "AWAITING_HUMAN_ATTESTATION"
    HUMAN_GATE_CLEARED = "HUMAN_GATE_CLEARED"
    HUMAN_GATE_REJECTED = "HUMAN_GATE_REJECTED"

    # -- validation + commit ----------------------------------------------
    CRITIQUE_CONCLUDED = "CRITIQUE_CONCLUDED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MUTATION_COMMITTED = "MUTATION_COMMITTED"

    # -- recovery ----------------------------------------------------------
    STATE_ROLLBACK_TRIGGERED = "STATE_ROLLBACK_TRIGGERED"
    RECOVERY_REPLAY_COMPLETED = "RECOVERY_REPLAY_COMPLETED"

    # -- learning signals --------------------------------------------------
    USER_CORRECTION = "USER_CORRECTION"
    MODEL_ESCALATED = "MODEL_ESCALATED"

    # -- lifecycle ---------------------------------------------------------
    TASK_ABANDONED = "TASK_ABANDONED"


#: Events after which a correlation is closed and needs no recovery sweep.
#: Used by the recovery engine to decide which lineages are still in flight.
#:
#: SPEC DEVIATION (docs/adr/0010): the RFC's recovery index excludes
#: ``EXECUTION_COMPLETED`` as though it closed a lineage, yet the RFC's own
#: happy path continues past it through ``CRITIQUE_CONCLUDED`` to
#: ``MUTATION_COMMITTED``. Both cannot be true. ``EXECUTION_COMPLETED`` is a
#: *step*-scoped event: on a multi-step plan it fires once per step, so
#: treating it as terminal would close the lineage after step 1 and make
#: recovery silently skip every remaining step. Only ``MUTATION_COMMITTED``
#: closes a successful lineage.
TERMINAL_EVENTS: Final[frozenset[EventType]] = frozenset(
    {
        EventType.MUTATION_COMMITTED,
        EventType.EXECUTION_FAILED,
        EventType.POLICY_BLOCKED,
        EventType.SECURITY_VIOLATION,
        EventType.HUMAN_GATE_REJECTED,
        EventType.TASK_ABANDONED,
    }
)


# ---------------------------------------------------------------------------
# Permission model
# ---------------------------------------------------------------------------


class Permission(str, enum.Enum):
    """Capability tokens a skill or agent may require.

    Permissions are *requested* by skills (in their contract) and *granted* by
    the active :class:`PermissionMode`. A skill whose required set is not a
    subset of the granted set is refused before any sandbox boots.
    """

    SANDBOX_RUN = "PERM_SANDBOX_RUN"
    WRITE_HOT = "PERM_WRITE_HOT"
    WRITE_COLD = "PERM_WRITE_COLD"
    NET_EGRESS = "PERM_NET_EGRESS"
    EXTERNAL_WRITE = "PERM_EXTERNAL_WRITE"
    FILE_DELETE = "PERM_FILE_DELETE"
    SCHEMA_MIGRATE = "PERM_SCHEMA_MIGRATE"
    SECRET_READ = "PERM_SECRET_READ"
    MODEL_ESCALATE = "PERM_MODEL_ESCALATE"
    CROSS_SESSION_READ = "PERM_CROSS_SESSION_READ"


class PermissionMode(str, enum.Enum):
    """System-wide autonomy posture. See RFC §9.1."""

    AUTO = "AUTO"
    ASK = "ASK"
    SUPERVISED = "SUPERVISED"
    LOCKDOWN = "LOCKDOWN"
    SAFE = "SAFE"
    HIGH_RISK = "HIGH_RISK"

    @property
    def granted(self) -> frozenset[Permission]:
        """Permissions this mode grants without a human gate."""
        return _MODE_GRANTS[self]

    @property
    def requires_human_gate_for_mutations(self) -> bool:
        """Whether every mutating call needs explicit human confirmation."""
        return self in (PermissionMode.SUPERVISED, PermissionMode.ASK)

    def grants(self, permission: Permission) -> bool:
        return permission in _MODE_GRANTS[self]


_ALL_PERMS: Final[frozenset[Permission]] = frozenset(Permission)

#: Explicit grant matrix. Written out in full rather than derived, because a
#: security boundary should be readable at a glance and diffable in review.
_MODE_GRANTS: Final[dict[PermissionMode, frozenset[Permission]]] = {
    PermissionMode.AUTO: _ALL_PERMS - {Permission.SCHEMA_MIGRATE},
    PermissionMode.ASK: _ALL_PERMS - {Permission.SCHEMA_MIGRATE},
    PermissionMode.SUPERVISED: frozenset(
        {
            Permission.SANDBOX_RUN,
            Permission.SECRET_READ,
            Permission.MODEL_ESCALATE,
            Permission.CROSS_SESSION_READ,
        }
    ),
    # LOCKDOWN drops every egress and external-write path at the policy layer;
    # the sandbox additionally severs the network namespace as defence in depth.
    PermissionMode.LOCKDOWN: frozenset({Permission.SANDBOX_RUN}),
    # SAFE permits workspace mutation but hard-blocks irreversible operations.
    PermissionMode.SAFE: _ALL_PERMS
    - {Permission.FILE_DELETE, Permission.SCHEMA_MIGRATE, Permission.EXTERNAL_WRITE},
    PermissionMode.HIGH_RISK: _ALL_PERMS,
}


# ---------------------------------------------------------------------------
# Complexity modalities
# ---------------------------------------------------------------------------


class ComplexityModality(str, enum.Enum):
    """Cognitive depth allocated to a task. See RFC §9.2."""

    SIMPLE = "SIMPLE"
    STANDARD = "STANDARD"
    COMPLEX = "COMPLEX"
    MAX = "MAX"


@dataclass(frozen=True, slots=True)
class ModalityProfile:
    """Hard resource envelope for one complexity modality.

    Every field here is enforced somewhere concrete: ``token_ceiling`` by the
    context gatherer, ``recursion_ceiling`` by the recursion guard,
    ``memory_mb``/``cpu_cores``/``timeout_seconds`` by the sandbox backend.
    Nothing in this struct is advisory.
    """

    modality: ComplexityModality
    graph_hops: int
    recursion_ceiling: int
    token_ceiling: int
    memory_mb: int
    cpu_cores: float
    timeout_seconds: float | None
    """``None`` means "block until a human decides" — used only by MAX."""

    branch_factor: int
    """Tree-of-thought branch expansion coefficient (``B`` in RFC §11.1)."""

    def max_plan_nodes(self) -> int:
        """Maximum reasoning nodes a planner may expand: ``(B^(D+1) - 1)/(B - 1)``.

        This is the closed form of the geometric series in RFC §11.1 and bounds
        tree-of-thought expansion. ``B == 1`` degenerates to a linear chain, so
        the series formula is undefined and we return ``D + 1`` instead.
        """
        b, d = self.branch_factor, self.recursion_ceiling
        if b <= 1:
            return d + 1
        return (b ** (d + 1) - 1) // (b - 1)

    def token_quota_at_depth(self, depth: int) -> int:
        """Token budget for a sub-agent at nesting ``depth``: ``ceiling / 2^depth``.

        RFC §15.7. Halving per level keeps a depth-2 recursion from consuming
        4x the parent's budget, which is what makes deep plans affordable on
        constrained hardware.
        """
        if depth < 0:
            raise ValueError(f"depth must be non-negative, got {depth}")
        return self.token_ceiling >> depth if depth < 32 else 0


#: The RFC §9.2 matrix, made executable.
#:
#: SPEC NOTE: the RFC gives SIMPLE a "0 token" ceiling meaning "LLM bypassed
#: entirely". We keep 0 as the sentinel — the planner refuses to run at SIMPLE
#: and the orchestrator dispatches straight to a deterministic handler.
MODALITY_PROFILES: Final[dict[ComplexityModality, ModalityProfile]] = {
    ComplexityModality.SIMPLE: ModalityProfile(
        modality=ComplexityModality.SIMPLE,
        graph_hops=0,
        recursion_ceiling=0,
        token_ceiling=0,
        memory_mb=64,
        cpu_cores=0.1,
        timeout_seconds=0.25,
        branch_factor=1,
    ),
    ComplexityModality.STANDARD: ModalityProfile(
        modality=ComplexityModality.STANDARD,
        graph_hops=1,
        recursion_ceiling=1,
        token_ceiling=2048,
        memory_mb=256,
        cpu_cores=0.25,
        timeout_seconds=10.0,
        branch_factor=2,
    ),
    ComplexityModality.COMPLEX: ModalityProfile(
        modality=ComplexityModality.COMPLEX,
        graph_hops=2,
        recursion_ceiling=2,
        token_ceiling=4096,
        memory_mb=512,
        cpu_cores=0.5,
        timeout_seconds=30.0,
        branch_factor=2,
    ),
    ComplexityModality.MAX: ModalityProfile(
        modality=ComplexityModality.MAX,
        graph_hops=3,
        recursion_ceiling=4,
        token_ceiling=16384,
        memory_mb=1024,
        cpu_cores=1.0,
        timeout_seconds=None,  # blocks on human attestation
        branch_factor=2,
    ),
}


# ---------------------------------------------------------------------------
# Agent roles
# ---------------------------------------------------------------------------


class AgentRole(str, enum.Enum):
    """The nine core hierarchy roles plus the optional router.

    SPEC DEVIATION (docs/adr/0011): the RFC hardcodes a router into every task.
    Per explicit user direction the router is *optional* — it exists to
    decompose and dispatch when many agents are eligible, and is bypassed when
    the caller names a target agent directly.
    """

    ORCHESTRATOR = "chief_orchestrator"
    ROUTER = "router"  # optional; see ADR-0011
    CONTEXT_BUILDER_PLANNER = "context_builder_planner"
    STRATEGIC_PLANNER = "strategic_planner"
    POLICY_RISK = "policy_risk"
    CONTEXT_BUILDER_WORKER = "context_builder_worker"
    WORKER = "worker"
    CRITIC = "critic"
    MEMORY_CREATOR = "memory_creator"
    MEMORY_CURATOR = "memory_curator"
