"""The runtime tools: what a testing agent can actually observe and change.

The read paths run against a *real* :class:`~paa.ledger.store.LedgerStore` (via
the shared ``ledger`` fixture) rather than a mock, because the property under
test is that the tools report what replay says — a mocked ledger would agree
with whatever the tool asserted, which is the opposite of a test. Only the
runtime shell is stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from paa.coder.runtime_tools import build_runtime_tools
from paa.core.types import EventType, PermissionMode, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore


class FakeProvider:
    """Named stand-in so ``type(...).__name__`` assertions read sensibly."""

    name = "openrouter"
    model = "openai/gpt-4o-mini"


class StubRuntime:
    """The slice of ``Runtime`` the tools actually touch."""

    def __init__(self, ledger: LedgerStore) -> None:
        self.ledger = ledger
        self.settings = SimpleNamespace(
            home="/tmp/paa-test",
            policy=SimpleNamespace(mode=PermissionMode.ASK),
            storage=SimpleNamespace(backend_relational="sqlite"),
        )
        self.model_router = SimpleNamespace(local=FakeProvider(), frontier=None)
        self.vector_store = None
        self.graph_store = None
        self.queue = None
        self.agents: dict[str, Any] = {}
        self.submitted: list[dict[str, Any]] = []
        self.ran: list[Any] = []
        self.outcome = "COMMITTED"

    async def submit(self, goal: str, **kw: Any) -> Any:
        self.submitted.append({"goal": goal, **kw})
        return new_correlation_id()

    async def run(self, cid: Any) -> Any:
        self.ran.append(cid)
        from paa.ledger.replay import TaskPhase, TaskProjection

        return SimpleNamespace(
            correlation_id=cid,
            phase=TaskPhase[self.outcome],
            ok=self.outcome == "COMMITTED",
            error=None,
            projection=TaskProjection(correlation_id=str(cid), completed_steps=[0], plan_steps=[]),
        )


@pytest.fixture
def rt(ledger: LedgerStore) -> StubRuntime:
    return StubRuntime(ledger)


@pytest.fixture
def tools(rt: StubRuntime) -> dict[str, Any]:
    return build_runtime_tools(cast(Any, rt))


async def _append(store: LedgerStore, cid: Any, etype: EventType, **kw: Any) -> None:
    await store.append(LedgerEvent.create(cid, etype, **kw))


class TestCatalogue:
    def test_names_are_unique(self, tools: dict[str, Any]) -> None:
        assert len(tools) == len(set(tools))

    def test_every_tool_advertises_a_name_description_and_schema(
        self, tools: dict[str, Any]
    ) -> None:
        for name, tool in tools.items():
            assert tool.name == name
            assert len(tool.description) >= 20, name
            assert tool.schema()["type"] == "object", name

    def test_mutating_tools_exist_and_are_named_for_the_policy(self, tools: dict[str, Any]) -> None:
        """``submit_task``/``run_task`` write to the ledger; the catalogue must
        name them so an operator can ``--deny`` them by name."""

        assert {"submit_task", "run_task"} <= set(tools)


class TestTaskStatus:
    async def test_reports_the_replayed_phase(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"goal": "x"})

        result = json.loads(await tools["task_status"].run({"correlation_id": str(cid)}))

        assert result["phase"] == "CREATED"
        assert result["is_terminal"] is False
        assert result["steps_total"] == 0

    async def test_terminal_lineage_is_reported_as_terminal(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.MUTATION_COMMITTED)

        result = json.loads(await tools["task_status"].run({"correlation_id": str(cid)}))

        assert result["phase"] == "COMMITTED"
        assert result["is_terminal"] is True

    async def test_bad_correlation_id_is_an_error_payload_not_an_exception(
        self, tools: dict[str, Any]
    ) -> None:
        """The loop turns a raised exception into ``"error: ..."`` with no
        structure. Reporting it as data is what lets the model recover."""

        result = json.loads(await tools["task_status"].run({"correlation_id": "not-a-uuid"}))

        assert result["error"] == "invalid correlation id"
        assert "not-a-uuid" in result["received"]


class TestReadLedger:
    async def test_empty_lineage_explains_itself(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        result = json.loads(
            await tools["read_ledger"].run({"correlation_id": str(new_correlation_id())})
        )

        assert result["event_count"] == 0
        assert "correlation id" in result["note"]

    async def test_events_come_back_in_replay_order_with_payloads(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED, payload={"goal": "summarise"})
        await _append(ledger, cid, EventType.TASK_QUEUED)

        result = json.loads(await tools["read_ledger"].run({"correlation_id": str(cid)}))

        assert result["event_count"] == 2
        assert [e["state_version"] for e in result["events"]] == [1, 2]
        assert result["events"][0]["event_type"] == "TASK_REQUESTED"
        assert result["events"][0]["payload"] == {"goal": "summarise"}

    async def test_from_version_pages_forward(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.TASK_QUEUED)
        await _append(ledger, cid, EventType.EXECUTION_STARTED)

        result = json.loads(
            await tools["read_ledger"].run(
                {"correlation_id": str(cid), "from_version": 2}
            )
        )

        assert [e["state_version"] for e in result["events"]] == [3]


class TestVerifyLedger:
    async def test_untouched_chain_verifies(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)

        result = json.loads(await tools["verify_ledger"].run({"correlation_id": str(cid)}))

        assert result["chain_ok"] is True
        assert result["problems"] == []


class TestListTasks:
    async def test_lists_open_lineages(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)

        result = json.loads(await tools["list_tasks"].run({}))

        assert result["count"] == 1
        assert result["tasks"][0]["correlation_id"] == str(cid)

    async def test_terminal_lineage_drops_out(
        self, tools: dict[str, Any], ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        await _append(ledger, cid, EventType.TASK_REQUESTED)
        await _append(ledger, cid, EventType.MUTATION_COMMITTED)

        result = json.loads(await tools["list_tasks"].run({}))

        assert result["count"] == 0

    async def test_limit_is_clamped(self, tools: dict[str, Any]) -> None:
        # 0 would mean "unbounded" to a naive caller; the clamp keeps it at 1.
        result = json.loads(await tools["list_tasks"].run({"limit": 0}))

        assert "tasks" in result


class TestSubmitAndRun:
    async def test_submit_returns_a_correlation_id(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        result = json.loads(await tools["submit_task"].run({"goal": "  summarise notes  "}))

        assert result["correlation_id"]
        assert rt.submitted[0]["goal"] == "summarise notes"

    async def test_submit_without_a_goal_is_refused(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        result = json.loads(await tools["submit_task"].run({"goal": "   "}))

        assert "error" in result
        assert rt.submitted == []

    async def test_modality_is_mapped_onto_the_enum(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        await tools["submit_task"].run({"goal": "g", "modality": "complex"})

        assert rt.submitted[0]["modality"].value == "COMPLEX"

    async def test_unknown_modality_falls_back_to_the_default(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        await tools["submit_task"].run({"goal": "g", "modality": "ENORMOUS"})

        assert rt.submitted[0]["modality"] is None

    async def test_run_reports_the_outcome(self, tools: dict[str, Any]) -> None:
        cid = new_correlation_id()
        result = json.loads(await tools["run_task"].run({"correlation_id": str(cid)}))

        assert result["phase"] == "COMMITTED"
        assert result["ok"] is True
        assert result["steps_completed"] == 1

    async def test_run_reports_a_failed_phase_without_raising(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        rt.outcome = "FAILED"
        cid = new_correlation_id()
        result = json.loads(await tools["run_task"].run({"correlation_id": str(cid)}))

        assert result["ok"] is False


class TestCheckRuntime:
    async def test_reports_live_backends_honestly(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        """Absent backends report as ``null``, never as a healthy stand-in."""

        result = json.loads(await tools["check_runtime"].run({}))

        assert result["vector_backend"] is None
        assert result["graph_backend"] is None
        assert result["frontier_provider"] is None
        assert result["local_provider"] == "FakeProvider"

    async def test_sandbox_absent_is_reported_not_assumed(
        self, tools: dict[str, Any], rt: StubRuntime
    ) -> None:
        result = json.loads(await tools["check_runtime"].run({}))

        assert result["sandbox"] is None
        assert result["sandbox_isolation"] == "none"