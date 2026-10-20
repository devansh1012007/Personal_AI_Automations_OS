"""Smoke test for the runner — drives the loop end-to-end against a fake runtime."""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
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


class _Mode:
    value = "ask"


class _Storage:
    backend_relational = "sqlite"


class FakeSettings:
    home: Any
    policy: Any
    storage: Any

    mode = _Mode()
    storage = _Storage()

    def __init__(self) -> None:
        self.home = "/tmp/paa-tui-test"
        self.policy = self


class FakeRuntime:
    def __init__(self) -> None:
        self.settings = FakeSettings()
        #: The runtime owns the containment backend; the TUI reads it from
        #: here rather than reaching into the worker's private attribute.
        self.sandbox = None
        self.ledger = FakeLedger()
        #: The relational substrate. The panel reports the *live* backend, so
        #: the fake has to carry one — a diagnostic that reads a setting
        #: instead of the object it describes is a diagnostic that lies.
        self.db = SimpleNamespace()
        self.orchestrator = FakeOrchestrator()
        self.vector_store = None
        self.graph_store = None
        self.queue = None
        self.model_router = None

    async def close(self) -> None:
        pass


class FakeSandbox:
    """A backend whose facts the TUI must actually carry to the panel."""

    name = "docker"
    isolation_level = "NAMESPACE"

    def __init__(self, facts: dict[str, Any]) -> None:
        self._facts = facts

    async def diagnose(self) -> dict[str, Any]:
        return self._facts


async def test_the_tui_carries_the_sandbox_facts_to_the_panel() -> None:
    """The TUI must *collect* the facts, not merely be able to draw them.

    A test that hands ``sandbox_facts`` straight to the renderer only proves
    the renderer is shared. If the TUI stopped collecting — say the
    dashboard quietly showed a stale isolation level — that test would
    still pass, and the operator would be told the wrong containment.
    """
    from paa.tui.runner import _build_info

    fake_rt = FakeRuntime()
    fake_rt.sandbox = FakeSandbox(
        {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "container_runtime": "runsc",
            "runtime_available": False,
            "image": "paa/base_worker:v4.1",
            "image_present": True,
        }
    )
    info = await _build_info(fake_rt.settings, fake_rt)
    assert info["sandbox_facts"]["container_runtime"] == "runsc"
    assert info["sandbox_facts"]["image"] == "paa/base_worker:v4.1"
    assert info["sandbox_facts"]["runtime_available"] is False


async def test_the_tui_reports_a_failed_diagnostic_instead_of_crashing() -> None:
    """A backend that breaks its own contract must not take the TUI down.

    And the failure must be *reported*: a diagnostic that throws is a
    diagnostic that lies, and the operator needs to know the facts are
    missing rather than being shown a stale level as if it were fresh.
    """

    class BrokenSandbox:
        name = "docker"
        isolation_level = "NAMESPACE"

        async def diagnose(self) -> dict[str, Any]:
            raise RuntimeError("the daemon timed out")

    from paa.tui.runner import _build_info

    fake_rt = FakeRuntime()
    fake_rt.sandbox = BrokenSandbox()
    info = await _build_info(fake_rt.settings, fake_rt)
    assert "the daemon timed out" in info["sandbox_facts"]["diagnostic_error"]


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
