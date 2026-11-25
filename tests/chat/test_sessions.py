"""Session isolation, provenance, and the fold that rebuilds a session.

The ledger is the truth, so every assertion here reads lineages through
:class:`~paa.chat.session.fold_session` or the raw events — there is no
private session state to assert against, which is exactly the property under
test.

What is pinned here
-------------------
* The episodic fence: one session's raw turns are invisible to another
  session's facade unless the operator lifted it with
  ``PAA_CHAT__CROSS_SESSION_EPISODIC`` — and the refusal is a
  ``MEMORY_RECALL_CONSULTED`` row with ``refused=True``, so the ledger shows
  the fence worked, not merely that a call raised.
* Semantic facts are global *with provenance*: a fact learned in session A is
  served to session B's recall, with A's id stamped on the served mapping and
  on the bundle's :class:`~paa.chat.context_bundle.BundleProvenance`.
* Resume, end, and list are folds over lineages: an unknown session id raises
  :class:`~paa.chat.session.SessionNotFoundError`, ending writes the fold's
  own counters, and more turns after an ENDED row read ``active`` again —
  the record is a view, not a gate.
* The fold is total: malformed payloads, unknown event types and empty
  lineages fold without raising.
* Replay properties (hypothesis): any ordering of the eight chat event types
  projects with no unknown events and no attempts, and folds to a record whose
  counters are exactly the multiset of what the stream carried.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.chat.context_bundle import build_context_bundle
from paa.chat.session import (
    STATUS_ACTIVE,
    STATUS_ENDED,
    ChatSessionJournal,
    ChatTurnStatus,
    fold_session,
    list_sessions,
    session_record,
)
from paa.config import ChatSettings
from paa.core.errors import PaaError
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import replay

CHAT_EVENT_TYPES = (
    EventType.CHAT_SESSION_STARTED,
    EventType.CHAT_SESSION_ENDED,
    EventType.CHAT_TURN_PROPOSED,
    EventType.CHAT_TURN_COMPLETED,
    EventType.CHAT_TURN_FAILED,
    EventType.CHAT_TURN_CANCELLED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
)

# -- shared builders ---------------------------------------------------------


def _event(
    etype: EventType, cid: uuid.UUID, version: int, **payload: object
) -> LedgerEvent:
    """A sealed-looking chat event without touching the store."""
    base: dict[str, object] = {"v": 1, "session_id": str(cid)}
    if etype in {
        EventType.CHAT_TURN_PROPOSED,
        EventType.CHAT_TURN_COMPLETED,
        EventType.CHAT_TURN_FAILED,
        EventType.CHAT_TURN_CANCELLED,
        EventType.MEMORY_RECALL_CONSULTED,
        EventType.MEMORY_SIGNAL_EXTRACTED,
    }:
        base["turn_id"] = f"turn-{version:03d}"
    base.update(payload)
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=version, payload=base
    )


def _stream(cid: uuid.UUID, etypes: list[EventType]) -> list[LedgerEvent]:
    """Build a chat stream; each turn event gets its own turn id."""
    events: list[LedgerEvent] = []
    for i, etype in enumerate(etypes):
        payload: dict[str, object] = {}
        if etype is EventType.CHAT_TURN_COMPLETED:
            payload["tokens_consumed"] = 7
        elif etype is EventType.CHAT_SESSION_STARTED:
            payload.update(
                {
                    "scope": "personal",
                    "mandate_ref": None,
                    "model": "echo",
                    "cross_session_episodic": False,
                }
            )
        elif etype is EventType.CHAT_SESSION_ENDED:
            payload["stopped_reason"] = "user"
        events.append(_event(etype, cid, i + 1, **payload))
    return events


class TestEpisodicFence:
    """Raw turns are session-private; lifting the fence is the operator's word."""

    async def test_cross_session_read_denied_by_default_and_ledgered(
        self, chat
    ) -> None:
        h = chat()
        sid_a = await h.service.create_session()
        sid_b = await h.service.create_session()
        await h.service.send(sid_b, "The Apollo rocket is heavy.")
        b_events_before = await h.events(sid_b)

        before = await h.ledger.count()
        facade_a = h.facade_for(sid_a)
        with pytest.raises(PaaError, match="CROSS_SESSION_EPISODIC"):
            await facade_a.episodic_turns(sid_b, k=6)
        # The refusal is a ledger row, not just an exception.
        consults = [
            e
            for e in await h.events(sid_a)
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
        ]
        assert len(consults) == 1
        assert consults[0].payload["refused"] is True
        assert consults[0].payload["refusal_reason"]
        assert consults[0].payload["returned"] == 0
        assert await h.ledger.count() == before + 1
        # Session B's lineage is untouched by A's denied read.
        b_after = await h.events(sid_b)
        assert b_after == b_events_before

    async def test_flag_lifts_the_fence_and_stamps_the_consult(self, chat) -> None:
        h = chat(settings=ChatSettings(cross_session_episodic=True))
        sid_a = await h.service.create_session()
        sid_b = await h.service.create_session()
        await h.service.send(sid_b, "The Apollo rocket is heavy.")

        turns = await h.facade_for(sid_a).episodic_turns(sid_b, k=6)
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert all("The Apollo rocket is heavy" in t["text"] for t in turns)
        consults = [
            e
            for e in await h.events(sid_a)
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
        ]
        assert consults[0].payload["refused"] is False
        assert consults[0].payload["returned"] == 2

    async def test_own_session_turns_need_no_flag(self, chat) -> None:
        h = chat()
        sid_a = await h.service.create_session()
        await h.service.send(sid_a, "The Apollo rocket is heavy.")

        turns = await h.facade_for(sid_a).episodic_turns(sid_a, k=6)
        assert [t["role"] for t in turns] == ["user", "assistant"]

    async def test_started_row_records_the_flag_it_served_under(self, chat) -> None:
        h_on = chat(settings=ChatSettings(cross_session_episodic=True))
        sid_on = await h_on.service.create_session()
        started_on = (await h_on.events(sid_on))[0]
        assert started_on.payload["cross_session_episodic"] is True

        h_off = chat()
        sid_off = await h_off.service.create_session()
        started_off = (await h_off.events(sid_off))[0]
        assert started_off.payload["cross_session_episodic"] is False


class TestSemanticFactsAreGlobal:
    """Facts cross sessions with their origin stamped; raw turns do not."""

    async def test_fact_learned_in_a_is_served_to_b_with_provenance(
        self, chat
    ) -> None:
        h = chat()
        sid_a = await h.service.create_session()
        sid_b = await h.service.create_session()
        await h.service.send(sid_a, "Apollo is blocked until Friday.")

        facade_b = h.facade_for(sid_b, turn_id="b-turn")
        served = await facade_b.recall("What happens on Friday?", k=5)

        assert len(served) == 1
        assert served[0]["statement"] == "Apollo is blocked until Friday"
        # The origin is stamped on the served mapping...
        assert served[0]["session_id"] == str(sid_a)
        # ...and survives into the bundle's provenance, where the memory rail
        # renders it.
        turns = await facade_b.episodic_turns(sid_b, k=6)
        bundle = build_context_bundle(
            persona=facade_b.persona,
            recalled_facts=served,
            recent_turns=turns,
            token_ceiling=facade_b.tokens_budget,
        )
        assert [p.origin_session for p in bundle.provenance] == [str(sid_a)]
        # ...and B's consult row names the fact it served.
        consults = [
            e
            for e in await h.events(sid_b)
            if e.event_type is EventType.MEMORY_RECALL_CONSULTED
        ]
        assert consults[0].payload["fact_ids"] == [served[0]["id"]]
        assert consults[0].payload["refused"] is False

    async def test_b_reply_notes_the_cross_session_fact(self, chat) -> None:
        h = chat()
        sid_a = await h.service.create_session()
        sid_b = await h.service.create_session()
        await h.service.send(sid_a, "Apollo is blocked until Friday.")
        r = await h.service.send(sid_b, "What happens on Friday?")
        assert r.reply == "What happens on Friday?\n(from memory: Apollo is blocked until Friday)"


class TestResumeEndList:
    """Resume is re-reading the lineage; ending is one more row on it."""

    async def test_unknown_session_raises_session_not_found(self, chat) -> None:
        from paa.chat.session import SessionNotFoundError

        h = chat()
        ghost = uuid.uuid4()
        with pytest.raises(SessionNotFoundError, match="no chat session lineage"):
            await h.service.resume_session(ghost)
        # The fold itself is honest about why: an empty lineage folds to None.
        assert await session_record(h.ledger, ghost) is None

    async def test_end_session_writes_the_folds_own_counters(self, chat) -> None:
        h = chat()
        sid = await h.service.create_session()
        await h.service.send(sid, "Apollo is blocked until Friday.")
        r2 = await h.service.send(sid, "What happens on Friday?")

        record = await h.service.end_session(sid, stopped_reason="user")
        assert record.status == STATUS_ENDED
        assert record.stopped_reason == "user"
        assert record.turns_proposed == 2
        assert record.turns_completed == 2
        completed = [
            e
            for e in await h.events(sid)
            if e.event_type is EventType.CHAT_TURN_COMPLETED
        ]
        expected_tokens = sum(
            int(e.payload["tokens_consumed"]) for e in completed
        )
        assert record.tokens_consumed == expected_tokens
        assert r2.tokens_consumed > 0
        ended = [
            e
            for e in await h.events(sid)
            if e.event_type is EventType.CHAT_SESSION_ENDED
        ]
        assert ended[0].payload["turns_proposed"] == 2
        assert ended[0].payload["tokens_consumed"] == record.tokens_consumed

    async def test_turns_after_an_ended_row_read_active_again(self, chat) -> None:
        """The fold follows the last session-scoped marker: the record is a
        view of the lineage, and the lineage says there was more conversation."""
        h = chat()
        sid = await h.service.create_session()
        await h.service.end_session(sid, stopped_reason="user")
        assert (await h.service.resume_session(sid)).status == STATUS_ENDED

        await h.service.send(sid, "Apollo is blocked until Friday.")
        record = await h.service.resume_session(sid)
        assert record.status == STATUS_ACTIVE
        assert record.stopped_reason is None
        kinds = await h.kinds(sid)
        assert kinds[0] == EventType.CHAT_SESSION_STARTED.value
        assert EventType.CHAT_SESSION_ENDED.value in kinds
        assert kinds[-1] == EventType.CHAT_TURN_COMPLETED.value

    async def test_list_sessions_folds_every_started_lineage(self, chat) -> None:
        h = chat()
        first = await h.service.create_session()
        second = await h.service.create_session()
        third = await h.service.create_session()
        await h.service.send(second, "Apollo is blocked until Friday.")

        records = await h.service.list_sessions(limit=10)
        assert [r.session_id for r in records] == [third, second, first]
        assert all(r.status == STATUS_ACTIVE for r in records)
        assert next(r for r in records if r.session_id == second).turns_completed == 1
        assert next(r for r in records if r.session_id == first).turns_proposed == 0

        limited = await h.service.list_sessions(limit=2)
        assert len(limited) == 2
        assert all(r.session_id in {first, second, third} for r in limited)

    async def test_list_sessions_reads_only_started_scans(self, chat) -> None:
        """A session-less turn lineage (no STARTED row ever) is not listed."""
        h = chat()
        orphan = uuid.uuid4()
        journal = ChatSessionJournal(h.ledger, orphan)
        await journal.turn_proposed("t-1", "hello")
        assert await list_sessions(h.ledger, limit=10) == []
        # ...but the fold still answers for the lineage itself.
        record = await session_record(h.ledger, orphan)
        assert record is not None
        assert record.turns_proposed == 1


class TestFoldSession:
    """The fold is total, defensive, and derived — nothing else writes it."""

    def test_empty_lineage_folds_to_none(self) -> None:
        assert fold_session([]) is None

    def test_unknown_event_types_do_not_break_the_fold(self) -> None:
        cid = new_correlation_id()
        stream = [
            _event(EventType.CHAT_SESSION_STARTED, cid, 1, **_started_payload(cid)),
            LedgerEvent(
                correlation_id=cid,
                event_type=EventType.RECOVERY_REPLAY_COMPLETED,
                state_version=2,
                payload={"v": 1},
            ),
            _event(EventType.CHAT_TURN_PROPOSED, cid, 3),
        ]
        record = fold_session(stream)
        assert record is not None
        assert record.turns_proposed == 1
        assert record.state_version == 3

    def test_malformed_payloads_fold_defensively(self) -> None:
        """Recovery folds histories written by older code: one malformed row
        must not cost the well-formed rows beside it."""
        cid = new_correlation_id()
        stream = [
            _event(EventType.CHAT_SESSION_STARTED, cid, 1, **_started_payload(cid)),
            _event(EventType.CHAT_TURN_COMPLETED, cid, 2, tokens_consumed="not-an-int"),
            _event(EventType.CHAT_TURN_COMPLETED, cid, 3, tokens_consumed=7),
            _event(EventType.CHAT_SESSION_STARTED, cid, 4, scope=""),
        ]
        record = fold_session(stream)
        assert record is not None
        assert record.tokens_consumed == 7
        assert record.scope == "personal"  # an empty scope keeps the default
        assert record.turns_completed == 2

    def test_last_session_scoped_marker_wins(self) -> None:
        cid = new_correlation_id()
        stream = [
            _event(EventType.CHAT_SESSION_STARTED, cid, 1, **_started_payload(cid)),
            _event(
                EventType.CHAT_SESSION_ENDED, cid, 2, stopped_reason="operator"
            ),
            _event(EventType.CHAT_TURN_PROPOSED, cid, 3),
        ]
        record = fold_session(stream)
        assert record is not None
        assert record.status == STATUS_ACTIVE
        assert record.stopped_reason is None

    def test_turn_status_names_the_terminal_states(self) -> None:
        assert set(ChatTurnStatus.__dict__) >= {"COMPLETED", "FAILED", "CANCELLED"}
        assert ChatTurnStatus.CANCELLED == "CANCELLED"


def _started_payload(cid: uuid.UUID) -> dict[str, object]:
    return {
        "scope": "personal",
        "mandate_ref": None,
        "model": "echo",
        "cross_session_episodic": False,
        "session_id": str(cid),
    }


class TestReplayProperties:
    """Hypothesis, following tests/ledger/test_replay.py: the fold and the
    projection are total over any ordering of the chat vocabulary."""

    @given(seq=st.lists(st.sampled_from(CHAT_EVENT_TYPES), min_size=1, max_size=14))
    @hyp_settings(max_examples=80, deadline=None)
    def test_any_chat_ordering_replays_clean_and_folds_to_its_counts(
        self, seq: list[EventType]
    ) -> None:
        cid = uuid.uuid4()
        events = _stream(cid, seq)

        state = replay(events)
        assert state.unknown_events == []
        assert state.attempts == 0
        assert not state.is_terminal

        record = fold_session(events)
        assert record is not None
        assert record.session_id == cid
        assert record.state_version == len(events)
        assert record.turns_proposed == seq.count(EventType.CHAT_TURN_PROPOSED)
        assert record.turns_completed == seq.count(EventType.CHAT_TURN_COMPLETED)
        assert record.turns_failed == seq.count(EventType.CHAT_TURN_FAILED)
        assert record.turns_cancelled == seq.count(EventType.CHAT_TURN_CANCELLED)
        assert record.tokens_consumed == 7 * seq.count(EventType.CHAT_TURN_COMPLETED)

        started_or_ended = [
            i
            for i, et in enumerate(seq)
            if et
            in (
                EventType.CHAT_SESSION_STARTED,
                EventType.CHAT_SESSION_ENDED,
                EventType.CHAT_TURN_PROPOSED,
            )
        ]
        if not started_or_ended:
            assert record.status == STATUS_ACTIVE  # the fold's initial value
        else:
            last = seq[started_or_ended[-1]]
            expected = (
                STATUS_ENDED if last is EventType.CHAT_SESSION_ENDED else STATUS_ACTIVE
            )
            assert record.status == expected

    @given(seq=st.lists(st.sampled_from(CHAT_EVENT_TYPES), min_size=2, max_size=10))
    @hyp_settings(max_examples=50, deadline=None)
    def test_fold_over_prefixes_is_consistent_with_the_whole(
        self, seq: list[EventType]
    ) -> None:
        """Snapshotting soundness, session-shaped: folding the whole stream
        equals folding a prefix and then the remainder onto that state."""
        cid = uuid.uuid4()
        events = _stream(cid, seq)
        cut = len(events) // 2
        whole = fold_session(events)
        head = fold_session(events[:cut])
        assert whole is not None and head is not None
        assert whole.turns_proposed >= head.turns_proposed
        assert whole.turns_completed >= head.turns_completed
        assert whole.tokens_consumed >= head.tokens_consumed
        tail = fold_session(events[cut:])
        assert tail is not None
        assert whole.turns_proposed == head.turns_proposed + tail.turns_proposed
        assert whole.turns_completed == head.turns_completed + tail.turns_completed
        assert whole.tokens_consumed == head.tokens_consumed + tail.tokens_consumed


class TestJournalIdempotency:
    """The ADR-0008 key: one session lineage, attempt 0, turn-and-kind."""

    async def test_redelivered_proposal_dedupes_in_the_store(
        self, chat
    ) -> None:
        h = chat()
        sid = await h.service.create_session()
        journal = ChatSessionJournal(h.ledger, sid)
        first = await journal.turn_proposed("t-1", "hello")
        again = await journal.turn_proposed("t-1", "hello")
        assert again.event_id == first.event_id
        assert await h.ledger.count() == 2  # STARTED + one PROPOSED

    async def test_one_turns_outcome_kinds_are_distinct_rows(self, chat) -> None:
        h = chat()
        sid = await h.service.create_session()
        journal = ChatSessionJournal(h.ledger, sid)
        await journal.turn_proposed("t-1", "hello")
        await journal.turn_completed(
            "t-1",
            reply="hello",
            tokens_consumed=1,
            recalled_fact_ids=[],
            signal_ids=[],
            facts_written=[],
        )
        await journal.turn_cancelled("t-1", partial_reply=None)
        discriminators = [e.discriminator for e in await h.events(sid)]
        assert discriminators == [
            "session-started",
            "t-1:PROPOSED",
            "t-1:COMPLETED",
            "t-1:CANCELLED",
        ]
