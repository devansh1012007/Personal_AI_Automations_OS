"""The ACP wire vocabulary on the shared framing kit — pinned, not forked.

SPEC 2's §10 drift mitigation is one framing codebase for three fronts, so
these tests pin the two halves of that claim for ACP: the messages this
surface writes round-trip through :mod:`paa.mcp.protocol`'s encode/read pair
(the MCP server's own framing, imported — never re-implemented), and the
vocabulary constants (versions, reserved codes) are the shared kit's own
objects where the protocols agree, ACP's own where they deliberately do not.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from paa import mcp
from paa.acp import protocol as acp


class TestFramingReuse:
    """ACP messages ride the MCP kit's encoder and reader, unchanged."""

    async def test_encoded_frames_are_one_ndjson_line_each(self) -> None:
        chunk = acp.agent_message_chunk("s1", "m1", "hello")
        wire = acp.encode_frame(chunk)
        assert wire.endswith(b"\n")
        assert wire.count(b"\n") == 1
        assert json.loads(wire) == chunk

    async def test_acp_messages_round_trip_through_the_shared_reader(self) -> None:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            acp.agent_message_chunk("s1", "m1", "streamed text"),
            acp.notification("session/cancel", {"sessionId": "s1"}),
            {"jsonrpc": "2.0", "id": 2, "result": acp.stop_reason_result("end_turn")},
        ]
        reader = asyncio.StreamReader()
        for message in messages:
            reader.feed_data(acp.encode_frame(message))
        reader.feed_eof()
        decoded = []
        while (payload := await acp.read_frame(reader)) is not None:
            decoded.append(json.loads(payload))
        assert decoded == messages

    async def test_split_writes_reassemble_into_whole_messages(self) -> None:
        """The pipe delivers chunks, not messages; the reader must not care."""
        wire = acp.encode_frame(acp.agent_message_chunk("s1", "m1", "split me"))
        reader = asyncio.StreamReader()
        reader.feed_data(wire[:5])
        reader.feed_data(wire[5:])
        reader.feed_eof()
        payload = await acp.read_frame(reader)
        assert payload is not None
        message = json.loads(payload)
        assert message["params"]["update"]["content"]["text"] == "split me"

    async def test_blank_lines_between_frames_are_skipped(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\n\n")
        reader.feed_data(acp.encode_frame({"jsonrpc": "2.0", "method": "session/cancel"}))
        reader.feed_eof()
        assert await acp.read_frame(reader) is not None
        assert await acp.read_frame(reader) is None

    def test_reserved_codes_are_the_shared_kits_own_objects(self) -> None:
        """The JSON-RPC reserved range is one vocabulary: the ACP names must
        be the MCP kit's constants, not same-valued copies that can drift."""
        assert acp.PARSE_ERROR is mcp.protocol.PARSE_ERROR
        assert acp.INVALID_REQUEST is mcp.protocol.INVALID_REQUEST
        assert acp.METHOD_NOT_FOUND is mcp.protocol.METHOD_NOT_FOUND
        assert acp.INVALID_PARAMS is mcp.protocol.INVALID_PARAMS
        assert acp.INTERNAL_ERROR is mcp.protocol.INTERNAL_ERROR

    def test_the_framing_functions_are_the_shared_kits_own(self) -> None:
        assert acp.encode_frame is mcp.protocol.encode_frame
        assert acp.read_frame is mcp.protocol.read_frame
        assert acp.result_response is mcp.protocol.result_response
        assert acp.error_response is mcp.protocol.error_response

    def test_acps_own_codes_deliberately_differ_from_the_mcp_table(self) -> None:
        """``-32000``/``-32002`` mean different things on the two surfaces
        (auth-required/resource-not-found here, PAA-extension/policy-denial
        there), so they are ACP-named constants that live only in the ACP
        vocabulary — reusing the MCP kit's objects would let a rename on one
        surface silently change the other's meaning."""
        assert acp.AUTH_REQUIRED == -32000
        assert acp.RESOURCE_NOT_FOUND == -32002
        assert mcp.protocol.PERMISSION_DENIED == -32002
        assert not hasattr(mcp.protocol, "RESOURCE_NOT_FOUND")
        assert not hasattr(mcp.protocol, "AUTH_REQUIRED") or mcp.protocol.AUTH_REQUIRED == -32009


class TestVocabulary:
    """The shapes both fronts write, in one place and one test."""

    def test_protocol_version_is_the_acp_major_integer(self) -> None:
        assert acp.PROTOCOL_VERSION == 1
        assert isinstance(acp.PROTOCOL_VERSION, int)

    def test_agent_identity_is_declared(self) -> None:
        assert acp.AGENT_NAME == "paa-acp"
        assert acp.AGENT_VERSION  # the runtime's own __version__

    def test_text_content_is_one_block(self) -> None:
        assert acp.text_content("hi") == {"type": "text", "text": "hi"}

    def test_notification_carries_no_id(self) -> None:
        frame = acp.notification("session/cancel", {"sessionId": "s"})
        assert frame == {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "s"},
        }
        assert "id" not in frame

    def test_agent_message_chunk_shape(self) -> None:
        frame = acp.agent_message_chunk("session-1", "turn-1", "delta")
        assert frame["method"] == "session/update"
        update = frame["params"]["update"]
        assert update["sessionUpdate"] == "agent_message_chunk"
        assert update["messageId"] == "turn-1"
        assert update["content"] == {"type": "text", "text": "delta"}
        assert frame["params"]["sessionId"] == "session-1"

    def test_stop_reason_result(self) -> None:
        assert acp.stop_reason_result("end_turn") == {"stopReason": "end_turn"}

    def test_acp_error_is_an_mcp_error_with_a_code(self) -> None:
        exc = acp.AcpError(acp.INVALID_PARAMS, "bad", path="x")
        assert isinstance(exc, mcp.protocol.McpError)
        assert exc.code == acp.INVALID_PARAMS
        assert exc.message == "bad"
        assert exc.details == {"path": "x"}


class TestPromptParsing:
    """``session/prompt``'s content array: text honoured, everything else
    refused loudly (a v1 deviation ADR-0053 records)."""

    def test_text_blocks_are_joined_with_newlines(self) -> None:
        prompt = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        assert acp.parse_prompt_text(prompt) == "first\nsecond"

    def test_a_single_block_has_no_separators(self) -> None:
        assert acp.parse_prompt_text([{"type": "text", "text": "only"}]) == "only"

    @pytest.mark.parametrize(
        "prompt",
        [
            None,
            [],
            "just a string",
            [42],
            [{"type": "image", "data": "..."}],
            [{"type": "resource_link", "uri": "file:///x"}],
            [{"type": "text", "text": 7}],
        ],
    )
    def test_malformed_prompts_are_invalid_params(self, prompt: object) -> None:
        with pytest.raises(acp.AcpError) as raised:
            acp.parse_prompt_text(prompt)
        assert raised.value.code == acp.INVALID_PARAMS
