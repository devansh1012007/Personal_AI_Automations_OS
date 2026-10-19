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

The last section goes the other way round. The permission tests here used to
assert against a hand-rolled *mirror* of :meth:`CoderAgent.build`'s policy
construction — a mirror proves the policy behaves, not that the composition
wires it, so a guarantee deleted from the composition passed the whole suite.
Those tests now go through the real composition: a real runtime, a real
``AgentLoop``, and the policy the composition actually built.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from paa.coder.agent import MUTATING_RUNTIME_TOOLS, CoderAgent
from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.permissions import (
    PermissionMode as CoderMode,
)
from paa.coder.runtime_tools import READ_ONLY_TOOLS, build_runtime_tools
from paa.coder.session import Session
from paa.config import Settings
from paa.core.types import EventType, PermissionMode, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, dumps, to_iso, utc_now


class FakeProvider:
    """Named stand-in so ``type(...).__name__`` assertions read sensibly."""

    name = "openrouter"
    model = "openai/gpt-4o-mini"


class StubRuntime:
    """The slice of ``Runtime`` the tools actually touch."""

    def __init__(self, ledger: LedgerStore, db: Database, cold_lake_path: Path) -> None:
        self.ledger = ledger
        self.db = db
        self.settings = SimpleNamespace(
            home="/tmp/paa-test",
            policy=SimpleNamespace(mode=PermissionMode.ASK),
            storage=SimpleNamespace(
                backend_relational="sqlite",
                cold_lake_path=cold_lake_path,
            ),
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
def rt(db: Database, ledger: LedgerStore, tmp_path: Path) -> StubRuntime:
    return StubRuntime(ledger, db, tmp_path / "cold_lake")


@pytest.fixture
def tools(rt: StubRuntime) -> dict[str, Any]:
    return build_runtime_tools(cast(Any, rt))


class ScriptedToolModel:
    """Emits a pre-programmed sequence of turns, one per ``propose()`` call.

    The coder agent's model seam is :class:`~paa.coder.loop.ToolCallingModel`
    (``propose(window, tools) -> ModelTurn``) — not the ``complete_structured``
    seam the planners use — so this is the double that fits it. It mirrors
    the one in ``tests/coder/test_agent.py``; each lives in its own module
    because each is a handful of lines and a shared conftest would be a worse
    trade than the duplication.
    """

    def __init__(self, *turns: ModelTurn) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls += 1
        return self._turns.pop(0) if self._turns else ModelTurn(text="nothing left to do")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated ``PAA_HOME`` so a composed agent never touches real state."""
    root = tmp_path / "paa_home"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def agent_settings(home: Path) -> Settings:
    settings = Settings(home=home)
    settings.ensure_directories()
    return settings


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


class TestRememberFactColdLake:
    """The write path: a cold-lake row first, facts derived from it.

    The defect these pin is architectural, not cosmetic. The tool used to
    write ``hot_serving_active_facts`` directly, with ``source_signal_id =
    None`` and a ``provenance`` blob naming a signal id that resolved to no
    row anywhere: the fact was not derivable from anything, so it could not
    be rebuilt. Now the assertion is filed in ``cold_lake_signals`` first and
    the fact links to it.
    """

    async def test_the_assertion_is_filed_in_the_cold_lake(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        result = json.loads(
            await tools["remember_fact"].run({"text": "Project Alpha is active"})
        )

        rows = await db.fetch_all(
            "SELECT id, channel, external_id, sync_status, raw_payload "
            "FROM cold_lake_signals"
        )
        assert len(rows) == 1
        assert rows[0]["channel"] == "coder"
        assert rows[0]["sync_status"] == "processed"
        assert "Project Alpha is active" in rows[0]["raw_payload"]
        assert result["source_signal_id"] == rows[0]["id"]

    async def test_the_fact_carries_a_resolvable_source_signal(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        """The foreign key must resolve to a row, not dangle."""

        await tools["remember_fact"].run({"text": "Project Alpha is active"})

        facts = await FactRepository(db).query_facts()
        assert len(facts) == 1
        signal_id = facts[0].source_signal_id
        assert signal_id is not None
        resolved = await db.fetch_one(
            "SELECT id FROM cold_lake_signals WHERE id = ?", (signal_id,)
        )
        assert resolved is not None, "source_signal_id resolves to no row"

    async def test_the_same_assertion_twice_is_one_row_and_one_fact(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        """The external id is the assertion's digest, so a retry is a no-op."""

        first = json.loads(
            await tools["remember_fact"].run({"text": "Project Alpha is active"})
        )
        second = json.loads(
            await tools["remember_fact"].run({"text": "Project Alpha is active"})
        )

        rows = await db.fetch_all("SELECT id FROM cold_lake_signals")
        assert len(rows) == 1
        facts = await FactRepository(db).query_facts()
        assert len(facts) == 1
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        assert second["source_signal_id"] == first["source_signal_id"]

    async def test_the_daemon_batch_cannot_reprocess_a_remembered_signal(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        """The daemon must not find the signal and write the fact a second time.

        This is the claim the docstring makes, so it is asserted rather than
        argued: after the tool has processed its own row, a daemon-style
        ``run_batch`` claims nothing and writes nothing.
        """

        await tools["remember_fact"].run({"text": "Project Alpha is active"})

        report = await MemoryCreator(db).run_batch()

        assert report.claimed == 0
        assert report.facts_written == 0
        facts = await FactRepository(db).query_facts()
        assert len(facts) == 1

    async def test_the_fact_is_derivable_from_the_cold_lake(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        """The row is enough: wipe hot serving and replay it, and the fact returns.

        This is the property the direct write lacked. The row in
        ``cold_lake_signals`` holds the verbatim assertion and the creator's
        extraction is deterministic, so the same row derives the same fact —
        which is what makes the agent's memories rebuildable the way every
        other memory in the system is.
        """
        await tools["remember_fact"].run({"text": "Project Alpha is active"})
        original = await FactRepository(db).query_facts()
        assert len(original) == 1

        # Wipe hot serving and put the signal back in the queue, the way a
        # rebuild does.
        await db.execute("DELETE FROM hot_serving_active_facts")
        await db.execute("DELETE FROM hot_serving_entity_index")
        await db.execute("UPDATE cold_lake_signals SET sync_status = 'unprocessed'")

        report = await MemoryCreator(db).run_batch()

        assert report.claimed == 1
        assert report.facts_written == 1
        rebuilt = await FactRepository(db).query_facts()
        assert len(rebuilt) == 1
        assert rebuilt[0].predicate == original[0].predicate
        assert rebuilt[0].object_value == original[0].object_value
        assert rebuilt[0].source_signal_id == original[0].source_signal_id

    async def test_the_report_names_the_cold_lake_row_as_the_evidence(
        self, tools: dict[str, Any]
    ) -> None:
        """The provenance must point at something that exists."""

        result = json.loads(
            await tools["remember_fact"].run({"text": "Project Alpha is active"})
        )

        assert result["provenance"]["source"] == "cold_lake_signals"
        assert result["provenance"]["channel"] == "coder"
        assert result["provenance"]["sync_status"] == "processed"
        assert result["provenance"]["external_id"].startswith("coder:")

    async def test_a_signal_another_worker_claimed_is_not_written_twice(
        self, tools: dict[str, Any], db: Database
    ) -> None:
        """The daemon's claim wins the race, and the tool writes no facts.

        The row is filed and claimed the way the daemon's ``run_batch`` would
        leave it, and the tool is then called with the same assertion. The
        fact must not be written a second time: the tool waits for the row to
        settle and reports that the fact is not in memory yet.
        """
        text = "Project Alpha is active"
        payload = dumps(text)
        await db.execute(
            "INSERT INTO cold_lake_signals (id, received_at, channel, external_id, "
            "raw_payload, content_hash, blob_uri, sync_status, processed_at, error_detail) "
            "VALUES (?, ?, 'coder', ?, ?, ?, NULL, 'processing', NULL, NULL)",
            (
                str(uuid.uuid4()),
                to_iso(utc_now()),
                f"coder:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ),
        )
        tools["remember_fact"]._SETTLE_TIMEOUT_SECONDS = 0.1

        result = json.loads(await tools["remember_fact"].run({"text": text}))

        assert result["outcome"] == "pending"
        assert result["remembered"] is False
        assert result["facts_written"] == 0
        assert result["provenance"]["sync_status"] == "processing"
        assert await FactRepository(db).query_facts() == []


class TestSearchFactsByText:
    """The keyword tier of recall: a scan bound, not a ranking.

    The method used to order the candidate pool by ``importance`` in SQL and
    then rank it by confidence in Python, so a high-confidence, low-importance
    fact outside the top slice was invisible — and the docstring's claim that
    the ranking "mirrors query_facts" was half true.
    """

    async def test_like_metacharacters_match_literally(self, db: Database) -> None:
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Widget")
        await repo.add_fact(eid, "is", "50% complete", confidence=0.9)
        await repo.add_fact(eid, "is", "a_b", confidence=0.9)
        await repo.add_fact(eid, "is", "back\\slash", confidence=0.9)
        await repo.add_fact(eid, "is", "plain value", confidence=0.9)

        assert [f.object_value for f in await repo.search_facts_by_text("%")] == [
            "50% complete"
        ]
        assert [f.object_value for f in await repo.search_facts_by_text("_")] == ["a_b"]
        assert [f.object_value for f in await repo.search_facts_by_text("\\")] == [
            "back\\slash"
        ]

    async def test_like_metacharacters_do_not_widen_the_scan(self, db: Database) -> None:
        """A stray ``%`` must not turn a keyword search into a table dump."""

        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Widget")
        await repo.add_fact(eid, "is", "50% complete", confidence=0.9)
        for index in range(5):
            await repo.add_fact(eid, "has", f"part {index}", confidence=0.9)

        found = await repo.search_facts_by_text("%")

        assert [f.object_value for f in found] == ["50% complete"]

    async def test_a_superseded_fact_is_not_returned(self, db: Database) -> None:
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", "Widget")
        old = await repo.add_fact(eid, "is", "active", confidence=0.9)
        new = await repo.add_fact(eid, "is", "paused", confidence=0.9)
        await repo.supersede(old, new)

        assert await repo.search_facts_by_text("active") == []
        assert [f.id for f in await repo.search_facts_by_text("paused")] == [new]


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
    """The policy *as a policy*: a hand-built mirror of the composition.

    A mirror cannot catch the composition being changed, so these are the
    behaviour of :class:`~paa.coder.permissions.ToolPermissionPolicy` under
    the arguments :meth:`CoderAgent.build` uses. The wiring itself is
    asserted in :class:`TestAskStandingAllowsOnTheRealWiring` below.
    """

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


class TestAskStandingAllowsOnTheRealWiring:
    """What :meth:`CoderAgent.build` actually wires into the loop.

    The mirror above proves the *policy* behaves. It cannot prove the
    *composition* wires it: deleting the read-only half from
    ``CoderAgent.build``'s ``allow=`` left the mirror green and the agent
    asking about ``check_runtime({})`` on every turn — and that headline
    behaviour is the one guarantee the whole change exists to deliver.

    So these go through the real thing — a real ``Runtime``, a real
    ``AgentLoop``, and the policy the composition actually built. Two routes
    are used on purpose:

    * the *policy* route reads the decisions off the built policy, which pins
      the exact outcomes (ALLOW / ASK / DENY) rather than their consequences;
    * the *loop* route drives the agent end to end and watches the confirm
      callback, which pins what an operator experiences — a read that runs
      and a write that stops.

    The policy route says *which* outcome the composition bought; the loop
    route says what the composition *did*, and it would still hold if the
    policy's internals were rewritten.
    """

    async def test_the_built_policy_allows_reads_and_asks_for_writes(
        self, agent_settings: Settings
    ) -> None:
        agent = await CoderAgent.build(agent_settings, model=ScriptedToolModel())
        try:
            # ``AgentLoop`` exposes no public accessor for the policy it was
            # handed, and the policy is the object under test here — the
            # end-to-end test below asserts the same guarantee through the
            # loop's public behaviour, so nothing here rests on the private
            # attribute alone.
            policy = agent.loop._perms
            assert policy.evaluate("check_runtime", {}).outcome is PermissionOutcome.ALLOW
            assert (
                policy.evaluate("recall_facts", {"query": "x"}).outcome
                is PermissionOutcome.ALLOW
            )
            assert (
                policy.evaluate("submit_task", {"goal": "g"}).outcome is PermissionOutcome.ASK
            )
            assert (
                policy.evaluate("remember_fact", {"text": "x"}).outcome
                is PermissionOutcome.ASK
            )
        finally:
            await agent.close()

    async def test_a_read_only_call_does_not_prompt_and_a_mutating_one_does(
        self, agent_settings: Settings
    ) -> None:
        """The behaviour an operator sees: reads run, writes ask.

        The confirm callback declines every ASK, so the mutating call is never
        executed and the test stays hermetic — the assertion is about *whether
        the prompt fired*, not about what happened after the answer.
        """

        prompted: list[str] = []

        async def confirm(tool: str, arguments: dict[str, Any], reason: str) -> bool:
            prompted.append(tool)
            return False

        model = ScriptedToolModel(
            ModelTurn(tool_calls=[ToolCall("check_runtime", {})]),
            ModelTurn(tool_calls=[ToolCall("recall_facts", {"query": "nothing at all"})]),
            ModelTurn(tool_calls=[ToolCall("submit_task", {"goal": "g"})]),
            ModelTurn(text="done"),
        )
        agent = await CoderAgent.build(agent_settings, model=model, confirm=confirm)
        try:
            result = await agent.ask("what is live?")
            results = [
                t.content for t in agent.session.turns if t.role.value == "tool_result"
            ]
        finally:
            await agent.close()

        # The read-only calls ran without a prompt; only the state-changing
        # one was put to the human.
        assert prompted == ["submit_task"]
        assert result.tool_calls == 3
        assert result.denied_calls == 1
        # ...and they really ran: the two reads returned their payloads and
        # the write stopped at the prompt.
        assert "open_tasks" in results[0]
        assert "count" in results[1]
        assert results[2] == "declined by user"

    async def test_an_explicit_deny_still_beats_the_standing_allow(
        self, agent_settings: Settings
    ) -> None:
        """Precedence step 1: deny outranks the allow the composition injected."""

        agent = await CoderAgent.build(
            agent_settings, model=ScriptedToolModel(), deny=["check_runtime"]
        )
        try:
            decision = agent.loop._perms.evaluate("check_runtime", {})
        finally:
            await agent.close()

        assert decision.outcome is PermissionOutcome.DENY

    async def test_plan_mode_still_refuses_remember_fact(
        self, agent_settings: Settings
    ) -> None:
        """PLAN is read-only whatever the standing allow says."""

        agent = await CoderAgent.build(
            agent_settings, model=ScriptedToolModel(), mode=CoderMode.PLAN
        )
        try:
            decision = agent.loop._perms.evaluate(
                "remember_fact", {"text": "Project Alpha is active"}
            )
        finally:
            await agent.close()

        assert decision.outcome is PermissionOutcome.DENY
        assert "plan mode" in decision.reason


class TestLatestSessionTieBreak:
    """Which transcript ``--continue`` picks when two share a recency moment.

    :meth:`Session.latest_session` breaks a recency tie on the session id, so
    two transcripts written in the same clock tick resolve the same way every
    time. A tie broken by "whichever the filesystem listed first" would answer
    differently on a different filesystem, and — worse — differently after a
    directory rewrite, for the same transcripts.

    The listing order is therefore the thing the guarantee refuses to depend
    on, so it is *forced* here rather than left to chance: a filesystem is
    free to hand back either order, and a test that happens to pass because
    the local filesystem happened to agree proves nothing.

    The transcripts carry no turn, so the recency signal is the file's mtime
    — the fallback path — which keeps the test about the tie-break itself
    rather than about whichever signal the transcript happens to hold.
    """

    def test_a_tie_breaks_on_the_id_not_on_the_listing_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "aaa.jsonl").write_text("{}\n", encoding="utf-8")
        (tmp_path / "zzz.jsonl").write_text("{}\n", encoding="utf-8")
        stamp = 1_000_000.0
        os.utime(tmp_path / "aaa.jsonl", (stamp, stamp))
        os.utime(tmp_path / "zzz.jsonl", (stamp, stamp))

        def listed_low_first(self: Path, pattern: str) -> list[Path]:
            return [self / "aaa.jsonl", self / "zzz.jsonl"]

        monkeypatch.setattr(Path, "glob", listed_low_first)

        assert Session.latest_session(tmp_path) == "zzz"