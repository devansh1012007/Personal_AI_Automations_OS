"""Request/response models for the ingestion API.

Kept separate from the app so they can be imported and validated without pulling
in FastAPI — the app module imports FastAPI lazily, but these pydantic models are
plain and testable on a minimal install.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from paa.core.types import ComplexityModality

__all__ = [
    "BatchReportView",
    "FactView",
    "GateRequest",
    "HealthResponse",
    "LedgerEventView",
    "MandateRequestBody",
    "SignalRequest",
    "SignalResponse",
    "SignalView",
    "TaskRequestBody",
    "TaskStatusResponse",
    "TaskSubmitResponse",
]


class MandateRequestBody(BaseModel):
    """An explicitly asserted mandate for a task submission (N2.4).

    The client asserts *who requested the work* and *on what basis*; it
    does **not** supply the effective policy version. The server stamps
    the effective policy (SHA-256 of the canonical policy) itself, so a
    client cannot forge which policy granted the authority. The requester
    is asserted — recorded, never authenticated — which the mandate
    payload records with the same honesty (`identity_assurance` stays
    ``"asserted"``).
    """

    kind: str = Field(min_length=1)
    id: str = Field(min_length=1)
    basis: Literal["explicit", "inferred", "implicit"] = "explicit"
    workspace: str | None = None
    budget: int | None = Field(default=None, ge=0)


class TaskRequestBody(BaseModel):
    """Submit a task."""

    goal: str = Field(min_length=1)
    modality: ComplexityModality | None = None
    required_slots: list[str] | None = None
    workspace_path: str | None = None
    target_agent: str | None = None
    mandate: MandateRequestBody | None = None
    run: bool = Field(
        default=False,
        description="Run to completion synchronously. When false, the task is "
        "recorded and queued for the daemon to execute.",
    )


class TaskSubmitResponse(BaseModel):
    correlation_id: str
    phase: str
    queued: bool


class TaskStatusResponse(BaseModel):
    correlation_id: str
    phase: str
    modality: str
    plan_steps: int
    completed_steps: list[int]
    current_step_index: int
    attempts: int
    tokens_consumed: int
    awaiting_reason: str | None = None
    policy_reason: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    is_terminal: bool


class GateRequest(BaseModel):
    """Clear a task parked on AWAITING_HUMAN_ATTESTATION."""

    approved: bool
    note: str | None = None


class SignalRequest(BaseModel):
    """Ingest a raw signal into the cold lake (the RFC ingestion edge)."""

    channel: str = Field(min_length=1)
    payload: dict[str, Any] | list[Any] | str
    external_id: str | None = None


class SignalResponse(BaseModel):
    signal_id: str
    channel: str
    duplicate: bool


class FactView(BaseModel):
    """A queryable hot-serving fact, for the /memory/facts and /memory/search edges."""

    id: str
    entity_id: str
    predicate: str
    object_value: str
    confidence: float
    domain: str
    source_signal_id: str | None = None


class SignalView(BaseModel):
    """One cold-lake signal, for the /memory/signals edge."""

    id: str
    channel: str
    external_id: str | None = None
    sync_status: str
    received_at: str
    processed_at: str | None = None
    error_detail: str | None = None


class BatchReportView(BaseModel):
    """One MemoryCreator.run_batch pass, for the /memory/drain edge."""

    claimed: int
    processed: int
    ignored: int
    malformed: int
    quarantined: int
    errors: int
    facts_written: int


class LedgerEventView(BaseModel):
    """One event in a lineage, for the explainability endpoint."""

    sequence_id: int | None
    state_version: int | None
    event_type: str
    agent_role: str | None
    recorded_at: str
    payload: dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    version: str
    backends: dict[str, str | None]
    open_tasks: int
    # PLAN:integration-fix:P006 — additive mirror of the five TUI panel
    # fields (same names, same builder). Old keys above are unchanged; a
    # field is null when its source has no data or failed in isolation.
    attention_top: Any | None = None
    memory_browser_counts: Any | None = None
    decision_tracker: Any | None = None
    autonomy_state: Any | None = None
    agent_monitor: Any | None = None
