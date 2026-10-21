"""Distributed tracing that works with nothing installed. RFC §10.

Two operating modes, chosen by configuration rather than by import success:

**Standalone** (the default, ``otlp_endpoint is None``). Spans are built,
nested, timed and retained in a bounded in-process ring. Nothing leaves the
machine. This is the posture a local-first runtime should ship in, and it is
still genuinely useful — a span tree answers "where did those 40 seconds go?"
without any collector at all.

**Bridged** (``otlp_endpoint`` set *and* opentelemetry importable). The same
spans are additionally handed to an OTLP exporter. ``opentelemetry`` is an
optional extra (``pip install "paa[otel]"``) and is imported lazily, inside a
function, so the package remains importable on a machine that has never seen
it.

The governing rule in this module: **a tracing failure must never break the
operation being traced.** Telemetry is a diagnostic aid; an exporter that
cannot reach its collector, a serialisation error on an attribute, or a bug in
this file must not be able to fail a user's task. Every internal step is
wrapped, and the only exception :meth:`Tracer.span` ever propagates is the one
raised by the caller's own body.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from paa.config import ObservabilitySettings, Settings
    from paa.observability.metrics import MetricsRegistry

__all__ = ["Span", "SpanStatus", "Tracer", "current_span"]

log = structlog.get_logger(__name__)

#: Retained finished spans. Bounded for the same reason histograms are: this
#: process is expected to run for weeks.
_MAX_RETAINED_SPANS: Final[int] = 2048

#: The active span, per async task. A ContextVar rather than an instance
#: attribute because parenting must follow the *call* structure, and two
#: concurrent tasks sharing one Tracer must not adopt each other's spans.
_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("paa_current_span", default=None)


def current_span() -> Span | None:
    """The innermost active span in this task, if any."""
    return _CURRENT_SPAN.get()


class SpanStatus:
    """Span outcome. Mirrors the OpenTelemetry status codes."""

    UNSET: Final[str] = "UNSET"
    OK: Final[str] = "OK"
    ERROR: Final[str] = "ERROR"


@dataclass
class Span:
    """One timed unit of work.

    The trace id is a **UUID** in the canonical hyphenated form, and the
    span id is sixteen hex characters. Neither is an OpenTelemetry id:
    a trace id is 128 bits *and* a UUID, and it is the UUID that decides
    how it is spelled, because the two substrates store a trace id
    differently otherwise — SQLite stores the text it was given, and
    PostgreSQL's ``UUID`` column normalises it — and a
    ``trace_id`` written into ``hot_serving_execution_runs`` has to mean
    the same thing whether or not a collector was ever attached.

    The span id is 64 bits and is *not* a UUID, which is why the column
    that holds a parent span id is text on both substrates.
    """

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    start_ns: int = field(default_factory=time.perf_counter_ns)
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = SpanStatus.UNSET
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        """Elapsed time. ``0.0`` while the span is still open."""
        if self.end_ns is None:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000.0

    @property
    def is_finished(self) -> bool:
        return self.end_ns is not None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, **attributes: Any) -> None:
        self.events.append(
            {"name": name, "timestamp_ns": time.perf_counter_ns(), "attributes": attributes}
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. Feeds the ``telemetry`` column."""
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }


def _new_trace_id() -> str:
    """A fresh trace id, in the canonical hyphenated UUID form.

    The hyphenated form, not ``uuid4().hex``: a PostgreSQL ``UUID`` column
    normalises to it on the way in, so a trace id written as 32 hex
    characters would be stored in two different forms by the two
    substrates — and a join between a span and the run that owns it would
    match on one and not the other.
    """
    return str(uuid.uuid4())


def _new_span_id() -> str:
    """A fresh span id.

    Sixteen hex characters, not a UUID: a span id is 64 bits, and it is
    *not* a UUID — the column that holds a parent span id is therefore
    text on both substrates, because a ``UUID`` column would refuse it.
    """
    return uuid.uuid4().hex[:16]  # 64-bit, 16 hex chars


class Tracer:
    """Creates and records spans.

    :param exporter: called with each finished :class:`Span`. Any exception it
        raises is caught and logged — this is the seam the OTLP bridge plugs
        into, and a collector being down is a normal condition, not an error the
        traced operation should learn about.
    :param metrics: when supplied, every finished span's duration is also
        observed into ``span_duration_ms``.
    """

    def __init__(
        self,
        settings: ObservabilitySettings | Settings | None = None,
        *,
        exporter: Callable[[Span], None] | None = None,
        metrics: MetricsRegistry | None = None,
        max_retained_spans: int = _MAX_RETAINED_SPANS,
    ) -> None:
        resolved = getattr(settings, "observability", settings)
        self._service_name = getattr(resolved, "service_name", "paa-runtime")
        self._enabled = bool(getattr(resolved, "enabled", True))
        self._otlp_endpoint = getattr(resolved, "otlp_endpoint", None)

        self._exporter = exporter
        self._metrics = metrics
        self._max_retained = max_retained_spans
        self._finished: list[Span] = []
        self._otel_bridge: Callable[[Span], None] | None = None
        self._otel_attempted = False

    # -- introspection ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def finished_spans(self) -> list[Span]:
        """Retained finished spans, oldest first."""
        return list(self._finished)

    def clear(self) -> None:
        self._finished.clear()

    def spans_for_trace(self, trace_id: str) -> list[Span]:
        return [s for s in self._finished if s.trace_id == trace_id]

    # -- span lifecycle -----------------------------------------------------

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]:
        """Open a span for the duration of the block.

        Nesting is automatic: a span opened inside another inherits its
        ``trace_id`` and takes it as ``parent_span_id``.

        The body's exceptions propagate untouched after being recorded on the
        span. Everything else — id generation, exporting, contextvar bookkeeping
        — is wrapped, so a broken tracer degrades to a no-op rather than
        becoming the reason a task failed.
        """
        span = self._begin(name, attributes)
        token = None
        try:
            token = _CURRENT_SPAN.set(span)
        except Exception as exc:  # pragma: no cover - contextvars do not fail
            log.debug("tracing.context_set_failed", error=str(exc))

        try:
            yield span
        except BaseException as exc:
            # The exception is bound to a *fresh* name so that the *value* is
            # captured rather than the name. Python unbinds the ``except ... as
            # exc`` name when the block exits, so a closure over that name
            # raises NameError if it is ever evaluated after the block — and
            # _safely swallows exceptions, so the span would carry no error at
            # all: the failure would be invisible in the one record an
            # operator reads. The assignment copies the value into a local
            # that is never unbound, so the mark holds however the lambda is
            # called.
            failure = exc
            self._safely(lambda: self._mark_error(span, failure), "mark_error")
            raise
        else:
            self._safely(lambda: self._mark_ok(span), "mark_ok")
        finally:
            if token is not None:
                self._safely(lambda: _CURRENT_SPAN.reset(token), "context_reset")
            self._safely(lambda: self._finish(span), "finish")

    def _begin(self, name: str, attributes: dict[str, Any]) -> Span:
        """Construct a span. Falls back to a detached one on any failure."""
        try:
            parent = _CURRENT_SPAN.get()
            return Span(
                name=name,
                trace_id=parent.trace_id if parent else _new_trace_id(),
                span_id=_new_span_id(),
                parent_span_id=parent.span_id if parent else None,
                attributes={"service.name": self._service_name, **attributes},
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("tracing.begin_failed", span_name=name, error=str(exc))
            return Span(name=name, trace_id=_new_trace_id(), span_id=_new_span_id())

    @staticmethod
    def _mark_ok(span: Span) -> None:
        if span.status == SpanStatus.UNSET:
            span.status = SpanStatus.OK

    @staticmethod
    def _mark_error(span: Span, exc: BaseException) -> None:
        span.status = SpanStatus.ERROR
        # Type plus message, never a full traceback: tracebacks carry local
        # variables into a record that may be exported off-machine.
        span.error = f"{type(exc).__name__}: {exc}"
        span.attributes["error.type"] = type(exc).__name__

    def _finish(self, span: Span) -> None:
        span.end_ns = time.perf_counter_ns()
        if not self._enabled:
            return

        self._finished.append(span)
        if len(self._finished) > self._max_retained:
            del self._finished[: len(self._finished) - self._max_retained]

        if self._metrics is not None:
            self._safely(
                lambda: self._metrics.observe(  # type: ignore[union-attr]
                    "span_duration_ms", span.duration_ms, span=span.name
                ),
                "metrics",
            )

        if self._exporter is not None:
            self._safely(lambda: self._exporter(span), "exporter")  # type: ignore[misc]

        bridge = self._otel()
        if bridge is not None:
            self._safely(lambda: bridge(span), "otel_bridge")

    @staticmethod
    def _safely(action: Callable[[], Any], what: str) -> None:
        """Run ``action``, swallowing and logging anything it raises.

        The module's governing rule, in one place. Logged at debug: a collector
        outage is not the user's problem and should not fill their console, but
        it must be discoverable when someone goes looking for missing traces.
        """
        try:
            action()
        except Exception as exc:
            log.debug("tracing.suppressed_failure", stage=what, error=str(exc))

    # -- OTLP bridge --------------------------------------------------------

    def _otel(self) -> Callable[[Span], None] | None:
        """Build the OTLP bridge once, or give up permanently.

        ``_otel_attempted`` matters: without it, a machine lacking the extra
        would retry a failing import on every single span.
        """
        if self._otel_bridge is not None or self._otel_attempted:
            return self._otel_bridge
        self._otel_attempted = True

        if not self._otlp_endpoint or not self._enabled:
            return None

        try:
            from opentelemetry import trace as otel_trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            log.warning(
                "tracing.otel_unavailable",
                otlp_endpoint=self._otlp_endpoint,
                impact="an OTLP endpoint is configured but opentelemetry is not "
                "installed; tracing continues locally. Install paa[otel].",
            )
            return None

        try:
            provider = TracerProvider(
                resource=Resource.create({"service.name": self._service_name})
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=self._otlp_endpoint))
            )
            otel_tracer = provider.get_tracer(self._service_name)
        except Exception as exc:  # pragma: no cover - requires the extra
            log.warning("tracing.otel_setup_failed", error=str(exc))
            return None

        def bridge(span: Span) -> None:  # pragma: no cover - requires the extra
            with otel_tracer.start_as_current_span(span.name) as otel_span:
                for key, value in span.attributes.items():
                    otel_span.set_attribute(key, value)
                if span.status == SpanStatus.ERROR:
                    otel_span.set_status(
                        otel_trace.Status(otel_trace.StatusCode.ERROR, span.error or "")
                    )

        self._otel_bridge = bridge
        log.info("tracing.otel_bridged", endpoint=self._otlp_endpoint)
        return bridge
