"""P-009 tier vocabulary, policy, and fold helper (PLAN:integration-fix:P009).

Pure-layer tests for the T-009a-P009a task: EventType members, `_NO_PHASE_CHANGE`
totality, the ADR-0037 tier-dict fold, and TierPolicy TTLs. No DB, no sweeper —
the sweeper is W3 and the DDL is W2-IMPL-03.
"""

from __future__ import annotations

import math
import uuid

import pytest

from paa.core.types import TERMINAL_EVENTS, EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskProjection, apply_tier_event, replay
from paa.memory.domains import TierPolicy


def _event(etype: EventType, cid: uuid.UUID, version: int, **payload) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=cid, event_type=etype, state_version=version, payload=payload
    )


def _tier_event(
    etype: EventType, cid: uuid.UUID, version: int, fact_id: str, to_tier: str
) -> LedgerEvent:
    return _event(
        etype,
        cid,
        version,
        fact_id=fact_id,
        from_tier="T0",
        to_tier=to_tier,
        reason="ttl",
    )


class TestTierVocabulary:
    def test_tier_members_exist(self) -> None:
        assert EventType.TIER_DEMOTED is EventType("TIER_DEMOTED")
        assert EventType.TIER_ARCHIVED is EventType("TIER_ARCHIVED")

    def test_tier_members_are_not_terminal(self) -> None:
        assert EventType.TIER_DEMOTED not in TERMINAL_EVENTS
        assert EventType.TIER_ARCHIVED not in TERMINAL_EVENTS

    def test_replay_totality_for_tier_events(self) -> None:
        """TIER_* must not land in unknown_events and must not change task phase."""
        cid = new_correlation_id()
        events = [
            _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"}),
            _tier_event(EventType.TIER_DEMOTED, cid, 2, "f-1", "T2"),
            _tier_event(EventType.TIER_ARCHIVED, cid, 3, "f-1", "T4"),
        ]
        state = replay(events)
        assert state.unknown_events == []
        assert state.phase is not None  # task phase untouched by fact-scoped events
        assert isinstance(state, TaskProjection)


class TestTierDictFold:
    def test_fold_builds_tier_dict_from_sequence(self) -> None:
        cid = new_correlation_id()
        events = [
            _tier_event(EventType.TIER_DEMOTED, cid, 1, "f-1", "T1"),
            _tier_event(EventType.TIER_ARCHIVED, cid, 2, "f-2", "T4"),
            _tier_event(EventType.TIER_DEMOTED, cid, 3, "f-1", "T2"),
        ]
        state: dict[str, str] = {}
        for event in events:
            state = apply_tier_event(state, event)
        assert state == {"f-1": "T2", "f-2": "T4"}

    def test_fold_does_not_mutate_input(self) -> None:
        cid = new_correlation_id()
        original: dict[str, str] = {"f-1": "T0"}
        event = _tier_event(EventType.TIER_DEMOTED, cid, 1, "f-1", "T1")
        updated = apply_tier_event(original, event)
        assert original == {"f-1": "T0"}  # pure: input untouched
        assert updated == {"f-1": "T1"}
        assert updated is not original

    def test_events_lacking_tier_default_to_t2(self) -> None:
        """Old events without tier fields default the fact to T2 (migrations backfill T2)."""
        cid = new_correlation_id()
        events = [
            _event(EventType.TIER_DEMOTED, cid, 1, fact_id="old-1", reason="legacy"),
            _event(EventType.TASK_REQUESTED, cid, 2, fact_id="old-2"),
        ]
        state: dict[str, str] = {}
        for event in events:
            state = apply_tier_event(state, event)
        assert state["old-1"] == "T2"
        assert state["old-2"] == "T2"

    def test_task_events_without_fact_id_are_noop_copies(self) -> None:
        cid = new_correlation_id()
        event = _event(EventType.TASK_REQUESTED, cid, 1, request={"goal": "x"})
        state: dict[str, str] = {"f-1": "T1"}
        assert apply_tier_event(state, event) == {"f-1": "T1"}

    def test_non_tier_event_does_not_resurrect_an_archived_fact(self) -> None:
        """A non-tier event naming a fact must never reset its tier to T2."""
        cid = new_correlation_id()
        archive = _tier_event(EventType.TIER_ARCHIVED, cid, 1, "f-1", "T4")
        unrelated = _event(EventType.TASK_REQUESTED, cid, 2, fact_id="f-1")
        state = apply_tier_event(apply_tier_event({}, archive), unrelated)
        assert state == {"f-1": "T4"}

    def test_non_tier_event_cannot_set_a_tier(self) -> None:
        """Only TIER_* events write tiers, even if another payload carries to_tier."""
        cid = new_correlation_id()
        event = _event(EventType.TASK_REQUESTED, cid, 1, fact_id="f-1", to_tier="T0")
        # Unseen fact + non-tier event -> T2 default, not the payload's T0.
        assert apply_tier_event({}, event) == {"f-1": "T2"}

    def test_double_emit_is_idempotent(self) -> None:
        """Folding the same tier move twice leaves the same rebuilt tier."""
        cid = new_correlation_id()
        event = _tier_event(EventType.TIER_DEMOTED, cid, 1, "f-1", "T1")
        once = apply_tier_event({}, event)
        twice = apply_tier_event(once, event)
        assert once == twice == {"f-1": "T1"}


class TestTierHostileInput:
    """Fold helper must be total: never raise on hostile payloads."""

    def test_missing_fact_id_is_noop(self) -> None:
        cid = new_correlation_id()
        event = _event(EventType.TIER_DEMOTED, cid, 1, to_tier="T1", reason="no-key")
        assert apply_tier_event({}, event) == {}

    def test_non_string_fact_id_is_coerced_not_crash(self) -> None:
        cid = new_correlation_id()
        event = _event(EventType.TIER_DEMOTED, cid, 1, fact_id=12345, to_tier="T3")
        assert apply_tier_event({}, event) == {"12345": "T3"}

    def test_non_string_to_tier_defaults_to_t2(self) -> None:
        cid = new_correlation_id()
        event = _event(
            EventType.TIER_DEMOTED, cid, 1, fact_id="f-1", to_tier={"bad": 1}
        )
        assert apply_tier_event({}, event) == {"f-1": "T2"}

    @pytest.mark.parametrize("to_tier", ["T9", "not-a-tier", ""])
    def test_unknown_or_empty_to_tier_defaults_to_t2(self, to_tier: str) -> None:
        """R-W2-003: a valid-format but unknown tier is not recorded verbatim.

        The fold shares TierPolicy.resolve, so `"T9"`/`"not-a-tier"`/`""`
        resolve to T2 — the same answer the DB CHECK enforces on the live
        column. Recording them verbatim would make rebuilt != live.
        """
        cid = new_correlation_id()
        event = _tier_event(EventType.TIER_DEMOTED, cid, 1, "f-1", to_tier)
        assert apply_tier_event({}, event) == {"f-1": "T2"}

    @pytest.mark.parametrize("fact_id", ["", 0, False])
    def test_falsy_fact_id_is_a_noop(self, fact_id: object) -> None:
        """R-W2-103: falsy fact_ids are absent — no junk dict key, no raise."""
        cid = new_correlation_id()
        event = _event(
            EventType.TIER_DEMOTED, cid, 1, fact_id=fact_id, to_tier="T1", reason="x"
        )
        assert apply_tier_event({"keep": "T0"}, event) == {"keep": "T0"}

    @pytest.mark.parametrize("fact_id", [["x"], {"bad": 1}, True])
    def test_non_scalar_fact_id_is_a_noop(self, fact_id: object) -> None:
        """A non-scalar (or boolean) fact_id is malformed input: no-op, never raise."""
        cid = new_correlation_id()
        event = _event(
            EventType.TIER_DEMOTED, cid, 1, fact_id=fact_id, to_tier="T1", reason="x"
        )
        assert apply_tier_event({}, event) == {}


class TestTierPolicy:
    def test_ttl_hours_exact_mapping(self) -> None:
        policy = TierPolicy()
        assert policy.ttl_hours["T0"] == 4.0
        assert policy.ttl_hours["T1"] == 24.0
        assert policy.ttl_hours["T2"] == 2160.0
        assert policy.ttl_hours["T3"] == 17520.0
        assert math.isinf(policy.ttl_hours["T4"])

    def test_ttl_hours_uses_float_infinity(self) -> None:
        assert TierPolicy().ttl_hours["T4"] == float("inf")

    @pytest.mark.parametrize("tier", ["T0", "T1", "T2", "T3", "T4"])
    def test_resolve_known_tiers(self, tier: str) -> None:
        assert TierPolicy().resolve(tier) == tier

    @pytest.mark.parametrize("tier", [None, "", "T5", "t2", "garbage", "TX"])
    def test_unknown_or_absent_defaults_to_t2(self, tier: str | None) -> None:
        policy = TierPolicy()
        assert policy.resolve(tier) == "T2"
        assert policy.ttl_for(tier) == 2160.0

    def test_default_tier_is_not_settable_via_the_constructor(self) -> None:
        """R-W2-104: DEFAULT_TIER is a ClassVar, not a field — it cannot be moved."""
        with pytest.raises(TypeError):
            TierPolicy(**{"DEFAULT_TIER": "T0"})

    def test_ttl_hours_is_isolated_per_instance(self) -> None:
        """R-W2-104: mutating one instance's table must not leak into the next."""
        policy = TierPolicy()
        policy.ttl_hours["T2"] = 999.0
        assert TierPolicy().ttl_hours["T2"] == 2160.0

    def test_instance_is_frozen(self) -> None:
        """R-W2-104: TierPolicy is a frozen dataclass — attribute assignment raises."""
        from dataclasses import FrozenInstanceError

        policy = TierPolicy()
        with pytest.raises(FrozenInstanceError):
            policy.ttl_hours = {"T0": 1.0}
