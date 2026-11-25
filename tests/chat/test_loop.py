"""The chat loop end to end: one turn per run, offline and deterministic.

What is pinned here
-------------------
* The deterministic two-turn proof: turn 2's reply quotes what memory served
  from turn 1's message. The echo model notes served facts in the reply text
  (``(from memory: ...)``), so the proof is visible in the product surface and
  not only in the ledger rows.
* The flow order inside one lineage: every effect is ledgered before it runs
  (PROPOSED before recall, recall consults before signals, COMPLETED last),
  and the assistant's signal is recorded episodic-only — extraction never
  runs on the model's own words.
* The payload discipline: a chat message is an opaque input, so its verbatim
  text never enters any payload — not even when the message carries a secret.
* Token budget: the turn's bundle stays at or under the ceiling, and the
  ceiling is ``ChatSettings.token_budget`` when set, else the context
  engine's planner ceiling.
* Cancel mid-stream: ``CHAT_TURN_CANCELLED`` with the partial reply attested,
  never ``FAILED``, never a hang, and :class:`asyncio.CancelledError` still
  reaches the caller.
* Failure: an exploding model records ``CHAT_TURN_FAILED`` with a redacted
  reason and returns a ``failed`` result — it does not raise past the service.
* The pipeline shape: the loop runs only on the host-minted
  :class:`~paa.chat.loop.ChatTurnFacade`, and the turn id is validated before
  the first ledger row lands.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from paa.chat.loop import ChatLoop, ChatReply, render_chat_prompt
from paa.chat.session import ChatSessionJournal, text_sha256
from paa.config import ChatSettings, ContextSettings
from paa.core.errors import PaaError
from paa.core.types import EventType
from paa.ledger.events import canonical_json
from paa.pipelines.base import (
    InvalidCallParamsError,
    PipelineStatus,
    PipelineTask,
)

USER_TURN_1 = "Apollo is blocked until Friday."
USER_TURN_2 = "What happens on Friday?"
FACT_STATEMENT = "Apollo is blocked until Friday"
RECALL_NOTE = f"(from memory: {FACT_STATEMENT})"

#: The per-turn flow, ledgered before its effects. The semantic recall consult
#: precedes the episodic one (the loop recalls, then reads this session's
#: turns), and the user signal is extracted before the assistant signal is
#: filed episodic-only.
TURN_FLOW = [
    EventType.CHAT_TURN_PROPOSED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
    EventType.CHAT_TURN_COMPLETED,
]


class TestTwoTurnRecall:
    """Turn 1 teaches; turn 2 proves it was learned. No network, no clock."""

    async def test_turn_one_learns_the_fact(self, chat) -> None:
        h = chat()
        sid = await h.service.create_session()
        r1 = await h.service.send(sid, USER_TURN_1)

        assert r1.status == PipelineStatus.COMPLETED.value
        # No facts served yet, so the echo answers verbatim — no memory note.
        assert r1.reply == USER_TURN_1
        assert r1.recalled_fact_ids == ()
        # Two signals: the user's message and the assistant's reply.
        assert len(r1.signal_ids) == 2
        # The user signal was extracted into the fact; the COMPLETED row
        # carries its id.
        completed = [
            e
            for e in await h.events(sid)
            if e.event_type is EventType.CHAT_TURN_COMPLETED
        ]
        assert len(completed[0].payload["facts_written"]) == 1

    async def test_turn_two_recalls_turn_ones_fact(self, chat) -> None:
        h = chat()
        sid = await h.service.create_session()
        await h.service.send(sid, USER_TURN_1)
        r2 = await h.service.send(sid, USER_TURN_2)

        assert r2.status == PipelineStatus.COMPLETED.value
        # The proof, in the reply itself: the echo names what memory served.
        assert r2.reply == f"{USER_TURN_2}\n{RECALL_NOTE}"
        assert len(r2.recalled_fact_ids) == 1
        # The ledger agrees with the reply: the COMPLETED row and the consult
        # row name the same fact.
        events = await h.events(sid)
        completed = [
            e for e in events if e.event_type is EventType.CHAT_TURN_COMPLETED
        ][-1]
        assert completed.payload["recalled_fact_ids"] == list(r2.recalled_fact_ids)
        consults = [
            e
            for e in events
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
            and e.payload["turn_id"] == r2.turn_id
        ]
        assert consults[0].payload["returned"] == 1
        assert consults[0].payload["fact_ids"] == list(r2.recalled_fact_ids)
        assert consults[0].payload["refused"] is False

    async def test_two_turn_flow_order_is_ledgered_before_effects(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        await h.service.send(sid, USER_TURN_1)
        await h.service.send(sid, USER_TURN_2)

        kinds = await h.kinds(sid)
        assert kinds == [
            EventType.CHAT_SESSION_STARTED.value,
            *[t.value for t in TURN_FLOW],  # turn 1
            *[t.value for t in TURN_FLOW],  # turn 2
        ]
        events = await h.events(sid)
        # The assistant's own words are filed, never extracted: the second
        # SIGNAL_EXTRACTED of each turn is episodic-only with no facts. Turn
        # 1's user message is an assertion (extraction runs, one fact lands);
        # turn 2's is a question (extraction runs, nothing asserts).
        extracted = [
            e for e in events if e.event_type is EventType.MEMORY_SIGNAL_EXTRACTED
        ]
        assert [e.payload["outcome"] for e in extracted] == [
            "processed",
            "episodic_only",
            "ignored",
            "episodic_only",
        ]
        assert all(e.payload["facts_written"] == [] for e in extracted[1::2])

    async def test_message_text_never_enters_a_payload(
        self, chat
    ) -> None:
        secret = "my api key is sk-supersecret-do-not-leak"
        h = chat()
        sid = await h.service.create_session()
        await h.service.send(sid, secret)

        for event in await h.events(sid):
            assert secret not in canonical_json(event.payload), (
                f"{event.event_type.value} carried the verbatim message"
            )
        # ...and the fact store holds the extracted statement, not the message:
        # "my api key is ..." is a hedge-free sentence the extractor may fold,
        # but the ledger rows above are the attestation surface under test.

    async def test_replayed_prompt_render_is_pure(self) -> None:
        """The render is a pure function: same inputs, byte-identical prompt."""
        first = render_chat_prompt(
            persona="p", facts=["f"], turns=["u: a"], message="m"
        )
        second = render_chat_prompt(
            persona="p", facts=["f"], turns=["u: a"], message="m"
        )
        assert first == second
        for section in ("[persona]", "[facts]", "[history]", "[message]"):
            assert section in first


class TestTokenBudget:
    """One budget line: the chat override, else the context engine's ceiling."""

    async def test_explicit_budget_caps_the_bundle(self, chat) -> None:
        h = chat(settings=ChatSettings(token_budget=64))
        sid = await h.service.create_session()
        await h.service.send(sid, USER_TURN_1)
        r2 = await h.service.send(sid, USER_TURN_2)

        assert r2.token_ceiling == 64
        assert 0 < r2.bundle_tokens <= 64
        assert r2.status == PipelineStatus.COMPLETED.value

    async def test_default_budget_is_the_context_engines_ceiling(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        r = await h.service.send(sid, USER_TURN_1)
        assert r.token_ceiling == ContextSettings().token_ceiling
        assert r.bundle_tokens <= r.token_ceiling

    async def test_tight_budget_still_serves_recall(
        self, chat
    ) -> None:
        """A tight ceiling shrinks the packet, not the recall verdict."""
        h = chat(settings=ChatSettings(token_budget=64))
        sid = await h.service.create_session()
        await h.service.send(sid, USER_TURN_1)
        r2 = await h.service.send(sid, USER_TURN_2)
        assert r2.recalled_fact_ids  # recall served the fact
        assert r2.bundle_tokens <= r2.token_ceiling


class TestCancelMidStream:
    """A cancelled turn is not a failed one, and the caller still sees the raise."""

    async def test_cancel_records_cancelled_with_partial_reply(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        collected: list[str] = []
        first_delta = asyncio.Event()

        async def on_delta(delta: str) -> None:
            collected.append(delta)
            first_delta.set()

        task = asyncio.create_task(
            h.service.send(sid, USER_TURN_1, on_delta=on_delta)
        )
        await asyncio.wait_for(first_delta.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

        partial = "".join(collected)
        assert partial  # deltas streamed before the cancel landed
        events = await h.events(sid)
        kinds = [e.event_type for e in events]
        assert EventType.CHAT_TURN_CANCELLED in kinds
        assert EventType.CHAT_TURN_COMPLETED not in kinds
        assert EventType.CHAT_TURN_FAILED not in kinds
        cancelled = next(
            e for e in events if e.event_type is EventType.CHAT_TURN_CANCELLED
        )
        assert cancelled.payload["partial_reply_chars"] == len(partial)
        assert cancelled.payload["partial_reply_sha256"] == text_sha256(partial)
        # A cancel is also not an eternally open turn: the session record
        # counts it, and the lineage stays alive (a session is not a task).
        record = await h.service.resume_session(sid)
        assert record.turns_cancelled == 1
        assert record.turns_completed == 0
        assert record.turns_failed == 0
        assert record.status == "active"

    async def test_recorded_cancel_resolves_the_proposal_for_the_sweep(
        self, chat
    ) -> None:
        """A PROPOSED row without an outcome is the sweep's input, so the
        recorded CANCELLED row must resolve it: a crash-redelivered cancel
        leaves nothing for recovery to re-close."""
        from paa.ledger.recovery import (
            RecoveryEngine,
            RecoveryOutcome,
            find_interrupted_chat_turns,
        )

        h = chat()
        sid = await h.service.create_session()
        first_delta = asyncio.Event()

        async def on_delta(delta: str) -> None:
            first_delta.set()

        task = asyncio.create_task(
            h.service.send(sid, USER_TURN_1, on_delta=on_delta)
        )
        await asyncio.wait_for(first_delta.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

        events = await h.events(sid)
        assert find_interrupted_chat_turns(events) == []
        report = await RecoveryEngine(h.ledger, h.db).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_CHAT_LINEAGE) == 1


class TestFailure:
    """A model error is a FAILED turn with a redacted reason, not a raise."""

    async def test_model_error_records_failed_turn(self, chat) -> None:
        class Exploding:
            async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
                raise RuntimeError("provider socket died")

        h = chat(model=Exploding())
        sid = await h.service.create_session()
        r = await h.service.send(sid, USER_TURN_1)

        assert r.status == PipelineStatus.FAILED.value
        assert r.error == "RuntimeError: provider socket died"
        events = await h.events(sid)
        failed = [
            e for e in events if e.event_type is EventType.CHAT_TURN_FAILED
        ]
        assert len(failed) == 1
        assert failed[0].payload["reason"] == "RuntimeError: provider socket died"
        record = await h.service.resume_session(sid)
        assert record.turns_failed == 1
        assert record.turns_completed == 0

    async def test_failure_reason_is_redacted(self, chat) -> None:
        class Leaking:
            async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
                raise RuntimeError("auth failed sk-live-abcdef0123456789")

        h = chat(model=Leaking())
        sid = await h.service.create_session()
        r = await h.service.send(sid, USER_TURN_1)

        assert r.status == PipelineStatus.FAILED.value
        assert "sk-live-abcdef0123456789" not in r.error
        events = await h.events(sid)
        failed = next(
            e for e in events if e.event_type is EventType.CHAT_TURN_FAILED
        )
        assert "sk-live-abcdef0123456789" not in failed.payload["reason"]

    async def test_empty_message_is_refused_before_any_ledger_row(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        before = await h.ledger.count()
        with pytest.raises(PaaError, match="must not be empty"):
            await h.service.send(sid, "   ")
        assert await h.ledger.count() == before


class TestPipelineShape:
    """Chat is pipeline #2: it runs on the host-minted facade, nothing else."""

    async def test_foreign_facade_is_refused_before_the_ledger_row(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        loop = ChatLoop(ChatSessionJournal(h.ledger, sid))

        class NotChatFacade:
            """Satisfies nothing in particular; that is the point."""

        task = PipelineTask(
            correlation_id=sid, session_id=sid, goal="hello", params={}
        )
        with pytest.raises(PaaError, match="ChatTurnFacade"):
            await loop.run(task, NotChatFacade())  # type: ignore[arg-type]
        # Only the SESSION_STARTED row exists — no PROPOSED was written.
        assert await h.kinds(sid) == [EventType.CHAT_SESSION_STARTED.value]

    async def test_turn_id_must_be_a_colon_free_nonempty_string(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        facade = h.facade_for(sid)
        loop = ChatLoop(ChatSessionJournal(h.ledger, sid))
        for bad in ({}, {"turn_id": ""}, {"turn_id": "a:b"}, {"turn_id": 7}):
            task = PipelineTask(
                correlation_id=sid, session_id=sid, goal="hello", params=bad
            )
            with pytest.raises(ValueError, match="turn_id"):
                await loop.run(task, facade)
        assert await h.kinds(sid) == [EventType.CHAT_SESSION_STARTED.value]

    async def test_facade_think_refuses_structured_calls(
        self, chat
    ) -> None:
        """Chat's think is free-text; a schema is the task pipeline's shape."""
        h = chat()
        sid = await h.service.create_session()
        facade = h.facade_for(sid)
        with pytest.raises(InvalidCallParamsError, match="free-text"):
            await facade.think("hello", schema={"type": "object"})


class TestStreaming:
    """``streaming`` shapes the transport, never the ledger."""

    async def test_streaming_on_delivers_the_reply_in_chunks(
        self, chat
    ) -> None:
        h = chat(settings=ChatSettings(streaming=True))
        sid = await h.service.create_session()
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        r = await h.service.send(sid, USER_TURN_1, on_delta=on_delta)
        assert deltas, "the echo streams more than one chunk for a real reply"
        assert "".join(deltas) == r.reply
        # The ledger records the final reply either way.
        completed = next(
            e
            for e in await h.events(sid)
            if e.event_type is EventType.CHAT_TURN_COMPLETED
        )
        assert completed.payload["reply_chars"] == len(r.reply)

    async def test_streaming_off_delivers_no_deltas(self, chat) -> None:
        h = chat(settings=ChatSettings(streaming=False))
        sid = await h.service.create_session()
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        r = await h.service.send(sid, USER_TURN_1, on_delta=on_delta)
        assert r.status == PipelineStatus.COMPLETED.value
        assert r.reply == USER_TURN_1
        assert deltas == []
