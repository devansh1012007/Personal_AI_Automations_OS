"""The chat context bundle: determinism, budget, and the quarantine filter.

C4.3's contract, pinned:

* **Golden determinism** — same inputs in, byte-identical packet JSON out;
  input mapping key order is irrelevant, and the provenance order is stable.
* **Token-budget compliance** — ``allocated_tokens`` never exceeds the
  ceiling, for any ceiling; a tight ceiling evicts turn lines before facts,
  a tiny ceiling assembles an empty packet and leaves the persona slot
  honestly vacant.
* **Quarantine exclusion** — a fact carrying a truthy ``quarantined`` marker
  is excluded *before* ranking, so no ceiling or floor setting can smuggle
  one into a packet.
* **Recall-served facts reach the packet** — the gatherer's planner floors do
  not re-filter what chat recall already decided to serve: a consult row that
  says "served" above a packet that dropped the fact would make the ledger
  lie about the turn.
* **Shaping** — statement composition, missing ids refused, unit-float
  clamping, position-indexed turn ids.
"""

from __future__ import annotations

import math
from typing import Any

from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.chat.context_bundle import PERSONA_SLOT, build_context_bundle
from paa.config import ContextSettings

PERSONA = "You are PAA, a local-first assistant with persistent memory."

FACT_A = {
    "id": "f-apollo",
    "statement": "Apollo is blocked until Friday",
    "confidence": 0.6,  # sentence-extracted: below the planner's 0.70 floor
    "importance": 0.3,
    "memory_domain": "semantic",
    "session_id": "session-a",
}
FACT_B = {
    "id": "f-vault",
    "statement": "The vault syncs at midnight",
    "confidence": 0.9,
    "importance": 0.5,
    "memory_domain": "semantic",
}
TURNS = (
    {"role": "user", "text": "Apollo is blocked until Friday."},
    {
        "role": "assistant",
        "text": "Apollo is blocked until Friday.\n(from memory: Apollo is blocked until Friday)",
    },
)


def _bundle(**overrides: Any):
    kwargs: dict[str, Any] = {
        "persona": PERSONA,
        "recalled_facts": [FACT_A, FACT_B],
        "recent_turns": list(TURNS),
        "token_ceiling": 1500,
    }
    kwargs.update(overrides)
    return build_context_bundle(**kwargs)


class TestGoldenDeterminism:
    def test_same_inputs_byte_identical_packet(self) -> None:
        first = _bundle()
        second = _bundle()
        assert first.rendered_json() == second.rendered_json()
        assert first.provenance == second.provenance
        assert first.turns == second.turns

    def test_input_mapping_key_order_is_irrelevant(self) -> None:
        shuffled = [dict(reversed(list(fact.items()))) for fact in (FACT_A, FACT_B)]
        plain = _bundle(recalled_facts=[FACT_A, FACT_B])
        reordered = _bundle(recalled_facts=shuffled)
        assert plain.rendered_json() == reordered.rendered_json()

    def test_provenance_is_in_packet_order(self) -> None:
        bundle = _bundle()
        packet_fact_ids = [
            e.id.removeprefix("fact:") for e in bundle.packet.elements
        ]
        packet_fact_ids = [i for i in packet_fact_ids if i.startswith("f-")]
        assert [p.fact_id for p in bundle.provenance] == packet_fact_ids

    def test_origin_session_survives_into_provenance(self) -> None:
        bundle = _bundle()
        origins = {p.fact_id: p.origin_session for p in bundle.provenance}
        assert origins["f-apollo"] == "session-a"
        assert origins["f-vault"] is None


class TestTokenBudget:
    def test_generous_ceiling_carries_every_candidate(self) -> None:
        bundle = _bundle(token_ceiling=1500)
        assert bundle.packet.allocated_tokens <= 1500
        # persona + 2 facts + 2 turns, nothing evicted
        assert len(bundle.packet.elements) == 5
        assert len(bundle.provenance) == 2
        assert len(bundle.turns) == 2

    def test_allocated_tokens_never_exceeds_the_ceiling(self) -> None:
        for ceiling in (1, 5, 17, 24, 30, 40, 64, 128, 1500):
            bundle = _bundle(token_ceiling=ceiling)
            assert bundle.packet.allocated_tokens <= ceiling, ceiling
            assert bundle.token_ceiling == ceiling

    def test_tight_ceiling_evicts_turns_but_keeps_the_slot(self) -> None:
        bundle = _bundle(token_ceiling=30)
        assert bundle.packet.allocated_tokens <= 30
        assert len(bundle.turns) < len(TURNS)  # something was evicted
        assert bundle.packet.vacant_slots == ()  # the persona still resolved

    def test_tiny_ceiling_assembles_an_empty_packet(self) -> None:
        bundle = _bundle(token_ceiling=1)
        assert bundle.packet.elements == ()
        assert bundle.packet.allocated_tokens == 0
        assert bundle.provenance == ()
        assert bundle.turns == ()
        # ...and says so honestly: the persona slot is vacant.
        assert bundle.packet.vacant_slots == (PERSONA_SLOT,)

    def test_packed_turns_keep_conversation_order(self) -> None:
        bundle = _bundle(token_ceiling=1500)
        assert bundle.turns == (
            f"user: {TURNS[0]['text']}",
            f"assistant: {TURNS[1]['text']}",
        )

    def test_ceiling_resolves_from_the_given_context_settings(self) -> None:
        settings = ContextSettings(token_ceiling=256)
        bundle = _bundle(token_ceiling=None, settings=settings)
        assert bundle.token_ceiling == 256


class TestQuarantineExclusion:
    def test_quarantined_fact_is_excluded_before_ranking(self) -> None:
        quarantined = {
            **FACT_B,
            "id": "f-quarantined",
            "confidence": 1.0,
            "importance": 1.0,
            "quarantined": True,
        }
        bundle = _bundle(recalled_facts=[FACT_A, quarantined])
        # The best-scoring candidate in the pool never enters the packet.
        assert all(p.fact_id != "f-quarantined" for p in bundle.provenance)
        assert all(
            e.id != "fact:f-quarantined" for e in bundle.packet.elements
        )
        assert [p.fact_id for p in bundle.provenance] == ["f-apollo"]

    def test_all_quarantined_leaves_persona_and_turns(self) -> None:
        facts = [{**FACT_A, "quarantined": True}, {**FACT_B, "quarantined": 1}]
        bundle = _bundle(recalled_facts=facts)
        assert bundle.provenance == ()
        assert bundle.turns  # turns are not facts; they still pack
        assert [e.id for e in bundle.packet.elements if e.slot == PERSONA_SLOT] == [
            "chat:persona"
        ]


class TestRecallServedFactsReachThePacket:
    def test_below_floor_facts_still_pack(self) -> None:
        """The planner floor would drop FACT_A (confidence 0.60 < 0.70). Chat
        recall already decided to serve it — the packet must not re-refuse it
        behind a consult row that says ``returned=1``."""
        bundle = _bundle(recalled_facts=[FACT_A], recent_turns=[])
        assert [p.fact_id for p in bundle.provenance] == ["f-apollo"]
        assert any(e.id == "fact:f-apollo" for e in bundle.packet.elements)

    def test_the_consult_row_and_the_packet_agree(self) -> None:
        """Shaped like the loop: what the facade returned is what the packet
        carries, so ``MEMORY_RECALL_CONSULTED.returned`` stays truthful."""
        served = [FACT_A, FACT_B]
        bundle = _bundle(recalled_facts=served, recent_turns=[])
        served_ids = {str(f["id"]) for f in served}
        packed_ids = {
            e.id.removeprefix("fact:")
            for e in bundle.packet.elements
            if e.id.startswith("fact:")
        }
        assert packed_ids == served_ids


class TestShaping:
    def test_missing_statement_composes_from_the_triple(self) -> None:
        fact = {
            "id": "f-triple",
            "entity_name": "Apollo",
            "predicate": "is",
            "object_value": "blocked until Friday",
            "confidence": 0.6,
            "importance": 0.3,
        }
        bundle = _bundle(recalled_facts=[fact], recent_turns=[])
        assert bundle.provenance[0].statement == "Apollo is blocked until Friday"

    def test_fact_without_a_usable_id_is_refused(self) -> None:
        no_id = {**FACT_A, "id": ""}
        bundle = _bundle(recalled_facts=[no_id, FACT_B])
        assert [p.fact_id for p in bundle.provenance] == ["f-vault"]

    def test_turn_without_text_is_refused(self) -> None:
        bundle = _bundle(recent_turns=[{"role": "user"}, TURNS[0]])
        assert bundle.turns == (f"user: {TURNS[0]['text']}",)

    def test_empty_persona_is_stated_not_hidden(self) -> None:
        bundle = _bundle(persona="")
        assert bundle.persona == ""
        assert all(e.slot != PERSONA_SLOT for e in bundle.packet.elements)

    def test_out_of_range_confidence_is_clamped(self) -> None:
        bundle = _bundle(
            recalled_facts=[{**FACT_A, "confidence": 1.7, "importance": -3}],
            recent_turns=[],
        )
        element = next(
            e for e in bundle.packet.elements if e.id == "fact:f-apollo"
        )
        assert element.confidence == 1.0
        assert element.importance == 0.0

    @given(
        confidence=st.one_of(
            st.floats(allow_nan=True, allow_infinity=True), st.none()
        ),
        importance=st.one_of(
            st.floats(allow_nan=True, allow_infinity=True), st.none()
        ),
    )
    @hyp_settings(max_examples=100, deadline=None)
    def test_any_float_clamps_into_the_unit_interval(
        self, confidence: Any, importance: Any
    ) -> None:
        """A score can arrive from any substrate; the bundle stays total."""
        fact = {
            "id": "f-anything",
            "statement": "x",
            "confidence": confidence,
            "importance": importance,
        }
        bundle = _bundle(recalled_facts=[fact], recent_turns=[])
        element = next(
            e for e in bundle.packet.elements if e.id == "fact:f-anything"
        )
        assert 0.0 <= element.confidence <= 1.0
        assert 0.0 <= element.importance <= 1.0
        assert not math.isnan(element.confidence)  # no NaN
        assert not math.isnan(element.importance)
