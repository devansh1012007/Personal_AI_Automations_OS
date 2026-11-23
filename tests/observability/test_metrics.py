"""The metrics registry — the percentile *values*, and the rule that makes them.

``paa.observability.metrics`` had no coverage at all, and the one part of it
a reader of a snapshot is most likely to misread is a percentile label:
``p99`` and ``p100`` are different keys, so a label that quietly changes
shape splits a series' history in two — and the change is invisible in the
*values*, because the value is computed by :func:`percentile` either way.
Only the key moves.

So the label is pinned here. It is **rounded, not truncated**: the 99.5th
percentile of a 0..1 scale is the 100th percentile of that scale, and
labelling it ``p99`` would understate it by half a percentile while looking
like nothing at all.

The label is not the value, though
--------------------------------------
A label that names a percentile the series does not hold is a dashboard that
lies in a way no reader can see, and the file this module shipped with pinned
the label while saying nothing about the number under it: one test compared
``summary["p95"]`` to ``percentile(sorted(values), 0.95)`` — the function
against *itself*. That test passes for any implementation whatsoever,
including a nearest-rank one that returns a completely different number.

So the values are pinned here too, **by hand**: the p95 of ``[1..5]`` is
4.8, written out and derived on paper, not read back off the function. And
the module's own argument for choosing interpolation — *"the nearest-rank
alternative can only ever return an observed sample"* — is turned into an
assertion, because an argument that nothing checks is a comment.

Then the **rule**, as a property: a hand-computed value pins one point, and a
property pins the line through every point, so a different algorithm cannot
be right by accident on the four numbers somebody wrote down.
"""

from __future__ import annotations

import json
import threading

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from paa.observability.metrics import (
    DEFAULT_PERCENTILES,
    METRIC_SPECS,
    Histogram,
    MetricKind,
    MetricSpec,
    MetricsRegistry,
    percentile,
)


def _histogram(values: list[float]) -> Histogram:
    histogram = Histogram()
    for value in values:
        histogram.observe(value)
    return histogram


class TestPercentileLabels:
    def test_the_default_labels_are_the_documented_ones(self) -> None:
        """The three the registry reports by default, named as documented."""
        summary = _histogram([1.0, 2.0, 3.0, 4.0]).summary()

        assert "p50" in summary
        assert "p95" in summary
        assert "p99" in summary

    def test_a_half_way_quantile_is_rounded_not_truncated(self) -> None:
        """The rounding is the point, and it is the only rounding.

        ``round`` and ``int`` disagree exactly here: ``int(99.5)`` is 99 and
        ``round(99.5)`` is 100. A truncation would relabel a 99.5th
        percentile as ``p99`` and a 1.5th as ``p1`` — and the value stored
        under the key would not change, so nothing but the key would reveal
        it.
        """
        summary = _histogram([1.0, 2.0, 3.0, 4.0]).summary(quantiles=(0.995, 0.015))

        assert "p100" in summary
        assert "p2" in summary
        assert "p99" not in summary
        assert "p1" not in summary

    def test_a_half_way_quantile_takes_the_even_label(self) -> None:
        """The rounding is Python's, which is **banker's** rounding.

        ``round(99.5)`` is 100 and ``round(98.5)`` is 98: a half-way
        percentage takes the *even* label, whichever way that falls. The
        test above pins the half-way case that rounds up; this one pins
        the half-way case that rounds down, which is the case the
        ``round``-is-not-``int`` argument does not cover.

        It is pinned because a label that quietly changes shape splits a
        series' history in two — so a change to the convention has to be
        a decision somebody makes, not something that happens because a
        rounding function was swapped.
        """
        summary = _histogram([1.0, 2.0, 3.0, 4.0]).summary(quantiles=(0.985,))

        assert "p98" in summary
        assert "p99" not in summary

    def test_the_label_names_the_quantile_that_was_asked_for(self) -> None:
        """The key and the value under it are the same percentile.

        A label that did not match the value it names would be worse than no
        label: the reader would trust the key and believe a number the
        series never held.

        This pins the *wiring* — that the value under ``p95`` is the p95 —
        and deliberately says nothing about what a p95 **is**. The number
        itself is pinned, by hand, in
        :class:`TestTheHandComputedValues`; comparing the summary against
        the function would pin the function against itself.
        """
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        summary = _histogram(values).summary(quantiles=(0.95,))

        assert summary["p95"] == pytest.approx(percentile(sorted(values), 0.95))

    def test_the_default_quantiles_are_the_three_reported(self) -> None:
        assert DEFAULT_PERCENTILES == (0.50, 0.95, 0.99)


class TestPercentileValues:
    def test_an_empty_series_is_zero(self) -> None:
        """No samples is zero, not an error."""
        assert percentile([], 0.5) == 0.0

    def test_a_single_sample_is_itself(self) -> None:
        assert percentile([7.0], 0.99) == 7.0

    def test_the_ends_are_the_ends(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 4.0

    def test_the_middle_interpolates(self) -> None:
        """Linear interpolation, not nearest-rank.

        Nearest-rank can only ever return an observed sample, so a p99 over
        twenty observations is forced to be the maximum — and a latency
        dashboard ends up reporting p50 and p99 as the same number.
        """
        values = [0.0, 10.0]
        assert percentile(values, 0.5) == pytest.approx(5.0)

    @pytest.mark.parametrize("quantile", [-0.1, 1.1])
    def test_a_quantile_outside_the_unit_interval_is_refused(self, quantile: float) -> None:
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], quantile)

    def test_a_bad_quantile_is_refused_even_for_an_empty_series(self) -> None:
        """The check does not depend on the data.

        An empty series answers ``0.0`` for a *valid* quantile. A
        quantile outside the unit interval is a bad argument whatever
        the series holds, and the same bad argument must be refused
        either way — otherwise the check exists only for the case a
        caller happens to hit first, and a caller that only ever had
        empty series would not know it was there.
        """
        with pytest.raises(ValueError):
            percentile([], 1.5)

    def test_a_quantile_that_is_not_a_number_is_refused(self) -> None:
        """``nan`` is not in ``[0, 1]``, and the comparison says so.

        ``0.0 <= nan <= 1.0`` is false — every comparison with ``nan``
        is — so the range check catches it. A quantile of ``nan`` that
        reached the rank formula would poison every summary it was
        written into.
        """
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], float("nan"))


class TestTheRegistryCarriesTheLabels:
    def test_a_snapshot_names_the_percentiles_it_reports(self) -> None:
        """The label is what a reader of a snapshot actually sees.

        The registry is where the histogram summary is published — into the
        ``telemetry`` column of a run row — so the label is not an internal
        detail of the summary but the name the series is filed under.
        """
        registry = MetricsRegistry()
        registry.observe("graph_traversal_latency", 12.0)
        registry.observe("graph_traversal_latency", 20.0)

        snapshot = registry.snapshot()
        summary = snapshot["histograms"]["graph_traversal_latency"]

        assert "p50" in summary
        assert "p95" in summary
        assert "p99" in summary


# ---------------------------------------------------------------------------
# The values, hand-computed
# ---------------------------------------------------------------------------


class TestTheHandComputedValues:
    """The percentile's *number*, derived on paper and written down.

    The rule the numbers come from, stated once so each derivation below
    can be checked against it by eye::

        rank  = (n - 1) · q
        lower = floor(rank),  upper = ceil(rank)
        value = v[lower] + (rank - lower) · (v[upper] - v[lower])

    Each test says which of those three lines it is exercising, so a
    failure names the step that went wrong rather than just a number that
    did not come out.
    """

    def test_the_p95_of_one_to_five_is_4_8(self) -> None:
        """rank = 4 · 0.95 = 3.8 → 4 + 0.8 · (5 - 4) = **4.8**.

        The value a nearest-rank implementation returns here is 5.0 — the
        maximum — so this one number separates the two algorithms.
        """
        assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)

    def test_the_p99_of_one_to_a_hundred_is_99_01(self) -> None:
        """rank = 99 · 0.99 = 98.01 → 99 + 0.01 · (100 - 99) = **99.01**.

        A hundred samples, so the p99 is not the maximum: nearest rank
        would answer 100.0, and the difference — a full percentile — is
        exactly the error the interpolation exists to avoid.
        """
        values = [float(number) for number in range(1, 101)]

        assert percentile(values, 0.99) == pytest.approx(99.01)

    def test_the_p95_of_one_to_a_hundred_is_95_05(self) -> None:
        """rank = 99 · 0.95 = 94.05 → 95 + 0.05 · (96 - 95) = **95.05**."""
        values = [float(number) for number in range(1, 101)]

        assert percentile(values, 0.95) == pytest.approx(95.05)

    def test_the_p50_of_one_to_a_hundred_is_50_5(self) -> None:
        """rank = 99 · 0.5 = 49.5 → 50 + 0.5 · (51 - 50) = **50.5**.

        An even count has no middle sample, so the median is *between* two
        of them. Nearest rank answers 51.0; the answer a reader expects
        from "the median of a hundred consecutive integers" is 50.5.
        """
        values = [float(number) for number in range(1, 101)]

        assert percentile(values, 0.5) == pytest.approx(50.5)

    def test_the_p50_of_one_to_five_is_the_middle_sample(self) -> None:
        """rank = 4 · 0.5 = 2 exactly → the third sample, un-interpolated.

        An odd count *does* have a middle sample, and the rule says it is
        the answer — weight zero, so the interpolation leaves it alone.
        """
        assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0

    def test_an_even_count_interpolates_across_the_middle(self) -> None:
        """rank = 3 · 0.5 = 1.5 → 2 + 0.5 · (3 - 2) = **2.5**."""
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)

    def test_the_result_is_not_one_of_the_samples(self) -> None:
        """The module's own argument, turned into an assertion.

        The docstring says the nearest-rank alternative *"can only ever
        return an observed sample"*, and the reason interpolation was
        chosen is that this one does not. So the property worth pinning
        is not the number — the number is pinned above — but that the
        number is **not a sample**. A nearest-rank implementation returns
        5.0 here and this assertion goes red, which is exactly the point:
        the argument in the docstring is the reason the algorithm is what
        it is, and it was the argument that nothing checked.
        """
        values = [1.0, 2.0, 3.0, 4.0, 5.0]

        assert percentile(values, 0.95) not in values

    def test_the_result_lies_strictly_between_the_samples_it_interpolates(self) -> None:
        """Strictly between — which is why it cannot be a sample, and cannot
        be out of range either.

        The two facts are the same fact: the value is a *convex
        combination* of two neighbours, so it is inside them and outside
        the set of observed values unless the neighbours are equal.
        """
        values = [1.0, 2.0, 3.0, 4.0, 5.0]

        result = percentile(values, 0.95)

        assert 4.0 < result < 5.0


# ---------------------------------------------------------------------------
# The rule, as a property
# ---------------------------------------------------------------------------


#: A sample of distinct values on a quarter-unit grid.
#:
#: The grid is deliberate, and it is not a convenience. The property below
#: needs the interpolated value to be *strictly* between two samples, and
#: that is only observable when the samples are far enough apart for the
#: arithmetic to show it: with two adjacent doubles near 1e6 the
#: interpolated value rounds straight back to the lower one, and the
#: property would fail for reasons of floating point rather than reasons
#: of algorithm. Multiples of a quarter are exactly representable and
#: never closer together than 0.25, so a thousandth of the gap is still
#: visible in the result.
_GRID_SAMPLES = st.lists(
    st.integers(min_value=-4000, max_value=4000),
    min_size=2,
    max_size=40,
    unique=True,
).map(lambda numbers: sorted(number / 4.0 for number in numbers))

#: Arbitrary finite samples, for the properties that do not need a gap.
_ARBITRARY_SAMPLES = st.lists(
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=40,
    unique=True,
).map(sorted)

#: Where on the interpolation grid a draw lands: the fraction of the way
#: from one sample to the next.
#:
#: ``0.0`` and ``1.0`` are drawn **explicitly** rather than left to
#: chance. They are the two positions where the answer is an observed
#: sample exactly — the boundaries of the rule — and a strategy that
#: practically never produced them would leave the boundary untested. The
#: interior values are kept away from the ends by a thousandth so that
#: "strictly between" is a statement about the arithmetic and not about a
#: float that happens to be one ulp short of the endpoint.
_FRACTION = st.one_of(
    st.just(0.0),
    st.just(1.0),
    st.floats(min_value=1e-3, max_value=1 - 1e-3, allow_nan=False, allow_infinity=False),
)


@st.composite
def _positions(draw: st.DrawFn) -> tuple[list[float], int, float]:
    """A sample, the index of a sample, and a fraction of the way to the next.

    The index is drawn *after* the sample, so it ranges over exactly the
    positions that sample has — including its first and its last
    interval, which are the two the boundary cases live on.
    """
    numbers = draw(
        st.lists(
            st.integers(min_value=-4000, max_value=4000),
            min_size=2,
            max_size=40,
            unique=True,
        )
    )
    samples = sorted(number / 4.0 for number in numbers)
    index = draw(st.integers(min_value=0, max_value=len(samples) - 2))
    fraction = draw(_FRACTION)
    return samples, index, fraction


class TestTheAlgorithm:
    """The rank formula, as a property over every position on the grid.

    The hand-computed values pin the *answers*; this pins the *rule* that
    produces them, so a different rule cannot be right by accident on the
    four numbers somebody wrote down.
    """

    @given(position=_positions())
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_value_sits_where_the_rank_formula_says(
        self, position: tuple[list[float], int, float]
    ) -> None:
        """``rank = (n - 1) · q`` puts the answer between two neighbours.

        Drawn, not enumerated: the index and the fraction both come from
        the strategy, and the fraction's endpoints are drawn explicitly,
        so the boundary between "the answer is a sample" and "the answer
        is between two samples" is crossed on purpose rather than by luck.

        The adversarial input here is the *interior* fraction, and it is
        the one the property is about: at a fraction of zero or one the
        answer is a sample, and at anything in between it is not — and
        that difference is the whole reason the algorithm interpolates.
        """
        samples, index, fraction = position
        count = len(samples)
        quantile = (index + fraction) / (count - 1)

        result = percentile(samples, quantile)

        lower = samples[index]
        upper = samples[index + 1]
        if fraction == 0.0:
            # A grid point: the answer is the sample itself.
            assert result == pytest.approx(lower, rel=1e-9)
        elif fraction == 1.0:
            # The next grid point: likewise, and the sample after it.
            assert result == pytest.approx(upper, rel=1e-9)
        else:
            # Between two grid points: strictly between the two samples,
            # and therefore not a sample at all.
            assert lower < result < upper
            assert all(result != sample for sample in samples)

    @given(samples=_GRID_SAMPLES)
    @hyp_settings(max_examples=100, deadline=None)
    def test_each_sample_sits_at_its_own_quantile(self, samples: list[float]) -> None:
        """The formula itself, read backwards: sample ``k`` is the answer at
        ``q = k / (n - 1)``.

        This is what ``rank = (n - 1) · q`` *means*, and it is the one
        statement that separates the formula from its neighbours. Nearest
        rank puts sample ``k`` at ``q = k / n``; a formula on ``n · q``
        puts it at ``k / n`` too, with the sample stretched across the
        wrong end of the range. Only this one puts the first sample at
        ``q = 0`` and the last at ``q = 1``.
        """
        count = len(samples)

        for index in range(count):
            quantile = index / (count - 1)
            assert percentile(samples, quantile) == pytest.approx(
                samples[index], rel=1e-9
            ), f"sample {index} is not the answer at q={quantile}"

    @given(samples=_ARBITRARY_SAMPLES, data=st.data())
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_value_never_leaves_the_range_of_the_samples(
        self, samples: list[float], data: st.DataObject
    ) -> None:
        """A quantile of a sample is inside the sample.

        Drawn from the strategy: the quantile includes both ends of the
        unit interval, so ``q = 0`` and ``q = 1`` are *positions in the
        strategy* rather than values the generator might never reach.
        """
        quantile = data.draw(st.floats(min_value=0.0, max_value=1.0))

        result = percentile(samples, quantile)

        assert min(samples) <= result <= max(samples)

    @given(samples=_ARBITRARY_SAMPLES, data=st.data())
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_value_does_not_fall_as_the_quantile_rises(
        self, samples: list[float], data: st.DataObject
    ) -> None:
        """Monotone in the quantile.

        A percentile that decreases as the quantile increases is not a
        percentile; and the *ordering* of two quantiles is a property
        that survives any change to the interpolation, which makes it the
        right check for a rewrite of the formula.
        """
        lower_quantile = data.draw(st.floats(min_value=0.0, max_value=1.0))
        upper_quantile = data.draw(st.floats(min_value=lower_quantile, max_value=1.0))

        assert percentile(samples, lower_quantile) <= percentile(samples, upper_quantile)

    @given(samples=_ARBITRARY_SAMPLES)
    @hyp_settings(max_examples=100, deadline=None)
    def test_the_ends_are_the_ends_of_the_sample(self, samples: list[float]) -> None:
        """``q = 0`` is the smallest sample and ``q = 1`` the largest.

        Not "some sample" — the *extremes*. A rule that put the first
        sample at ``q = 1/n`` instead would still be monotone, and would
        still pass the grid-point property for the interior samples; only
        the endpoints give it away.
        """
        assert percentile(samples, 0.0) == min(samples)
        assert percentile(samples, 1.0) == max(samples)


# ---------------------------------------------------------------------------
# The summary a reader of a snapshot actually sees
# ---------------------------------------------------------------------------


class TestTheSummary:
    """The numbers :meth:`Histogram.summary` publishes.

    This is the shape that lands in the ``telemetry`` column of a run
    row, so it is a **published contract**, not an internal detail: a
    consumer that reads ``summary["window"]`` on a series that has no
    samples must not get a ``KeyError`` for a question it has a perfectly
    good answer to.
    """

    def test_the_numeric_fields_are_the_ones_a_reader_uses(self) -> None:
        """Count, sum, min, max, mean, window — and the percentiles.

        Every one of them, by hand. ``count`` is 5, the sum is 15, the
        mean is 3, the window is 5 (all five samples are retained), and
        the percentiles are the ones computed above.
        """
        summary = _histogram([1.0, 2.0, 3.0, 4.0, 5.0]).summary()

        assert summary["count"] == 5
        assert summary["sum"] == pytest.approx(15.0)
        assert summary["min"] == pytest.approx(1.0)
        assert summary["max"] == pytest.approx(5.0)
        assert summary["mean"] == pytest.approx(3.0)
        assert summary["window"] == 5
        assert summary["p50"] == pytest.approx(3.0)
        assert summary["p95"] == pytest.approx(4.8)
        assert summary["p99"] == pytest.approx(4.96)

    def test_the_numeric_fields_are_present_when_there_is_nothing_to_summarise(
        self,
    ) -> None:
        """The same fields, whatever the sample count.

        A summary that reports ``window`` for a series with samples and
        omits it for a series without makes every consumer write
        ``summary.get("window", 0)`` — and the ``get`` is how the
        difference stops being visible. The empty answer is zero, not a
        missing key.
        """
        summary = Histogram().summary()

        assert summary["count"] == 0
        assert summary["sum"] == pytest.approx(0.0)
        assert summary["min"] == pytest.approx(0.0)
        assert summary["max"] == pytest.approx(0.0)
        assert summary["mean"] == pytest.approx(0.0)
        assert summary["window"] == 0

    def test_a_summary_carries_the_numbers_and_not_only_the_labels(self) -> None:
        """What the registry publishes is the summary, numbers included.

        The registry is where the summary reaches the ``telemetry``
        column, so the fields a snapshot reader depends on have to
        survive the trip.
        """
        registry = MetricsRegistry()
        registry.observe("graph_traversal_latency", 12.0)
        registry.observe("graph_traversal_latency", 20.0)

        summary = registry.snapshot()["histograms"]["graph_traversal_latency"]

        assert summary["count"] == 2
        assert summary["sum"] == pytest.approx(32.0)
        assert summary["min"] == pytest.approx(12.0)
        assert summary["max"] == pytest.approx(20.0)
        assert summary["mean"] == pytest.approx(16.0)
        assert summary["window"] == 2
        assert summary["p50"] == pytest.approx(16.0)


# ---------------------------------------------------------------------------
# The ring
# ---------------------------------------------------------------------------


class TestTheRing:
    """The bounded sample buffer, and what it means for a reader.

    The histogram's docstring makes a promise with two halves: *count and
    sum are exact for the lifetime; the percentiles describe only the
    retained window.* The halves are what make the summary readable — a
    reader who cannot tell them apart reads a truncated window as the
    whole history.
    """

    def test_the_count_and_the_sum_are_for_the_lifetime_and_the_window_is_not(
        self,
    ) -> None:
        """The promise, as a measurement.

        Five observations into a ring that holds three: the count is 5,
        the sum is 15, the smallest is 1 and the largest is 5 — all four
        are lifetime facts. The window is 3, and the median of the
        *window* is 4.0, where the median of the whole history is 3.0.
        """
        histogram = Histogram(capacity=3)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            histogram.observe(value)

        summary = histogram.summary()

        assert summary["count"] == 5
        assert summary["sum"] == pytest.approx(15.0)
        assert summary["min"] == pytest.approx(1.0)
        assert summary["max"] == pytest.approx(5.0)
        assert summary["window"] == 3
        assert summary["p50"] == pytest.approx(4.0)

    def test_the_window_keeps_the_most_recent_observations(self) -> None:
        """Recency, not an unbiased sample of the history.

        The ring overwrites the oldest sample first, so the window holds
        the last ``capacity`` observations. That is the deliberate choice
        recorded in the source: this is a latency series a human is
        reading *now*, so recency beats an unbiased estimate of a
        distribution that has already changed.
        """
        histogram = Histogram(capacity=3)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            histogram.observe(value)

        assert sorted(histogram.samples) == [3.0, 4.0, 5.0]

    def test_a_ring_that_retains_nothing_is_refused(self) -> None:
        """``capacity=0`` is a configuration mistake, and it is named.

        Left alone it reaches ``observe`` as an ``IndexError`` from
        ``self.samples[self._cursor] = value`` — an error about a list
        index, raised some distance from the mistake that caused it, and
        raised only when the first observation arrives. A histogram that
        retains no samples is not a histogram; the refusal says so at the
        point where the capacity is chosen.
        """
        with pytest.raises(ValueError):
            Histogram(capacity=0)

    def test_a_registry_that_cannot_retain_a_sample_is_refused(self) -> None:
        """The same rule, one layer up.

        The registry takes the capacity as a *parameter*, so a caller
        who passes zero to it should hear about it when they pass it —
        not when the first observation arrives, and not with an error
        about a list index.
        """
        with pytest.raises(ValueError):
            MetricsRegistry(histogram_capacity=0)


class TestTheRegistryRefusesNonsense:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_observation_is_refused(self, value: float) -> None:
        """A latency of ``nan`` is not a latency.

        A non-finite value would poison the sum and the mean for the
        lifetime of the series, and no reader of the summary could tell
        that the numbers were nonsense.
        """
        registry = MetricsRegistry()

        with pytest.raises(ValueError):
            registry.observe("graph_traversal_latency", value)

    def test_an_observation_that_would_overflow_the_total_is_refused(self) -> None:
        """The published summary has to be JSON.

        The telemetry column carries a ``json_valid`` constraint, and
        ``json.dumps`` writes an infinite float as the bare token
        ``Infinity`` — which is not JSON, and which the column refuses.
        So a total that overflowed would make **every later snapshot**
        for the series unwritable, and the row it belongs to is the run
        record the optimiser reads.

        The refusal is at the observation, because that is where the
        nonsense enters; refusing it later would leave the series
        permanently poisoned instead.
        """
        registry = MetricsRegistry()
        registry.observe("graph_traversal_latency", 1e308)

        with pytest.raises(ValueError):
            registry.observe("graph_traversal_latency", 1e308)

    def test_a_counter_that_would_overflow_is_refused(self) -> None:
        """The same rule on the other side of the registry.

        A counter is published in the same snapshot as a histogram, so
        the same JSON constraint applies to it.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 1e308)

        with pytest.raises(ValueError):
            registry.counter("user_correction_frequency", 1e308)

    def test_a_non_finite_counter_is_refused(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError):
            registry.counter("user_correction_frequency", float("inf"))

    def test_the_snapshot_of_a_healthy_series_is_json(self) -> None:
        """The end of the chain, checked end to end.

        The snapshot is what lands in the ``telemetry`` column of a run
        row, and that column is JSON-constrained. So the snapshot has to
        be JSON — which is a property of the *numbers in it*, not of the
        containers around them.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 3)
        registry.gauge("queue_backpressure", 7)
        registry.observe("graph_traversal_latency", 12.0)
        registry.observe("graph_traversal_latency", 20.0)

        snapshot = registry.snapshot()

        assert json.dumps(snapshot)
        assert json.loads(json.dumps(snapshot)) == snapshot


# ---------------------------------------------------------------------------
# Counters, gauges, and the series a label makes
# ---------------------------------------------------------------------------


class TestCounters:
    """A counter is a monotonically increasing total.

    These are the numbers an operator watches — ``user_correction_frequency``
    is the self-improvement loop's primary signal — so the accumulation and
    the refusal to go down are both operator-visible contracts, not internal
    bookkeeping.
    """

    def test_increments_accumulate(self) -> None:
        registry = MetricsRegistry()

        registry.counter("user_correction_frequency", 2)
        registry.counter("user_correction_frequency", 3)

        assert registry.get_counter("user_correction_frequency") == pytest.approx(5.0)

    def test_the_default_increment_is_one(self) -> None:
        """``counter(name)`` counts one event, which is what a call site means."""
        registry = MetricsRegistry()

        registry.counter("user_correction_frequency")
        registry.counter("user_correction_frequency")

        assert registry.get_counter("user_correction_frequency") == pytest.approx(2.0)

    def test_a_counter_cannot_decrease(self) -> None:
        """A negative increment is a bug in the caller, not a correction.

        The :attr:`MetricKind.COUNTER` docstring says so, and the refusal
        is what keeps a programming mistake from silently rewriting an
        operator's total downward.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 5)

        with pytest.raises(ValueError, match="cannot decrease"):
            registry.counter("user_correction_frequency", -1)

        # And the refusal was not a write: the total is what it was.
        assert registry.get_counter("user_correction_frequency") == pytest.approx(5.0)

    def test_a_counter_that_was_never_incremented_reads_zero(self) -> None:
        """Zero, not ``None`` and not a ``KeyError``.

        A counter is a total, and the total of no events is zero — a reader
        rendering a dashboard should not have to distinguish "never
        incremented" from "incremented zero times".
        """
        registry = MetricsRegistry()

        assert registry.get_counter("user_correction_frequency") == 0.0


class TestGauges:
    """A gauge is an instantaneous value that may move in either direction."""

    def test_a_gauge_that_was_never_set_has_no_value(self) -> None:
        """``None``, not zero.

        This is the deliberate difference from a counter: a queue depth of
        zero is a *measurement*, and a queue that was never measured has no
        answer. Reporting ``0`` for both would make an uninstrumented gauge
        read as "empty and healthy".
        """
        registry = MetricsRegistry()

        assert registry.get_gauge("queue_backpressure") is None

    def test_a_gauge_is_set_not_incremented(self) -> None:
        registry = MetricsRegistry()

        registry.gauge("queue_backpressure", 7)
        registry.gauge("queue_backpressure", 2)

        assert registry.get_gauge("queue_backpressure") == pytest.approx(2.0)

    def test_a_gauge_may_move_down(self) -> None:
        """The whole difference from a counter, in one direction.

        A queue drains; refusing the downward move would make the gauge
        useless for exactly the workload it exists to watch.
        """
        registry = MetricsRegistry()

        registry.gauge("queue_backpressure", 9)
        registry.gauge("queue_backpressure", 4)

        assert registry.get_gauge("queue_backpressure") == pytest.approx(4.0)

    def test_a_histogram_that_was_never_observed_is_absent(self) -> None:
        registry = MetricsRegistry()

        assert registry.get_histogram("graph_traversal_latency") is None


class TestLabels:
    """A series is a metric name plus its label set.

    The snapshot is the published artefact — it lands in the ``telemetry``
    column of a run row — so the key a labelled series is filed under is
    part of the contract, not a rendering detail a consumer can re-derive.
    """

    def test_labels_make_distinct_series(self) -> None:
        """Two values of one label are two answers, not one.

        A regression here — folding the label into the name, or dropping it
        — would overwrite one series with another, and an operator's split
        by label would silently become a total.
        """
        registry = MetricsRegistry()

        registry.counter("user_correction_frequency", 1, source="chat")
        registry.counter("user_correction_frequency", 1, source="cli")
        registry.counter("user_correction_frequency", 1, source="chat")

        assert registry.get_counter("user_correction_frequency", source="chat") == (
            pytest.approx(2.0)
        )
        assert registry.get_counter("user_correction_frequency", source="cli") == (
            pytest.approx(1.0)
        )

    def test_a_label_value_is_its_text(self) -> None:
        """``source=2`` and ``source="2"`` are the same series.

        Labels are stringified on the way in, so a caller passing an int
        and a caller passing the text of that int are talking about the
        same series — and cannot split one series in two by spelling its
        label differently.
        """
        registry = MetricsRegistry()

        registry.counter("user_correction_frequency", 1, source=2)
        registry.counter("user_correction_frequency", 1, source="2")

        assert registry.get_counter("user_correction_frequency", source=2) == (
            pytest.approx(2.0)
        )

    def test_the_snapshot_renders_a_labelled_series_under_its_key(self) -> None:
        """The golden shape: ``name{k=v}`` with the labels sorted.

        The key is what the ``telemetry`` column carries, so its spelling
        is the join a reader makes. A brace instead of a comma, or labels
        in insertion order rather than sorted, would change the key a
        series is filed under without changing any number in it.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 2, severity="warn")
        registry.gauge("queue_backpressure", 5, queue="ingest")
        registry.observe("graph_traversal_latency", 12.0, hop="two")

        snapshot = registry.snapshot()

        assert snapshot["counters"] == {"user_correction_frequency{severity=warn}": 2.0}
        assert snapshot["gauges"] == {"queue_backpressure{queue=ingest}": 5.0}
        assert set(snapshot["histograms"]) == {"graph_traversal_latency{hop=two}"}

    def test_the_snapshot_is_the_three_sections_and_nothing_else(self) -> None:
        """The top-level golden: counters, gauges, histograms.

        A consumer of the ``telemetry`` column reads these three keys; an
        extra section would be a silent schema change to the run row, and
        a missing one would make every consumer's ``get`` fail loudly
        after having worked quietly.
        """
        registry = MetricsRegistry()

        assert set(registry.snapshot()) == {"counters", "gauges", "histograms"}
        assert registry.snapshot() == {"counters": {}, "gauges": {}, "histograms": {}}

    def test_an_unlabelled_series_is_filed_under_its_bare_name(self) -> None:
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 1)

        assert set(registry.snapshot()["counters"]) == {"user_correction_frequency"}


class TestTheDeclaredKinds:
    """A declared kind is pinned: an update that contradicts it is refused.

    The point of declaring the RFC §10.1 list up front — the module's own
    argument — is that ``observe()`` on something declared a counter fails
    loudly rather than producing a histogram nobody will ever look at.
    That argument had no test: nothing ever *tried* the wrong kind.
    """

    def test_observing_into_a_declared_counter_is_refused(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError, match="cannot update it as a histogram"):
            registry.observe("user_correction_frequency", 1.0)

    def test_gauging_a_declared_counter_is_refused(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError, match="cannot update it as a gauge"):
            registry.gauge("user_correction_frequency", 1)

    def test_incrementing_a_declared_gauge_is_refused(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError, match="cannot update it as a counter"):
            registry.counter("queue_backpressure")

    def test_observing_into_a_declared_gauge_is_refused(self) -> None:
        registry = MetricsRegistry()

        with pytest.raises(ValueError):
            registry.observe("queue_backpressure", 1.0)

    def test_an_undeclared_metric_is_refused_when_the_registry_refuses(self) -> None:
        """The typo the declaration list exists to catch.

        ``vram_saturaton`` — one ``i`` short of the real name — would
        otherwise spring into existence as a new series that silently
        reads zero forever while the intended one never moves.
        """
        registry = MetricsRegistry(allow_undeclared=False)

        with pytest.raises(KeyError, match="vram_saturaton"):
            registry.gauge("vram_saturaton", 0.5)

    def test_an_undeclared_metric_is_accepted_by_default(self) -> None:
        """The default posture: a skill's own metrics are not refused.

        The RFC list is the *runtime's* metrics; refusing everything else
        would make ``register`` the only way in, and a skill's own counter
        is not a mistake the declaration list exists to catch.
        """
        registry = MetricsRegistry()

        registry.counter("a_skills_own_metric")

        assert registry.get_counter("a_skills_own_metric") == pytest.approx(1.0)

    def test_register_declares_a_metric_and_the_kind_is_then_pinned(self) -> None:
        """The declaration path for a skill's own metrics, end to end.

        Before the ``register`` call, the name is undeclared; after it,
        the kind is enforced exactly as it is for the RFC list.
        """
        registry = MetricsRegistry(allow_undeclared=False)
        spec = MetricSpec(
            name="skill_steps",
            kind=MetricKind.COUNTER,
            unit="steps",
            description="A skill's own step count.",
        )

        with pytest.raises(KeyError):
            registry.counter("skill_steps")
        registry.register(spec)
        registry.counter("skill_steps", 3)

        assert registry.spec("skill_steps") == spec
        assert registry.get_counter("skill_steps") == pytest.approx(3.0)

    def test_the_rfc_metric_list_declares_the_kinds_it_names(self) -> None:
        """The RFC §10.1 list, transcribed verbatim and pinned by kind.

        The names are the contract a dashboard or a run row joins on; the
        kinds are what keep an update path honest. A name that drifted —
        or a counter that quietly became a gauge — would change what an
        operator's number *means*, which is worse than changing the
        number.
        """
        for name in (
            "recursive_budget_exhaustion",
            "syscall_interception_count",
            "user_correction_frequency",
        ):
            assert METRIC_SPECS[name].kind is MetricKind.COUNTER
        for name in ("graph_traversal_latency", "crash_recovery_duration"):
            assert METRIC_SPECS[name].kind is MetricKind.HISTOGRAM
        assert METRIC_SPECS["queue_backpressure"].kind is MetricKind.GAUGE

        # Every declaration carries the unit and the description a reader
        # of the registry's own docs would expect to find beside it.
        for spec_ in METRIC_SPECS.values():
            assert spec_.unit
            assert spec_.description


class TestReset:
    """``reset`` drops every series; declarations survive.

    The distinction is the whole point of the method: a test harness or
    a long-lived worker that resets between jobs wants the *numbers* gone
    without re-declaring the metric list it was built against.
    """

    def test_reset_drops_every_series(self) -> None:
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 2)
        registry.gauge("queue_backpressure", 5)
        registry.observe("graph_traversal_latency", 12.0)

        registry.reset()

        assert registry.get_counter("user_correction_frequency") == 0.0
        assert registry.get_gauge("queue_backpressure") is None
        assert registry.get_histogram("graph_traversal_latency") is None
        assert registry.snapshot() == {"counters": {}, "gauges": {}, "histograms": {}}

    def test_reset_survives_the_declarations(self) -> None:
        """The RFC list is still enforced after a reset.

        A reset that also dropped the declarations would leave the
        registry in its ``allow_undeclared=True`` posture for the names it
        was built to police — the kind checks would silently stop firing.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency", 2)

        registry.reset()

        with pytest.raises(ValueError, match="cannot update it as a histogram"):
            registry.observe("user_correction_frequency", 1.0)


class TestTheTimer:
    """The context manager that times a block into a histogram."""

    def test_the_timer_records_the_block_into_the_histogram(self) -> None:
        registry = MetricsRegistry()

        with registry.timer("graph_traversal_latency"):
            pass

        histogram = registry.get_histogram("graph_traversal_latency")
        assert histogram is not None
        assert histogram.count == 1
        assert histogram.summary()["min"] >= 0.0

    def test_the_timer_records_even_when_the_block_raises(self) -> None:
        """The docstring's promise, as an assertion.

        *"Records on the way out even when the block raises: a failure that
        took 30 seconds is exactly the observation worth having, and
        dropping it would make an error path look free."*
        """
        registry = MetricsRegistry()

        with pytest.raises(ValueError, match="kaboom"), registry.timer(
            "graph_traversal_latency"
        ):
            raise ValueError("kaboom")

        histogram = registry.get_histogram("graph_traversal_latency")
        assert histogram is not None
        assert histogram.count == 1


class TestTheThreadSafety:
    """The registry's own claim about itself.

    :class:`MetricsRegistry` is documented as *"Thread-safe in-process
    metric store"*, guarded by a :class:`threading.Lock` chosen explicitly
    because the sandbox watchdog samples from a real thread. An increment
    that could be lost between two threads is an operator-visible number
    that reads lower than the truth — and never obviously.
    """

    def test_concurrent_increments_lose_none(self) -> None:
        registry = MetricsRegistry()
        threads: list[threading.Thread] = []

        def worker() -> None:
            for _ in range(250):
                registry.counter("user_correction_frequency")

        for _ in range(4):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()

        assert registry.get_counter("user_correction_frequency") == pytest.approx(1000.0)

    def test_a_read_while_series_are_being_added_is_not_a_torn_read(self) -> None:
        """A snapshot taken from another thread is a whole answer.

        The hazard the lock exists for, concretely: the snapshot iterates
        ``self._counters`` while a writer inserts *new* series (a fresh
        label value each time), and iterating a dict that another thread
        is resizing raises ``RuntimeError: dictionary changed size during
        iteration`` — an operator's snapshot failing because somebody
        else dared to emit a metric. The lock makes the two mutually
        exclusive, so the reader either sees the old series set or the
        new one, never a half-built iteration.
        """
        registry = MetricsRegistry()
        registry.counter("user_correction_frequency")
        stop = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            for index in range(500):
                registry.counter("user_correction_frequency", 1, shard=f"shard-{index}")

        def reader() -> None:
            while not stop.is_set():
                try:
                    snapshot = registry.snapshot()
                    json.dumps(snapshot)
                except Exception as exc:  # the failure under test, reported below
                    failures.append(exc)
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        writer_thread.join()
        stop.set()
        reader_thread.join()

        assert failures == []
        # The unlabelled series is one; the 500 labelled shards are one each.
        assert registry.get_counter("user_correction_frequency") == pytest.approx(1.0)
        assert sum(registry.snapshot()["counters"].values()) == pytest.approx(501.0)
