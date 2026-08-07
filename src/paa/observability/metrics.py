"""In-process metrics registry. RFC §10.1.

Zero egress by default. When ``ObservabilitySettings.otlp_endpoint`` is
``None`` — which is the default and the recommended posture — nothing here
opens a socket. Metrics accumulate in memory, are read through
:meth:`MetricsRegistry.snapshot`, and are persisted (if at all) into the local
SQLite telemetry column by :mod:`paa.observability.runs`. A local-first runtime
whose *observability layer* phones home would be a contradiction.

The RFC §10.1 metric list is transcribed into :data:`METRIC_SPECS` verbatim, by
name. Declaring them up front rather than letting them spring into existence at
first use buys two things: a typo (``vram_saturaton``) creates a *new* series
that silently reads zero forever instead of updating the intended one, and each
metric's kind is pinned, so calling :meth:`MetricsRegistry.observe` on
something declared a counter fails loudly rather than producing a histogram
nobody will ever look at.

Histograms retain a bounded ring of recent samples rather than every
observation. An unbounded list on ``graph_traversal_latency`` is a slow memory
leak in a process designed to run for weeks on a machine with ~3.5 GB free.
"""

from __future__ import annotations

import enum
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import structlog

__all__ = [
    "DEFAULT_PERCENTILES",
    "METRIC_SPECS",
    "Histogram",
    "MetricKind",
    "MetricSpec",
    "MetricsRegistry",
    "percentile",
]

log = structlog.get_logger(__name__)

#: Samples retained per histogram series. 4096 keeps p99 stable for any
#: realistic single-user workload while capping the series at ~32 KB.
_HISTOGRAM_CAPACITY: Final[int] = 4096

DEFAULT_PERCENTILES: Final[tuple[float, ...]] = (0.50, 0.95, 0.99)


class MetricKind(str, enum.Enum):
    """What a series means, which determines how it may be updated."""

    COUNTER = "counter"
    """Monotonically increasing total. Never decreases; a negative increment is
    a bug in the caller, not a legitimate correction."""

    GAUGE = "gauge"
    """Instantaneous value that may move in either direction."""

    HISTOGRAM = "histogram"
    """Distribution of observations, summarised by percentiles."""


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Declaration of one RFC §10.1 metric."""

    name: str
    kind: MetricKind
    unit: str
    description: str


def _spec(name: str, kind: MetricKind, unit: str, description: str) -> tuple[str, MetricSpec]:
    return name, MetricSpec(name=name, kind=kind, unit=unit, description=description)


#: The RFC §10.1 metric list, made executable. Names are verbatim from the spec.
METRIC_SPECS: Final[dict[str, MetricSpec]] = dict(
    (
        _spec(
            "context_pollution_ratio",
            MetricKind.GAUGE,
            "ratio",
            "Fraction of a context packet that no downstream step referenced (RFC §15.9).",
        ),
        _spec(
            "context_utility_score",
            MetricKind.GAUGE,
            "score",
            "Decision-usable signal carried by a context packet (RFC §15.1).",
        ),
        _spec(
            "entropy_drift",
            MetricKind.GAUGE,
            "bits",
            "Change in context entropy across a task's lifetime; rising drift means "
            "attention is scattering rather than converging.",
        ),
        _spec(
            "fact_decay_velocity",
            MetricKind.GAUGE,
            "confidence/day",
            "Rate at which stored facts lose confidence under the RFC §4.1 decay matrix.",
        ),
        _spec(
            "loop_interception_efficiency",
            MetricKind.GAUGE,
            "ratio",
            "Fraction of runaway agent loops caught by the recursion guard before "
            "the budget was exhausted.",
        ),
        _spec(
            "planning_cost_coefficient",
            MetricKind.GAUGE,
            "cost",
            "Discounted tree-of-thought expansion cost (RFC §15.6).",
        ),
        _spec(
            "recursive_budget_exhaustion",
            MetricKind.COUNTER,
            "events",
            "Times a delegation chain consumed its entire token or node budget.",
        ),
        _spec(
            "retrieval_precision_index",
            MetricKind.GAUGE,
            "ratio",
            "Share of retrieved facts that survived the relevance floor and were used.",
        ),
        _spec(
            "sandbox_memory_strain",
            MetricKind.GAUGE,
            "ratio",
            "Peak sandbox RSS as a fraction of its modality memory ceiling.",
        ),
        _spec(
            "syscall_interception_count",
            MetricKind.COUNTER,
            "events",
            "Syscalls refused by the containment layer. SPEC NOTE (docs/adr/0006): "
            "gVisor is unavailable on Windows, so on the subprocess backend there is "
            "no syscall boundary and this counter records AST pre-scan rejections "
            "instead. It is NOT equivalent, and a non-zero value must not be read as "
            "evidence of kernel-level containment.",
        ),
        _spec(
            "queue_backpressure",
            MetricKind.GAUGE,
            "messages",
            "Depth of the dispatch queue (RFC §6).",
        ),
        _spec(
            "idempotency_collision_rate",
            MetricKind.GAUGE,
            "ratio",
            "Share of ledger appends suppressed as duplicates. Healthy under "
            "at-least-once delivery; a spike means something is retrying in a loop.",
        ),
        _spec(
            "vram_saturation",
            MetricKind.GAUGE,
            "ratio",
            "Fraction of accelerator memory in use. SPEC NOTE (docs/adr/0007): the "
            "RFC's 85% VRAM target assumes a CUDA/ROCm device. On the target 2 GB "
            "AMD iGPU this tracks host RAM used by the local model instead.",
        ),
        _spec(
            "graph_traversal_latency",
            MetricKind.HISTOGRAM,
            "ms",
            "Wall-clock cost of a multi-hop provenance traversal.",
        ),
        _spec(
            "user_correction_frequency",
            MetricKind.COUNTER,
            "events",
            "USER_CORRECTION events. The self-improvement loop's primary signal.",
        ),
        _spec(
            "crash_recovery_duration",
            MetricKind.HISTOGRAM,
            "ms",
            "Time from process start to a completed recovery sweep.",
        ),
        _spec(
            "replay_correctness",
            MetricKind.GAUGE,
            "ratio",
            "Share of replayed lineages whose projected state matched disk.",
        ),
    )
)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def percentile(sorted_values: list[float], quantile: float) -> float:
    r"""Linear-interpolated percentile of an **already sorted** list.

    The NumPy ``"linear"`` method, and the one Prometheus and OpenTelemetry
    agree on:

    .. math:: rank = (n - 1) \cdot q

    with the result interpolated between the values either side of ``rank``.

    The interpolation is not decoration. The nearest-rank alternative can only
    ever return an observed sample, so a p99 computed over 20 observations is
    forced to be the maximum — which is how a latency dashboard ends up
    reporting that p50 and p99 are identical on a quiet service. Interpolating
    keeps the estimate meaningful at small ``n``, which is the normal case for a
    single-user runtime.

    :param sorted_values: ascending. Not sorted here: callers sort once and take
        several quantiles, and re-sorting per quantile is the kind of quiet
        O(k·n log n) that shows up on a hot path.
    :param quantile: in ``[0, 1]``.
    """
    if not sorted_values:
        return 0.0
    if not 0.0 <= quantile <= 1.0:
        raise ValueError(f"quantile must lie in [0, 1], got {quantile!r}")
    if len(sorted_values) == 1:
        return float(sorted_values[0])

    rank = (len(sorted_values) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(sorted_values[int(rank)])
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


@dataclass
class Histogram:
    """Bounded sample buffer with percentile summarisation.

    ``count`` and ``sum`` are exact for the lifetime of the process; the
    percentiles describe only the retained window. Reporting both, rather than
    silently letting ``count`` mean "samples I still hold", is what keeps a
    reader from mistaking a truncated window for the whole history.
    """

    capacity: int = _HISTOGRAM_CAPACITY
    samples: list[float] = field(default_factory=list)
    count: int = 0
    total: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf
    _cursor: int = 0

    def observe(self, value: float) -> None:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"histogram observations must be finite, got {value!r}")

        self.count += 1
        self.total += numeric
        self.minimum = min(self.minimum, numeric)
        self.maximum = max(self.maximum, numeric)

        if len(self.samples) < self.capacity:
            self.samples.append(numeric)
        else:
            # Ring buffer, not reservoir sampling: this is a latency series read
            # by a human debugging *now*, so recency beats an unbiased estimate
            # of a distribution that has already changed.
            self.samples[self._cursor] = numeric
            self._cursor = (self._cursor + 1) % self.capacity

    def summary(
        self, quantiles: tuple[float, ...] = DEFAULT_PERCENTILES
    ) -> dict[str, float | int]:
        if not self.samples:
            return {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "mean": 0.0}
        ordered = sorted(self.samples)
        out: dict[str, float | int] = {
            "count": self.count,
            "sum": self.total,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.total / self.count,
            "window": len(self.samples),
        }
        for quantile in quantiles:
            out[f"p{int(round(quantile * 100))}"] = percentile(ordered, quantile)
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: A series is a metric name plus its label set.
_SeriesKey = tuple[str, tuple[tuple[str, str], ...]]


def _series_key(name: str, labels: dict[str, Any]) -> _SeriesKey:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


def _render_key(key: _SeriesKey) -> str:
    name, labels = key
    if not labels:
        return name
    rendered = ",".join(f"{k}={v}" for k, v in labels)
    return f"{name}{{{rendered}}}"


class MetricsRegistry:
    """Thread-safe in-process metric store.

    Guarded by a plain :class:`threading.Lock` rather than an
    :class:`asyncio.Lock`. Every operation is a handful of arithmetic ops with
    no ``await`` inside, so an async lock would add scheduler round-trips to a
    critical section measured in nanoseconds — and a threading lock additionally
    covers the sandbox watchdog, which samples from a real thread.
    """

    def __init__(
        self,
        *,
        specs: dict[str, MetricSpec] | None = None,
        allow_undeclared: bool = True,
        histogram_capacity: int = _HISTOGRAM_CAPACITY,
    ) -> None:
        self._specs = dict(specs if specs is not None else METRIC_SPECS)
        self._allow_undeclared = allow_undeclared
        self._histogram_capacity = histogram_capacity
        self._lock = threading.Lock()

        self._counters: dict[_SeriesKey, float] = {}
        self._gauges: dict[_SeriesKey, float] = {}
        self._histograms: dict[_SeriesKey, Histogram] = {}

    # -- declaration --------------------------------------------------------

    def register(self, spec: MetricSpec) -> None:
        """Declare a metric not in the RFC list (a skill's own, typically)."""
        with self._lock:
            self._specs[spec.name] = spec

    def spec(self, name: str) -> MetricSpec | None:
        return self._specs.get(name)

    def _check_kind(self, name: str, kind: MetricKind) -> None:
        """Refuse an update that contradicts the declared kind.

        Raising beats coercing. ``observe()`` on a counter means the caller has
        a different model of what the metric *is*, and quietly creating a second
        series under the same name would leave two different answers to the same
        question in one snapshot.
        """
        declared = self._specs.get(name)
        if declared is None:
            if not self._allow_undeclared:
                raise KeyError(
                    f"metric {name!r} is not declared; register a MetricSpec first "
                    "or construct the registry with allow_undeclared=True"
                )
            return
        if declared.kind is not kind:
            raise ValueError(
                f"metric {name!r} is declared as a {declared.kind.value}, "
                f"cannot update it as a {kind.value}"
            )

    # -- updates ------------------------------------------------------------

    def counter(self, name: str, value: float = 1.0, **labels: Any) -> None:
        """Increment a counter. Refuses negative increments."""
        self._check_kind(name, MetricKind.COUNTER)
        if value < 0:
            raise ValueError(
                f"counter {name!r} cannot decrease (got {value!r}); use a gauge "
                "if the value legitimately moves in both directions"
            )
        key = _series_key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        """Set a gauge to ``value``."""
        self._check_kind(name, MetricKind.GAUGE)
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"gauge {name!r} must be finite, got {value!r}")
        with self._lock:
            self._gauges[_series_key(name, labels)] = numeric

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Record one histogram observation."""
        self._check_kind(name, MetricKind.HISTOGRAM)
        key = _series_key(name, labels)
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = Histogram(capacity=self._histogram_capacity)
                self._histograms[key] = histogram
            histogram.observe(value)

    @contextmanager
    def timer(self, name: str, **labels: Any) -> Iterator[None]:
        """Time a block into a histogram, in milliseconds.

        Records on the way out even when the block raises: a failure that took
        30 seconds is exactly the observation worth having, and dropping it
        would make an error path look free.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - started) * 1000.0, **labels)

    # -- reads --------------------------------------------------------------

    def get_counter(self, name: str, **labels: Any) -> float:
        with self._lock:
            return self._counters.get(_series_key(name, labels), 0.0)

    def get_gauge(self, name: str, **labels: Any) -> float | None:
        with self._lock:
            return self._gauges.get(_series_key(name, labels))

    def get_histogram(self, name: str, **labels: Any) -> Histogram | None:
        with self._lock:
            return self._histograms.get(_series_key(name, labels))

    def snapshot(
        self, quantiles: tuple[float, ...] = DEFAULT_PERCENTILES
    ) -> dict[str, Any]:
        """Everything, as a plain JSON-serialisable dict.

        Plain builtins only — no dataclasses, no enums. This goes straight into
        the ``telemetry`` column of ``hot_serving_execution_runs``, which is
        ``TEXT`` under a ``json_valid`` constraint, so anything needing a custom
        encoder would fail at the database rather than here.
        """
        with self._lock:
            counters = {_render_key(k): v for k, v in self._counters.items()}
            gauges = {_render_key(k): v for k, v in self._gauges.items()}
            histograms = {
                _render_key(k): h.summary(quantiles) for k, h in self._histograms.items()
            }
        return {
            "counters": dict(sorted(counters.items())),
            "gauges": dict(sorted(gauges.items())),
            "histograms": dict(sorted(histograms.items())),
        }

    def reset(self) -> None:
        """Drop every series. Declarations survive."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def __repr__(self) -> str:
        return (
            f"MetricsRegistry(counters={len(self._counters)}, "
            f"gauges={len(self._gauges)}, histograms={len(self._histograms)})"
        )
