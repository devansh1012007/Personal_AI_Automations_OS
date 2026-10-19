"""Smoke test for the runner — drives the loop end-to-end against a fake runtime."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from paa.core.types import ComplexityModality, CorrelationId
from paa.ledger.replay import TaskPhase
from paa.tui.runner import run_tui


class FakeProject:
    def __init__(self, phase: TaskPhase = TaskPhase.CREATED) -> None:
        self.phase = phase
        self.modality = ComplexityModality.STANDARD
        self.request = {"goal": "echo test"}
        self.attempts = 0
        self.tokens_consumed = 0
        self.created_at = datetime.now()
        self.completed_steps: list[int] = []
        self.plan_steps: list[dict] = []
        self.policy_decision: str | None = None
        self.validation_verdict: str | None = None
        self.awaiting_reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "modality": self.modality,
            "request": self.request,
            "attempts": self.attempts,
            "tokens_consumed": self.tokens_consumed,
            "completed_steps": self.completed_steps,
            "plan_steps": self.plan_steps,
        }


class FakeLedger:
    async def open_correlations(self) -> list[str]:
        return [str(uuid.UUID("12345678-1234-1234-1234-123456789012"))]

    async def read_correlation(self, cid: CorrelationId) -> list[Any]:
        return []

    async def verify_chain(self, cid: CorrelationId) -> tuple[bool, str]:
        return True, "ok"


class FakeOrchestrator:
    async def submit(self, request: Any) -> str:
        return str(uuid.uuid4())

    async def run(self, cid: CorrelationId) -> Any:
        proj = FakeProject()
        proj.phase = TaskPhase.EXECUTING
        return type(
            "Outcome", (), {"ok": True, "phase": proj.phase, "error": None}
        )()

    async def clear_human_gate(
        self, cid: CorrelationId, *, approved: bool, note: str | None
    ):
        proj = FakeProject()
        proj.phase = TaskPhase.EXECUTING
        return proj


class FakeSettings:
    home: Any
    policy: Any
    storage: Any

    class _Mode:
        value = "ask"

    mode = _Mode()

    class _Storage:
        backend_relational = "sqlite"

    storage = _Storage()


class FakeRuntime:
    def __init__(self) -> None:
        self.settings = FakeSettings()
        self.agents = {"worker": type("W", (), {"_sandbox": None})()}
        self.ledger = FakeLedger()
        self.orchestrator = FakeOrchestrator()
        self.vector_store = None
        self.graph_store = None
        self.queue = None
        self.model_router = None

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_run_tui_quit_after_help() -> None:
    fake_rt = FakeRuntime()

    lines = iter(["help", "q"])

    def reader() -> str | None:  # type: ignore[override]
        try:
            return next(lines)
        except StopIteration:
            return None

    # Inject a stdin reader and a fake runtime; run_tui never calls Runtime.build.
    await run_tui(stdin=reader, rt=fake_rt)
    # If we get here, the loop ran to completion.
