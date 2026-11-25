"""The server's request router in-process: envelopes, handshake, sessions.

The ``mcp_server`` suite's shape applied to the ACP front — every request
gets the honest JSON-RPC answer (a code, a message, no crash), the
handshake pins the protocol version, and a session is a chat session with
the ACP session id as its ledger lineage, so the wire and the journal
cannot disagree about what happened.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from paa.acp import protocol as wire
from paa.chat.loop import ChatReply
from paa.core.types import EventType

LONG_TEXT = "stream me across several twelve-character chunks, please. " * 3


class TestHandshake:
    async def test_initialize_pins_the_major_version(self, acp) -> None:
        h = acp()
        result = await h.initialize()
        assert result["protocolVersion"] == wire.PROTOCOL_VERSION
        assert result["agentCapabilities"] == {"loadSession": False}
        assert result["agentInfo"]["name"] == wire.AGENT_NAME
        assert result["agentInfo"]["version"] == wire.AGENT_VERSION
        assert result["authMethods"] == []

    async def test_a_different_client_version_still_gets_our_pinned_one(self, acp) -> None:
        """ACP's negotiation contract: the reply carries OUR version; the
        client decides what to do about the mismatch."""
        h = acp()
        result = await h.initialize(protocolVersion=99)
        assert result["protocolVersion"] == wire.PROTOCOL_VERSION

    async def test_no_version_is_malformed_params(self, acp) -> None:
        h = acp()
        response = await h.call("initialize", {"clientInfo": {}})
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_a_non_integer_version_is_malformed_params(self, acp) -> None:
        h = acp()
        response = await h.call("initialize", {"protocolVersion": "1"})
        assert response["error"]["code"] == wire.INVALID_PARAMS


class TestEnvelopes:
    async def test_a_request_before_the_handshake_is_an_invalid_request(self, acp) -> None:
        h = acp()
        response = await h.call("session/new", {"cwd": str(h.workspace)})
        assert response["error"]["code"] == wire.INVALID_REQUEST

    async def test_an_unknown_method_is_method_not_found(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call("session/load", {"sessionId": "x"})
        assert response["error"]["code"] == wire.METHOD_NOT_FOUND

    async def test_invalid_json_is_a_parse_error(self, acp) -> None:
        h = acp()
        response = await h.server.handle_payload("{not json")
        assert response is not None
        assert response["error"]["code"] == wire.PARSE_ERROR

    async def test_a_non_object_frame_is_an_invalid_request(self, acp) -> None:
        h = acp()
        response = await h.server.handle_message([1, 2, 3])
        assert response is not None
        assert response["error"]["code"] == wire.INVALID_REQUEST

    async def test_a_frame_with_no_method_is_an_invalid_request(self, acp) -> None:
        h = acp()
        response = await h.server.handle_message({"jsonrpc": "2.0", "id": 1})
        assert response is not None
        assert response["error"]["code"] == wire.INVALID_REQUEST

    async def test_non_object_params_on_a_request_is_invalid_params(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": [1]}
        )
        assert response is not None
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_an_unknown_notification_is_silence(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.server.handle_message(
            {"jsonrpc": "2.0", "method": "workspace/didChangeConfiguration"}
        )
        assert response is None

    async def test_a_malformed_notification_is_silence_not_a_crash(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.server.handle_message(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": [1]}
        )
        assert response is None

    async def test_the_router_survives_garbage_and_keeps_serving(self, acp) -> None:
        """Totality over input: junk frames answer errors, and the very
        next request still works — no read loop dies on a bad message."""
        h = acp()
        for junk in ("null", '"a string"', "42", "{bad", "[]"):
            response = await h.server.handle_payload(junk)
            assert response is not None
            assert "error" in response
        result = await h.initialize()
        assert result["protocolVersion"] == wire.PROTOCOL_VERSION


class TestSessions:
    async def test_session_new_binds_the_chat_lineage_to_the_acp_id(self, acp) -> None:
        h = acp()
        await h.initialize()
        session_id = await h.new_session()
        assert uuid.UUID(session_id)  # the ACP id IS the chat session id
        events = await h.events(uuid.UUID(session_id))
        started = [e for e in events if e.event_type is EventType.CHAT_SESSION_STARTED]
        assert started, "no CHAT_SESSION_STARTED row for the wire session"
        assert started[0].payload["scope"] == "acp"

    async def test_session_new_without_a_cwd_is_invalid_params(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call("session/new", {})
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_session_new_with_a_missing_cwd_is_invalid_params(self, acp, tmp_path) -> None:
        h = acp()
        await h.initialize()
        response = await h.call("session/new", {"cwd": str(tmp_path / "not-there")})
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_session_new_with_a_file_cwd_is_invalid_params(self, acp, tmp_path) -> None:
        h = acp()
        target = tmp_path / "a-file"
        target.write_text("not a directory")
        await h.initialize()
        response = await h.call("session/new", {"cwd": str(target)})
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_malformed_mcp_servers_are_refused_for_shape(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call(
            "session/new", {"cwd": str(h.workspace), "mcpServers": [{"name": "x"}]}
        )
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_well_shaped_mcp_servers_are_accepted_and_ignored(self, acp) -> None:
        """v1 connects none of them, but mis-declared config must still be
        heard about now (the handshake declares no mcpCapabilities)."""
        h = acp()
        await h.initialize()
        response = await h.call(
            "session/new",
            {
                "cwd": str(h.workspace),
                "mcpServers": [{"name": "x", "command": "run-x", "args": []}],
            },
        )
        assert "error" not in response

    async def test_an_unknown_session_is_resource_not_found_with_the_id(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call(
            "fs/read_text_file", {"sessionId": "nope", "path": "/etc/hostname"}
        )
        assert response["error"]["code"] == wire.RESOURCE_NOT_FOUND
        assert response["error"]["data"]["sessionId"] == "nope"

    async def test_an_absent_session_id_names_no_session(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call("fs/read_text_file", {"path": "/etc/hostname"})
        assert response["error"]["code"] == wire.RESOURCE_NOT_FOUND


class TestPromptValidation:
    async def test_a_non_text_prompt_block_is_invalid_params(self, acp) -> None:
        h = acp()
        await h.initialize()
        session_id = await h.new_session()
        response = await h.call(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "image", "data": "x"}]},
        )
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_an_empty_prompt_array_is_invalid_params(self, acp) -> None:
        h = acp()
        await h.initialize()
        session_id = await h.new_session()
        response = await h.call("session/prompt", {"sessionId": session_id, "prompt": []})
        assert response["error"]["code"] == wire.INVALID_PARAMS

    async def test_a_prompt_for_an_unknown_session_is_resource_not_found(self, acp) -> None:
        h = acp()
        await h.initialize()
        response = await h.call(
            "session/prompt",
            {"sessionId": "nope", "prompt": [{"type": "text", "text": "hi"}]},
        )
        assert response["error"]["code"] == wire.RESOURCE_NOT_FOUND

    async def test_one_prompt_per_session_at_a_time(self, acp) -> None:
        """v1's stated contract: a second prompt while one is in flight is
        refused, and the refusal names the cure (session/cancel)."""
        release = asyncio.Event()

        class Held:
            async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
                await release.wait()
                return ChatReply(text="done", tokens_used=1)

        h = acp(model=Held())
        await h.initialize()
        session_id = await h.new_session()
        first = await h.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": LONG_TEXT}],
                },
            }
        )
        assert first is None  # the turn task owns this request's reply
        second = await h.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": "too soon"}],
                },
            }
        )
        assert second is not None
        assert second["error"]["code"] == wire.INVALID_PARAMS
        release.set()
        await h.server.quiesce()
