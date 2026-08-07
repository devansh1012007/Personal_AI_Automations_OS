"""Tests for the RFC §5.1 / §15 metric equations.

Structure per metric: hand-computed known values, boundary conventions,
input-domain rejection, then the monotonicity and range properties that the
gatherer's routing logic actually relies on.

The two corrected equations get dedicated tests asserting the *fixed* behaviour
explicitly, so that a well-meaning future edit "restoring the RFC formula"
fails loudly rather than silently reintroducing a division by zero or an
undecayed semantic term.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from paa.context.metrics import (
    attention_allocation_score,
    context_density,
    context_entropy,
    context_pollution_ratio,
    context_utility_score,
    hybrid_retrieval_score,
    memory_importance_index,
    narrative_coherence_score,
    planning_cost,
    provider_reliability,
    skill_rank,
    token_efficiency,
)


@dataclass(frozen=True)
class Fact:
    """Minimal structural stand-in for `metrics.ScoredFact`."""

    relevance: float
    confidence: float


unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
positive = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
non_negative = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# §15.9 context_pollution_ratio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unreferenced", "total", "expected"),
    [
        (0, 0, 0.0),
        (0, 100, 0.0),
        (25, 100, 0.25),
        (50, 100, 0.5),
        (100, 100, 1.0),
        (1, 3, 1 / 3),
    ],
)
def test_pollution_ratio_known_values(unreferenced: int, total: int, expected: float) -> None:
    assert context_pollution_ratio(unreferenced, total) == pytest.approx(expected)


def test_pollution_ratio_empty_packet_is_clean_not_an_error() -> None:
    """Zero tokens carry zero pollution — the vacuous case must not raise."""
    assert context_pollution_ratio(0, 0) == 0.0


@pytest.mark.parametrize(
    ("unreferenced", "total"),
    [
        (-1, 10),
        (10, -1),
        (11, 10),  # more unreferenced than exist
        (1.5, 10),  # float where a token count is required
        (True, 10),  # bool masquerading as a count
    ],
)
def test_pollution_ratio_rejects_out_of_domain(unreferenced: object, total: object) -> None:
    with pytest.raises(ValueError):
        context_pollution_ratio(unreferenced, total)  # type: ignore[arg-type]


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=10_000))
def test_pollution_ratio_always_in_unit_interval(unreferenced: int, total: int) -> None:
    assume(unreferenced <= total)
    assert 0.0 <= context_pollution_ratio(unreferenced, total) <= 1.0


# ---------------------------------------------------------------------------
# §15.1 context_utility_score
# ---------------------------------------------------------------------------


def test_utility_score_known_value() -> None:
    facts = [Fact(1.0, 1.0), Fact(0.5, 0.5)]  # 1.0 + 0.25
    assert context_utility_score(facts, 0.0) == pytest.approx(1.25)
    assert context_utility_score(facts, 0.5) == pytest.approx(0.625)


def test_utility_score_empty_is_zero() -> None:
    assert context_utility_score([], 0.0) == 0.0


def test_utility_score_total_pollution_annihilates_signal() -> None:
    assert context_utility_score([Fact(1.0, 1.0)] * 5, 1.0) == pytest.approx(0.0)


def test_utility_score_is_a_total_not_a_mean() -> None:
    """Two corroborating facts are worth more than one; the score is not normalised."""
    one = context_utility_score([Fact(1.0, 1.0)], 0.0)
    two = context_utility_score([Fact(1.0, 1.0), Fact(1.0, 1.0)], 0.0)
    assert two > one


@pytest.mark.parametrize(
    ("facts", "pollution"),
    [
        ([Fact(1.5, 1.0)], 0.0),
        ([Fact(1.0, -0.1)], 0.0),
        ([Fact(1.0, 1.0)], 1.5),
        ([Fact(1.0, 1.0)], -0.1),
        ([Fact(float("nan"), 1.0)], 0.0),
    ],
)
def test_utility_score_rejects_out_of_domain(facts: list[Fact], pollution: float) -> None:
    with pytest.raises(ValueError):
        context_utility_score(facts, pollution)


@given(st.lists(st.tuples(unit, unit), max_size=20), unit, unit)
def test_utility_score_decreases_monotonically_with_pollution(
    pairs: list[tuple[float, float]], p_low: float, p_high: float
) -> None:
    """More pollution must never raise utility. The routing logic depends on it."""
    assume(p_low <= p_high)
    facts = [Fact(r, c) for r, c in pairs]
    assert context_utility_score(facts, p_low) >= context_utility_score(facts, p_high)


@given(st.lists(st.tuples(unit, unit), max_size=20), unit)
def test_utility_score_bounded_by_fact_count(
    pairs: list[tuple[float, float]], pollution: float
) -> None:
    facts = [Fact(r, c) for r, c in pairs]
    assert 0.0 <= context_utility_score(facts, pollution) <= len(facts) + 1e-9


# ---------------------------------------------------------------------------
# §5.1(3) context_density
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filled", "required", "expected"),
    [
        (0, 4, 0.0),
        (1, 4, 0.25),
        (3, 4, 0.75),
        (4, 4, 1.0),
        (17, 20, 0.85),
    ],
)
def test_density_known_values(filled: int, required: int, expected: float) -> None:
    assert context_density(filled, required) == pytest.approx(expected)


def test_density_with_no_required_slots_is_vacuously_satisfied() -> None:
    """A task declaring no needs is fully satisfied, not maximally starved.

    Returning 0.0 here would route every requirement-free task to
    HARD_STOP_ESCALATE_TO_USER. See the docstring on `context_density`.
    """
    assert context_density(0, 0) == 1.0


@pytest.mark.parametrize(("filled", "required"), [(-1, 4), (4, -1), (5, 4), (1.0, 4), (True, 4)])
def test_density_rejects_out_of_domain(filled: object, required: object) -> None:
    with pytest.raises(ValueError):
        context_density(filled, required)  # type: ignore[arg-type]


@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=0, max_value=1000))
def test_density_always_in_unit_interval(filled: int, required: int) -> None:
    assume(filled <= required)
    assert 0.0 <= context_density(filled, required) <= 1.0


# ---------------------------------------------------------------------------
# §15.4 context_entropy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probabilities", "expected"),
    [
        ([], 0.0),
        ([1.0], 0.0),
        ([0.5, 0.5], 1.0),
        ([0.25, 0.25, 0.25, 0.25], 2.0),
        ([0.5, 0.25, 0.25], 1.5),
        ([0.125] * 8, 3.0),
    ],
)
def test_entropy_known_values(probabilities: list[float], expected: float) -> None:
    assert context_entropy(probabilities) == pytest.approx(expected)


def test_entropy_skips_zero_probabilities() -> None:
    """0 * log 0 is defined as 0, so zeros must not raise and must not contribute."""
    assert context_entropy([1.0, 0.0, 0.0]) == pytest.approx(0.0)
    assert context_entropy([0.5, 0.5, 0.0]) == pytest.approx(1.0)
    assert context_entropy([0.0, 0.0]) == 0.0


def test_entropy_normalises_an_unnormalised_distribution() -> None:
    """Raw weights are normalised, so scaling the whole vector changes nothing."""
    assert context_entropy([0.2, 0.2]) == pytest.approx(1.0)
    assert context_entropy([0.1, 0.1, 0.1, 0.1]) == pytest.approx(2.0)
    assert context_entropy([0.3, 0.1]) == pytest.approx(context_entropy([0.75, 0.25]))


@pytest.mark.parametrize("probabilities", [[-0.1], [1.1], [0.5, float("nan")], [float("inf")]])
def test_entropy_rejects_out_of_domain(probabilities: list[float]) -> None:
    with pytest.raises(ValueError):
        context_entropy(probabilities)


@given(st.lists(unit, max_size=30))
def test_entropy_bounded_by_log2_of_support(probabilities: list[float]) -> None:
    """0 <= H <= log2(n) for n strictly-positive outcomes. The defining bound."""
    entropy = context_entropy(probabilities)
    support = sum(1 for p in probabilities if p > 0.0)
    assert entropy >= 0.0
    ceiling = math.log2(support) if support > 0 else 0.0
    assert entropy <= ceiling + 1e-9


@given(st.lists(st.floats(min_value=1e-6, max_value=1.0), min_size=1, max_size=15))
def test_entropy_is_scale_invariant(weights: list[float]) -> None:
    scaled = [w * 0.5 for w in weights]
    assert context_entropy(weights) == pytest.approx(context_entropy(scaled), abs=1e-9)


# ---------------------------------------------------------------------------
# §5.1(5) token_efficiency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("density", "tokens", "expected"),
    [
        (1.0, 1000, 1.0),
        (0.5, 500, 1.0),
        (0.8, 400, 2.0),
        (1.0, 1, 1000.0),
        (0.0, 500, 0.0),
    ],
)
def test_token_efficiency_known_values(density: float, tokens: int, expected: float) -> None:
    assert token_efficiency(density, tokens) == pytest.approx(expected)


def test_token_efficiency_zero_tokens_is_zero_not_infinite() -> None:
    assert token_efficiency(0.9, 0) == 0.0


def test_token_efficiency_prefers_the_cheaper_packet() -> None:
    """Same density, fewer tokens => strictly better. This is the metric's point."""
    assert token_efficiency(0.9, 400) > token_efficiency(0.9, 1400)


@pytest.mark.parametrize(("density", "tokens"), [(1.5, 100), (-0.1, 100), (0.5, -1), (0.5, 1.0)])
def test_token_efficiency_rejects_out_of_domain(density: object, tokens: object) -> None:
    with pytest.raises(ValueError):
        token_efficiency(density, tokens)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §15.2 attention_allocation_score  (SPEC DEVIATION docs/adr/0012)
# ---------------------------------------------------------------------------


def test_attention_score_survives_zero_pollution_and_zero_risk() -> None:
    """The whole point of ADR-0012.

    The RFC's published divisor `effort * pollution * risk` raises
    ZeroDivisionError on its own best-case input. The corrected form must not.
    """
    assert attention_allocation_score(1.0, 1.0, 1.0, 0.0, 0.0) == pytest.approx(1.0)
    assert attention_allocation_score(0.5, 0.4, 2.0, 0.0, 0.0) == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("criticality", "density", "effort", "pollution", "risk", "expected"),
    [
        (1.0, 1.0, 1.0, 1.0, 1.0, 0.25),  # 1 / (1 * 2 * 2)
        (1.0, 1.0, 2.0, 1.0, 1.0, 0.125),
        (0.8, 0.5, 1.0, 0.5, 0.25, 0.4 / 1.875),
        (0.0, 1.0, 1.0, 0.0, 0.0, 0.0),
    ],
)
def test_attention_score_known_values(
    criticality: float,
    density: float,
    effort: float,
    pollution: float,
    risk: float,
    expected: float,
) -> None:
    score = attention_allocation_score(criticality, density, effort, pollution, risk)
    assert score == pytest.approx(expected)


@given(unit, unit, positive, unit, unit)
def test_attention_score_is_always_finite_and_bounded(
    criticality: float, density: float, effort: float, pollution: float, risk: float
) -> None:
    """Bounded above by K*I/F for every input in the domain — never explodes."""
    score = attention_allocation_score(criticality, density, effort, pollution, risk)
    assert math.isfinite(score)
    assert 0.0 <= score <= (criticality * density) / effort + 1e-9


@given(unit, unit, positive, unit, unit, unit)
def test_attention_score_decreases_with_pollution(
    criticality: float,
    density: float,
    effort: float,
    p_low: float,
    p_high: float,
    risk: float,
) -> None:
    """Monotonicity is the property ADR-0012 promised to preserve."""
    assume(p_low <= p_high)
    low = attention_allocation_score(criticality, density, effort, p_low, risk)
    high = attention_allocation_score(criticality, density, effort, p_high, risk)
    assert low >= high


@given(unit, unit, positive, unit, unit, unit)
def test_attention_score_decreases_with_hallucination_risk(
    criticality: float,
    density: float,
    effort: float,
    pollution: float,
    r_low: float,
    r_high: float,
) -> None:
    assume(r_low <= r_high)
    low = attention_allocation_score(criticality, density, effort, pollution, r_low)
    high = attention_allocation_score(criticality, density, effort, pollution, r_high)
    assert low >= high


@pytest.mark.parametrize(
    ("criticality", "density", "effort", "pollution", "risk"),
    [
        (1.5, 1.0, 1.0, 0.0, 0.0),
        (1.0, -0.1, 1.0, 0.0, 0.0),
        (1.0, 1.0, 0.0, 0.0, 0.0),  # zero effort is not rankable
        (1.0, 1.0, -1.0, 0.0, 0.0),
        (1.0, 1.0, 1.0, 1.1, 0.0),
        (1.0, 1.0, 1.0, 0.0, 1.1),
    ],
)
def test_attention_score_rejects_out_of_domain(
    criticality: float, density: float, effort: float, pollution: float, risk: float
) -> None:
    with pytest.raises(ValueError):
        attention_allocation_score(criticality, density, effort, pollution, risk)


# ---------------------------------------------------------------------------
# §15.8 hybrid_retrieval_score  (SPEC DEVIATION docs/adr/0013)
# ---------------------------------------------------------------------------


def test_hybrid_score_decays_the_semantic_term_too() -> None:
    """The whole point of ADR-0013.

    As published, `(Sim*w_sem) + (Match*w_graph)*e^-lt` leaves the semantic term
    undecayed, so a stale embedding scores like a fresh one. The corrected form
    decays the fused score.
    """
    fixed = hybrid_retrieval_score(1.0, 1.0, 0.6, 0.4, 0.1, 10.0)
    assert fixed == pytest.approx(math.exp(-1.0))

    rfc_as_written = 1.0 * 0.6 + (1.0 * 0.4) * math.exp(-1.0)
    assert fixed < rfc_as_written  # the undecayed semantic term inflated the old score


def test_hybrid_score_semantic_only_match_still_ages() -> None:
    """A pure vector hit with no graph support must lose value over time."""
    fresh = hybrid_retrieval_score(1.0, 0.0, 1.0, 0.0, 0.002, 0.0)
    stale = hybrid_retrieval_score(1.0, 0.0, 1.0, 0.0, 0.002, 1000.0)
    assert stale < fresh


@pytest.mark.parametrize(("decay", "age"), [(0.0, 100.0), (0.5, 0.0), (0.0, 0.0)])
def test_hybrid_score_matches_rfc_form_when_decay_is_inert(decay: float, age: float) -> None:
    """With lambda=0 or t=0 the two formulations coincide exactly."""
    fixed = hybrid_retrieval_score(0.9, 0.4, 0.6, 0.4, decay, age)
    rfc_as_written = 0.9 * 0.6 + (0.4 * 0.4) * math.exp(-decay * age)
    assert fixed == pytest.approx(rfc_as_written)


def test_hybrid_score_known_values() -> None:
    assert hybrid_retrieval_score(1.0, 1.0, 0.6, 0.4, 0.0, 0.0) == pytest.approx(1.0)
    assert hybrid_retrieval_score(0.5, 0.5, 1.0, 1.0, 0.0, 0.0) == pytest.approx(1.0)
    assert hybrid_retrieval_score(0.0, 0.0, 1.0, 1.0, 0.0, 0.0) == 0.0


@pytest.mark.parametrize(
    ("sim", "match", "w_sem", "w_graph", "decay", "age"),
    [
        (1.1, 0.5, 1.0, 1.0, 0.0, 0.0),
        (-0.1, 0.5, 1.0, 1.0, 0.0, 0.0),
        (0.5, 1.1, 1.0, 1.0, 0.0, 0.0),
        (0.5, 0.5, -1.0, 1.0, 0.0, 0.0),
        (0.5, 0.5, 1.0, -1.0, 0.0, 0.0),
        (0.5, 0.5, 1.0, 1.0, -0.1, 0.0),
        (0.5, 0.5, 1.0, 1.0, 0.1, -1.0),
    ],
)
def test_hybrid_score_rejects_out_of_domain(
    sim: float, match: float, w_sem: float, w_graph: float, decay: float, age: float
) -> None:
    with pytest.raises(ValueError):
        hybrid_retrieval_score(sim, match, w_sem, w_graph, decay, age)


@given(unit, unit, non_negative, non_negative, non_negative, non_negative)
def test_hybrid_score_bounded_by_total_weight(
    sim: float, match: float, w_sem: float, w_graph: float, decay: float, age: float
) -> None:
    score = hybrid_retrieval_score(sim, match, w_sem, w_graph, decay, age)
    assert 0.0 <= score <= w_sem + w_graph + 1e-9


@given(unit, unit, non_negative, non_negative, non_negative, non_negative, non_negative)
def test_hybrid_score_decreases_with_age(
    sim: float,
    match: float,
    w_sem: float,
    w_graph: float,
    decay: float,
    age_young: float,
    age_old: float,
) -> None:
    assume(age_young <= age_old)
    young = hybrid_retrieval_score(sim, match, w_sem, w_graph, decay, age_young)
    old = hybrid_retrieval_score(sim, match, w_sem, w_graph, decay, age_old)
    assert young >= old - 1e-12


# ---------------------------------------------------------------------------
# §15.12 memory_importance_index
# ---------------------------------------------------------------------------


def test_memory_importance_known_values() -> None:
    assert memory_importance_index(0, 0.01, 0.5, 0.0, 0.0) == pytest.approx(0.5)
    assert memory_importance_index(10, 0.01, 0.5, 0.0, 0.0) == pytest.approx(0.6)
    # One half-life of idleness at lambda = ln 2.
    assert memory_importance_index(0, 0.01, 1.0, math.log(2), 1.0) == pytest.approx(0.5)


def test_memory_importance_is_not_clamped_at_one() -> None:
    """Heavy reuse must be able to outrank a fresh invariant; clamping would tie them."""
    assert memory_importance_index(100, 0.01, 0.5, 0.0, 0.0) == pytest.approx(1.5)


def test_memory_importance_reinforcement_applies_before_decay() -> None:
    """Idle time discounts what a record earned, rather than being cancelled by it."""
    heavily_used_but_idle = memory_importance_index(100, 0.01, 0.5, 1.0, 10.0)
    assert heavily_used_but_idle < 0.001


@pytest.mark.parametrize(
    ("uses", "delta", "initial", "decay", "idle"),
    [
        (-1, 0.01, 0.5, 0.0, 0.0),
        (1.5, 0.01, 0.5, 0.0, 0.0),
        (1, -0.01, 0.5, 0.0, 0.0),
        (1, 0.01, 1.5, 0.0, 0.0),
        (1, 0.01, 0.5, -0.1, 0.0),
        (1, 0.01, 0.5, 0.1, -1.0),
    ],
)
def test_memory_importance_rejects_out_of_domain(
    uses: object, delta: float, initial: float, decay: float, idle: float
) -> None:
    with pytest.raises(ValueError):
        memory_importance_index(uses, delta, initial, decay, idle)  # type: ignore[arg-type]


@given(
    st.integers(min_value=0, max_value=10_000),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    unit,
    st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False),
)
def test_memory_importance_bounded_by_reinforced_base(
    uses: int, delta: float, initial: float, decay: float, idle: float
) -> None:
    index = memory_importance_index(uses, delta, initial, decay, idle)
    assert 0.0 <= index <= initial + delta * uses + 1e-9


# ---------------------------------------------------------------------------
# §15.13 narrative_coherence_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("similarities", "contradictions", "facts", "expected"),
    [
        ([1.0, 1.0], 0, 10, 1.0),
        ([0.8, 0.6], 0, 10, 0.7),
        ([1.0], 2, 10, 0.8),
        ([0.5], 5, 10, 0.25),
        ([1.0], 10, 10, 0.0),
        ([1.0], 0, 0, 1.0),  # no facts => the contradiction term is inert
    ],
)
def test_narrative_coherence_known_values(
    similarities: list[float], contradictions: int, facts: int, expected: float
) -> None:
    score = narrative_coherence_score(similarities, contradictions, facts)
    assert score == pytest.approx(expected)


def test_narrative_coherence_with_no_themes_is_zero() -> None:
    """Unlike density, an absent narrative is incoherent rather than vacuously perfect."""
    assert narrative_coherence_score([], 0, 0) == 0.0
    assert narrative_coherence_score([], 0, 100) == 0.0


@pytest.mark.parametrize(
    ("similarities", "contradictions", "facts"),
    [
        ([1.1], 0, 10),
        ([-0.1], 0, 10),
        ([1.0], -1, 10),
        ([1.0], 11, 10),  # more contradictions than facts
        ([1.0], 0, -1),
    ],
)
def test_narrative_coherence_rejects_out_of_domain(
    similarities: list[float], contradictions: int, facts: int
) -> None:
    with pytest.raises(ValueError):
        narrative_coherence_score(similarities, contradictions, facts)


@given(st.lists(unit, max_size=20), st.integers(min_value=0, max_value=500))
def test_narrative_coherence_always_in_unit_interval(
    similarities: list[float], facts: int
) -> None:
    contradictions = facts // 2
    assert 0.0 <= narrative_coherence_score(similarities, contradictions, facts) <= 1.0


# ---------------------------------------------------------------------------
# §15.6 planning_cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nodes", "cost", "depth", "gamma", "expected"),
    [
        (10, 0.5, 0, 0.9, 5.0),
        (10, 0.5, 2, 0.5, 1.25),
        (0, 1.0, 3, 0.5, 0.0),
        (4, 2.0, 1, 1.0, 8.0),
        (7, 1.0, 0, 0.0, 7.0),  # 0**0 == 1: a root expansion is charged in full
    ],
)
def test_planning_cost_known_values(
    nodes: int, cost: float, depth: int, gamma: float, expected: float
) -> None:
    assert planning_cost(nodes, cost, depth, gamma) == pytest.approx(expected)


def test_planning_cost_discount_makes_depth_cheaper() -> None:
    assert planning_cost(10, 1.0, 3, 0.5) < planning_cost(10, 1.0, 1, 0.5)


@pytest.mark.parametrize(
    ("nodes", "cost", "depth", "gamma"),
    [(-1, 1.0, 0, 0.5), (1, -1.0, 0, 0.5), (1, 1.0, -1, 0.5), (1, 1.0, 0, -0.5), (1.5, 1.0, 0, 0.5)],
)
def test_planning_cost_rejects_out_of_domain(
    nodes: object, cost: float, depth: object, gamma: float
) -> None:
    with pytest.raises(ValueError):
        planning_cost(nodes, cost, depth, gamma)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §15.10 provider_reliability
# ---------------------------------------------------------------------------


def test_provider_reliability_cold_start_is_a_neutral_prior() -> None:
    """0.0 would deadlock a new provider: never picked, so never proven."""
    assert provider_reliability([], 0.9) == 0.5


@pytest.mark.parametrize(
    ("runs", "beta", "expected"),
    [
        ([1.0, 1.0, 1.0], 1.0, 1.0),
        ([0.0, 0.0], 1.0, 0.0),
        ([0.0, 1.0], 1.0, 0.5),  # beta = 1 degenerates to the unweighted mean
        ([0.0, 1.0], 0.5, 1.0 / 1.5),  # weights [0.5, 1.0]
        ([1.0, 0.0], 0.5, 0.5 / 1.5),
        ([1.0], 0.5, 1.0),
    ],
)
def test_provider_reliability_known_values(
    runs: list[float], beta: float, expected: float
) -> None:
    assert provider_reliability(runs, beta) == pytest.approx(expected)


def test_provider_reliability_weights_recent_runs_more_heavily() -> None:
    """Same runs, opposite order: the newest outcome must dominate."""
    recovering = provider_reliability([0.0, 0.0, 1.0], 0.4)
    degrading = provider_reliability([1.0, 0.0, 0.0], 0.4)
    assert recovering > degrading


def test_provider_reliability_accepts_boolean_outcomes() -> None:
    assert provider_reliability([True, True, True], 1.0) == pytest.approx(1.0)
    assert provider_reliability([True, False], 1.0) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("runs", "beta"),
    [([1.0], 0.0), ([1.0], -0.5), ([1.0], 1.5), ([1.5], 0.9), ([-0.1], 0.9)],
)
def test_provider_reliability_rejects_out_of_domain(runs: list[float], beta: float) -> None:
    with pytest.raises(ValueError):
        provider_reliability(runs, beta)


@given(st.lists(unit, max_size=25), st.floats(min_value=0.01, max_value=1.0, allow_nan=False))
def test_provider_reliability_always_in_unit_interval(runs: list[float], beta: float) -> None:
    assert 0.0 <= provider_reliability(runs, beta) <= 1.0


# ---------------------------------------------------------------------------
# §15.11 skill_rank
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reliability", "risk", "w_rel", "w_risk", "expected"),
    [
        (1.0, 0.0, 1.0, 1.0, 1.0),
        (0.0, 1.0, 1.0, 1.0, 0.0),
        (0.8, 0.2, 1.0, 1.0, 0.8),
        (0.8, 0.9, 1.0, 0.0, 0.8),  # risk ignored when its weight is zero
        (0.1, 0.2, 0.0, 1.0, 0.8),  # reliability ignored likewise
        (1.0, 1.0, 1.0, 1.0, 0.5),
    ],
)
def test_skill_rank_known_values(
    reliability: float, risk: float, w_rel: float, w_risk: float, expected: float
) -> None:
    assert skill_rank(reliability, risk, w_rel, w_risk) == pytest.approx(expected)


def test_skill_rank_rewards_reliability_and_penalises_risk() -> None:
    assert skill_rank(0.9, 0.1, 1.0, 1.0) > skill_rank(0.4, 0.1, 1.0, 1.0)
    assert skill_rank(0.9, 0.1, 1.0, 1.0) > skill_rank(0.9, 0.8, 1.0, 1.0)


def test_skill_rank_requires_at_least_one_positive_weight() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        skill_rank(0.9, 0.1, 0.0, 0.0)


@pytest.mark.parametrize(
    ("reliability", "risk", "w_rel", "w_risk"),
    [(1.5, 0.1, 1.0, 1.0), (0.9, 1.5, 1.0, 1.0), (0.9, 0.1, -1.0, 1.0), (0.9, 0.1, 1.0, -1.0)],
)
def test_skill_rank_rejects_out_of_domain(
    reliability: float, risk: float, w_rel: float, w_risk: float
) -> None:
    with pytest.raises(ValueError):
        skill_rank(reliability, risk, w_rel, w_risk)


@given(unit, unit, non_negative, non_negative)
def test_skill_rank_always_in_unit_interval(
    reliability: float, risk: float, w_rel: float, w_risk: float
) -> None:
    """Bounded for every weight pair, so a configured threshold stays meaningful."""
    assume(w_rel + w_risk > 0.0)
    assert 0.0 <= skill_rank(reliability, risk, w_rel, w_risk) <= 1.0
