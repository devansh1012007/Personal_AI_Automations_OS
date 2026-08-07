"""Tests for bounded context assembly.

The single most important test in this file is
`test_gatherer_never_exceeds_its_ceiling`: the DoD requires the 1500-token
ceiling to be unbreakable for *any* input, so it is asserted as a hypothesis
property rather than over a handful of examples.

The four RFC §5.2 defects each get a test that fails against the original
pseudocode:

* deterministic ordering  -> `test_output_is_identical_across_runs`
* `break` vs `continue`   -> `test_a_small_fact_after_an_oversized_one_is_still_packed`
* ceiling-checked appends -> `test_invariants_are_prioritised_but_not_exempt_from_the_ceiling`
* linear deduplication    -> `test_duplicate_ids_are_collapsed_hot_wins`
"""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paa.config import ContextSettings
from paa.context.budget import CharEstimator
from paa.context.gatherer import (
    NARRATIVE_MEMORY_DOMAINS,
    BoundedContextGatherer,
    ContextElement,
    ContextPacket,
)
from paa.core.errors import ContextInsufficientError

SLOTS = ["s0", "s1", "s2", "s3"]
DOMAINS = ["semantic", "tool", "operational", "temporal", "reflection", "narrative"]


@pytest.fixture
def settings() -> ContextSettings:
    """Explicit settings so tests never depend on the developer's environment."""
    return ContextSettings()


@pytest.fixture
def gatherer(settings: ContextSettings) -> BoundedContextGatherer:
    return BoundedContextGatherer(settings, estimator=CharEstimator(4.0))


def element(
    element_id: str,
    *,
    tokens: int = 10,
    slot: str | None = None,
    relevance: float = 1.0,
    confidence: float = 1.0,
    importance: float = 0.0,
    domain: str = "semantic",
    content: str = "",
) -> ContextElement:
    """Terse builder. `token_cost` is set explicitly so packing maths is exact."""
    return ContextElement(
        id=element_id,
        content=content or f"content-{element_id}",
        token_cost=tokens,
        slot=slot,
        relevance=relevance,
        confidence=confidence,
        importance=importance,
        memory_domain=domain,
    )


def _ids(packet: ContextPacket) -> list[str]:
    return [item.id for item in packet.elements]


# ---------------------------------------------------------------------------
# Routing bands (RFC §5.2)
# ---------------------------------------------------------------------------


def test_full_density_routes_to_the_planner(gatherer: BoundedContextGatherer) -> None:
    facts = [element(f"f{i}", slot=f"s{i}") for i in range(3)]
    packet = gatherer.compile(facts, [], ["s0", "s1", "s2"])
    assert packet.density == pytest.approx(1.0)
    assert packet.routing_directive == "PROCEED_TO_PLANNER"
    assert packet.is_sufficient is True
    assert packet.vacant_slots == ()


def test_density_exactly_at_the_proceed_threshold_proceeds(
    gatherer: BoundedContextGatherer,
) -> None:
    """`density_proceed` reads as "at or above", so the boundary is permissive."""
    required = [f"slot{i}" for i in range(20)]
    facts = [element(f"f{i}", slot=f"slot{i}") for i in range(17)]  # 17/20 == 0.85
    packet = gatherer.compile(facts, [], required)
    assert packet.density == pytest.approx(0.85)
    assert packet.routing_directive == "PROCEED_TO_PLANNER"


def test_density_just_below_the_proceed_threshold_hydrates(
    gatherer: BoundedContextGatherer,
) -> None:
    required = [f"slot{i}" for i in range(20)]
    facts = [element(f"f{i}", slot=f"slot{i}") for i in range(16)]  # 16/20 == 0.80
    packet = gatherer.compile(facts, [], required)
    assert packet.density == pytest.approx(0.80)
    assert packet.routing_directive == "TRIGGER_BACKGROUND_HYDRATION"


def test_density_exactly_at_the_hydrate_threshold_hydrates(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element("a", slot="s0"), element("b", slot="s1")]  # 2/5 == 0.40
    packet = gatherer.compile(facts, [], ["s0", "s1", "s2", "s3", "s4"])
    assert packet.density == pytest.approx(0.40)
    assert packet.routing_directive == "TRIGGER_BACKGROUND_HYDRATION"


def test_low_density_hard_stops(gatherer: BoundedContextGatherer) -> None:
    facts = [element("a", slot="s0")]  # 1/5 == 0.20
    packet = gatherer.compile(facts, [], ["s0", "s1", "s2", "s3", "s4"])
    assert packet.density == pytest.approx(0.20)
    assert packet.routing_directive == "HARD_STOP_ESCALATE_TO_USER"
    assert packet.is_sufficient is False


def test_no_required_slots_is_vacuously_sufficient(gatherer: BoundedContextGatherer) -> None:
    """A task with no declared needs must not be escalated to a human."""
    packet = gatherer.compile([element("a")], [], [])
    assert packet.density == 1.0
    assert packet.routing_directive == "PROCEED_TO_PLANNER"


def test_empty_input_with_required_slots_hard_stops(gatherer: BoundedContextGatherer) -> None:
    packet = gatherer.compile([], [], ["s0", "s1"])
    assert packet.elements == ()
    assert packet.allocated_tokens == 0
    assert packet.density == 0.0
    assert packet.pollution_ratio == 0.0
    assert packet.entropy == 0.0
    assert packet.utility_score == 0.0
    assert packet.routing_directive == "HARD_STOP_ESCALATE_TO_USER"


def test_raise_if_insufficient_only_fires_on_a_hard_stop(
    gatherer: BoundedContextGatherer,
) -> None:
    """Compilation itself never raises; the escalation is opt-in."""
    stopped = gatherer.compile([], [], ["s0", "s1"])
    with pytest.raises(ContextInsufficientError) as excinfo:
        stopped.raise_if_insufficient()
    assert excinfo.value.vacant_slots == ["s0", "s1"]
    assert excinfo.value.density == 0.0

    fine = gatherer.compile([element("a", slot="s0")], [], ["s0"])
    fine.raise_if_insufficient()  # must not raise


# ---------------------------------------------------------------------------
# Ceiling enforcement — the DoD bound
# ---------------------------------------------------------------------------


def test_packet_respects_the_default_1500_token_ceiling(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element(f"f{i}", tokens=400, slot=f"s{i}") for i in range(20)]
    packet = gatherer.compile(facts, [], [f"s{i}" for i in range(20)])
    assert packet.allocated_tokens <= 1500
    assert packet.allocated_tokens == 1200  # three 400-token facts fit
    assert len(packet.elements) == 3


def test_token_ceiling_override_is_honoured(gatherer: BoundedContextGatherer) -> None:
    facts = [element(f"f{i}", tokens=100) for i in range(20)]
    packet = gatherer.compile(facts, [], [], token_ceiling=250)
    assert packet.allocated_tokens <= 250
    assert packet.token_ceiling == 250
    assert len(packet.elements) == 2


def test_zero_ceiling_yields_an_empty_packet(gatherer: BoundedContextGatherer) -> None:
    facts = [element(f"f{i}", tokens=10, slot=f"s{i}") for i in range(3)]
    packet = gatherer.compile(facts, [], ["s0", "s1", "s2"], token_ceiling=0)
    assert packet.elements == ()
    assert packet.allocated_tokens == 0
    assert packet.routing_directive == "HARD_STOP_ESCALATE_TO_USER"


def test_a_single_oversized_fact_is_dropped_not_truncated(
    gatherer: BoundedContextGatherer,
) -> None:
    packet = gatherer.compile([element("huge", tokens=9999, slot="s0")], [], ["s0"])
    assert packet.elements == ()
    assert packet.allocated_tokens == 0


@pytest.mark.parametrize("bad", [-1, 1.5, "1500", True])
def test_token_ceiling_rejects_non_counts(
    gatherer: BoundedContextGatherer, bad: object
) -> None:
    with pytest.raises(ValueError, match="token_ceiling must be"):
        gatherer.compile([], [], [], token_ceiling=bad)  # type: ignore[arg-type]


def test_content_is_measured_when_no_token_cost_is_given(
    gatherer: BoundedContextGatherer,
) -> None:
    """`token_cost=None` falls back to the estimator over `content`."""
    packet = gatherer.compile(
        [ContextElement(id="a", content="x" * 40, slot="s0")], [], ["s0"]
    )
    assert packet.allocated_tokens == 10  # 40 chars / 4.0


# ---------------------------------------------------------------------------
# Prioritisation
# ---------------------------------------------------------------------------


def test_slot_resolvers_outrank_higher_scoring_colour(
    gatherer: BoundedContextGatherer,
) -> None:
    """Only slot-resolving facts move density, so they win regardless of score."""
    resolver = element("resolver", tokens=10, slot="s0", relevance=0.8, confidence=0.8)
    colour = element("colour", tokens=10, relevance=1.0, confidence=1.0)
    packet = gatherer.compile([resolver, colour], [], ["s0"], token_ceiling=10)
    assert _ids(packet) == ["resolver"]


def test_invariants_outrank_ordinary_supporting_facts(
    gatherer: BoundedContextGatherer, settings: ContextSettings
) -> None:
    """Facts at or above `invariant_importance` survive pruning (RFC §5.2)."""
    invariant = element("invariant", tokens=10, importance=settings.invariant_importance)
    ordinary = element("ordinary", tokens=10, relevance=1.0, confidence=1.0, importance=0.1)
    packet = gatherer.compile([invariant, ordinary], [], [], token_ceiling=10)
    assert _ids(packet) == ["invariant"]


def test_full_priority_ordering_is_slots_then_invariants_then_colour(
    gatherer: BoundedContextGatherer,
) -> None:
    candidates = [
        element("colour", tokens=10, relevance=1.0, confidence=1.0),
        element("invariant", tokens=10, importance=0.9),
        # Above both quality floors, but the lowest score of the three.
        element("resolver", tokens=10, slot="s0", relevance=0.8, confidence=0.75),
    ]
    packet = gatherer.compile(candidates, [], ["s0"])
    assert _ids(packet) == ["resolver", "invariant", "colour"]


def test_invariants_are_prioritised_but_not_exempt_from_the_ceiling(
    gatherer: BoundedContextGatherer,
) -> None:
    """SPEC FIX: the RFC sketch appends invariants without checking the ceiling.

    Priority is the right reward for importance; an exemption from the bound is
    not, because enough invariants would silently blow past 1500 tokens.
    """
    invariants = [element(f"inv{i}", tokens=200, importance=0.95) for i in range(20)]
    packet = gatherer.compile(invariants, [], [])
    assert packet.allocated_tokens <= 1500
    assert packet.allocated_tokens == 1400
    assert len(packet.elements) == 7  # not 20


def test_ties_are_broken_on_id_not_on_input_order(gatherer: BoundedContextGatherer) -> None:
    """Identical scores must still yield a total, reproducible order."""
    identical = [element(name, tokens=10, slot="s0") for name in ("zulu", "alpha", "mike")]
    packet = gatherer.compile(identical, [], ["s0"])
    assert _ids(packet) == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# Packing efficiency (SPEC FIX: break -> continue)
# ---------------------------------------------------------------------------


def test_a_small_fact_after_an_oversized_one_is_still_packed(
    gatherer: BoundedContextGatherer,
) -> None:
    """SPEC FIX: the RFC sketch `break`s on the first fact that does not fit.

    Because the pool is priority-sorted, that oversized fact is often a
    high-priority one, so a single fat candidate would truncate the packet
    exactly when context matters most.
    """
    candidates = [
        element("first", tokens=50, slot="s0", relevance=0.9, confidence=1.0),
        element("oversized", tokens=200, slot="s1", relevance=0.8, confidence=1.0),
        element("small", tokens=40, slot="s2", relevance=0.7, confidence=1.0),
    ]
    packet = gatherer.compile(candidates, [], ["s0", "s1", "s2"], token_ceiling=100)

    assert _ids(packet) == ["first", "small"]  # with `break` this would be ["first"]
    assert packet.allocated_tokens == 90
    assert packet.density == pytest.approx(2 / 3)
    assert packet.vacant_slots == ("s1",)


def test_packing_continues_past_several_oversized_candidates(
    gatherer: BoundedContextGatherer,
) -> None:
    candidates = [
        element("big1", tokens=900, relevance=1.00, confidence=1.0),
        element("big2", tokens=900, relevance=0.99, confidence=1.0),
        element("big3", tokens=900, relevance=0.98, confidence=1.0),
        element("tiny", tokens=5, relevance=0.97, confidence=1.0),
    ]
    packet = gatherer.compile(candidates, [], [], token_ceiling=1000)
    assert _ids(packet) == ["big1", "tiny"]
    assert packet.allocated_tokens == 905


# ---------------------------------------------------------------------------
# Candidate pool: floors and deduplication
# ---------------------------------------------------------------------------


def test_hot_facts_below_the_confidence_floor_are_discarded(
    gatherer: BoundedContextGatherer, settings: ContextSettings
) -> None:
    keep = element("keep", slot="s0", confidence=settings.confidence_floor)
    drop = element("drop", slot="s1", confidence=settings.confidence_floor - 0.01)
    packet = gatherer.compile([keep, drop], [], ["s0", "s1"])
    assert _ids(packet) == ["keep"]
    assert packet.vacant_slots == ("s1",)


def test_semantic_matches_below_the_relevance_floor_are_discarded(
    gatherer: BoundedContextGatherer, settings: ContextSettings
) -> None:
    keep = element("keep", slot="s0", relevance=settings.relevance_floor)
    drop = element("drop", slot="s1", relevance=settings.relevance_floor - 0.01)
    packet = gatherer.compile([], [keep, drop], ["s0", "s1"])
    assert _ids(packet) == ["keep"]


def test_floors_apply_to_slot_resolvers_too(gatherer: BoundedContextGatherer) -> None:
    """A barely-believed fact filling a slot is worse than an honest vacancy."""
    packet = gatherer.compile([element("weak", slot="s0", confidence=0.1)], [], ["s0"])
    assert packet.elements == ()
    assert packet.vacant_slots == ("s0",)
    assert packet.routing_directive == "HARD_STOP_ESCALATE_TO_USER"


def test_duplicate_ids_are_collapsed_hot_wins(gatherer: BoundedContextGatherer) -> None:
    """Both substrates can surface one record; the relational row is authoritative."""
    hot = element("shared", slot="s0", content="from-hot", confidence=0.9)
    semantic = element("shared", slot="s0", content="from-vector", relevance=0.99)
    packet = gatherer.compile([hot], [semantic], ["s0"])
    assert len(packet.elements) == 1
    assert packet.elements[0].content == "from-hot"


def test_duplicates_within_one_source_are_collapsed(gatherer: BoundedContextGatherer) -> None:
    repeated = [element("same", tokens=10, slot="s0") for _ in range(5)]
    packet = gatherer.compile(repeated, [], ["s0"])
    assert len(packet.elements) == 1
    assert packet.allocated_tokens == 10


def test_a_large_candidate_pool_is_handled_without_quadratic_blowup(
    gatherer: BoundedContextGatherer,
) -> None:
    """SPEC FIX: the sketch rescans the pool per match. 4000 candidates must be fine."""
    hot = [element(f"h{i}", tokens=1) for i in range(2000)]
    semantic = [element(f"s{i}", tokens=1, relevance=0.9) for i in range(2000)]
    packet = gatherer.compile(hot, semantic, [])
    assert packet.allocated_tokens == 1500
    assert len(packet.elements) == 1500


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_output_is_identical_across_runs(gatherer: BoundedContextGatherer) -> None:
    """SPEC FIX: without an id tiebreak, replay would diverge from the ledger."""
    hot = [element(f"h{i}", tokens=30, slot=f"s{i % 3}") for i in range(10)]
    semantic = [element(f"v{i}", tokens=30, relevance=0.9) for i in range(10)]
    slots = ["s0", "s1", "s2"]

    first = gatherer.compile(hot, semantic, slots)
    second = gatherer.compile(hot, semantic, slots)
    assert first == second


def test_output_is_invariant_to_input_ordering(gatherer: BoundedContextGatherer) -> None:
    """Vector indexes do not promise a stable result order; the packet must anyway."""
    hot = [element(f"h{i}", tokens=30, slot=f"s{i % 3}", relevance=0.9) for i in range(12)]
    slots = ["s0", "s1", "s2"]

    baseline = gatherer.compile(hot, [], slots)
    shuffled = list(hot)
    random.Random(1234).shuffle(shuffled)
    assert gatherer.compile(shuffled, [], slots) == baseline
    assert gatherer.compile(list(reversed(hot)), [], slots) == baseline


def test_two_gatherer_instances_agree(settings: ContextSettings) -> None:
    """The gatherer is stateless; instances are interchangeable."""
    facts = [element(f"f{i}", tokens=40, slot=f"s{i % 4}") for i in range(9)]
    one = BoundedContextGatherer(settings, estimator=CharEstimator(4.0))
    two = BoundedContextGatherer(settings, estimator=CharEstimator(4.0))
    assert one.compile(facts, [], SLOTS) == two.compile(facts, [], SLOTS)


def test_repeated_compiles_do_not_leak_budget_state(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element(f"f{i}", tokens=500) for i in range(5)]
    for _ in range(4):
        packet = gatherer.compile(facts, [], [])
        assert packet.allocated_tokens == 1500


# ---------------------------------------------------------------------------
# Derived metrics on the packet
# ---------------------------------------------------------------------------


def test_pollution_counts_tokens_that_resolve_no_required_slot(
    gatherer: BoundedContextGatherer,
) -> None:
    resolver = element("resolver", tokens=100, slot="s0")
    colour = element("colour", tokens=100, importance=0.9)
    packet = gatherer.compile([resolver, colour], [], ["s0"])
    assert packet.allocated_tokens == 200
    assert packet.pollution_ratio == pytest.approx(0.5)


def test_a_packet_of_pure_slot_resolvers_is_unpolluted(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element(f"f{i}", tokens=50, slot=f"s{i}") for i in range(3)]
    packet = gatherer.compile(facts, [], ["s0", "s1", "s2"])
    assert packet.pollution_ratio == 0.0
    assert packet.utility_score == pytest.approx(3.0)


def test_a_fact_whose_slot_was_not_required_counts_as_pollution(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element("wanted", tokens=100, slot="s0"), element("stray", tokens=100, slot="sX")]
    packet = gatherer.compile(facts, [], ["s0"])
    assert packet.pollution_ratio == pytest.approx(0.5)
    assert packet.vacant_slots == ()


def test_entropy_reflects_how_evenly_token_mass_is_spread(
    gatherer: BoundedContextGatherer,
) -> None:
    even = gatherer.compile([element(f"f{i}", tokens=100) for i in range(4)], [], [])
    assert even.entropy == pytest.approx(2.0)  # log2(4), a uniform packet

    focused = gatherer.compile(
        [element("dominant", tokens=1000), element("scrap", tokens=1)], [], []
    )
    assert focused.entropy < 0.1

    single = gatherer.compile([element("only", tokens=100)], [], [])
    assert single.entropy == pytest.approx(0.0)


def test_efficiency_and_filled_slots_read_back_off_the_packet(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [element(f"f{i}", tokens=50, slot=f"s{i}") for i in range(2)]
    packet = gatherer.compile(facts, [], ["s0", "s1"])
    assert packet.filled_slots == ("s0", "s1")
    assert packet.efficiency() == pytest.approx(10.0)  # 1.0 / 100 * 1000


def test_vacant_slots_preserve_declaration_order_and_collapse_duplicates(
    gatherer: BoundedContextGatherer,
) -> None:
    packet = gatherer.compile([element("a", slot="mid")], [], ["zed", "mid", "zed", "abc"])
    assert packet.vacant_slots == ("zed", "abc")
    assert packet.density == pytest.approx(1 / 3)


def test_packet_is_immutable(gatherer: BoundedContextGatherer) -> None:
    """A packet is an audit record; mutating it would falsify the ledger."""
    packet = gatherer.compile([element("a", slot="s0")], [], ["s0"])
    with pytest.raises(ValueError, match="frozen"):
        packet.density = 0.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="frozen"):
        packet.elements[0].content = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Worker packets (RFC §2.2 planner/worker separation)
# ---------------------------------------------------------------------------


def test_worker_packet_uses_the_tighter_ceiling(gatherer: BoundedContextGatherer) -> None:
    facts = [element(f"f{i}", tokens=100) for i in range(30)]
    packet = gatherer.compile_worker_packet(facts, [], [])
    assert packet.token_ceiling == 1000
    assert packet.allocated_tokens == 1000

    planner = gatherer.compile(facts, [], [])
    assert planner.allocated_tokens == 1500


@pytest.mark.parametrize("domain", sorted(NARRATIVE_MEMORY_DOMAINS))
def test_worker_packet_drops_every_narrative_domain(
    gatherer: BoundedContextGatherer, domain: str
) -> None:
    """A worker executes a step; narrative invites it to re-litigate the plan."""
    facts = [element("prose", tokens=10, domain=domain), element("fact", tokens=10)]
    packet = gatherer.compile_worker_packet(facts, [], [])
    assert _ids(packet) == ["fact"]


def test_worker_packet_keeps_file_and_primitive_domains(
    gatherer: BoundedContextGatherer,
) -> None:
    facts = [
        element("f1", tokens=10, domain="semantic"),
        element("f2", tokens=10, domain="tool"),
        element("f3", tokens=10, domain="operational"),
        element("f4", tokens=10, domain="file"),
    ]
    packet = gatherer.compile_worker_packet(facts, [], [])
    assert sorted(_ids(packet)) == ["f1", "f2", "f3", "f4"]


def test_worker_packet_drops_narrative_even_when_it_would_fill_a_slot(
    gatherer: BoundedContextGatherer,
) -> None:
    """The resulting vacancy is the correct signal, not a reason to smuggle prose in."""
    facts = [element("recall", tokens=10, slot="s0", domain="conversation")]
    packet = gatherer.compile_worker_packet(facts, [], ["s0"])
    assert packet.elements == ()
    assert packet.vacant_slots == ("s0",)
    assert packet.routing_directive == "HARD_STOP_ESCALATE_TO_USER"


def test_worker_narrative_filter_applies_to_semantic_matches_too(
    gatherer: BoundedContextGatherer,
) -> None:
    matches = [element("prose", tokens=10, domain="episodic", relevance=0.99)]
    assert gatherer.compile_worker_packet([], matches, []).elements == ()


def test_planner_packet_keeps_narrative(gatherer: BoundedContextGatherer) -> None:
    """Narrative is the planner's input; only the worker is shielded from it."""
    facts = [element("prose", tokens=10, domain="reflection")]
    assert _ids(gatherer.compile(facts, [], [])) == ["prose"]


def test_worker_packet_honours_a_ceiling_override(gatherer: BoundedContextGatherer) -> None:
    facts = [element(f"f{i}", tokens=100) for i in range(10)]
    packet = gatherer.compile_worker_packet(facts, [], [], token_ceiling=300)
    assert packet.allocated_tokens == 300


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_elements = st.builds(
    ContextElement,
    id=st.text(min_size=1, max_size=6),
    content=st.text(max_size=150),
    relevance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    importance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    slot=st.none() | st.sampled_from(SLOTS),
    memory_domain=st.sampled_from(DOMAINS),
    token_cost=st.none() | st.integers(min_value=0, max_value=4000),
)

_element_lists = st.lists(_elements, max_size=25)
_unique_element_lists = st.lists(_elements, max_size=25, unique_by=lambda item: item.id)


@given(
    hot=_element_lists,
    semantic=_element_lists,
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
    ceiling=st.integers(min_value=0, max_value=2000),
)
def test_gatherer_never_exceeds_its_ceiling(
    hot: list[ContextElement],
    semantic: list[ContextElement],
    required: list[str],
    ceiling: int,
) -> None:
    """THE invariant. The DoD requires this bound to be unbreakable, for any input.

    Both the reported total and an independent recomputation from the selected
    elements are checked, so a bookkeeping error cannot hide a real overrun.
    """
    estimator = CharEstimator(4.0)
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=estimator)
    packet = gatherer.compile(hot, semantic, required, token_ceiling=ceiling)

    assert packet.allocated_tokens <= ceiling
    assert sum(item.tokens(estimator) for item in packet.elements) <= ceiling
    assert packet.allocated_tokens == sum(item.tokens(estimator) for item in packet.elements)


@given(
    hot=_element_lists,
    semantic=_element_lists,
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
)
def test_default_planner_ceiling_of_1500_is_unbreakable(
    hot: list[ContextElement],
    semantic: list[ContextElement],
    required: list[str],
) -> None:
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
    assert gatherer.compile(hot, semantic, required).allocated_tokens <= 1500


@given(
    hot=_element_lists,
    semantic=_element_lists,
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
)
def test_worker_ceiling_of_1000_is_unbreakable(
    hot: list[ContextElement],
    semantic: list[ContextElement],
    required: list[str],
) -> None:
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
    packet = gatherer.compile_worker_packet(hot, semantic, required)
    assert packet.allocated_tokens <= 1000
    assert all(item.memory_domain not in NARRATIVE_MEMORY_DOMAINS for item in packet.elements)


@given(
    hot=_element_lists,
    semantic=_element_lists,
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
)
def test_packet_metrics_stay_in_their_documented_ranges(
    hot: list[ContextElement],
    semantic: list[ContextElement],
    required: list[str],
) -> None:
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
    packet = gatherer.compile(hot, semantic, required)

    assert 0.0 <= packet.density <= 1.0
    assert 0.0 <= packet.pollution_ratio <= 1.0
    assert packet.entropy >= 0.0
    assert packet.utility_score >= 0.0
    assert len(packet.vacant_slots) == len(set(packet.vacant_slots))
    assert set(packet.vacant_slots) <= set(required)
    assert len({item.id for item in packet.elements}) == len(packet.elements)


@given(
    elements=_unique_element_lists,
    split=st.integers(min_value=0, max_value=25),
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
def test_compilation_is_deterministic_under_reordering(
    elements: list[ContextElement],
    split: int,
    required: list[str],
    seed: int,
) -> None:
    """Same facts, any arrival order => byte-identical packet. Replay depends on it."""
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
    hot, semantic = elements[:split], elements[split:]

    baseline = gatherer.compile(hot, semantic, required)

    rng = random.Random(seed)
    shuffled_hot, shuffled_semantic = list(hot), list(semantic)
    rng.shuffle(shuffled_hot)
    rng.shuffle(shuffled_semantic)

    assert gatherer.compile(shuffled_hot, shuffled_semantic, required) == baseline
    assert gatherer.compile(hot, semantic, required) == baseline


@given(
    hot=_element_lists,
    required=st.lists(st.sampled_from(SLOTS), max_size=6),
    ceiling=st.integers(min_value=0, max_value=1500),
)
def test_a_larger_ceiling_never_yields_a_smaller_packet(
    hot: list[ContextElement], required: list[str], ceiling: int
) -> None:
    """Greedy priority packing must be monotone in the budget it is given."""
    gatherer = BoundedContextGatherer(ContextSettings(), estimator=CharEstimator(4.0))
    small = gatherer.compile(hot, [], required, token_ceiling=ceiling)
    large = gatherer.compile(hot, [], required, token_ceiling=ceiling + 500)
    assert large.allocated_tokens >= small.allocated_tokens
