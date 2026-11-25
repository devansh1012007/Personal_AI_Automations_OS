"""The six MCP tools end to end: echo-model path, real ledger, real memory.

The SPEC 2 §3.3 contract, exercised for real:
* ``memory_remember`` writes a session-scoped signal through the creator that
  becomes a fact (the ChatService path), and journals
  ``MEMORY_SIGNAL_EXTRACTED`` on the session lineage.
* ``memory_search`` / ``memory_query`` find that fact and journal consults.
* ``chat_send`` holds a two-turn conversation where turn 2 recalls turn 1's
  fact — the same deterministic proof ``tests/chat/test_loop.py`` pins — with
  the full chat-turn event set on the session lineage.
* ``context_bundle`` returns the packet with provenance (origin session
  stamped).
* ``sessions_list`` lists what the ledger folds.

Every tool call also lands the ADR-0046 tool-call audit rows
(``TOOL_CALL_PROPOSED`` → ``COMPLETED`` / ``DENIED`` / ``FAILED``) on their
own per-call lineage, and that lineage never mixes with the session lineage —
the recovery exemptions in ``ledger/recovery.py`` depend on the separation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from paa.config import ContextSettings, MCPSettings
from paa.core.types import EventType, PermissionMode
from paa.mcp.protocol import (
    CAPABILITY_NOT_GRANTED,
    INVALID_PARAMS,
    PERMISSION_DENIED,
)
from paa.memory.facts import FactRepository
from paa.pipelines.base import Capability
from tests.mcp_server.conftest import error_code, structured

TURN_1 = "Apollo is blocked until Friday."
TURN_2 = "What happens on Friday?"
FACT_STATEMENT = "Apollo is blocked until Friday"

#: The chat-turn flow the ledger records per turn (tests/chat/test_loop.py's
#: contract — the MCP path must produce exactly the same rows).
TURN_FLOW = [
    EventType.CHAT_TURN_PROPOSED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
    EventType.CHAT_TURN_COMPLETED,
]


class TestMemoryRemember:
    async def test_remember_writes_a_signal_that_becomes_a_fact(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        response = await h.call(
            "tools/call",
            {
                "name": "memory_remember",
                "arguments": {"statement": TURN_1, "session_id": str(sid)},
            },
        )
        payload = structured(response["result"])
        assert payload["outcome"] == "processed"
        assert payload["session_id"] == str(sid)
        assert payload["signal_id"]
        assert len(payload["facts_written"]) == 1

        # The session lineage carries the outcome row; the fact exists.
        events = await h.events(sid)
        extracted = [
            e for e in events if e.event_type is EventType.MEMORY_SIGNAL_EXTRACTED
        ]
        assert len(extracted) == 1
        assert extracted[0].payload["v"] == 1
        assert extracted[0].payload["outcome"] == "processed"
        assert extracted[0].payload["facts_written"] == payload["facts_written"]
        fact = await FactRepository(h.db).get_fact(payload["facts_written"][0])
        assert fact is not None
        assert fact.statement == FACT_STATEMENT

    async def test_remember_requires_a_session(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call(
            "tools/call", {"name": "memory_remember", "arguments": {"statement": TURN_1}}
        )
        assert error_code(response) == INVALID_PARAMS
        assert "session-scoped" in response["error"]["message"]

    async def test_default_session_setting_satisfies_the_contract(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        h2 = mcp(mcp_settings=MCPSettings(default_session=str(sid)))
        await h2.initialize()
        payload = structured(
            (
                await h2.call(
                    "tools/call",
                    {"name": "memory_remember", "arguments": {"statement": TURN_1}},
                )
            )["result"]
        )
        assert payload["session_id"] == str(sid)

    async def test_proposed_row_precedes_the_write(self, mcp) -> None:
        """Ledger-before-effect: PROPOSED lands before the signal write."""
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        await h.call(
            "tools/call",
            {"name": "memory_remember", "arguments": {"statement": TURN_1, "session_id": str(sid)}},
        )
        call_lineage = await h.tool_call_lineage()
        proposed = next(iter(await h.events(call_lineage)))
        extracted = next(
            e
            for e in await h.events(sid)
            if e.event_type is EventType.MEMORY_SIGNAL_EXTRACTED
        )
        assert proposed.sequence_id is not None
        assert extracted.sequence_id is not None
        assert proposed.sequence_id < extracted.sequence_id
        # The audit lineage and the session lineage never mix.
        assert call_lineage != sid


class TestMemoryReads:
    async def test_search_finds_the_remembered_fact(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        await h.call(
            "tools/call",
            {"name": "memory_remember", "arguments": {"statement": TURN_1, "session_id": str(sid)}},
        )
        payload = structured(
            (
                await h.call(
                    "tools/call",
                    {
                        "name": "memory_search",
                        "arguments": {"query": "Friday", "limit": 5, "session_id": str(sid)},
                    },
                )
            )["result"]
        )
        assert len(payload["facts"]) == 1
        assert payload["facts"][0]["statement"] == FACT_STATEMENT
        # The consult is journaled on the session's lineage when one is in play.
        consults = [
            e
            for e in await h.events(sid)
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
        ]
        assert len(consults) == 1
        assert consults[0].payload["fact_ids"] == [payload["facts"][0]["id"]]

    async def test_sessionless_read_journals_its_own_consult_lineage(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        await h.call(
            "tools/call",
            {"name": "memory_remember", "arguments": {"statement": TURN_1, "session_id": str(sid)}},
        )
        await h.call("tools/call", {"name": "memory_search", "arguments": {"query": "Friday"}})
        # The consult row must live on a lineage of its own: chat-only, so the
        # recovery sweep skips it, and never beside the tool-call audit rows.
        events = await h.ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.MEMORY_RECALL_CONSULTED],
        )
        assert events
        consult_lineage = events[0].correlation_id
        assert consult_lineage != sid
        kinds = await h.kinds(consult_lineage)
        assert kinds == ["MEMORY_RECALL_CONSULTED"]

    async def test_query_by_fact_id_returns_fact_and_lineage(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        remembered = structured(
            (
                await h.call(
                    "tools/call",
                    {
                        "name": "memory_remember",
                        "arguments": {"statement": TURN_1, "session_id": str(sid)},
                    },
                )
            )["result"]
        )
        fact_id = remembered["facts_written"][0]
        payload = structured(
            (
                await h.call(
                    "tools/call", {"name": "memory_query", "arguments": {"fact_id": fact_id}}
                )
            )["result"]
        )
        assert payload["fact"] is not None
        assert payload["fact"]["statement"] == FACT_STATEMENT
        assert payload["lineage"] == [fact_id]

    async def test_query_by_alias_resolves_the_entity(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        await h.call(
            "tools/call",
            {"name": "memory_remember", "arguments": {"statement": TURN_1, "session_id": str(sid)}},
        )
        response = await h.call(
            "tools/call", {"name": "memory_query", "arguments": {"alias": "Apollo"}}
        )
        payload = structured(response["result"])
        assert payload["entity"] is not None
        assert payload["entity"]["canonical_name"] == "Apollo"
        assert payload["fact"] is not None
        assert payload["fact"]["statement"] == FACT_STATEMENT

    async def test_query_with_neither_id_nor_alias_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call("tools/call", {"name": "memory_query", "arguments": {}})
        assert error_code(response) == INVALID_PARAMS

    async def test_unknown_fact_id_answers_empty_not_error(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        payload = structured(
            (
                await h.call(
                    "tools/call", {"name": "memory_query", "arguments": {"fact_id": "no-such-fact"}}
                )
            )["result"]
        )
        assert payload["fact"] is None
        assert payload["lineage"] == []

    async def test_nul_query_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call(
            "tools/call", {"name": "memory_search", "arguments": {"query": "friday\x00"}}
        )
        assert error_code(response) == INVALID_PARAMS


class TestChatSend:
    async def test_two_turn_conversation_recalls_turn_one(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        first = structured(
            (await h.call("tools/call", {"name": "chat_send", "arguments": {"message": TURN_1}}))[
                "result"
            ]
        )
        sid = uuid.UUID(first["session_id"])
        assert first["status"] == "completed"
        assert first["reply"] == TURN_1  # nothing served yet: echo answers verbatim

        second = structured(
            (
                await h.call(
                    "tools/call",
                    {"name": "chat_send", "arguments": {"session_id": str(sid), "message": TURN_2}},
                )
            )["result"]
        )
        # The deterministic proof, in the reply itself: echo names what memory
        # served from turn 1.
        assert second["reply"] == f"{TURN_2}\n(from memory: {FACT_STATEMENT})"
        assert len(second["recalled_fact_ids"]) == 1
        assert second["bundle_tokens"] <= second["token_ceiling"]

        # The full chat-turn event set, exactly the C4.1 contract, twice.
        kinds = await h.kinds(sid)
        assert kinds == [
            EventType.CHAT_SESSION_STARTED.value,
            *[t.value for t in TURN_FLOW],
            *[t.value for t in TURN_FLOW],
        ]
        started = (await h.events(sid))[0]
        assert started.payload["scope"] == "mcp"

    async def test_send_to_unknown_session_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call(
            "tools/call",
            {
                "name": "chat_send",
                "arguments": {"session_id": str(uuid.uuid4()), "message": "hello"},
            },
        )
        assert error_code(response) == INVALID_PARAMS
        assert "no chat session lineage" in response["error"]["message"]

    async def test_empty_message_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call("tools/call", {"name": "chat_send", "arguments": {"message": "  "}})
        assert error_code(response) == INVALID_PARAMS

    async def test_unknown_argument_key_is_invalid_params(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        response = await h.call(
            "tools/call",
            {"name": "chat_send", "arguments": {"message": "hi", "explode": True}},
        )
        assert error_code(response) == INVALID_PARAMS


class TestContextBundle:
    async def test_bundle_returns_packet_with_provenance(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        await h.call(
            "tools/call",
            {"name": "memory_remember", "arguments": {"statement": TURN_1, "session_id": str(sid)}},
        )
        payload = structured(
            (
                await h.call(
                    "tools/call",
                    {
                        "name": "context_bundle",
                        "arguments": {"session_id": str(sid), "query": TURN_2},
                    },
                )
            )["result"]
        )
        assert payload["session_id"] == str(sid)
        assert payload["provenance"], "the remembered fact must be served"
        provenance = payload["provenance"][0]
        assert provenance["statement"] == FACT_STATEMENT
        assert provenance["origin_session"] == str(sid)
        assert payload["packet"]["elements"]
        assert payload["token_ceiling"] == ContextSettings().token_ceiling
        # Both consults (semantic + episodic) are journaled, like a real turn.
        consults = [
            e
            for e in await h.events(sid)
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
        ]
        assert len(consults) == 2

    async def test_bundle_without_a_session_answers_query_only(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        payload = structured(
            (
                await h.call(
                    "tools/call",
                    {"name": "context_bundle", "arguments": {"query": "anything at all"}},
                )
            )["result"]
        )
        assert payload["session_id"] is None
        assert payload["provenance"] == []
        assert payload["turns"] == []


class TestSessionsList:
    async def test_sessions_list_folds_every_session(self, mcp) -> None:
        h = mcp()
        await h.initialize()
        await h.service.create_session(scope="mcp")
        await h.call("tools/call", {"name": "chat_send", "arguments": {"message": TURN_1}})
        payload = structured((await h.call("tools/call", {"name": "sessions_list"}))["result"])
        sessions = payload["sessions"]
        assert len(sessions) == 2
        assert all(s["status"] == "active" for s in sessions)
        by_turns = sorted(s["turns_proposed"] for s in sessions)
        assert by_turns == [0, 1]  # one bare session, one with a completed turn


class TestPolicyGate:
    async def test_mandate_narrowing_refuses_the_write(self, mcp) -> None:
        """The manifest ∩ mandate ∩ policy seam: a narrowed mandate refuses."""
        h = mcp(mandate_capabilities=frozenset({Capability.RECALL}))
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        response = await h.call(
            "tools/call",
            {
                "name": "memory_remember",
                "arguments": {"statement": "x", "session_id": str(sid)},
            },
        )
        assert error_code(response) == CAPABILITY_NOT_GRANTED
        lineage = await h.tool_call_lineage()
        assert await h.kinds(lineage) == ["TOOL_CALL_PROPOSED", "TOOL_CALL_DENIED"]

    async def test_lockdown_refuses_writes_and_model_calls(self, mcp) -> None:
        h = mcp(permission_mode=PermissionMode.LOCKDOWN)
        await h.initialize()
        sid = await h.service.create_session(scope="mcp")
        for tool, args in (
            ("memory_remember", {"statement": "x", "session_id": str(sid)}),
            ("chat_send", {"message": "x"}),
        ):
            response = await h.call("tools/call", {"name": tool, "arguments": args})
            assert error_code(response) == PERMISSION_DENIED
        # Reads survive the air-gap; the pre-existing session is listed, and
        # the denied chat_send never proposed a turn into it.
        payload = structured((await h.call("tools/call", {"name": "sessions_list"}))["result"])
        assert len(payload["sessions"]) == 1
        assert payload["sessions"][0]["turns_proposed"] == 0


class TestArgsAreAttestedNotCarried:
    async def test_argument_text_never_enters_the_audit_rows(self, mcp) -> None:
        secret = "my api key is sk-supersecret-do-not-leak"
        h = mcp()
        await h.initialize()
        response = await h.call(
            "tools/call", {"name": "chat_send", "arguments": {"message": secret}}
        )
        payload = structured(response["result"])
        sid = uuid.UUID(payload["session_id"])
        call_lineage = await h.tool_call_lineage()
        for event in [*await h.events(call_lineage), *await h.events(sid)]:
            blob = json.dumps(event.payload)
            assert secret not in blob
        # The preview attests the shape, the hash attests the content.
        proposed = (await h.events(call_lineage))[0]
        assert proposed.payload["args_preview_redacted"].startswith("chat_send(")
        assert "sk-supersecret" not in proposed.payload["args_preview_redacted"]
