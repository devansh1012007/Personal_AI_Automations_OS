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


def test_doctor_renders_the_shared_sandbox_rows() -> None:
    """The TUI must use the *same* renderer as the CLI.

    Two copies of the vocabulary drift, and a runtime that tells the
    operator two different things about the same host — depending on which
    surface they opened — is worse than either of them being wrong alone,
    because the operator has no way to tell which to believe. This test is
    what would notice if the TUI grew its own copy again.
    """
    info = {
        "home": "/tmp/paa",
        "policy_mode": "ask",
        "relational": "sqlite",
        "vector_store": "VectorStoreX",
        "graph_store": "GraphStoreY",
        "queue": "QueueZ",
        "model_router": "RouterA",
        "sandbox": "docker",
        "sandbox_facts": {
            "backend": "docker",
            "isolation_level": "NAMESPACE",
            "daemon": "reachable, but 'runsc' is not registered",
            "container_runtime": "runsc",
            "runtime_available": False,
            "image": "paa/base_worker:v4.1",
            "image_present": True,
        },
        "open_tasks": [],
    }
    console = _console()
    console.print(render_doctor(info))
    out = console.file.getvalue()
    assert "docker daemon" in out
    assert "oci runtime" in out
    assert "sandbox image" in out
    # The same words the CLI uses, character for character.
    assert "NOT registered" in out
    assert "NAMESPACE" in out
    assert "paa/base_worker:v4.1" in out


# ---------------------------------------------------------------------------
# P006 panels (T-006-P006) — appended, existing tests untouched above.
# ---------------------------------------------------------------------------


def test_p006_all_null_panels_render_no_data_yet() -> None:
    console = _console()
    console.print(render_dashboard(Snapshot(rows=[])))
    out = console.file.getvalue()
    assert "no active tasks" in out.lower()
    for name in (
        "attention_top",
        "memory_browser_counts",
        "decision_tracker",
        "autonomy_state",
        "agent_monitor",
    ):
        assert name in out, f"missing panel {name}"
    assert out.count("No data yet") == 5, out


def test_p006_populated_panels_render_rows() -> None:
    console = _console()
    snapshot = Snapshot(
        rows=[
            CorrelationRow(
                correlation_id=CorrelationId(uuid.UUID("12345678-1234-1234-1234-123456789012")),
                phase=TaskPhase.EXECUTING,
                modality=ComplexityModality.STANDARD,
                attempts=1,
                tokens_consumed=10,
                created_at="12:34:56",
                goal_preview="goal",
            )
        ],
        runtime_info={},
        attention_top=[
            {"correlation_id": "aaaa-1", "score": 0.9},
            {"correlation_id": "bbbb-2", "score": 0.5},
        ],
        memory_browser_counts={
            "total": 3,
            "by_domain": {"semantic": 2, "episodic": 1},
            "by_status": {"indexed": 3},
        },
        decision_tracker=[
            {
                "correlation_id": "12345678-1234-1234-1234-123456789012",
                "policy_decision": "AUTO_APPROVE",
                "policy_reason": "low risk",
            }
        ],
        autonomy_state={"mode": "ask", "awaiting_gates": 1, "awaiting_ids": ["12345678-x"]},
        agent_monitor={"agents": ["planner", "worker"], "recent_roles": ["worker"]},
    )
    console.print(render_dashboard(snapshot))
    out = console.file.getvalue()
    # Ranked attention rows keep list order.
    assert "aaaa-1" in out and "bbbb-2" in out
    assert out.index("aaaa-1") < out.index("bbbb-2")
    assert "semantic" in out and "total: 3" in out
    assert "AUTO_APPROVE" in out and "low risk" in out
    assert "mode: ask" in out and "awaiting gates: 1" in out
    assert "planner" in out and "worker" in out
    assert out.count("No data yet") == 0


def test_p006_one_null_panel_does_not_hide_the_others() -> None:
    console = _console()
    snapshot = Snapshot(
        rows=[],
        runtime_info={},
        attention_top=None,
        memory_browser_counts={"total": 1, "by_domain": {"semantic": 1}, "by_status": {}},
        decision_tracker=None,
        autonomy_state={"mode": "auto", "awaiting_gates": 0, "awaiting_ids": []},
        agent_monitor={"agents": ["planner"], "recent_roles": []},
    )
    console.print(render_dashboard(snapshot))
    out = console.file.getvalue()
    assert out.count("No data yet") == 2
    assert "semantic" in out
    assert "mode: auto" in out
    assert "planner" in out


def test_p006_hostile_strings_render_literally_without_markup_crash() -> None:
    # A bare "[/red]" in a str cell / Panel(str) raises MarkupError and kills
    # the loop. Text cells render it literally. ANSI escapes pass through
    # (documented passthrough, not stripped — W1 FINDING), so assert only
    # that the render succeeds and the payload text survives.
    hostile = "[red]hacked[/red] \x1b[31mpwned\x1b[0m"
    console = _console()
    snapshot = Snapshot(
        rows=[
            CorrelationRow(
                correlation_id=CorrelationId(uuid.UUID("12345678-1234-1234-1234-123456789012")),
                phase=TaskPhase.EXECUTING,
                modality=ComplexityModality.STANDARD,
                attempts=1,
                tokens_consumed=10,
                created_at="12:34:56",
                goal_preview=hostile,
            )
        ],
        runtime_info={},
        decision_tracker=[
            {
                "correlation_id": "12345678-1234-1234-1234-123456789012",
                "policy_decision": "CLEAR",
                "policy_reason": hostile,
            }
        ],
    )
    console.print(render_dashboard(snapshot))
    out = console.file.getvalue()
    assert "hacked" in out and "pwned" in out
    # Literal, not parsed: the markup tags are visible in the output.
    assert "[red]hacked[/red]" in out


def test_p006_panel_builder_isolates_a_raising_source() -> None:
    # Per-panel isolation at the data-source level: one source raising
    # empties only its panel. Driven through the shared builder the TUI and
    # /health both use, with a fake runtime whose db blows up the counts
    # query while the ledger stays readable.
    import asyncio
    from types import SimpleNamespace

    from paa.tui.runner import build_panel_fields

    class ExplodingDb:
        async def fetch_all(self, _sql: str, _params: Any = ()) -> list[Any]:
            raise RuntimeError("counts exploded")

    class FakeLedger:
        async def open_correlations(self) -> list[Any]:
            return []

        async def read_correlation(self, _cid: Any) -> list[Any]:
            return []

    settings = SimpleNamespace(policy=SimpleNamespace(mode=SimpleNamespace(value="ask")))
    rt = SimpleNamespace(db=ExplodingDb(), ledger=FakeLedger(), agents={})
    panels = asyncio.run(build_panel_fields(settings, rt))
    assert panels["attention_top"] is None
    assert panels["memory_browser_counts"] is None
    assert panels["decision_tracker"] == []
    assert panels["autonomy_state"] == {
        "mode": "ask",
        "awaiting_gates": 0,
        "awaiting_ids": [],
    }
    assert panels["agent_monitor"] == {"agents": [], "recent_roles": []}


# ---------------------------------------------------------------------------
# Reflection doctor mirror (P013 wiring — REV-W4 R-W4-003)
# ---------------------------------------------------------------------------
#
# The doctor row renders the same value as the CLI doctor — the shared
# format_reflection_health rule over health the runner collects from
# reflection_health(settings.home). Hand-built info dicts pin the RENDER
# contract (enabled+running, enabled+stale, disabled); the runner-level
# tests below pin the population: a real marker file under tmp_path flows
# through _build_info into the renderer's keys, so a runner that stops
# populating them fails here rather than rendering "disabled" forever.


def _doctor_info(**keys: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "home": "/tmp/paa",
        "policy_mode": "ask",
        "relational": "sqlite",
        "vector_store": "VectorStoreX",
        "graph_store": "GraphStoreY",
        "queue": "QueueZ",
        "model_router": "RouterA",
        "sandbox": "SubprocessBackend",
        "open_tasks": [],
    }
    info.update(keys)
    return info


def test_reflection_enabled_and_running_renders_the_age_not_disabled() -> None:
    """Enabled + recently run: the row shows an age, never "disabled".

    This is the R-W4-003 tripwire: before the runner populated the keys,
    the mirror rendered "disabled" in every state — a state the value
    could never come from this info dict.
    """
    console = _console()
    console.print(
        render_doctor(
            _doctor_info(
                reflection_enabled=True,
                reflection_last_run_age_days=2.5,
                reflection_stale=False,
            )
        )
    )
    out = console.file.getvalue()
    assert "reflection.last_run_age_days" in out
    assert "2.5d" in out
    assert "disabled" not in out
    assert "stale" not in out


def test_reflection_enabled_and_stale_renders_the_stale_value() -> None:
    console = _console()
    console.print(
        render_doctor(
            _doctor_info(
                reflection_enabled=True,
                reflection_last_run_age_days=9.25,
                reflection_stale=True,
            )
        )
    )
    out = console.file.getvalue()
    assert "9.2d (stale)" in out


def test_reflection_disabled_renders_disabled() -> None:
    console = _console()
    console.print(render_doctor(_doctor_info()))
    out = console.file.getvalue()
    assert "reflection.last_run_age_days" in out
    assert "disabled" in out


async def test_runner_build_info_populates_the_reflection_keys(tmp_path: Any) -> None:
    """_build_info is the runner's side of the mirror: it must supply the keys.

    A marker written just now (enabled) flows through reflection_health to
    the info dict: enabled=True, an age near zero, stale=False. A home
    with no marker reports unknown (None age) rather than a fabricated
    one. Both are the same data the CLI doctor renders from the same home.
    """
    from types import SimpleNamespace

    from paa.improvement.reflection import write_reflection_last_run
    from paa.storage.relational.database import utc_now
    from paa.tui.runner import _build_info

    write_reflection_last_run(tmp_path, utc_now())

    class _Ledger:
        async def open_correlations(self) -> list[Any]:
            return []

    settings = SimpleNamespace(
        home=str(tmp_path),
        policy=SimpleNamespace(mode=SimpleNamespace(value="ask")),
        reflection=SimpleNamespace(enabled=True),
    )
    rt = SimpleNamespace(
        db=SimpleNamespace(),
        ledger=_Ledger(),
        sandbox=None,
        vector_store=None,
        graph_store=None,
        queue=None,
        model_router=None,
    )
    info = await _build_info(settings, rt)
    assert info["reflection_enabled"] is True
    assert isinstance(info["reflection_last_run_age_days"], float)
    assert info["reflection_last_run_age_days"] < 1.0
    assert info["reflection_stale"] is False


async def test_runner_build_info_without_a_marker_reports_unknown(tmp_path: Any) -> None:
    from types import SimpleNamespace

    from paa.tui.runner import _build_info

    class _Ledger:
        async def open_correlations(self) -> list[Any]:
            return []

    settings = SimpleNamespace(
        home=str(tmp_path),
        policy=SimpleNamespace(mode=SimpleNamespace(value="ask")),
        reflection=SimpleNamespace(enabled=True),
    )
    rt = SimpleNamespace(
        db=SimpleNamespace(),
        ledger=_Ledger(),
        sandbox=None,
        vector_store=None,
        graph_store=None,
        queue=None,
        model_router=None,
    )
    info = await _build_info(settings, rt)
    assert info["reflection_enabled"] is True
    assert info["reflection_last_run_age_days"] is None
    assert info["reflection_stale"] is False

