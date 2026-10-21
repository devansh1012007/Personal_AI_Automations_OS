"""The metrics registry — the percentile *labels*, and what they claim.

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
"""

from __future__ import annotations

import pytest

from paa.observability.metrics import (
    DEFAULT_PERCENTILES,
    Histogram,
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

    def test_the_label_names_the_quantile_that_was_asked_for(self) -> None:
        """The key and the value under it are the same percentile.

        A label that did not match the value it names would be worse than no
        label: the reader would trust the key and believe a number the
        series never held.
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
