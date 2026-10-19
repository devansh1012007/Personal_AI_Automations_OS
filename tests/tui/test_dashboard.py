"""Tests for the rendering layer — every ``render_*`` function draws without error."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

from rich.console import Console

from paa.core.types import ComplexityModality, CorrelationId
from paa.ledger.replay import TaskPhase
from paa.tui.dashboard import (
    render_dashboard,
    render_doctor,
    render_help,
    render_ledger,
    render_status,
)
from paa.tui.snapshot import CorrelationRow, Snapshot


def _console() -> Console:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False)


def test_help_renders() -> None:
    console = _console()
    console.print(render_help())
    out = console.file.getvalue()
    assert "command" in out.lower()
    assert "new <goal>" in out
    assert "drive <id>" in out
    assert "gate" in out
    assert "doctor" in out


def _row(cid_str: str = "12345678-1234-1234-1234-123456789012") -> Snapshot:
    uid = CorrelationId(uuid.UUID(cid_str))
    return Snapshot(rows=[
        CorrelationRow(
            correlation_id=uid,
            phase=TaskPhase.EXECUTING,
            modality=ComplexityModality.STANDARD,
            attempts=2,
            tokens_consumed=4096,
            created_at="12:34:56",
            goal_preview="summarise today's notes",
        )
    ])


def test_empty_dashboard_renders() -> None:
    console = _console()
    console.print(render_dashboard(Snapshot(rows=[])))
    out = console.file.getvalue()
    assert "no active tasks" in out.lower()


def test_dashboard_with_rows() -> None:
    console = _console()
    snapshot = _row()
    console.print(render_dashboard(snapshot))
    out = console.file.getvalue()
    assert "EXECUTING" in out
    assert "summarise" in out
    assert "4,096" in out or "4096" in out  # depends on table formatting


def test_status_renders_projection() -> None:
    cid = "12345678-1234-1234-1234-123456789012"
    proj: dict[str, Any] = {
        "correlation_id": cid,
        "phase": TaskPhase.VALIDATING,
        "modality": ComplexityModality.COMPLEX,
        "completed_steps": [0, 1],
        "plan_steps": [0, 1, 2],
        "attempts": 1,
        "tokens_consumed": 8192,
        "policy_decision": "AUTO_APPROVE",
        "validation_verdict": "PASS",
        "awaiting_reason": None,
    }
    console = _console()
    console.print(render_status(proj))
    out = console.file.getvalue()
    assert "VALIDATING" in out
    assert "PASS" in out
    assert "COMPLEX" in out


def test_status_empty_errors() -> None:
    cid = "12345678-1234-1234-1234-123456789012"
    proj: dict[str, Any] = {
        "correlation_id": cid,
        "phase": TaskPhase.CREATED,
        "completed_steps": [],
        "plan_steps": [],
        "errors": [],
    }
    console = _console()
    console.print(render_status(proj))
    out = console.file.getvalue()
    assert "none" in out.lower()


def test_ledger_empty() -> None:
    cid = "12345678-1234-1234-1234-123456789012"
    console = _console()
    console.print(render_ledger([], cid))
    out = console.file.getvalue()
    assert "no events" in out.lower()


def test_ledger_with_events() -> None:
    cid = "12345678-1234-1234-1234-123456789012"
    class FakeEvent:
        event_type = type("ET", (), {"value": "TASK_REQUESTED"})()
        agent_role = "planner"
        state_version = 1
        recorded_at = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)

    console = _console()
    console.print(render_ledger([FakeEvent()], cid))
    out = console.file.getvalue()
    assert "TASK_REQUESTED" in out
    assert "v1" in out
    assert "planner" in out


def test_doctor_renders() -> None:
    info = {
        "home": "/tmp/paa",
        "policy_mode": "ask",
        "relational": "sqlite",
        "vector_store": "VectorStoreX",
        "graph_store": "GraphStoreY",
        "queue": "QueueZ",
        "model_router": "RouterA",
        "sandbox": "SubprocessBackend",
        "open_tasks": ["12345678-..."],
    }
    console = _console()
    console.print(render_doctor(info))
    out = console.file.getvalue()
    assert "SQLite" in out or "sqlite" in out
    assert "VectorStoreX" in out
    assert "open tasks" in out
