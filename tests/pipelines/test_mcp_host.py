"""``builtin.mcp-host`` — the externally-driven MCP session as a pipeline (W9-C9.4).

What these tests pin, in the loopback shape ``tests/mcp_server`` proved
(an ``asyncio.StreamReader`` fed the client's frames, an in-memory
:class:`~paa.mcp.protocol.FrameSink` collecting the server's replies):

* **the session is a pipeline run**: ``run_mcp_host_session`` — the one
  shared driver both CLI doors call — writes ``PIPELINE_RUN_STARTED``
  (``run_kind="mcp-host"``, ``resume="none"``, ``evidence_tier="T2"``),
  one ``mcp.session`` call PROPOSED before the first frame is read and
  COMPLETED at EOF, and ``PIPELINE_RUN_COMPLETED`` with the one metric
  the accounting wrapper owns (requests answered);
* **the dying-session and cancel paths close their rows before the
  exception propagates** — nothing half-closed, the fault named by type
  only (frame text is the client's words, never journalled);
* **the facade is an accounting wrapper, not a capability door**: every
  capability method refuses by naming the server's own per-call gate,
  and the harness label rides the PROPOSED row's fingerprint and shape
  preview — never a metric, never a payload echo.

The registration pins (the declaration, the registry row, the
conformance report) live with the neighbours: ``test_registry.py``,
``test_overlay.py`` and ``test_conformance.py`` carry the set-wide
locks every registered builtin must keep.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.chat.service import ChatService
from paa.config import ChatSettings, ContextSettings
from paa.core.errors import PaaError
from paa.core.types import EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.mcp.host import (
    MCP_HOST_CAPABILITIES,
    SESSION_METHOD,
    McpHostFacade,
    McpHostPipeline,
    run_mcp_host_session,
)
from paa.mcp.protocol import PROTOCOL_VERSION, SERVER_NAME, encode_frame
from paa.mcp.server import TOOL_MANIFEST, MCPServer
from paa.memory.creator import MemoryCreator
from paa.pipelines.base import PipelineStatus
from paa.pipelines.journal import CallJournal, fingerprint, read_call_journal
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database


def _server(ledger: LedgerStore, db: Database, tmp_path: Path) -> MCPServer:
    """One real server over the shared ledger/db — the conftest wiring, inline."""
    creator = MemoryCreator(db)
    signals = SignalRepository(db, ContentAddressedStore(tmp_path / "cold"))
    chat_settings = ChatSettings()  # the offline echo model
    service = ChatService(
        ledger=ledger,
        db=db,
        creator=creator,
        settings=chat_settings,
        permission_mode=PermissionMode.ASK,
        context_settings=ContextSettings(),
        signals=signals,
    )
    return MCPServer(
        chat=service,
        ledger=ledger,
        db=db,
        creator=creator,
        signals=signals,
        chat_settings=chat_settings,
    )


def _client_frames(*messages: dict[str, Any]) -> asyncio.StreamReader:
    """A fed reader: the harness's NDJSON frames, then EOF."""
    reader = asyncio.StreamReader()
    for message in messages:
        reader.feed_data(encode_frame(message))
    reader.feed_eof()
    return reader


def _initialize(id_: int = 0) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "loopback-test", "version": "0"},
        },
    }


async def _last_mcp_host_run(ledger: LedgerStore) -> uuid.UUID:
    """The newest ``builtin.mcp-host`` run lineage — for the paths that raise.

    ``run_mcp_host_session`` returns its run id on success only; a dying
    or cancelled session leaves the rows as the only witness, and the
    newest STARTED row with this pipeline's name is that witness.
    """
    events = await ledger.events_since(
        datetime(1970, 1, 1, tzinfo=UTC),
        event_types=[EventType.PIPELINE_RUN_STARTED],
        descending=True,
        limit=1,
    )
    assert events, "no PIPELINE_RUN_STARTED row was written"
    assert events[0].payload["pipeline"] == "builtin.mcp-host"
    return events[0].correlation_id


class _Recorder:
    """An in-memory :class:`paa.mcp.protocol.FrameSink`."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None


class _ExplodingSink(_Recorder):
    """A sink whose transport died: the first write raises."""

    def write(self, data: bytes) -> None:
        raise RuntimeError("broken pipe")


class _EventSink(_Recorder):
    """A recorder that signals the first answered request."""

    def __init__(self) -> None:
        super().__init__()
        self.answered = asyncio.Event()

    def write(self, data: bytes) -> None:
        super().write(data)
        self.answered.set()


_SCRIPTED_SESSION = (
    _initialize(),
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "sessions_list", "arguments": {}},
    },
)


class TestTheSessionRun:
    async def test_a_served_session_is_a_pipeline_run(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        server = _server(ledger, db, tmp_path)
        sink = _Recorder()
        run_id, result = await run_mcp_host_session(
            server,
            ledger,
            reader=_client_frames(*_SCRIPTED_SESSION),
            sink=sink,
            harness="loopback-test",
        )

        # The wire side: one reply per request, the notification silent.
        replies = [json.loads(frame.decode("utf-8")) for frame in sink.frames]
        assert [r.get("id") for r in replies] == [0, 1, 2]
        assert replies[0]["result"]["serverInfo"]["name"] == SERVER_NAME
        assert len(replies[1]["result"]["tools"]) == 6

        # The pipeline side: a completed run whose one metric is the count.
        assert result.status is PipelineStatus.COMPLETED
        assert result.metrics["requests_answered"] == 3
        assert result.metrics["evidence_tier"] == "T2"
        assert result.metrics["resume"] == "none"

        events = await ledger.read_correlation(run_id)
        assert [e.event_type for e in events] == [
            EventType.PIPELINE_RUN_STARTED,
            EventType.PIPELINE_CALL_PROPOSED,
            EventType.PIPELINE_CALL_COMPLETED,
            EventType.PIPELINE_RUN_COMPLETED,
        ]
        started = events[0].payload
        assert started["pipeline"] == "builtin.mcp-host"
        assert started["run_kind"] == "mcp-host"
        assert started["resume"] == "none"
        assert started["evidence_tier"] == "T2"
        proposed = events[1].payload
        assert proposed["method"] == SESSION_METHOD
        assert proposed["seq"] == 1

    async def test_the_session_call_identity_is_the_catalogue_plus_label(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        """The PROPOSED fingerprint covers exactly the served catalogue.

        The wrapper's call identity is what the session promised up front
        — the server, the pinned protocol revision, the six tools and the
        operator's harness label — so two identically-labelled sessions
        fold to the same fingerprint, and a different label does not.
        """
        server = _server(ledger, db, tmp_path)
        run_id, _ = await run_mcp_host_session(
            server,
            ledger,
            reader=_client_frames(_initialize()),
            sink=_Recorder(),
            harness="claude-code",
        )
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1
        expected = fingerprint(
            SESSION_METHOD,
            {
                "server": SERVER_NAME,
                "protocol_version": PROTOCOL_VERSION,
                "tools": list(TOOL_MANIFEST),
                "harness": "claude-code",
            },
        )
        assert records[0].fingerprint == expected
        assert records[0].outcome.value == "COMPLETED"
        assert records[0].result == {"requests_answered": 1}

    async def test_two_identical_sessions_fold_identically(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        """The wrapper's own sequence is deterministic, client notwithstanding."""
        folds: list[list[tuple[Any, ...]]] = []
        for _ in range(2):
            server = _server(ledger, db, tmp_path)
            run_id, _ = await run_mcp_host_session(
                server,
                ledger,
                reader=_client_frames(*_SCRIPTED_SESSION),
                sink=_Recorder(),
                harness="same-harness",
            )
            records = await read_call_journal(ledger, run_id)
            folds.append([(r.seq, r.method, r.fingerprint, r.outcome) for r in records])
        assert folds[0] == folds[1]
        assert len(folds[0]) == 1

    async def test_a_dying_transport_closes_the_rows_before_raising(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        """A session whose transport dies resolves FAILED, named by type only."""
        server = _server(ledger, db, tmp_path)
        with pytest.raises(RuntimeError):
            await run_mcp_host_session(
                server,
                ledger,
                reader=_client_frames(*_SCRIPTED_SESSION),
                sink=_ExplodingSink(),
            )
        run_id = await _last_mcp_host_run(ledger)
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1
        assert records[0].outcome.value == "FAILED"
        # The fault's type, never its message: a transport error can carry
        # frame text, and frame text is the client's words.
        assert records[0].error == "session did not resolve (RuntimeError)"
        events = await ledger.read_correlation(run_id)
        assert [e.event_type for e in events] == [
            EventType.PIPELINE_RUN_STARTED,
            EventType.PIPELINE_CALL_PROPOSED,
            EventType.PIPELINE_CALL_FAILED,
            EventType.PIPELINE_RUN_FAILED,
        ]
        assert events[3].payload["reason"] == "RuntimeError"

    async def test_a_cancelled_session_closes_its_rows(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        """A cancel landing between frames closes call and run FAILED('cancelled')."""
        server = _server(ledger, db, tmp_path)
        sink = _EventSink()
        # One frame, no EOF: the server answers it and parks in readline.
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(_initialize()))
        task = asyncio.create_task(
            run_mcp_host_session(
                server, ledger, reader=reader, sink=sink, harness="cancel-me"
            )
        )
        await sink.answered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        run_id = await _last_mcp_host_run(ledger)
        records = await read_call_journal(ledger, run_id)
        assert len(records) == 1
        assert records[0].outcome.value == "FAILED"
        assert records[0].error == "cancelled"
        events = await ledger.read_correlation(run_id)
        assert [e.event_type for e in events] == [
            EventType.PIPELINE_RUN_STARTED,
            EventType.PIPELINE_CALL_PROPOSED,
            EventType.PIPELINE_CALL_FAILED,
            EventType.PIPELINE_RUN_FAILED,
        ]
        assert events[3].payload["reason"] == "cancelled"


class TestTheFacade:
    async def test_capability_doors_name_the_real_door(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        server = _server(ledger, db, tmp_path)
        facade = McpHostFacade(
            server=server,
            journal=CallJournal(ledger, uuid.uuid4()),
            reader=_client_frames(),
            sink=_Recorder(),
        )
        with pytest.raises(PaaError, match="server's own"):
            await facade.think("what next?")
        with pytest.raises(PaaError, match="server's own"):
            await facade.use_skill("read_file", {"path": "x"})
        with pytest.raises(PaaError, match="server's own"):
            await facade.recall("anything")
        with pytest.raises(PaaError, match="server's own"):
            await facade.remember({"statement": "s"})
        with pytest.raises(PaaError, match="delegates nothing"):
            await facade.sub_run("builtin.chat", {})

    async def test_the_harness_label_never_rides_a_payload_or_metric(
        self, ledger: LedgerStore, db: Database, tmp_path: Path
    ) -> None:
        """The label is fingerprint-and-shape only — the N2.5 canary rule.

        An operator's label can hold anything (it is free text on a CLI
        flag), so the wrapper treats it exactly as the coder journal
        treats a prompt: attested by the call's fingerprint, rendered as
        a shape in the preview, echoed nowhere else.
        """
        server = _server(ledger, db, tmp_path)
        marker = f"SEKRIT-{uuid.uuid4().hex[:12]}"
        run_id, result = await run_mcp_host_session(
            server,
            ledger,
            reader=_client_frames(_initialize()),
            sink=_Recorder(),
            harness=f"label-with-{marker}",
        )
        assert marker not in json.dumps(result.metrics)
        for event in await ledger.read_correlation(run_id):
            assert marker not in json.dumps(dict(event.payload), sort_keys=True)
        # The positive control: the marker really did ride the call's
        # identity — the fingerprint covers it.
        records = await read_call_journal(ledger, run_id)
        assert records[0].fingerprint == fingerprint(
            SESSION_METHOD,
            {
                "server": SERVER_NAME,
                "protocol_version": PROTOCOL_VERSION,
                "tools": list(TOOL_MANIFEST),
                "harness": f"label-with-{marker}",
            },
        )

    def test_the_ask_is_the_six_tools_own_union(self) -> None:
        """No drift between the session ask and the tool manifest it restates."""
        union = frozenset().union(
            *(spec.capabilities for spec in TOOL_MANIFEST.values())
        )
        assert union == MCP_HOST_CAPABILITIES
        assert McpHostPipeline.capabilities == union

    def test_the_pipeline_identity(self) -> None:
        assert McpHostPipeline.name == "builtin.mcp-host"
        assert McpHostPipeline.version == "4.1.0"
        assert McpHostPipeline.requires_runtime == ">=4.1,<5"
        assert McpHostPipeline.required_permissions == frozenset()
        # Honest limits: unbounded, because the session is externally
        # paced and the binding disciplines are the server's own.
        limits = McpHostPipeline.limits
        assert limits.max_tokens is None
        assert limits.max_wall_s is None
        assert limits.max_calls is None
        assert limits.max_sub_runs is None
