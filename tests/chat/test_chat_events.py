"""W4: the eight chat-session / memory-consult event types (ADR-0046 pattern).

What is pinned here
-------------------
* The v1 payload contract of all eight types (golden dicts), driven through
  the journal that emits them — so the golden cannot drift from the emitter.
* Preview discipline: a chat message is an opaque input; only its SHA-256,
  length and shape preview are ledgered, and a secret-bearing message never
  leaks into any payload.
* The idempotency discriminator `<turn_id>:<KIND>` fits the ADR-0008 key —
  the journal cannot silently change it, and a redelivered append dedupes in
  the store.
* Phase inertness: in ``_NO_PHASE_CHANGE``, not in ``_PHASE_MAP``, not in
  ``TERMINAL_EVENTS`` — replay never records a chat row as unknown, and the
  boot sweep never reads conversation as resumable task work.
* The tier fold ignores them (no scalar ``fact_id`` in any chat payload).
* Store round-trip: the eight types read back through ``_row_to_event`` and
  the chain verifies.
* The sweep's chat-half (W4's recovery rule): a chat-only lineage is skipped
  as conversation (``SKIPPED_CHAT_LINEAGE``), a crash-interrupted turn is
  closed as ``CHAT_TURN_FAILED(reason="interrupted")`` with the golden v1
  shape, and a lineage mixing chat rows with task rows keeps the ordinary
  task recovery path.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.chat.session import (
    ChatSessionJournal,
    session_record,
    turn_discriminator,
)
from paa.core.types import TERMINAL_EVENTS, EventType, new_correlation_id
from paa.ledger.events import (
    LedgerEvent,
    canonical_json,
    compute_idempotency_key,
)
from paa.ledger.recovery import (
    RecoveryEngine,
    RecoveryOutcome,
    close_interrupted_chat_turns,
    find_interrupted_chat_turns,
)
from paa.ledger.replay import _NO_PHASE_CHANGE, _PHASE_MAP, apply_tier_event, replay
from paa.ledger.store import LedgerStore
from tests.conftest import RecordingRequeuer

SESSION_UUID = uuid.UUID("5f0e1d2c-3b4a-4c6d-8e9f-0a1b2c3d4e5f")
SID = str(SESSION_UUID)
TURN = "aa6e0f11-22b3-4c5d-9e0f-1a2b3c4d5e6f"

CHAT_TYPES = (
    EventType.CHAT_SESSION_STARTED,
    EventType.CHAT_SESSION_ENDED,
    EventType.CHAT_TURN_PROPOSED,
    EventType.CHAT_TURN_COMPLETED,
    EventType.CHAT_TURN_FAILED,
    EventType.CHAT_TURN_CANCELLED,
    EventType.MEMORY_RECALL_CONSULTED,
    EventType.MEMORY_SIGNAL_EXTRACTED,
)

# -- golden v1 payloads (ADR-0046) -----------------------------------------
# These dicts are the frozen contracts, written the way the journal writes
# them (hashes computed over the same fixed inputs). A version bump requires
# updating these rows in the same commit, which is the intended tripwire.

HELLO_SHA = hashlib.sha256(b"hello").hexdigest()
HELLO_PREVIEW = f"<str len=5 sha256={HELLO_SHA[:8]}>"
HI_SHA = hashlib.sha256(b"hi there").hexdigest()

GOLDEN_PAYLOADS: dict[EventType, dict] = {
    EventType.CHAT_SESSION_STARTED: {
        "v": 1,
        "session_id": SID,
        "scope": "personal",
        "mandate_ref": None,
        "model": "echo",
        "cross_session_episodic": False,
    },
    EventType.CHAT_SESSION_ENDED: {
        "v": 1,
        "session_id": SID,
        "stopped_reason": "user",
        "turns_proposed": 3,
        "turns_completed": 2,
        "turns_failed": 0,
        "turns_cancelled": 1,
        "tokens_consumed": 44,
    },
    EventType.CHAT_TURN_PROPOSED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "text_sha256": HELLO_SHA,
        "text_chars": 5,
        "text_preview_redacted": HELLO_PREVIEW,
    },
    EventType.CHAT_TURN_COMPLETED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "reply_sha256": HI_SHA,
        "reply_chars": 8,
        "tokens_consumed": 2,
        "recalled_fact_ids": ["f-1"],
        "signal_ids": ["s-1", "s-2"],
        "facts_written": ["f-9"],
    },
    EventType.CHAT_TURN_FAILED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "reason": "model unavailable",
    },
    EventType.CHAT_TURN_CANCELLED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "partial_reply_sha256": HI_SHA,
        "partial_reply_chars": 8,
    },
    EventType.MEMORY_RECALL_CONSULTED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "query_sha256": HELLO_SHA,
        "query_chars": 5,
        "k": 5,
        "returned": 1,
        "fact_ids": ["f-1"],
        "refused": False,
        "refusal_reason": None,
    },
    EventType.MEMORY_SIGNAL_EXTRACTED: {
        "v": 1,
        "session_id": SID,
        "turn_id": TURN,
        "signal_id": "s-1",
        "outcome": "processed",
        "facts_written": ["f-9"],
    },
}

EMITTERS = {
    EventType.CHAT_SESSION_STARTED: lambda j: j.session_started(
        scope="personal",
        mandate_ref=None,
        model="echo",
        cross_session_episodic=False,
    ),
    EventType.CHAT_SESSION_ENDED: lambda j: j.session_ended(
        stopped_reason="user",
        turns_proposed=3,
        turns_completed=2,
        turns_failed=0,
        turns_cancelled=1,
        tokens_consumed=44,
    ),
    EventType.CHAT_TURN_PROPOSED: lambda j: j.turn_proposed(TURN, "hello"),
    EventType.CHAT_TURN_COMPLETED: lambda j: j.turn_completed(
        TURN,
        reply="hi there",
        tokens_consumed=2,
        recalled_fact_ids=["f-1"],
        signal_ids=["s-1", "s-2"],
        facts_written=["f-9"],
    ),
    EventType.CHAT_TURN_FAILED: lambda j: j.turn_failed(
        TURN, reason="model unavailable"
    ),
    EventType.CHAT_TURN_CANCELLED: lambda j: j.turn_cancelled(
        TURN, partial_reply="hi there"
    ),
    EventType.MEMORY_RECALL_CONSULTED: lambda j: j.recall_consulted(
        turn_id=TURN, query="hello", k=5, fact_ids=["f-1"]
    ),
    EventType.MEMORY_SIGNAL_EXTRACTED: lambda j: j.signal_extracted(
        turn_id=TURN, signal_id="s-1", outcome="processed", facts_written=["f-9"]
    ),
}

DISCRIMINATORS = {
    EventType.CHAT_SESSION_STARTED: "session-started",
    EventType.CHAT_SESSION_ENDED: "session-ended",
    EventType.CHAT_TURN_PROPOSED: f"{TURN}:PROPOSED",
    EventType.CHAT_TURN_COMPLETED: f"{TURN}:COMPLETED",
    EventType.CHAT_TURN_FAILED: f"{TURN}:FAILED",
    EventType.CHAT_TURN_CANCELLED: f"{TURN}:CANCELLED",
    EventType.MEMORY_RECALL_CONSULTED: f"recall:{TURN}:{HELLO_SHA[:12]}",
    EventType.MEMORY_SIGNAL_EXTRACTED: "signal:s-1",
}


# -- golden v1 payloads ------------------------------------------------------


class TestGoldenV1Payloads:
    """Each of the 8 types carries its exact v1 payload, byte-stable, as the
    journal emits it."""

    def test_eight_golden_payloads_are_pinned(self) -> None:
        assert set(GOLDEN_PAYLOADS) == set(CHAT_TYPES)
        assert len(GOLDEN_PAYLOADS) == 8

    def test_every_payload_carries_v1(self) -> None:
        for etype, payload in GOLDEN_PAYLOADS.items():
            assert payload["v"] == 1, f"{etype.value} lost its schema version"

    @pytest.mark.parametrize("etype,payload", list(GOLDEN_PAYLOADS.items()))
    def test_payload_round_trips_through_canonical_json(
        self, etype: EventType, payload: dict
    ) -> None:
        """canonical_json is frozen — reordered keys produce identical bytes."""
        reordered = dict(reversed(list(payload.items())))
        assert canonical_json(payload) == canonical_json(reordered)
        assert json.loads(canonical_json(payload)) == payload

    async def test_journal_writes_the_golden_payload_and_discriminator(
        self, ledger: LedgerStore
    ) -> None:
        """The golden cannot drift from the emitter: every payload here is
        what the journal actually appended."""
        journal = ChatSessionJournal(ledger, SESSION_UUID)
        for etype in CHAT_TYPES:
            event = await EMITTERS[etype](journal)
            assert event.payload == GOLDEN_PAYLOADS[etype], etype.value
            assert event.correlation_id == SESSION_UUID
            assert event.discriminator == DISCRIMINATORS[etype]

    @pytest.mark.parametrize("etype", list(GOLDEN_PAYLOADS))
    def test_v1_payloads_carry_no_scalar_fact_id(self, etype: EventType) -> None:
        """``apply_tier_event`` folds any event naming a scalar ``fact_id``
        into tier state; a chat payload has no business moving a tier."""
        payload = GOLDEN_PAYLOADS[etype]
        assert "fact_id" not in payload, f"{etype.value} would leak into tier state"

    @pytest.mark.parametrize("etype", list(GOLDEN_PAYLOADS))
    def test_payloads_carry_no_verbatim_message(self, etype: EventType) -> None:
        """The text behind every hashed field stays out of the payload."""
        payload = GOLDEN_PAYLOADS[etype]
        blob = canonical_json(payload)
        assert "hello" not in blob
        assert "hi there" not in blob

    async def test_secret_bearing_message_is_never_ledgered(
        self, ledger: LedgerStore
    ) -> None:
        secret = "sk-supersecret-abcdef0123456789"
        journal = ChatSessionJournal(ledger, SESSION_UUID)
        proposed = await journal.turn_proposed(TURN, f"my api key is {secret}")
        completed = await journal.turn_completed(
            TURN,
            reply=f"your key {secret} is stored",
            tokens_consumed=1,
            recalled_fact_ids=[],
            signal_ids=[],
            facts_written=[],
        )
        cancelled = await journal.turn_cancelled(TURN, partial_reply=secret)
        consulted = await journal.recall_consulted(
            turn_id=TURN, query=f"where is {secret}?", k=5, fact_ids=[]
        )
        for event in (proposed, completed, cancelled, consulted):
            assert secret not in canonical_json(event.payload)


class TestIdempotencyDiscriminator:
    """`<turn_id>:<KIND>` fits the ADR-0008 key — the journal cannot change it."""

    def test_key_shape_matches_adr0008(self) -> None:
        event = LedgerEvent.create(
            SESSION_UUID,
            EventType.CHAT_TURN_PROPOSED,
            session_id=SESSION_UUID,
            attempt=0,
            discriminator=f"{TURN}:PROPOSED",
            payload=dict(GOLDEN_PAYLOADS[EventType.CHAT_TURN_PROPOSED]),
        )
        assert event.idempotency_key == compute_idempotency_key(
            SESSION_UUID,
            EventType.CHAT_TURN_PROPOSED,
            attempt=0,
            discriminator=f"{TURN}:PROPOSED",
        )

    def test_each_outcome_kind_is_a_distinct_key(self) -> None:
        keys = {
            compute_idempotency_key(
                SESSION_UUID,
                etype,
                attempt=0,
                discriminator=f"{TURN}:{kind}",
            )
            for etype, kind in (
                (EventType.CHAT_TURN_PROPOSED, "PROPOSED"),
                (EventType.CHAT_TURN_COMPLETED, "COMPLETED"),
                (EventType.CHAT_TURN_FAILED, "FAILED"),
                (EventType.CHAT_TURN_CANCELLED, "CANCELLED"),
            )
        }
        assert len(keys) == 4

    @pytest.mark.parametrize("bad", ["", "a:b", ":", None])
    def test_malformed_turn_id_is_refused_before_it_can_dedupe(self, bad) -> None:
        with pytest.raises(ValueError, match="turn id"):
            turn_discriminator(bad, "PROPOSED")  # type: ignore[arg-type]

    def test_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown turn discriminator kind"):
            turn_discriminator(TURN, "WEIRD")

    async def test_redelivery_dedupes_in_the_store(self, ledger: LedgerStore) -> None:
        journal = ChatSessionJournal(ledger, SESSION_UUID)
        first = await journal.turn_proposed(TURN, "hello")
        second = await journal.turn_proposed(TURN, "hello")
        assert second.event_id == first.event_id
        assert await ledger.count() == 1


class TestPhaseInertness:
    """Chat events move no phase, close nothing, and are never unknown."""

    def test_all_eight_are_in_no_phase_change(self) -> None:
        for etype in CHAT_TYPES:
            assert etype in _NO_PHASE_CHANGE, f"{etype.value} must be informational"
            assert etype not in _PHASE_MAP, f"{etype.value} must not move a phase"

    def test_none_is_terminal(self) -> None:
        for etype in CHAT_TYPES:
            assert etype not in TERMINAL_EVENTS, (
                f"{etype.value} must never close a lineage"
            )

    @pytest.mark.parametrize("etype", list(CHAT_TYPES))
    def test_single_chat_event_leaves_projection_alone(self, etype: EventType) -> None:
        cid = new_correlation_id()
        stream = [
            LedgerEvent.create(
                cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "x"}}
            ),
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0}),
            LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype])),
        ]
        state = replay(stream)
        assert state.unknown_events == []
        assert state.attempts == 1
        assert state.tokens_consumed == 0

    def test_chat_events_interleaved_in_a_task_history_do_not_perturb_it(self) -> None:
        cid = new_correlation_id()
        task = [
            LedgerEvent.create(
                cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "x"}}
            ),
            LedgerEvent.create(cid, EventType.EXECUTION_STARTED, payload={"step_index": 0}),
            LedgerEvent.create(
                cid,
                EventType.EXECUTION_COMPLETED,
                payload={"step_index": 0, "tokens_consumed": 10},
            ),
            LedgerEvent.create(
                cid, EventType.MUTATION_COMMITTED, payload={"applied_patch_sha256": "a" * 64}
            ),
        ]
        interleaved = [
            LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype]))
            for etype in CHAT_TYPES
        ]
        full = replay(task)
        mixed = replay(task[:2] + interleaved + task[2:])
        assert mixed.phase is full.phase
        assert mixed.unknown_events == []
        assert mixed.completed_steps == full.completed_steps
        assert mixed.tokens_consumed == full.tokens_consumed

    def test_chat_only_history_projects_to_initial_phase(self) -> None:
        cid = new_correlation_id()
        stream = [
            LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype]))
            for etype in CHAT_TYPES
        ]
        state = replay(stream)
        assert state.unknown_events == []
        assert not state.is_terminal
        assert state.attempts == 0

    @given(seq=st.lists(st.sampled_from(list(CHAT_TYPES)), min_size=1, max_size=10))
    @hyp_settings(max_examples=60, deadline=None)
    def test_replay_total_over_chat_orderings(self, seq: list[EventType]) -> None:
        cid = uuid.uuid4()
        events = [
            LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype]))
            for etype in seq
        ]
        state = replay(events)
        assert state.unknown_events == []
        assert state.attempts == 0


class TestTierFoldIgnoresChatEvents:
    def test_apply_tier_event_is_a_noop_for_all_eight(self) -> None:
        cid = new_correlation_id()
        state: dict[str, str] = {}
        for etype in CHAT_TYPES:
            folded = apply_tier_event(
                state,
                LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype])),
            )
            assert folded == state == {}


class TestStoreRoundTrip:
    async def test_all_eight_round_trip_and_chain_verifies(
        self, ledger: LedgerStore
    ) -> None:
        journal = ChatSessionJournal(ledger, SESSION_UUID)
        for etype in CHAT_TYPES:
            await EMITTERS[etype](journal)
        rows = await ledger.read_correlation(SESSION_UUID)
        assert [row.event_type for row in rows] == list(CHAT_TYPES)
        assert [row.payload for row in rows] == [
            dict(GOLDEN_PAYLOADS[et]) for et in CHAT_TYPES
        ]
        ok, problems = await ledger.verify_chain(SESSION_UUID)
        assert ok, problems
        head = await ledger.head(SESSION_UUID)
        assert head is not None
        assert not head.is_terminal  # the head must not close a chat lineage


# -- the sweep's chat half ----------------------------------------------------


async def _chat_lineage(
    ledger: LedgerStore, *, outcomes: int = 1, turn_id: str = TURN
) -> uuid.UUID:
    """One session lineage: STARTED, then a proposal, then ``outcomes`` rows
    closing it (0 leaves the turn proposed-but-outcome-less)."""
    sid = uuid.uuid4()
    journal = ChatSessionJournal(ledger, sid)
    await journal.session_started(
        scope="personal",
        mandate_ref=None,
        model="echo",
        cross_session_episodic=False,
    )
    await journal.turn_proposed(turn_id, "hello")
    if outcomes >= 1:
        await journal.turn_completed(
            turn_id,
            reply="hello",
            tokens_consumed=7,
            recalled_fact_ids=[],
            signal_ids=[],
            facts_written=[],
        )
    if outcomes >= 2:
        await journal.turn_cancelled(turn_id, partial_reply=None)
    return sid


class TestRecoverySweepChatLineages:
    async def test_chat_only_lineage_is_skipped_never_requeued(
        self, ledger: LedgerStore, db, requeuer: RecordingRequeuer
    ) -> None:
        sid = await _chat_lineage(ledger, outcomes=1)
        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()

        assert report.scanned == 1
        assert report.count(RecoveryOutcome.SKIPPED_CHAT_LINEAGE) == 1
        assert requeuer.calls == []  # conversation is never re-queued as a task
        # The sweep wrote nothing: the turn is closed, the session is not a task.
        kinds = [e.event_type for e in await ledger.read_correlation(sid)]
        assert EventType.CHAT_TURN_FAILED not in kinds

    async def test_interrupted_turn_is_closed_as_failed_with_the_golden_shape(
        self, ledger: LedgerStore, db
    ) -> None:
        turn_id = "aa6e0f11-22b3-4c5d-9e0f-1a2b3c4d5e6f-interrupted"
        sid = await _chat_lineage(ledger, outcomes=0, turn_id=turn_id)
        engine = RecoveryEngine(ledger, db)
        report = await engine.boot_sweep()

        assert report.count(RecoveryOutcome.SKIPPED_CHAT_LINEAGE) == 1
        events = await ledger.read_correlation(sid)
        failed = [
            e for e in events if e.event_type is EventType.CHAT_TURN_FAILED
        ]
        assert len(failed) == 1
        assert failed[0].payload == {
            "v": 1,
            "session_id": str(sid),
            "turn_id": turn_id,
            "reason": "interrupted",
        }
        assert failed[0].discriminator == f"{turn_id}:FAILED"
        ok, problems = await ledger.verify_chain(sid)
        assert ok, problems
        # The fold reads the close: one failed turn, not an eternally open one.
        record = await session_record(ledger, sid)
        assert record is not None
        assert record.turns_failed == 1
        assert record.turns_proposed == 1

    async def test_second_sweep_is_a_noop(self, ledger: LedgerStore, db) -> None:
        await _chat_lineage(ledger, outcomes=0)
        engine = RecoveryEngine(ledger, db)
        await engine.boot_sweep()
        count_after_first = await ledger.count()

        report = await engine.boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_CHAT_LINEAGE) == 1
        assert await ledger.count() == count_after_first

    async def test_outcomed_turns_are_never_interrupted(
        self, ledger: LedgerStore, db
    ) -> None:
        turn_id = "aa6e0f11-22b3-4c5d-9e0f-1a2b3c4d5e6f-outcomed"
        sid = await _chat_lineage(ledger, outcomes=2, turn_id=turn_id)
        await RecoveryEngine(ledger, db).boot_sweep()

        assert find_interrupted_chat_turns(await ledger.read_correlation(sid)) == []
        kinds = [e.event_type for e in await ledger.read_correlation(sid)]
        assert EventType.CHAT_TURN_FAILED not in kinds

    async def test_malformed_proposal_rows_are_skipped_not_fatal(
        self, ledger: LedgerStore, db
    ) -> None:
        sid = uuid.uuid4()
        await ledger.append(
            LedgerEvent.create(
                sid,
                EventType.CHAT_SESSION_STARTED,
                payload=dict(GOLDEN_PAYLOADS[EventType.CHAT_SESSION_STARTED]),
            )
        )
        await ledger.append(
            LedgerEvent.create(
                sid,
                EventType.CHAT_TURN_PROPOSED,
                payload={"v": 1, "session_id": str(sid)},  # no usable turn_id
            )
        )
        await RecoveryEngine(ledger, db).boot_sweep()
        events = await ledger.read_correlation(sid)
        assert [e.event_type for e in events] == [
            EventType.CHAT_SESSION_STARTED,
            EventType.CHAT_TURN_PROPOSED,
        ]

    async def test_mixed_lineage_keeps_the_ordinary_task_path(
        self, ledger: LedgerStore, db, requeuer: RecordingRequeuer
    ) -> None:
        """A lineage mixing chat rows with task rows is NOT exempt — the task
        half decides, and its interrupted turn is never closed by the chat rule."""
        sid = await _chat_lineage(ledger, outcomes=0)
        await ledger.append(
            LedgerEvent.create(sid, EventType.TASK_REQUESTED, payload={"request": {"goal": "x"}})
        )
        await ledger.append(
            LedgerEvent.create(sid, EventType.EXECUTION_STARTED, payload={"step_index": 0})
        )

        report = await RecoveryEngine(ledger, db, requeuer=requeuer).boot_sweep()
        assert report.count(RecoveryOutcome.SKIPPED_CHAT_LINEAGE) == 0
        assert report.count(RecoveryOutcome.RESUMED) == 1
        assert len(requeuer.calls) == 1
        kinds = [e.event_type for e in await ledger.read_correlation(sid)]
        # The chat rule did not close the proposal; the task path owns this lineage.
        assert EventType.CHAT_TURN_FAILED not in kinds

    async def test_find_helper_returns_the_proposal_event_in_ledger_order(
        self, ledger: LedgerStore, db
    ) -> None:
        turn_a, turn_b = "turn-a", "turn-b"
        sid = uuid.uuid4()
        journal = ChatSessionJournal(ledger, sid)
        await journal.session_started(
            scope="personal",
            mandate_ref=None,
            model="echo",
            cross_session_episodic=False,
        )
        await journal.turn_proposed(turn_a, "first")
        await journal.turn_proposed(turn_b, "second")
        await journal.turn_completed(
            turn_b,
            reply="done",
            tokens_consumed=1,
            recalled_fact_ids=[],
            signal_ids=[],
            facts_written=[],
        )

        events = await ledger.read_correlation(sid)
        interrupted = find_interrupted_chat_turns(events)
        assert [e.payload["turn_id"] for e in interrupted] == [turn_a]

        closed = await close_interrupted_chat_turns(ledger, interrupted)
        assert [e.payload["turn_id"] for e in closed] == [turn_a]
        assert await ledger.count() == 5  # STARTED + 2 PROPOSED + COMPLETED + FAILED
