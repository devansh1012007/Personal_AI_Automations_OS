"""The server over real pipes: the C7.6 acceptance, in-sandbox form.

An editor does not call methods on PAA; it spawns PAA and talks to a pipe.
These tests do the same — ``serve_child.py`` is a real child process running
the real ``ACPServer`` on the production stdio transport, and the test is
its client: initialize, ``session/new`` bound to a working directory, one
prompt turn whose reply **streams** as more-than-one ``session/update``
before the stop, a cancel that actually stops a mid-flight turn and leaves
the session usable, and a clean EOF teardown whose ledger rows say what the
wire did. Every wait is bounded; every child is killed in ``finally``.
"""

from __future__ import annotations

import time
import uuid

from paa.core.types import EventType

from .conftest import ServeChild, read_child_ledger

#: Long enough that the slow model's 24-char/50ms chunks cannot finish
#: before the cancel lands (125 chunks ≈ 6 s unccancelled).
CANCEL_PROMPT = "count slowly, please. " * 125

#: Short enough for the slow model to finish in two chunks (~0.1 s).
SHORT_PROMPT = "quick turn"


def _updates(child: ServeChild) -> list[dict[str, object]]:
    """The collected ``session/update`` notifications, agent chunks only."""
    chunks = []
    for frame in child.notifications:
        if frame.get("method") != "session/update":
            continue
        params = frame.get("params")
        if isinstance(params, dict) and params.get("update", {}).get(
            "sessionUpdate"
        ) == "agent_message_chunk":
            chunks.append(params)
    return chunks


def _chunk_text(update: dict[str, object]) -> str:
    inner = update["update"]
    assert isinstance(inner, dict)
    content = inner["content"]
    assert isinstance(content, dict)
    text = content["text"]
    assert isinstance(text, str)
    return text


class TestStreamingTurn:
    async def test_a_prompt_streams_chunks_then_stops_cleanly(
        self, spawn_serve, tmp_path
    ) -> None:
        child = await spawn_serve(model="echo")
        result = await child.initialize()
        assert result["protocolVersion"] == 1

        session_id = (await child.request("session/new", {"cwd": str(tmp_path)}))[
            "result"
        ]["sessionId"]
        prompt = "stream me across several twelve-character chunks, please."
        reply = await child.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
        )

        # The stop: a clean end_turn for a turn that ran to completion.
        assert "error" not in reply, reply["error"]
        assert reply["result"]["stopReason"] == "end_turn"

        # The stream: more than one update for a multi-chunk turn, all
        # message chunks, all sharing one messageId (one turn, one message),
        # and they were read BEFORE the response — the request() helper only
        # returns after the reply frame, so anything it collected en route
        # arrived earlier on the wire.
        chunks = _updates(child)
        assert len(chunks) > 1, f"expected streamed chunks, got {len(chunks)}"
        message_ids = {c["update"]["messageId"] for c in chunks}
        assert len(message_ids) == 1
        assert "".join(_chunk_text(c) for c in chunks) == prompt

        # The ledger and the wire agree on the session: the ACP session id
        # is the chat lineage, and EOF ends it with the stated reason.
        code = await child.close_stdin_and_wait()
        assert code == 0, "".join(child.stderr_lines[-10:])
        events = await read_child_ledger(child.db_path, uuid.UUID(session_id))
        kinds = [event.event_type for event in events]
        assert EventType.CHAT_SESSION_STARTED in kinds
        assert EventType.CHAT_TURN_COMPLETED in kinds
        ended = [event for event in events if event.event_type is EventType.CHAT_SESSION_ENDED]
        assert ended[0].payload["stopped_reason"] == "acp-eof"

    async def test_a_second_turn_on_the_same_session_streams_a_new_message(
        self, spawn_serve, tmp_path
    ) -> None:
        child = await spawn_serve(model="echo")
        await child.initialize()
        session_id = (await child.request("session/new", {"cwd": str(tmp_path)}))[
            "result"
        ]["sessionId"]
        first = await child.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "first turn"}]},
        )
        assert first["result"]["stopReason"] == "end_turn"
        child.notifications.clear()
        second = await child.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "second turn"}]},
        )
        assert second["result"]["stopReason"] == "end_turn"
        ids = {c["update"]["messageId"] for c in _updates(child)}
        assert len(ids) == 1  # a new turn is a new message


class TestCancel:
    async def test_cancel_stops_a_midflight_turn_and_keeps_the_session_usable(
        self, spawn_serve, tmp_path
    ) -> None:
        child = await spawn_serve(model="slow", chunk_delay=0.05)
        await child.initialize()
        session_id = (await child.request("session/new", {"cwd": str(tmp_path)}))[
            "result"
        ]["sessionId"]

        # Start the turn, then read updates directly until a few have
        # streamed — the turn is visibly in flight when the cancel lands.
        await child.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": CANCEL_PROMPT}],
                },
            }
        )
        seen = 0
        while seen < 3:
            frame = await child.read_frame(timeout_seconds=10.0)
            if frame.get("method") == "session/update":
                seen += 1
        cancelled_at = time.monotonic()
        await child.notify("session/cancel", {"sessionId": session_id})

        # The acknowledgement is the prompt's own response, and it must not
        # make the test wait out the full unc cancelled turn (≈6 s).
        while True:
            frame = await child.read_frame(timeout_seconds=5.0)
            if frame.get("id") == 7 and "method" not in frame:
                reply = frame
                break
            if frame.get("method") == "session/update":
                seen += 1
        assert "error" not in reply, reply["error"]
        assert reply["result"]["stopReason"] == "cancelled"
        assert time.monotonic() - cancelled_at < 5.0

        # The chunks actually stopped: far fewer than the turn's 125 chunks
        # were served after a cancel aimed at the third one.
        assert seen < 12, f"stream kept going after the cancel: {seen} chunks"

        # The session stays usable: the next turn on the same session runs.
        child.notifications.clear()
        reply = await child.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": SHORT_PROMPT}]},
            timeout_seconds=10.0,
        )
        assert reply["result"]["stopReason"] == "end_turn"

        # Teardown is clean, and the cancelled turn is a cancelled row, not
        # a failed one and not a hole.
        code = await child.close_stdin_and_wait()
        assert code == 0, "".join(child.stderr_lines[-10:])
        events = await read_child_ledger(child.db_path, uuid.UUID(session_id))
        kinds = [event.event_type for event in events]
        assert EventType.CHAT_TURN_CANCELLED in kinds
        assert EventType.CHAT_TURN_FAILED not in kinds

    async def test_cancelling_an_idle_session_is_a_silent_noop(
        self, spawn_serve, tmp_path
    ) -> None:
        child = await spawn_serve(model="echo")
        await child.initialize()
        session_id = (await child.request("session/new", {"cwd": str(tmp_path)}))[
            "result"
        ]["sessionId"]
        await child.notify("session/cancel", {"sessionId": session_id})
        # Nothing was in flight; the next request still answers.
        reply = await child.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "still here"}]},
        )
        assert reply["result"]["stopReason"] == "end_turn"
        code = await child.close_stdin_and_wait()
        assert code == 0
