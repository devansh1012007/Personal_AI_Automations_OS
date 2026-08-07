"""Telemetry: metrics, tracing, execution runs and structured logging. RFC §10.

Zero egress by default. With ``ObservabilitySettings.otlp_endpoint`` unset —
the default — nothing in this package opens a network connection: metrics live
in process, spans live in a bounded in-memory ring, runs live in the local
SQLite file, and logs go to the console. Observability that phoned home would
contradict the local-first guarantee the rest of the runtime is built to keep.

Typical wiring::

    from paa.observability import (
        ExecutionRunRepository, MetricsRegistry, Tracer, configure_logging,
    )

    configure_logging(settings)
    metrics = MetricsRegistry()
    tracer = Tracer(settings, metrics=metrics)
    runs = ExecutionRunRepository(db)
"""

from __future__ import annotations

from paa.observability.logging import (
    bind_correlation,
    clear_correlation,
    configure_logging,
    get_correlation_id,
    get_trace_id,
    set_trace_id,
)
from paa.observability.metrics import (
    DEFAULT_PERCENTILES,
    METRIC_SPECS,
    Histogram,
    MetricKind,
    MetricSpec,
    MetricsRegistry,
    percentile,
)
from paa.observability.runs import ExecutionRun, ExecutionRunRepository, SkillStats
from paa.observability.tracing import Span, SpanStatus, Tracer, current_span

__all__ = [
    "DEFAULT_PERCENTILES",
    "METRIC_SPECS",
    "ExecutionRun",
    "ExecutionRunRepository",
    "Histogram",
    "MetricKind",
    "MetricSpec",
    "MetricsRegistry",
    "SkillStats",
    "Span",
    "SpanStatus",
    "Tracer",
    "bind_correlation",
    "clear_correlation",
    "configure_logging",
    "current_span",
    "get_correlation_id",
    "get_trace_id",
    "percentile",
    "set_trace_id",
]
