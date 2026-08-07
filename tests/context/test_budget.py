"""Tests for token accounting and estimation.

The load-bearing property is conservation: a budget can never go negative, can
never exceed its allocation, and — because sub-budgets are linked — a whole
delegation tree can never collectively outspend its root.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paa.config import ContextSettings
from paa.context.budget import (
    CharEstimator,
    TiktokenEstimator,
    TokenBudget,
    TokenEstimator,
    default_estimator,
    estimate_total,
    tiktoken_available,
)
from paa.core.errors import BudgetExceededError
from paa.core.types import MODALITY_PROFILES, ComplexityModality

# ---------------------------------------------------------------------------
# CharEstimator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "chars_per_token", "expected"),
    [
        ("", 4.0, 0),
        ("a", 4.0, 1),
        ("abcd", 4.0, 1),
        ("abcde", 4.0, 2),  # rounds up
        ("a" * 100, 4.0, 25),
        ("a" * 101, 4.0, 26),
        ("a" * 10, 2.5, 4),
    ],
)
def test_char_estimator_rounds_up(text: str, chars_per_token: float, expected: int) -> None:
    """Over-estimating packs a smaller context; under-estimating breaches the ceiling."""
    assert CharEstimator(chars_per_token).estimate(text) == expected


def test_char_estimator_from_settings_uses_configured_ratio() -> None:
    estimator = CharEstimator.from_settings(ContextSettings(chars_per_token=2.0))
    assert estimator.chars_per_token == 2.0
    assert estimator.estimate("abcd") == 2


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_char_estimator_rejects_bad_ratio(bad: float) -> None:
    with pytest.raises(ValueError, match="chars_per_token"):
        CharEstimator(bad)


@given(st.text(max_size=500), st.floats(min_value=0.5, max_value=20.0, allow_nan=False))
def test_char_estimator_is_monotonic_over_prefixes(text: str, ratio: float) -> None:
    """Required by the compactor's binary search over prefix lengths."""
    estimator = CharEstimator(ratio)
    half = estimator.estimate(text[: len(text) // 2])
    full = estimator.estimate(text)
    assert 0 <= half <= full


def test_char_estimator_satisfies_the_protocol() -> None:
    assert isinstance(CharEstimator(4.0), TokenEstimator)


def test_char_estimator_repr_is_informative() -> None:
    assert "chars_per_token=4.0" in repr(CharEstimator(4.0))


# ---------------------------------------------------------------------------
# Optional tokenizer
# ---------------------------------------------------------------------------


def test_tiktoken_availability_matches_importability() -> None:
    """`tiktoken` is not a declared dependency; both outcomes must be handled."""
    try:
        import tiktoken  # noqa: F401
    except ImportError:
        assert tiktoken_available() is False
    else:
        assert tiktoken_available() is True


def test_tiktoken_estimator_raises_a_clear_error_when_absent() -> None:
    if tiktoken_available():
        pytest.skip("tiktoken is installed in this environment")
    with pytest.raises(ImportError, match="optional 'tiktoken' package"):
        TiktokenEstimator()


def test_default_estimator_always_returns_a_working_estimator() -> None:
    """Degrades to CharEstimator rather than failing when tiktoken is missing."""
    estimator = default_estimator(ContextSettings())
    assert isinstance(estimator, TokenEstimator)
    assert estimator.estimate("") == 0
    assert estimator.estimate("hello world") > 0


def test_estimate_total_sums_per_string_estimates() -> None:
    """Charged per string, so any pre-flight check must measure it the same way."""
    estimator = CharEstimator(4.0)
    assert estimate_total(["abcd", "abcd"], estimator) == 2
    assert estimate_total([], estimator) == 0
    # "a" and "a" cost 1 each, whereas the concatenation would cost 1 in total.
    assert estimate_total(["a", "a"], estimator) == 2


# ---------------------------------------------------------------------------
# TokenBudget basics
# ---------------------------------------------------------------------------


def test_budget_tracks_allocated_consumed_and_remaining() -> None:
    budget = TokenBudget(1500)
    assert (budget.allocated, budget.consumed, budget.remaining) == (1500, 0, 1500)

    assert budget.try_consume(500) is True
    assert (budget.allocated, budget.consumed, budget.remaining) == (1500, 500, 1000)

    assert budget.try_consume(1000) is True
    assert budget.remaining == 0
    assert budget.exhausted is True


def test_try_consume_refuses_rather_than_overspending() -> None:
    budget = TokenBudget(100)
    assert budget.try_consume(101) is False
    assert budget.consumed == 0
    assert budget.remaining == 100


def test_a_refused_consumption_leaves_no_partial_debit() -> None:
    budget = TokenBudget(100)
    budget.try_consume(60)
    assert budget.try_consume(50) is False
    assert budget.consumed == 60  # unchanged, not partially applied


def test_zero_token_consumption_always_succeeds() -> None:
    budget = TokenBudget(0)
    assert budget.try_consume(0) is True
    assert budget.consumed == 0


def test_can_afford_does_not_mutate() -> None:
    budget = TokenBudget(100)
    assert budget.can_afford(100) is True
    assert budget.can_afford(101) is False
    assert budget.consumed == 0


def test_consume_or_raise_reports_the_overrun_in_structured_detail() -> None:
    budget = TokenBudget(100, kind="context_tokens")
    budget.consume_or_raise(90)

    with pytest.raises(BudgetExceededError) as excinfo:
        budget.consume_or_raise(20)

    error = excinfo.value
    assert error.budget_kind == "context_tokens"
    assert error.limit == 100.0
    assert error.consumed == 90.0
    assert error.details["requested"] == 20
    assert error.details["remaining"] == 10
    # Payload must be ledger-writable without reconstruction.
    assert error.to_payload()["error_type"] == "BudgetExceededError"
    assert budget.consumed == 90  # the failed call debited nothing


def test_consume_text_charges_the_estimated_cost() -> None:
    budget = TokenBudget(10)
    assert budget.consume_text("a" * 20, CharEstimator(4.0)) is True
    assert budget.consumed == 5


@pytest.mark.parametrize("bad", [-1, 1.5, "10", True])
def test_budget_rejects_non_count_consumption(bad: object) -> None:
    budget = TokenBudget(100)
    with pytest.raises(ValueError, match="tokens must be"):
        budget.try_consume(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_budget_rejects_bad_allocation(bad: object) -> None:
    with pytest.raises(ValueError, match="allocated must be"):
        TokenBudget(bad)  # type: ignore[arg-type]


def test_budget_rejects_negative_depth() -> None:
    with pytest.raises(ValueError, match="depth must be non-negative"):
        TokenBudget(100, depth=-1)


def test_budget_repr_shows_the_live_numbers() -> None:
    budget = TokenBudget(100)
    budget.try_consume(30)
    assert "consumed=30" in repr(budget)
    assert "remaining=70" in repr(budget)


# ---------------------------------------------------------------------------
# Construction from settings and modalities
# ---------------------------------------------------------------------------


def test_from_settings_picks_the_planner_or_worker_ceiling() -> None:
    settings = ContextSettings()
    assert TokenBudget.from_settings(settings).allocated == settings.token_ceiling
    worker = TokenBudget.from_settings(settings, worker=True)
    assert worker.allocated == settings.worker_token_ceiling
    assert worker.allocated < settings.token_ceiling
    assert worker.kind == "worker_tokens"


@pytest.mark.parametrize("modality", list(ComplexityModality))
def test_for_modality_matches_the_profile_ceiling(modality: ComplexityModality) -> None:
    budget = TokenBudget.for_modality(modality)
    assert budget.allocated == MODALITY_PROFILES[modality].token_ceiling


def test_simple_modality_yields_a_zero_budget() -> None:
    """SIMPLE bypasses the LLM entirely (RFC §9.2); spending must be refused."""
    budget = TokenBudget.for_modality(ComplexityModality.SIMPLE)
    assert budget.allocated == 0
    assert budget.try_consume(1) is False


# ---------------------------------------------------------------------------
# Sub-budgets (RFC §15.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("depth", "expected"), [(0, 1600), (1, 800), (2, 400), (3, 200), (4, 100)])
def test_child_halves_the_allocation_per_level(depth: int, expected: int) -> None:
    """RFC §15.7: quota(d) = ceiling / 2**d."""
    assert TokenBudget(1600).child(depth).allocated == expected


def test_child_of_a_modality_budget_defers_to_the_profile() -> None:
    """The halving rule must live in exactly one place: ModalityProfile."""
    profile = MODALITY_PROFILES[ComplexityModality.COMPLEX]
    budget = TokenBudget.for_modality(ComplexityModality.COMPLEX)
    for depth in range(5):
        assert budget.child(depth).allocated == profile.token_quota_at_depth(depth)


def test_child_depth_accumulates_down_the_chain() -> None:
    root = TokenBudget(1600)
    grandchild = root.child(1).child(1)
    assert grandchild.depth == 2
    assert grandchild.allocated == 400


def test_child_is_capped_by_what_the_parent_still_holds() -> None:
    """A child envelope larger than the parent's remainder is a promise it cannot keep."""
    root = TokenBudget(1000)
    root.try_consume(900)
    child = root.child(1)
    assert child.allocated == 100  # not 500


def test_child_spending_debits_the_parent() -> None:
    root = TokenBudget(1000)
    child = root.child(1)
    assert child.try_consume(400) is True
    assert child.consumed == 400
    assert root.consumed == 400
    assert root.remaining == 600


def test_child_is_refused_when_the_parent_cannot_afford_it() -> None:
    root = TokenBudget(1000)
    child = root.child(1)  # 500
    root.try_consume(800)  # parent now holds 200
    assert child.try_consume(300) is False
    assert child.consumed == 0
    assert root.consumed == 800  # no partial debit anywhere in the chain


def test_siblings_cannot_collectively_outspend_the_root() -> None:
    """Without the parent link, four depth-1 children of 1500 could spend 3000."""
    root = TokenBudget(1500)
    children = [root.child(1) for _ in range(4)]  # 750 each
    assert all(child.allocated == 750 for child in children)

    granted = sum(750 for _ in children)
    assert granted > root.allocated  # the envelopes genuinely do oversubscribe

    spent = sum(750 for child in children if child.try_consume(750))
    assert spent == 1500
    assert root.consumed == 1500
    assert root.remaining == 0


def test_deep_chain_never_outspends_the_root() -> None:
    root = TokenBudget(1024)
    node = root
    for _ in range(6):
        node = node.child(1)
        node.try_consume(node.allocated)
    assert root.consumed <= root.allocated
    assert root.remaining >= 0


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_child_rejects_bad_depth(bad: object) -> None:
    with pytest.raises(ValueError, match="depth must be"):
        TokenBudget(100).child(bad)  # type: ignore[arg-type]


def test_child_at_absurd_depth_is_empty_not_an_error() -> None:
    assert TokenBudget(1500).child(64).allocated == 0


# ---------------------------------------------------------------------------
# Conservation properties
# ---------------------------------------------------------------------------


@given(
    st.integers(min_value=0, max_value=5000),
    st.lists(st.integers(min_value=0, max_value=6000), max_size=40),
)
def test_budget_never_goes_negative(allocated: int, requests: list[int]) -> None:
    """The core invariant, over arbitrary request sequences."""
    budget = TokenBudget(allocated)
    for request in requests:
        budget.try_consume(request)
        assert budget.consumed >= 0
        assert budget.consumed <= budget.allocated
        assert budget.remaining >= 0
        assert budget.remaining == budget.allocated - budget.consumed


@given(
    st.integers(min_value=0, max_value=5000),
    st.lists(st.integers(min_value=0, max_value=6000), max_size=40),
)
def test_consumed_equals_the_sum_of_accepted_requests(
    allocated: int, requests: list[int]
) -> None:
    budget = TokenBudget(allocated)
    accepted = sum(request for request in requests if budget.try_consume(request))
    assert budget.consumed == accepted


@given(
    st.integers(min_value=0, max_value=4096),
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=5000)),
        max_size=30,
    ),
)
def test_a_delegation_tree_never_outspends_its_root(
    allocated: int, operations: list[tuple[int, int]]
) -> None:
    """Conservation must hold across the whole tree, not just per node."""
    root = TokenBudget(allocated)
    children = [root.child(depth) for depth in range(1, 4)]

    for index, amount in operations:
        target = children[index] if index < len(children) else root
        target.try_consume(amount)

    assert root.consumed <= root.allocated
    assert root.remaining >= 0
    assert sum(child.consumed for child in children) <= root.consumed


@given(st.integers(min_value=0, max_value=100_000), st.integers(min_value=0, max_value=40))
def test_child_allocation_never_exceeds_the_rfc_quota(allocated: int, depth: int) -> None:
    budget = TokenBudget(allocated)
    child = budget.child(depth)
    expected = allocated >> depth if depth < 32 else 0
    assert child.allocated <= expected
    assert child.allocated <= budget.remaining
    assert child.allocated >= 0


@given(st.text(max_size=300))
def test_consume_text_is_bounded_by_the_estimate(text: str) -> None:
    estimator = CharEstimator(4.0)
    budget = TokenBudget(10_000)
    budget.consume_text(text, estimator)
    assert budget.consumed == math.ceil(len(text) / 4.0)
