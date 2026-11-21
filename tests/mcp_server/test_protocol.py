"""MCP protocol conformance: handshake, framing, error codes, token refusal.

What is pinned here
-------------------
* The ``initialize`` handshake: the pinned protocol version
  (``2024-11-05`` — the exact string the client adapter pins, so PAA-client
  ↔ PAA-server loopback is symmetric), server info, tools capability; a
  missing version is invalid params; an unsupported-but-present version gets
  the server's pinned version back (the MCP contract — the client decides).
* The auth gate: with a token configured, every method before an
  authenticated handshake is refused ``-32009`` BEFORE anything executes,
  and every refusal is a ledger event (``POLICY_BLOCKED`` on its own
  lineage) that never carries the token value.
* The error table: parse/request/params/method-not-found, per
  :mod:`paa.mcp.protocol`.
* Notifications answer nothing; the loop keeps serving after malformed
  frames (one bad line must not end the connection — the client adapter's
  own resilience contract, mirrored).
* Framing: NDJSON out; NDJSON and ``Content-Length`` both read (per-message
  autodetect, the client adapter's framing inverted).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from paa.ledger.events import canonical_json
from paa.mcp.protocol import (
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PERMISSION_DENIED,
    PROTOCOL_VERSION,
    SERVER_NAME,
    encode_frame,
)
from tests.mcp_server.conftest import error_code, structured

INITIALIZE = {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "test-harness", "version": "0"},
}


class TestHandshake:
    async def test_initialize_pins_the_protocol_version(self, mcp) -> None:
        h = mcp()
        result = await h.initialize()
        assert result["protocolVersion"] == PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == SERVER_NAME
        assert result["capabilities"]["tools"]["listChanged"] is False

    async def test_missing_protocol_version_is_invalid_params(self, mcp) -> None:
        h = mcp()
        response = await h.call("initialize", {"clientInfo": {}})
        assert error_code(response) == INVALID_PARAMS

    async def test_unsupported_version_gets_the_pinned_version_back(self, mcp) -> None:
        h = mcp()
        response = await h.call("initialize", {**INITIALIZE, "protocolVersion": "1900-01-01"})
        assert "error" not in response
        # The MCP contract: the server answers with its own version and the
        # *client* decides whether it can live with that.
        assert response["result"]["protocolVersion"] == PROTOCOL_VERSION

    async def test_reinitialize_is_idempotent(self, mcp) -> None:
        h = mcp()
        first = await h.initialize()
        second = await h.initialize()
        assert first["serverInfo"] == second["serverInfo"]


class TestSurface:
    async def test_every_method_before_handshake_is_refused_and_journaled(self, mcp) -> None:
        h = mcp()
        for method in ("tools/list", "tools/call", "ping"):
            response = await h.call(method, {"name": "sessions_list", "arguments": {}})
            assert error_code(response) == AUTH_REQUIRED
        refusals = await h.refusal_lineages()
        assert len(refusals) == 3
        assert all(row.payload["surface"] == "mcp" for row in refusals)
        assert all(row.payload["authenticated"] is False for row in refusals)

    async def test_tools_list_serves_the_six_contract_tools(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call("tools/list")
        tools = response["result"]["tools"]
        assert [t["name"] for t in tools] == [
            "memory_query",
            "memory_search",
            "memory_remember",
            "context_bundle",
            "chat_send",
            "sessions_list",
        ]
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            # The client adapter needs a description it can carry verbatim.
            assert len(tool["description"]) >= 20

    async def test_ping_after_handshake(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call("ping")
        assert response["result"] == {}

    async def test_unknown_method_is_method_not_found(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call("resources/list")
        assert error_code(response) == METHOD_NOT_FOUND

    async def test_unknown_tool_is_invalid_params_without_a_journal_row(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        before = await h.ledger.count()
        response = await h.call("tools/call", {"name": "not_a_tool", "arguments": {}})
        assert error_code(response) == INVALID_PARAMS
        # An unknown tool never formed a call, so nothing was proposed.
        assert await h.ledger.count() == before

    async def test_notification_produces_no_response(self, mcp) -> None:
        h = mcp()
        assert (
            await h.server.handle_message(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            is None
        )
        # An unknown notification is likewise silently accepted: a
        # notification has no id, so there is nothing to reply to.
        assert (
            await h.server.handle_message({"jsonrpc": "2.0", "method": "notifications/other"})
            is None
        )


class TestMalformedInput:
    async def test_parse_error(self, mcp) -> None:
        h = mcp()
        response = await h.server.handle_payload("this is not json")
        assert response is not None
        assert error_code(response) == PARSE_ERROR

    async def test_non_object_frame_is_invalid_request(self, mcp) -> None:
        h = mcp()
        response = await h.server.handle_message(["not", "an", "object"])
        assert response is not None
        assert error_code(response) == INVALID_REQUEST

    async def test_response_shaped_object_is_invalid_request(self, mcp) -> None:
        h = mcp()
        response = await h.server.handle_message({"jsonrpc": "2.0", "id": 3, "result": {}})
        assert response is not None
        assert error_code(response) == INVALID_REQUEST

    async def test_non_object_params_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.server.handle_message(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": ["x"]}
        )
        assert response is not None
        assert error_code(response) == INVALID_PARAMS

    async def test_the_loop_survives_malformed_frames(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        first = await h.server.handle_payload("{oops")
        assert first is not None
        assert error_code(first) == PARSE_ERROR
        second = await h.call("ping")
        assert second["result"] == {}


class TestTokenAuth:
    async def test_missing_token_is_refused_before_anything_executes(self, mcp) -> None:
        h = mcp(expected_token="t-opaque")
        response = await h.call("initialize", INITIALIZE)
        assert error_code(response) == AUTH_REQUIRED
        refusals = await h.refusal_lineages()
        assert len(refusals) == 1
        assert refusals[0].payload["method"] == "initialize"

    async def test_wrong_token_is_refused_and_journaled(self, mcp) -> None:
        h = mcp(expected_token="t-opaque")
        response = await h.call("initialize", {**INITIALIZE, "token": "t-wrong"})
        assert error_code(response) == AUTH_REQUIRED
        # A refused handshake never opens the surface.
        follow_up = await h.call("tools/list")
        assert error_code(follow_up) == AUTH_REQUIRED
        assert len(await h.refusal_lineages()) == 2

    async def test_correct_token_opens_the_surface(self, mcp) -> None:
        h = mcp(expected_token="t-opaque")
        await h.initialize(token="t-opaque")
        response = await h.call("tools/list")
        assert len(response["result"]["tools"]) == 6

    async def test_token_value_never_enters_the_ledger(self, mcp) -> None:
        secret = "t-opaque"
        h = mcp(expected_token=secret)
        await h.call("initialize", INITIALIZE)  # refused: missing
        await h.call("initialize", {**INITIALIZE, "token": "t-wrong"})  # refused: wrong
        await h.initialize(token=secret)  # accepted
        await h.call("sessions_list")  # one real tool call on top
        events = await h.ledger.events_since(datetime(1970, 1, 1, tzinfo=UTC))
        assert events, "the run wrote events"
        for event in events:
            blob = canonical_json(event.payload)
            assert secret not in blob
            assert "t-wrong" not in blob

    async def test_no_token_configured_means_open_spawn_pipe(self, mcp) -> None:
        h = mcp()  # no expected_token
        await h.initialize()  # no token param needed
        response = await h.call("ping")
        assert response["result"] == {}
        assert await h.refusal_lineages() == []


class _Recorder:
    """An in-memory :class:`paa.mcp.protocol.FrameSink` for the framing tests."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None


def _feed(frames: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(frames)
    reader.feed_eof()
    return reader


class TestFraming:
    async def test_ndjson_round_trip_through_serve(self, mcp) -> None:
        h = mcp()
        reader = _feed(
            encode_frame(
                {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INITIALIZE}
            )
            + encode_frame({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + encode_frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "sessions_list", "arguments": {}},
                }
            )
        )
        sink = _Recorder()
        await h.server.serve(reader, sink)

        received = [json.loads(frame.decode("utf-8")) for frame in sink.frames]
        # One reply per request, in order; the notification stays silent.
        assert [r.get("id") for r in received] == [0, 1, 2]
        assert received[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert len(received[1]["result"]["tools"]) == 6
        payload = structured(received[2]["result"])
        assert payload["sessions"] == []

    async def test_content_length_framed_request_is_read(self, mcp) -> None:
        h = mcp()
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": INITIALIZE}
        )
        frame = f"Content-Length: {len(body.encode())}\r\n\r\n{body}".encode()
        sink = _Recorder()
        await h.server.serve(_feed(frame + b"\n"), sink)
        assert len(sink.frames) == 1
        reply = json.loads(sink.frames[0].decode("utf-8"))
        assert reply["id"] == 7
        assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION

    async def test_replies_are_valid_ndjson_frames(self, mcp) -> None:
        frame = encode_frame({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert frame.endswith(b"\n")
        assert json.loads(frame.decode("utf-8"))["id"] == 1

    async def test_malformed_line_inside_a_stream_still_gets_its_reply(
        self, mcp
    ) -> None:
        h = mcp()
        sink = _Recorder()
        await h.server.serve(_feed(b"{oops\n" + encode_frame(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INITIALIZE}
        )), sink)
        assert len(sink.frames) == 2
        assert json.loads(sink.frames[0].decode("utf-8"))["error"]["code"] == PARSE_ERROR
        assert json.loads(sink.frames[1].decode("utf-8"))["id"] == 1


class TestInternalFault:
    async def test_internal_fault_is_internal_error_not_a_crash(
        self, mcp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = mcp()
        await h.initialize()

        async def exploding() -> tuple[dict[str, Any], bool]:
            raise RuntimeError("boom")

        # A handler that explodes unexpectedly must come back as -32603 with
        # the connection intact, never as a hung or dropped request.
        monkeypatch.setattr(h.server, "_tool_sessions_list", exploding)
        response = await h.call("tools/call", {"name": "sessions_list", "arguments": {}})
        assert error_code(response) == INTERNAL_ERROR
        # The call's FAILED row landed: the ledger shows the attempt, not a
        # silent exception.
        lineage = await h.tool_call_lineage()
        assert await h.kinds(lineage) == ["TOOL_CALL_PROPOSED", "TOOL_CALL_FAILED"]

    async def test_ledger_rows_for_one_call_carry_the_payload_contract(
        self, mcp
    ) -> None:
        h = mcp()
        await h.initialize()
        await h.call("tools/call", {"name": "sessions_list", "arguments": {}})
        lineage = await h.tool_call_lineage()
        events = await h.events(lineage)
        proposed = next(e for e in events if e.event_type.value == "TOOL_CALL_PROPOSED")
        completed = next(e for e in events if e.event_type.value == "TOOL_CALL_COMPLETED")
        for row in (proposed, completed):
            payload = row.payload
            assert payload["v"] == 1
            assert payload["tool"] == "sessions_list"
            assert ":" not in payload["call_id"]
        assert proposed.payload["isolation_level"] == "NONE"
        assert completed.payload["result_sha256"]
        # One call, one lineage, proposed strictly before the outcome.
        assert proposed.sequence_id is not None
        assert completed.sequence_id is not None
        assert proposed.sequence_id < completed.sequence_id

    async def test_denial_never_executes_the_tool(self, mcp) -> None:
        """A policy refusal is a journaled DENIED row, not a half-run."""
        from paa.core.types import PermissionMode

        h = mcp(permission_mode=PermissionMode.LOCKDOWN)
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        response = await h.call(
            "tools/call",
            {
                "name": "memory_remember",
                "arguments": {"statement": "should never land", "session_id": str(sid)},
            },
        )
        assert error_code(response) == PERMISSION_DENIED
        lineage = await h.tool_call_lineage()
        assert await h.kinds(lineage) == ["TOOL_CALL_PROPOSED", "TOOL_CALL_DENIED"]
        # ...and no signal exists: the refusal preceded the write.
        signals = [s async for s in h.signals.iter_by_channel("chat.turn", newest_first=True)]
        assert signals == []
