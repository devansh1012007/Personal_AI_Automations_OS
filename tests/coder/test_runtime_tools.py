"""The runtime tools: what a testing agent can actually observe and change.

The read paths run against a *real* :class:`~paa.ledger.store.LedgerStore` (via
the shared ``ledger`` fixture) rather than a mock, because the property under
test is that the tools report what replay says — a mocked ledger would agree
with whatever the tool asserted, which is the opposite of a test. Only the
runtime shell is stubbed.

The memory tools run against the same *real* :class:`~paa.storage.relational
.database.Database` the memory subsystem itself is tested with. They never
touch a model or router: the stub runtime's ``model_router`` is a display-only
namespace (see ``check_runtime`` tests), so any tool that tried to call a
model would fail loudly here — which is how the no-model guarantee holds by
construction rather than by assertion.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from paa.coder.agent import MUTATING_RUNTIME_TOOLS
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.permissions import (
    PermissionMode as CoderMode,
)
from paa.coder.runtime_tools import READ_ONLY_TOOLS, build_runtime_tools
from paa.core.types import EventType, PermissionMode, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database


class FakeProvider:
    """Named stand-in so ``type(...).__name__`` assertions read sensibly."""

    name = "openrouter"
    model = "openai/gpt-4o-mini"


class StubRuntime:
    """The slice of ``Runtime`` the tools actually touch."""

    def __init__(self, ledger: LedgerStore, db: Database) -> None:
        self.ledger = ledger
        self.db = db
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
def rt(db: Database, ledger: LedgerStore) -> StubRuntime:
    return StubRuntime(ledger, db)


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


class TestRememberAndRecall:
    """The memory tools, end to end over the real relational database.

    ``StubRuntime``'s ``model_router`` is a display-only namespace, so these
    round trips also prove the no-model guarantee: extraction is the creator's
    deterministic rules, recall is SQL, and nothing here could call a model
    even if one were configured.
    """

    async def test_remember_then_recall_round_trips(
        self, tools: dict[str, Any]
    ) -> None:
        remember = json.loads(
            await tools["remember_fact"].run({"text": "Project Alpha is active"})
        )
        assert remember["remembered"] is True
        assert remember["facts_written"] == 1
        assert remember["outcome"] == "processed"
        assert remember["requires_human_attestation"] is False

        recall = json.loads(await tools["recall_facts"].run({"query": "Project Alpha"}))
        assert recall["count"] == 1
        fact = recall["facts"][0]
        assert fact["entity"] == "Project Alpha"
        assert fact["predicate"] == "is"
        assert fact["object_value"] == "active"
        assert 0.0 < fact["confidence"] <= 0.6
        assert "semantic recall unavailable" in recall["note"]

    async def test_empty_text_is_an_error_value_not_an_exception(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(await tools["remember_fact"].run({"text": "   "}))

        assert result["error"] == "text must be non-empty"

    async def test_prose_that_extracts_nothing_admits_zero_facts(
        self, tools: dict[str, Any]
    ) -> None:
        """The extractor is rule-based; free chatter must not fabricate success."""

        result = json.loads(
            await tools["remember_fact"].run({"text": "thanks, that's great!"})
        )

        assert result["remembered"] is False
        assert result["facts_written"] == 0
        assert result["outcome"] == "ignored"

    async def test_contradiction_quarantine_writes_nothing(self, db: Database) -> None:
        """A confident incumbent plus a conflicting challenger quarantines.

        Driven at the ``process_signal`` level with the same structured
        envelope shape ``tests/memory/test_creator.py`` uses, because the
        branch is unreachable through the tool: free-text extraction caps the
        challenger at confidence 0.60, and ``harmonic_confidence(1.0, 0.6)``
        evaluates to ``0.74999...`` in IEEE doubles — just under the detector's
        0.75 default threshold — so even a perfect incumbent cannot make two
        ``remember_fact`` calls conflict. The tool's reporting of a quarantined
        result is a direct pass-through of
        ``ProcessingResult.requires_human_attestation``; it is left unexercised
        rather than faked with a monkeypatched detector.
        """
        from paa.memory.creator import MemoryCreator, ProcessingOutcome, SignalEnvelope

        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Project Alpha")
        await repo.add_fact(eid, "is", "active", confidence=1.0)
        payload = {"facts": [{"subject": "Project Alpha", "predicate": "is", "object": "blocked"}]}

        result = await MemoryCreator(db).process_signal(
            SignalEnvelope(
                id=str(uuid.uuid4()),
                channel="coder",
                payload=payload,
                raw=json.dumps(payload),
                persisted=False,
            )
        )

        assert result.outcome is ProcessingOutcome.QUARANTINED
        assert result.requires_human_attestation
        assert result.facts_written == []
        live = await db.fetch_all(
            "SELECT object_value FROM hot_serving_active_facts WHERE superseded_by IS NULL"
        )
        assert "blocked" not in {r["object_value"] for r in live}

    async def test_malformed_input_is_reported_not_raised(
        self, tools: dict[str, Any]
    ) -> None:
        """A hostile payload is contained by the creator and reported as a value."""

        result = json.loads(
            await tools["remember_fact"].run({"text": "Project\x00 Alpha is active"})
        )

        assert result["outcome"] == "malformed"
        assert result["remembered"] is False
        assert result["facts_written"] == 0

    async def test_recall_on_empty_memory_is_empty_not_an_error(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(await tools["recall_facts"].run({"query": "nobody anywhere"}))

        assert result["count"] == 0
        assert result["facts"] == []

    async def test_recall_keyword_fallback_matches_object_values(
        self, tools: dict[str, Any]
    ) -> None:
        """"Acme" is not an entity — the fallback tier must still find the fact."""

        await tools["remember_fact"].run({"text": "Alice works at Acme"})

        result = json.loads(await tools["recall_facts"].run({"query": "Acme"}))

        assert result["count"] >= 1
        assert any(f["object_value"] == "Acme" for f in result["facts"])

    async def test_recall_rejects_an_empty_query(self, tools: dict[str, Any]) -> None:
        result = json.loads(await tools["recall_facts"].run({"query": "  "}))

        assert result["error"] == "query must be non-empty"

    async def test_recall_limit_is_clamped_not_trusted(
        self, tools: dict[str, Any]
    ) -> None:
        await tools["remember_fact"].run({"text": "Project Alpha is active"})

        result = json.loads(
            await tools["recall_facts"].run({"query": "Project Alpha", "limit": 0})
        )

        assert "facts" in result
        assert result["count"] <= 1


class TestReadOnlyAndMutatingSets:
    def test_read_only_tools_are_catalogue_members_and_not_mutating(
        self, tools: dict[str, Any]
    ) -> None:
        assert set(tools) >= READ_ONLY_TOOLS
        assert not READ_ONLY_TOOLS & set(MUTATING_RUNTIME_TOOLS)
        assert set(tools) - READ_ONLY_TOOLS == set(MUTATING_RUNTIME_TOOLS)

    def test_remember_fact_is_declared_mutating(self) -> None:
        """A mutating tool outside the tuple is plan-mode-exempt by accident."""

        assert "remember_fact" in MUTATING_RUNTIME_TOOLS


class TestAskStandingAllows:
    """What CoderAgent.build now wires into ToolPermissionPolicy."""

    @staticmethod
    def _policy(**kw: Any) -> ToolPermissionPolicy:
        """Mirror agent.build's construction: standing allow for reads."""

        return ToolPermissionPolicy(
            mode=kw.pop("mode", CoderMode.ASK),
            allow=[*READ_ONLY_TOOLS, *(kw.pop("allow", None) or [])],
            deny=kw.pop("deny", None),
            mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS),
            **kw,
        )

    def test_read_only_tools_are_allowed_without_a_prompt(self) -> None:
        policy = self._policy()

        assert policy.evaluate("check_runtime", {}).outcome is PermissionOutcome.ALLOW
        assert (
            policy.evaluate("recall_facts", {"query": "x"}).outcome
            is PermissionOutcome.ALLOW
        )

    def test_mutating_tools_still_ask(self) -> None:
        policy = self._policy()

        assert (
            policy.evaluate("submit_task", {"goal": "g"}).outcome is PermissionOutcome.ASK
        )
        assert (
            policy.evaluate("remember_fact", {"text": "x"}).outcome
            is PermissionOutcome.ASK
        )

    def test_explicit_deny_beats_the_standing_allow(self) -> None:
        """Precedence step 1: deny outranks the allow agent.build injected."""

        policy = self._policy(deny=["check_runtime"])

        assert policy.evaluate("check_runtime", {}).outcome is PermissionOutcome.DENY

    def test_plan_mode_still_refuses_remember_fact(self) -> None:
        policy = ToolPermissionPolicy(
            mode=CoderMode.PLAN,
            allow=[*READ_ONLY_TOOLS],
            mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS),
        )

        decision = policy.evaluate("remember_fact", {"text": "Project Alpha is active"})

        assert decision.outcome is PermissionOutcome.DENY
        assert "plan mode" in decision.reason