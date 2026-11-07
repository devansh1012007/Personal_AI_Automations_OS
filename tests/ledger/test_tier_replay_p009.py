"""T10b-core: replay rebuilds tier state via the ADR-0037 tier-dict fold.

PLAN:integration-fix:P009. `TaskProjection` is unchanged (task-lineage state);
tier state lives in the `apply_tier_event` dict, so interleaved TIER_* events
must not perturb task replay at all — and the rebuilt tier dict must be stable
under prefix/suffix splitting (snapshot soundness).
"""

from __future__ import annotations

import uuid

from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent, compute_idempotency_key
from paa.ledger.replay import TaskPhase, apply_tier_event, replay


def _event(etype: EventType, cid: uuid.UUID, version: int, **payload) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=version, payload=payload
    )


def _task_stream(cid: uuid.UUID) -> list[LedgerEvent]:
    return [
        _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
        _event(EventType.TASK_QUEUED, cid, 2),
        _event(EventType.EXECUTION_STARTED, cid, 3, step_index=0),
        _event(EventType.EXECUTION_COMPLETED, cid, 4, step_index=0, tokens_consumed=10),
        _event(EventType.MUTATION_COMMITTED, cid, 5, applied_patch_sha256="a" * 64),
    ]


def _fold(events: list[LedgerEvent]) -> dict[str, str]:
    state: dict[str, str] = {}
    for event in events:
        state = apply_tier_event(state, event)
    return state


class TestTaskReplayUnchanged:
    def test_interleaved_tier_events_do_not_perturb_task_replay(self) -> None:
        cid = new_correlation_id()
        tasks = _task_stream(cid)
        tiered = [
            _event(EventType.TIER_DEMOTED, cid, 2, fact_id="f-1", to_tier="T1"),
            _event(EventType.TIER_ARCHIVED, cid, 4, fact_id="f-2", to_tier="T4"),
        ]
        combined = tasks[:2] + tiered[:1] + tasks[2:] + tiered[1:]
        full = replay(tasks)
        mixed = replay(combined)
        assert mixed.phase is full.phase is TaskPhase.COMMITTED
        assert mixed.unknown_events == []
        assert mixed.completed_steps == full.completed_steps
        assert mixed.tokens_consumed == full.tokens_consumed

    def test_tier_events_alone_keep_initial_phase(self) -> None:
        cid = new_correlation_id()
        events = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _event(EventType.TIER_ARCHIVED, cid, 2, fact_id="f-1", to_tier="T4"),
        ]
        state = replay(events)
        assert state.phase is TaskPhase.CREATED
        assert state.unknown_events == []


class TestTierRebuild:
    def test_rebuild_equals_fold_of_full_stream(self) -> None:
        cid = new_correlation_id()
        events = [
            *_task_stream(cid),
            _event(EventType.TIER_DEMOTED, cid, 6, fact_id="f-1", to_tier="T1"),
            _event(EventType.TIER_ARCHIVED, cid, 7, fact_id="f-1", to_tier="T4"),
            _event(EventType.TIER_DEMOTED, cid, 8, fact_id="f-2", to_tier="T1"),
        ]
        assert _fold(events) == {"f-1": "T4", "f-2": "T1"}

    def test_prefix_suffix_fold_is_associative(self) -> None:
        """Snapshot soundness: prefix fold + suffix fold == full fold."""
        cid = new_correlation_id()
        events = [
            _event(EventType.TIER_DEMOTED, cid, 1, fact_id="f-1", to_tier="T1"),
            _event(EventType.TIER_DEMOTED, cid, 2, fact_id="f-1", to_tier="T2"),
            _event(EventType.TIER_ARCHIVED, cid, 3, fact_id="f-1", to_tier="T4"),
        ]
        prefix = _fold(events[:2])
        suffix: dict[str, str] = prefix
        for event in events[2:]:
            suffix = apply_tier_event(suffix, event)
        assert suffix == _fold(events) == {"f-1": "T4"}


class TestDiscriminatorContract:
    """W3 PLACEHOLDER (R-W2-105): pins compute_idempotency_key's shape, not a
    sweeper construction site — the W3 sweeper does not exist yet, so there
    is no production `LedgerEvent(discriminator=fact_id + to_tier)` site to
    test against. The real sweeper-side assertion lands with the sweeper.

    The ledger dedupe derives its key from (correlation_id, event_type,
    attempt, discriminator) — see ledger/events.py. The sweeper must construct
    `LedgerEvent(discriminator=fact_id + to_tier)`; this test pins that the
    key built here equals the ADR-0008 key for the same discriminator, so a
    double emit with the same (fact_id, to_tier) is one logical event.
    """

    def test_discriminator_key_matches_adr0008(self) -> None:
        cid = new_correlation_id()
        event = _event(EventType.TIER_ARCHIVED, cid, 1, fact_id="f-1", to_tier="T4")
        event = event.model_copy(update={"discriminator": "f-1" + "T4"})
        assert event.idempotency_key == compute_idempotency_key(
            cid, EventType.TIER_ARCHIVED, attempt=0, discriminator="f-1" + "T4"
        )

    def test_distinct_to_tier_is_distinct_key(self) -> None:
        """A later move of the same fact to a new tier is a NEW logical event."""
        cid = new_correlation_id()
        a = _event(EventType.TIER_DEMOTED, cid, 1, fact_id="f-1", to_tier="T1")
        b = _event(EventType.TIER_DEMOTED, cid, 2, fact_id="f-1", to_tier="T2")
        ka = a.model_copy(update={"discriminator": "f-1" + "T1"}).idempotency_key
        kb = b.model_copy(update={"discriminator": "f-1" + "T2"}).idempotency_key
        assert ka != kb
