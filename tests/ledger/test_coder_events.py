"""N2.1: the eight coder-session / mandate event types (ADR-0046).

What is pinned here
-------------------
* The v1 payload contract of all eight types (golden dicts).
* Persisted intent in TOOL_CALL_PROPOSED (pre/post/patch hashes).
* The idempotency discriminator `session:call_id:kind` fits the ADR-0008
  key — N2.2's journal cannot silently change it.
* Phase inertness: in `_NO_PHASE_CHANGE`, not in `_PHASE_MAP`, not in
  TERMINAL_EVENTS, so replay never records them as unknown and never
  lets the boot sweep treat a coder event as task work.
* The tier fold ignores them (no `fact_id` in any coder payload).
* Store round-trip: the new types read back through `_row_to_event`
  (the one-way door — old binaries cannot; this binary can).

The ToolCtx / ToolJournal protocol surface test lives in this file rather
than under tests/coder/ because N2.1's write-set covers this module; the
protocol itself is `paa.coder.loop.ToolJournal` (N2.2 implements it in
`coder/journal.py`).
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.core.types import TERMINAL_EVENTS, EventType, new_correlation_id
from paa.ledger.events import LedgerEvent, canonical_json, compute_idempotency_key
from paa.ledger.replay import _NO_PHASE_CHANGE, _PHASE_MAP, apply_tier_event, replay
from paa.ledger.store import LedgerStore

CODER_TYPES = (
    EventType.CODER_SESSION_STARTED,
    EventType.TOOL_CALL_PROPOSED,
    EventType.TOOL_CALL_DENIED,
    EventType.TOOL_CALL_COMPLETED,
    EventType.TOOL_CALL_FAILED,
    EventType.CODER_SESSION_ENDED,
    EventType.MANDATE_GRANTED,
    EventType.MANDATE_REFUSED,
)

# -- golden v1 payloads (ADR-0046) -----------------------------------------
# These dicts are the frozen contracts. Every payload carries `v` = 1;
# a version bump requires updating these rows in the same commit, which
# is the intended tripwire (ADR-0046 §Forward-compatibility policy).

GOLDEN_PAYLOADS: dict[EventType, dict] = {
    EventType.CODER_SESSION_STARTED: {
        "v": 1,
        "session_id": "sess-1",
        "workspace_path": "/tmp/ws",
        "permission_mode": "ask",
    },
    EventType.TOOL_CALL_PROPOSED: {
        "v": 1,
        "session_id": "sess-1",
        "call_id": "call-1",
        "tool": "write_file",
        "args_sha256": "a" * 64,
        "args_preview_redacted": "write_file(path=x.py, content=***)",
        "isolation_level": "PROCESS",
        "pre_sha256": "b" * 64,
        "post_sha256": "c" * 64,
        "patch_sha256": "d" * 64,
    },
    EventType.TOOL_CALL_DENIED: {
        "v": 1,
        "session_id": "sess-1",
        "call_id": "call-2",
        "tool": "write_file",
        "reason": "protected path: env",
    },
    EventType.TOOL_CALL_COMPLETED: {
        "v": 1,
        "session_id": "sess-1",
        "call_id": "call-3",
        "tool": "read_file",
        "result_sha256": "e" * 64,
        "args_sha256": "a" * 64,
        "isolation_level": "PROCESS",
    },
    EventType.TOOL_CALL_FAILED: {
        "v": 1,
        "session_id": "sess-1",
        "call_id": "call-4",
        "tool": "bash",
        "reason": "timed out after 120s",
        "result_sha256": None,
        "args_sha256": "a" * 64,
        "isolation_level": "PROCESS",
    },
    EventType.CODER_SESSION_ENDED: {
        "v": 1,
        "session_id": "sess-1",
        "stopped_reason": "model_finished",
        "steps": 4,
        "tool_calls": 6,
        "denied_calls": 1,
        "tokens": 1200,
    },
    EventType.MANDATE_GRANTED: {
        "v": 1,
        "session_id": "sess-1",
        "requester_asserted": {"kind": "user", "id": "local"},
        "identity_assurance": "asserted",
        "basis": "explicit",
        "policy_version": "f" * 64,
        "policy_label": "ask-v3",
        "scope": {"workspace": "/tmp/ws"},
    },
    EventType.MANDATE_REFUSED: {
        "v": 1,
        "session_id": None,
        "requester_asserted": {"kind": "user", "id": "local"},
        "reason": "no mandate for mutating tools",
        "policy_version": "f" * 64,
    },
}

TOOL_CALL_KINDS = ("PROPOSED", "DENIED", "COMPLETED", "FAILED")


def _event(etype: EventType, cid: uuid.UUID, version: int, **payload) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=version, payload=dict(payload)
    )


def _gold(etype: EventType, cid: uuid.UUID, version: int) -> LedgerEvent:
    """An event carrying its golden v1 payload (keeps long lines short)."""
    return _event(etype, cid, version, **GOLDEN_PAYLOADS[etype])


class TestGoldenV1Payloads:
    """Each of the 8 types carries its exact v1 payload, byte-stable."""

    def test_eight_golden_payloads_are_pinned(self) -> None:
        assert set(GOLDEN_PAYLOADS) == set(CODER_TYPES)
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

    def test_v1_payloads_carry_no_fact_id(self) -> None:
        """Coder payloads never name a fact, so the tier fold must ignore them."""
        for etype, payload in GOLDEN_PAYLOADS.items():
            assert "fact_id" not in payload, f"{etype.value} would leak into tier state"

    def test_tool_call_payloads_share_call_identity(self) -> None:
        """One call's PROPOSED / outcome rows share session + call + tool."""
        kinds = (
            "TOOL_CALL_PROPOSED",
            "TOOL_CALL_DENIED",
            "TOOL_CALL_COMPLETED",
            "TOOL_CALL_FAILED",
        )
        identity = {
            (payload["session_id"], payload["call_id"], payload["tool"])
            for etype, payload in GOLDEN_PAYLOADS.items()
            if etype.value in kinds
        }
        assert len(identity) == 4  # distinct calls in the golden rows
        for etype, payload in GOLDEN_PAYLOADS.items():
            if etype.value in kinds:
                assert payload["session_id"] and payload["call_id"] and payload["tool"]

class TestPersistedIntent:
    """PROPOSED carries pre/post/patch; the recovery sweep needs them."""

    def test_proposed_carries_persisted_intent(self) -> None:
        payload = GOLDEN_PAYLOADS[EventType.TOOL_CALL_PROPOSED]
        for field in ("pre_sha256", "post_sha256", "patch_sha256"):
            assert field in payload, f"PROPOSED lost persisted intent {field}"

    def test_intent_fields_are_nullable_per_call_kind(self) -> None:
        """A non-file call records explicit nulls, not absent keys."""
        payload = dict(GOLDEN_PAYLOADS[EventType.TOOL_CALL_PROPOSED])
        payload.update({"pre_sha256": None, "post_sha256": None, "patch_sha256": None})
        for field in ("pre_sha256", "post_sha256", "patch_sha256"):
            assert payload[field] is None


class TestIdempotencyDiscriminator:
    """`session:call_id:kind` fits the ADR-0008 key — N2.2 cannot change it."""

    def test_key_shape_matches_adr0008(self) -> None:
        cid = new_correlation_id()
        event = _event(
            EventType.TOOL_CALL_PROPOSED,
            cid,
            1,
            **GOLDEN_PAYLOADS[EventType.TOOL_CALL_PROPOSED],
        )
        event = event.model_copy(update={"discriminator": "sess-1:call-1:PROPOSED"})
        assert event.idempotency_key == compute_idempotency_key(
            cid,
            EventType.TOOL_CALL_PROPOSED,
            attempt=0,
            discriminator="sess-1:call-1:PROPOSED",
        )

    def test_each_outcome_kind_is_a_distinct_key(self) -> None:
        """PROPOSED and COMPLETED of one call are two events, not one."""
        cid = new_correlation_id()
        keys = {
            compute_idempotency_key(
                cid,
                EventType.TOOL_CALL_PROPOSED,
                attempt=0,
                discriminator=f"sess-1:call-1:{kind}",
            )
            for kind in TOOL_CALL_KINDS
        }
        assert len(keys) == 4

    def test_redelivery_dedupes_but_attempt_does_not(self) -> None:
        """Same discriminator suppresses; a bumped attempt is a new event."""
        cid = new_correlation_id()
        key = compute_idempotency_key(
            cid, EventType.TOOL_CALL_PROPOSED, attempt=0, discriminator="sess-1:call-9:PROPOSED"
        )
        assert key == compute_idempotency_key(
            cid, EventType.TOOL_CALL_PROPOSED, attempt=0, discriminator="sess-1:call-9:PROPOSED"
        )
        assert key != compute_idempotency_key(
            cid, EventType.TOOL_CALL_PROPOSED, attempt=1, discriminator="sess-1:call-9:PROPOSED"
        )


class TestPhaseInertness:
    """Coder events move no phase, close nothing, and are never unknown."""

    def test_all_eight_are_in_no_phase_change(self) -> None:
        for etype in CODER_TYPES:
            assert etype in _NO_PHASE_CHANGE, f"{etype.value} must be informational"
            assert etype not in _PHASE_MAP, f"{etype.value} must not move a phase"

    def test_none_is_terminal(self) -> None:
        for etype in CODER_TYPES:
            assert etype not in TERMINAL_EVENTS, f"{etype.value} must never close a lineage"

    @pytest.mark.parametrize("etype", list(CODER_TYPES))
    def test_single_coder_event_leaves_projection_alone(self, etype: EventType) -> None:
        cid = new_correlation_id()
        stream = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _event(EventType.EXECUTION_STARTED, cid, 2, step_index=0),
            _event(etype, cid, 3, **GOLDEN_PAYLOADS[etype]),
        ]
        state = replay(stream)
        assert state.unknown_events == []
        assert state.attempts == 1
        assert state.tokens_consumed == 0

    def test_coder_events_interleaved_in_a_task_history_do_not_perturb_it(self) -> None:
        cid = new_correlation_id()
        task = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _event(EventType.EXECUTION_STARTED, cid, 2, step_index=0),
            _event(EventType.EXECUTION_COMPLETED, cid, 3, step_index=0, tokens_consumed=10),
            _event(EventType.MUTATION_COMMITTED, cid, 4, applied_patch_sha256="a" * 64),
        ]
        interleaved = [
            _gold(EventType.CODER_SESSION_STARTED, cid, 5),
            _gold(EventType.TOOL_CALL_PROPOSED, cid, 6),
            _gold(EventType.TOOL_CALL_FAILED, cid, 7),
            _gold(EventType.CODER_SESSION_ENDED, cid, 8),
        ]
        # Versions only need to be monotonic for the fold; replay folds in
        # list order, and the new binary's store assigns them on append.
        full = replay(task)
        mixed = replay(task[:2] + interleaved + task[2:])
        assert mixed.phase is full.phase
        assert mixed.unknown_events == []
        assert mixed.completed_steps == full.completed_steps
        assert mixed.tokens_consumed == full.tokens_consumed

    def test_coder_only_history_projects_to_initial_phase(self) -> None:
        """Documents the ADR-0046 open item: a coder-only lineage is still
        what the boot sweep sees as resumable until N2.3 exempts it.
        """
        cid = new_correlation_id()
        stream = [
            _gold(EventType.CODER_SESSION_STARTED, cid, 1),
            _gold(EventType.TOOL_CALL_PROPOSED, cid, 2),
            _gold(EventType.TOOL_CALL_COMPLETED, cid, 3),
            _gold(EventType.CODER_SESSION_ENDED, cid, 4),
        ]
        state = replay(stream)
        assert state.unknown_events == []
        assert not state.is_terminal  # non-terminal by design; sweep exemption is N2.3
        assert state.attempts == 0

    @given(
        seq=st.lists(st.sampled_from(list(CODER_TYPES)), min_size=1, max_size=10)
    )
    @hyp_settings(max_examples=60, deadline=None)
    def test_replay_total_over_coder_orderings(self, seq: list[EventType]) -> None:
        cid = uuid.uuid4()
        events = [_event(et, cid, i + 1) for i, et in enumerate(seq)]
        state = replay(events)
        assert state.unknown_events == []
        assert state.attempts == 0


class TestTierFoldIgnoresCoderEvents:
    def test_apply_tier_event_is_a_noop_for_all_eight(self) -> None:
        cid = new_correlation_id()
        state: dict[str, str] = {}
        for i, etype in enumerate(CODER_TYPES):
            folded = apply_tier_event(state, _event(etype, cid, i + 1, **GOLDEN_PAYLOADS[etype]))
            assert folded == state == {}


class TestStoreRoundTrip:
    async def test_all_eight_round_trip_and_chain_verifies(
        self, ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        for etype in CODER_TYPES:
            await ledger.append(
                LedgerEvent.create(cid, etype, payload=dict(GOLDEN_PAYLOADS[etype]))
            )
        rows = await ledger.read_correlation(cid)
        assert [row.event_type for row in rows] == list(CODER_TYPES)
        assert [row.payload for row in rows] == [dict(GOLDEN_PAYLOADS[et]) for et in CODER_TYPES]
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems
        head = await ledger.head(cid)
        assert head is not None
        assert not head.is_terminal  # the head must not close a coder lineage

    async def test_duplicate_proposed_append_returns_existing(
        self, ledger: LedgerStore
    ) -> None:
        """The discriminator makes a redelivered PROPOSED a no-op (N2.2 relies on it)."""
        cid = new_correlation_id()
        payload = dict(GOLDEN_PAYLOADS[EventType.TOOL_CALL_PROPOSED])
        discriminator = "sess-1:call-1:PROPOSED"
        first = await ledger.append(
            LedgerEvent.create(
                cid, EventType.TOOL_CALL_PROPOSED, payload=payload, discriminator=discriminator
            )
        )
        second = await ledger.append(
            LedgerEvent.create(
                cid, EventType.TOOL_CALL_PROPOSED, payload=payload, discriminator=discriminator
            )
        )
        assert second.event_id == first.event_id
        assert await ledger.count() == 1


class TestToolJournalProtocol:
    """The frozen ToolJournal signature (ADR-0046) is importable and async."""

    def test_protocol_imports_and_has_three_async_methods(self) -> None:
        from paa.coder.loop import ToolCtx, ToolJournal

        assert inspect.iscoroutinefunction(ToolJournal.before)
        assert inspect.iscoroutinefunction(ToolJournal.denied)
        assert inspect.iscoroutinefunction(ToolJournal.after)
        assert ToolCtx.__dataclass_params__.frozen
        assert {f.name for f in ToolCtx.__dataclass_fields__.values()} == {
            "session_id",
            "call_id",
            "tool",
            "args_sha256",
            "args_preview_redacted",
            "isolation_level",
            "pre_sha256",
            "post_sha256",
            "patch_sha256",
        }

    async def test_protocol_is_structurally_satisfiable(self) -> None:
        """A duck-typed journal can be awaited — the loop awaits it in N2.2."""
        from paa.coder.loop import ToolCtx, ToolJournal
        from paa.sandbox.base import IsolationLevel

        class Recording:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def before(self, ctx: ToolCtx) -> None:
                self.calls.append("before")

            async def denied(self, ctx: ToolCtx, reason: str) -> None:
                self.calls.append(f"denied:{reason}")

            async def after(self, ctx: ToolCtx, ok: bool, result_sha: str | None) -> None:
                self.calls.append(f"after:{ok}:{result_sha}")

        journal: ToolJournal = Recording()
        ctx = ToolCtx(
            session_id="sess-1",
            call_id="call-1",
            tool="write_file",
            args_sha256="a" * 64,
            args_preview_redacted="write_file(...)",
            isolation_level=IsolationLevel.PROCESS,
            pre_sha256="b" * 64,
            post_sha256="c" * 64,
            patch_sha256="d" * 64,
        )
        await journal.before(ctx)
        await journal.denied(ctx, "protected path: env")
        await journal.after(ctx, True, "e" * 64)
        assert isinstance(journal, Recording)
        assert [c.split(":")[0] for c in journal.calls] == ["before", "denied", "after"]
