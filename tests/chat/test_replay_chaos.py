"""The C6.3 chaos matrix, chat half: a killed turn, swept and redelivered.

Where the coder suite (``tests/pipelines/test_replay_chaos.py``) kills a
run and *resumes the same lineage*, chat is turn-shaped: a ``kill -9``
mid-turn leaves ``CHAT_TURN_PROPOSED`` dangling, the startup sweep closes
it as ``FAILED(reason="interrupted")``, and recovery is a **redelivery** —
the operator sends the message again, which is a new turn on the same
session. The property this suite pins is that the crash costs nothing
twice:

* a turn killed mid-think produced **no effects at all** (remember runs
  after think), so the redelivered turn's effects are the only ones:
  exactly one user signal, one assistant signal, one extraction, one
  ``CHAT_TURN_COMPLETED`` — and the killed turn never completes;
* the sweep is idempotent (run twice, no second close) and the killed
  turn reads exactly ``PROPOSED → FAILED(interrupted)``;
* a turn killed *between* the user and assistant remembers (the one
  boundary where an effect already happened) is pinned for what it
  honestly is: the user signal of the killed turn is a real episodic
  record — the message *was* received — and the redelivery writes its
  own. Two cold-lake signals for the re-sent text, exactly one
  extraction between them (the killed turn's signal was never
  extracted), one assistant signal, one completed turn. Episodic by
  turn, documented, not hidden.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from paa.chat.loop import ChatModel, ChatReply
from paa.ledger.recovery import (
    close_interrupted_chat_turns,
    find_interrupted_chat_turns,
)
from tests.chat.conftest import ChatHarness


class Kill9(BaseException):
    """A hard kill, in process: nothing catches it, no cleanup row."""


class DyingModel(ChatModel):
    """Dies like a kill on its first reply; answers normally afterwards."""

    def __init__(self, inner: ChatModel | None = None) -> None:
        self.inner = inner
        self.died = False
        self.replies = 0

    async def reply(self, prompt: str, *, on_delta: Any = None) -> ChatReply:
        if not self.died:
            self.died = True
            raise Kill9("simulated SIGKILL mid-reply")
        self.replies += 1
        if self.inner is not None:
            return await self.inner.reply(prompt, on_delta=on_delta)
        return ChatReply(text="recovered reply", tokens_used=5)


class DyingCreator:
    """Wraps a MemoryCreator; the first process_signal dies like a kill.

    The crash lands after the cold-lake signal row exists but before any
    extraction — the boundary between "the message was received" and
    "the message was understood".
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.died = False
        self.extractions = 0

    async def process_signal(self, signal: Any, **kwargs: Any) -> Any:
        if not self.died:
            self.died = True
            raise Kill9("simulated SIGKILL between signal and extraction")
        self.extractions += 1
        return await self.inner.process_signal(signal, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def _sweep_twice(harness: ChatHarness, session_id: Any) -> None:
    """The production startup sweep, run twice — idempotence is the gate."""
    events = await harness.ledger.read_correlation(session_id)
    interrupted = find_interrupted_chat_turns(events)
    await close_interrupted_chat_turns(harness.ledger, interrupted)
    await close_interrupted_chat_turns(harness.ledger, interrupted)


def _turn_outcomes(harness: ChatHarness, kinds: list[str]) -> dict[str, int]:
    return {
        "proposed": kinds.count("CHAT_TURN_PROPOSED"),
        "completed": kinds.count("CHAT_TURN_COMPLETED"),
        "failed": kinds.count("CHAT_TURN_FAILED"),
        "cancelled": kinds.count("CHAT_TURN_CANCELLED"),
    }


class TestChatKillChaos:
    async def test_kill_mid_think_sweep_redeliver_zero_duplicate_effects(
        self, chat: Callable[..., ChatHarness], tmp_path: Any
    ) -> None:
        model = DyingModel()
        harness = chat(model=model)
        session_id = await harness.service.create_session()
        text = "Apollo is blocked until Friday."

        with pytest.raises(Kill9):
            await harness.service.send(session_id, text)

        await _sweep_twice(harness, session_id)

        # The redelivery: the operator sends the message again.
        result = await harness.service.send(session_id, text)
        assert result.reply == "recovered reply"

        kinds = await harness.kinds(session_id)
        outcomes = _turn_outcomes(harness, kinds)
        # Two turns were proposed (the killed one and the redelivery),
        # exactly one completed, exactly one failed-with-interrupted.
        assert outcomes == {"proposed": 2, "completed": 1, "failed": 1, "cancelled": 0}

        # Zero duplicate effects: the killed turn ran remember never, so
        # the whole session holds exactly the redelivered turn's two
        # signal rows — the user's (extracted) and the assistant's
        # (episodic-only, deliberately unextracted).
        signal_rows = [
            row
            for row in await harness.events(session_id)
            if row.event_type.value == "MEMORY_SIGNAL_EXTRACTED"
        ]
        assert len(signal_rows) == 2
        outcomes = sorted(str(row.payload.get("outcome")) for row in signal_rows)
        assert "episodic_only" in outcomes

        completed = [
            row
            for row in await harness.events(session_id)
            if row.event_type.value == "CHAT_TURN_COMPLETED"
        ]
        assert len(completed) == 1
        payload = completed[0].payload
        # The user's fact was written exactly once ...
        assert len(payload.get("facts_written") or []) == 1
        # ... and both of the turn's signals are named on the row.
        assert len(payload.get("signal_ids") or []) == 2
        # ... and a second sweep after the redelivery still finds nothing.
        events = await harness.ledger.read_correlation(session_id)
        assert find_interrupted_chat_turns(events) == []

    async def test_kill_between_the_remembers_is_episodic_not_duplicated(
        self, chat: Callable[..., ChatHarness]
    ) -> None:
        """The one boundary where an effect already happened, pinned honestly.

        The killed turn's user signal stands (the message was received);
        the redelivered turn writes its own. Two signals for the re-sent
        text — episodic by turn — but exactly one extraction, because the
        killed turn's signal was never extracted and the sweep never
        extracts. The assistant's reply exists exactly once.
        """
        harness = chat()  # the echo model; the creator is the one that dies
        real_creator = harness.creator
        dying = DyingCreator(real_creator)
        # Rebuild the service with the dying creator wired in.
        dying_harness = chat(creator=dying)
        session_id = await dying_harness.service.create_session()
        text = "Apollo is blocked until Friday."

        with pytest.raises(Kill9):
            await dying_harness.service.send(session_id, text)

        await _sweep_twice(dying_harness, session_id)
        result = await dying_harness.service.send(session_id, text)
        assert result.reply  # the echo answered the redelivery

        kinds = await dying_harness.kinds(session_id)
        outcomes = _turn_outcomes(dying_harness, kinds)
        assert outcomes == {"proposed": 2, "completed": 1, "failed": 1, "cancelled": 0}

        # Exactly one extraction across both turns: the killed turn's
        # signal reached the lake but not the creator.
        assert dying.extractions == 1
        signal_rows = [
            row
            for row in await dying_harness.events(session_id)
            if row.event_type.value == "MEMORY_SIGNAL_EXTRACTED"
        ]
        # Both rows belong to the redelivered turn (user + assistant);
        # the killed turn wrote none — its crash preceded its first row.
        assert len(signal_rows) == 2

        # The completed turn's row carries one user fact and both signals.
        completed = [
            row
            for row in await dying_harness.events(session_id)
            if row.event_type.value == "CHAT_TURN_COMPLETED"
        ]
        payload = completed[0].payload
        assert len(payload.get("facts_written") or []) == 1
        assert len(payload.get("signal_ids") or []) == 2

    async def test_killed_turn_is_closed_exactly_once(
        self, chat: Callable[..., ChatHarness]
    ) -> None:
        """A redelivered sweep after redelivery adds no second close."""
        model = DyingModel()
        harness = chat(model=model)
        session_id = await harness.service.create_session()

        with pytest.raises(Kill9):
            await harness.service.send(session_id, "first")
        await _sweep_twice(harness, session_id)
        await harness.service.send(session_id, "second")

        # Sweep a third time, post-redelivery: nothing left to close.
        events = await harness.ledger.read_correlation(session_id)
        assert find_interrupted_chat_turns(events) == []
        await close_interrupted_chat_turns(
            harness.ledger, find_interrupted_chat_turns(events)
        )
        kinds = await harness.kinds(session_id)
        assert kinds.count("CHAT_TURN_FAILED") == 1
