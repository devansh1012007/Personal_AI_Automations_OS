"""Request/response models for the ingestion API.

Kept separate from the app so they can be imported and validated without pulling
in FastAPI — the app module imports FastAPI lazily, but these pydantic models are
plain and testable on a minimal install.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from paa.core.types import ComplexityModality

__all__ = [
    "GateRequest",
    "HealthResponse",
    "LedgerEventView",
    "SignalRequest",
    "SignalResponse",
    "TaskRequestBody",
    "TaskStatusResponse",
    "TaskSubmitResponse",
]


class TaskRequestBody(BaseModel):
    """Submit a task."""

    goal: str = Field(min_length=1)
    modality: ComplexityModality | None = None
    required_slots: list[str] | None = None
    workspace_path: str | None = None
    target_agent: str | None = None
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
