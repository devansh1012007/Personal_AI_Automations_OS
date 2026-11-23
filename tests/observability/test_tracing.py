"""The tracer — and the rule it must never break.

Why this is tested at all
-------------------------
``paa.observability.tracing`` had no coverage, and it is the module with the
strongest promise in the observability subsystem: **a tracing failure must
never break the operation being traced**. A span tree is a diagnostic aid; an
exporter that cannot reach its collector, a serialisation error on an
attribute, or a bug in the tracer itself must not be able to fail a user's
task.

The one that bites hardest
--------------------------
The span's error is recorded in the ``except`` block of an
``@asynccontextmanager``, and Python *unbinds* the ``except ... as exc``
name when that block exits. A reference to it that is evaluated later — from
a closure — raises ``NameError``. And :meth:`Tracer._safely` swallows
exceptions, so that ``NameError`` would be swallowed too: every failed span
would be silently left marked ``OK``. A failed operation recorded as a
successful one is worse than no record at all, so the binding has to survive
the block.

The tests below pin that, and the module's other promises.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from paa.config import ObservabilitySettings
from paa.observability.metrics import MetricsRegistry
from paa.observability.tracing import Span, SpanStatus, Tracer, current_span


class TestAFailedSpan:
    """The span records the failure, and the failure still propagates."""

    async def test_the_exception_is_attached_to_the_span(self) -> None:
        tracer = Tracer()

        with pytest.raises(ValueError, match="kaboom"):
            async with tracer.span("risky") as span:
                raise ValueError("kaboom")

        assert span.status == SpanStatus.ERROR
        assert span.error == "ValueError: kaboom"
        assert span.attributes["error.type"] == "ValueError"

    async def test_a_failed_span_is_never_marked_ok(self) -> None:
        """The property the ``except``-block binding exists to protect.

        A span that failed and then reports ``OK`` is a lie in the one
        record an operator will look at. If the exception's name were
        unbound before the span was marked, this assertion is what would
        notice.
        """
        tracer = Tracer()

        with pytest.raises(RuntimeError):
            async with tracer.span("risky") as span:
                raise RuntimeError("nope")

        assert span.status != SpanStatus.OK
        assert span.status == SpanStatus.ERROR

    async def test_the_caller_exception_is_untouched(self) -> None:
        """The tracer records the failure; it does not replace it.

        The exception the caller raised is the one the caller catches — the
        same type, the same message, the same object.
        """
        tracer = Tracer()
        original = ValueError("kaboom")

        with pytest.raises(ValueError) as caught:
            async with tracer.span("risky"):
                raise original

        assert caught.value is original

    async def test_a_base_exception_is_recorded_too(self) -> None:
        """``BaseException`` is what the block catches, and it is caught on purpose.

        A cancellation that escaped unrecorded would leave a span open
        forever — no end time, no status — and the ring buffer holding it.
        The span is finished on the way out either way.
        """
        tracer = Tracer()

        with pytest.raises(KeyboardInterrupt):
            async with tracer.span("interrupted") as span:
                raise KeyboardInterrupt

        assert span.status == SpanStatus.ERROR
        assert span.is_finished


class TestASuccessfulSpan:
    async def test_a_clean_span_is_ok_and_finished(self) -> None:
        tracer = Tracer()

        async with tracer.span("work", task="demo") as span:
            assert span.status == SpanStatus.UNSET

        assert span.status == SpanStatus.OK
        assert span.error is None
        assert span.is_finished
        assert span.attributes["task"] == "demo"
        assert span.duration_ms >= 0.0

    async def test_the_service_name_is_on_every_span(self) -> None:
        tracer = Tracer()

        async with tracer.span("work") as span:
            pass

        assert span.attributes["service.name"] == "paa-runtime"

    async def test_a_finished_span_is_retained(self) -> None:
        tracer = Tracer()

        async with tracer.span("one") as span:
            pass

        assert [s.name for s in tracer.finished_spans] == ["one"]
        assert tracer.spans_for_trace(span.trace_id)[0] is span


class TestNesting:
    async def test_a_child_inherits_the_trace_and_names_its_parent(self) -> None:
        tracer = Tracer()

        async with tracer.span("parent") as parent, tracer.span("child") as child:
            pass

        assert child.trace_id == parent.trace_id
        assert child.parent_span_id == parent.span_id
        assert parent.parent_span_id is None

    async def test_the_current_span_follows_the_call_structure(self) -> None:
        tracer = Tracer()

        assert current_span() is None
        async with tracer.span("outer") as outer:
            assert current_span() is outer
            async with tracer.span("inner") as inner:
                assert current_span() is inner
            assert current_span() is outer
        assert current_span() is None


class TestTheGoverningRule:
    """A tracing failure must never break the operation being traced."""

    async def test_an_exporter_that_raises_does_not_break_the_body(self) -> None:
        def exploding_exporter(span: Span) -> None:
            raise RuntimeError("the collector is down")

        tracer = Tracer(exporter=exploding_exporter)
        reached = False

        async with tracer.span("work") as span:
            reached = True

        assert reached is True
        assert span.status == SpanStatus.OK
        assert span.is_finished

    async def test_a_metrics_registry_that_raises_does_not_break_the_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = MetricsRegistry()

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("no metrics today")

        monkeypatch.setattr(registry, "observe", boom)
        tracer = Tracer(metrics=registry)
        reached = False

        async with tracer.span("work") as span:
            reached = True

        assert reached is True
        assert span.status == SpanStatus.OK

    async def test_a_disabled_tracer_still_finishes_the_span(self) -> None:
        """``enabled=False`` stops the *export*, not the bookkeeping.

        A span that is never finished has no duration and no status, and
        the ring buffer holds it forever.
        """
        tracer = Tracer(ObservabilitySettings(enabled=False))

        async with tracer.span("work") as span:
            pass

        assert span.is_finished
        assert span.status == SpanStatus.OK
        assert tracer.finished_spans == []


class TestTheIdentity:
    async def test_the_trace_id_is_the_hyphenated_uuid_form(self) -> None:
        """Both substrates store a trace id the same way.

        SQLite stores the text it was given and PostgreSQL's ``UUID`` column
        normalises it, so a trace id written as 32 hex characters would be
        stored in two different forms by the two substrates — and a join
        between a span and the run that owns it would match on one and not
        the other.
        """
        tracer = Tracer()

        async with tracer.span("work") as span:
            pass

        assert str(uuid.UUID(span.trace_id)) == span.trace_id
        assert len(span.span_id) == 16


class TestTheBaseExceptionBoundary:
    """Where the governing rule stops, and why that is the right place.

    The module's rule is that a tracing failure must never break the
    operation being traced, and :meth:`Tracer._safely` is where the rule
    is implemented — with one deliberate hole. It catches ``Exception``,
    not ``BaseException``, so an exporter that raises ``KeyboardInterrupt``
    (or ``SystemExit``, or a cancellation that arrives as one)
    propagates out of the span and out of the traced operation.

    The hole is right, and it is worth saying why. ``KeyboardInterrupt``
    and ``SystemExit`` are how a process is *told to stop*, and
    swallowing them would produce a runtime that cannot be interrupted;
    swallowing a ``CancelledError`` would break the event loop's
    cancellation entirely. A rule that a telemetry failure never
    interrupts the user has stopped being a safety rule and become a
    way of ignoring the user.

    What was missing was not the behaviour but the *statement* of it.
    The module's docstring claimed that the only exception
    :meth:`Tracer.span` ever propagates is the one raised by the
    caller's own body — and that claim is false for a
    ``BaseException``. So the boundary is pinned here, and the
    docstring corrected to say what is true.
    """

    async def test_an_exporter_that_interrupts_propagates(self) -> None:
        """The boundary, from the outside.

        The interruption reaches the caller rather than being logged and
        forgotten, and the caller's own code is the thing that decides
        what to do about it.
        """

        def interrupting_exporter(span: Span) -> None:
            raise KeyboardInterrupt("the operator pressed ctrl-c")

        tracer = Tracer(exporter=interrupting_exporter)

        with pytest.raises(KeyboardInterrupt):
            async with tracer.span("work"):
                pass

    async def test_the_span_is_still_finished_and_retained_when_the_exporter_interrupts(
        self,
    ) -> None:
        """The interruption does not leave the span open.

        The span's end time and status are set before the exporter is
        called, so an interrupted span is still a *record*: it is the
        operation that was interrupted, not the bookkeeping that
        records it.
        """

        def interrupting_exporter(span: Span) -> None:
            raise KeyboardInterrupt

        tracer = Tracer(exporter=interrupting_exporter)

        with pytest.raises(KeyboardInterrupt):
            async with tracer.span("work") as span:
                pass

        assert span.is_finished
        assert span.status == SpanStatus.OK
        assert [s.name for s in tracer.finished_spans] == ["work"]

    async def test_an_ordinary_failure_from_the_exporter_does_not_replace_the_bodies(
        self,
    ) -> None:
        """The other half of the asymmetry, and the one the rule is about.

        An ``Exception`` from the exporter is swallowed, so the body's
        own exception is the one the caller catches — the same type, the
        same message, the same object.
        """

        def failing_exporter(span: Span) -> None:
            raise RuntimeError("the collector is down")

        tracer = Tracer(exporter=failing_exporter)
        original = ValueError("kaboom")

        with pytest.raises(ValueError) as caught:
            async with tracer.span("work"):
                raise original

        assert caught.value is original

    async def test_an_interruption_replaces_the_bodies_own_exception(self) -> None:
        """The asymmetry, stated exactly.

        The body's exception is raised from the ``except`` block and the
        exporter's from the ``finally`` block, and a ``finally`` block
        that raises replaces the exception in flight. So a
        ``BaseException`` from the exporter wins over the body's
        ``Exception`` — the opposite of what the rule does for ordinary
        exceptions.

        That is the price of the hole, and it is a price worth paying
        only because the hole is small: it takes an exporter that
        raises ``KeyboardInterrupt``, which is not something a collector
        outage does.
        """

        def interrupting_exporter(span: Span) -> None:
            raise KeyboardInterrupt

        tracer = Tracer(exporter=interrupting_exporter)

        with pytest.raises(KeyboardInterrupt):
            async with tracer.span("work") as span:
                raise ValueError("kaboom")

        # The span still records what happened to the body.
        assert span.status == SpanStatus.ERROR
        assert span.error == "ValueError: kaboom"


class TestTheRetainedSpans:
    """The bounded ring of finished spans."""

    async def test_the_ring_keeps_the_newest_spans_and_drops_the_oldest(self) -> None:
        """The same bound the histograms have, for the same reason.

        This process is expected to run for weeks, so a span ring that
        only grew would be a slow memory leak in a process designed to
        run for weeks on a machine with ~3.5 GB free.
        """
        tracer = Tracer(max_retained_spans=3)

        for index in range(5):
            async with tracer.span(f"span-{index}"):
                pass

        assert [span.name for span in tracer.finished_spans] == [
            "span-2",
            "span-3",
            "span-4",
        ]

    async def test_the_retained_spans_are_a_copy(self) -> None:
        """The list a caller gets is not the ring itself.

        A caller iterating the list while spans are being finished would
        otherwise be iterating a list that changes under it.
        """
        tracer = Tracer()
        async with tracer.span("one"):
            pass

        retained = tracer.finished_spans
        retained.clear()

        assert [span.name for span in tracer.finished_spans] == ["one"]

    async def test_clear_empties_the_ring(self) -> None:
        """The ring can be emptied, and the tracer keeps working.

        A worker returning to an idle pool wants the previous run's
        spans out of the way; the tracer itself is not affected.
        """
        tracer = Tracer()
        async with tracer.span("one"):
            pass
        async with tracer.span("two"):
            pass

        tracer.clear()

        assert tracer.finished_spans == []

        async with tracer.span("three"):
            pass

        assert [span.name for span in tracer.finished_spans] == ["three"]

    async def test_the_spans_of_a_trace_are_grouped_together(self) -> None:
        """One trace, many spans — and the grouping is by trace id.

        This is the join the run row makes: a run row carries a trace
        id, and the spans of that trace are the ones that ran inside
        it.

        The order is **finish** order, not start order, and the inner
        span finishes first because it is nested — so the ring reads
        ``inner`` then ``outer``. "Oldest first" in the ring's
        docstring means oldest *finished*, which is the order a
        duration-sorted reader wants.
        """
        tracer = Tracer()

        async with tracer.span("outer") as outer, tracer.span("inner") as inner:
            pass

        other = Tracer()
        async with other.span("elsewhere"):
            pass

        assert [span.name for span in tracer.spans_for_trace(outer.trace_id)] == [
            "inner",
            "outer",
        ]
        assert inner.trace_id == outer.trace_id
        assert tracer.spans_for_trace("not-a-trace") == []


# ---------------------------------------------------------------------------
# Context propagation across concurrent tasks
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """The ContextVar's whole reason for being, as a behaviour.

    ``_CURRENT_SPAN`` is documented as *"per async task"* because parenting
    must follow the *call* structure, and two concurrent tasks sharing one
    Tracer must not adopt each other's spans. Nothing tested that: every
    nesting test above ran its spans sequentially in one task, where a
    plain instance attribute — or a module global — would have passed
    just as well. The distinction only exists while two tasks are live
    at once.
    """

    async def test_concurrent_children_of_one_parent_do_not_adopt_each_other(
        self,
    ) -> None:
        """Two sibling spans open at the same moment, each its own current span.

        The assertion runs *inside* the body, while both children are
        live: the current span of each task is its own child, not the
        sibling that happens to be open beside it. A tracer that kept the
        active span on the Tracer (rather than per task) would hand the
        first-finished child's span — or the sibling's — to the other
        task's ``_begin``, and the parentage of one lineage would be
        decided by scheduling order.
        """
        tracer = Tracer()

        async def work(tag: str) -> Span:
            async with tracer.span(f"child-{tag}") as span:
                await asyncio.sleep(0)  # one yield, so both are open together
                assert current_span() is span
                return span

        async with tracer.span("parent") as parent:
            first, second = await asyncio.gather(work("a"), work("b"))

        assert first.parent_span_id == parent.span_id
        assert second.parent_span_id == parent.span_id
        assert first.span_id != second.span_id
        assert first.trace_id == second.trace_id == parent.trace_id

    async def test_a_task_started_outside_a_span_keeps_its_own_trace(self) -> None:
        """Parenting follows the context at creation, not the await.

        A task created *before* the enclosing span opens runs with a copy
        of the then-current context — no span in it — so its span gets a
        fresh trace id, however deeply the caller that awaits it is later
        nested. The alternative, parenting by await, would splice an
        unrelated operation into the caller's trace and make the trace
        tree lie about what ran inside it.
        """
        tracer = Tracer()

        async def detached() -> Span:
            async with tracer.span("detached") as span:
                return span

        task = asyncio.create_task(detached())
        async with tracer.span("enclosing") as enclosing:
            outside = await task

        assert outside.trace_id != enclosing.trace_id
        assert outside.parent_span_id is None


# ---------------------------------------------------------------------------
# The span record — the published JSON shape
# ---------------------------------------------------------------------------


class TestTheSpanRecord:
    """``Span.to_dict`` feeds the ``telemetry`` column, so it is a published
    contract rather than an internal detail: the keys, the grouping of the
    attributes, and the rounding of the duration are the shape a reader of
    a run row joins against.
    """

    async def test_the_finished_record_is_the_documented_shape(self) -> None:
        tracer = Tracer()

        async with tracer.span("work", k="v") as span:
            span.set_attribute("attr", 7)
            span.add_event("checkpoint", phase="mid")

        record = span.to_dict()

        assert set(record) == {
            "name",
            "trace_id",
            "span_id",
            "parent_span_id",
            "duration_ms",
            "status",
            "error",
            "attributes",
            "events",
        }
        assert record["name"] == "work"
        assert record["trace_id"] == span.trace_id
        assert record["span_id"] == span.span_id
        assert record["parent_span_id"] is None
        assert record["status"] == "OK"
        assert record["error"] is None
        assert record["attributes"] == {"service.name": "paa-runtime", "k": "v", "attr": 7}
        assert record["duration_ms"] == round(span.duration_ms, 3)
        assert len(record["events"]) == 1
        event = record["events"][0]
        assert set(event) == {"name", "timestamp_ns", "attributes"}
        assert event["name"] == "checkpoint"
        assert event["attributes"] == {"phase": "mid"}
        assert isinstance(event["timestamp_ns"], int)
        assert json.dumps(record)

    async def test_an_open_span_reports_a_zero_duration(self) -> None:
        """The documented degradation for a span that has not ended.

        ``duration_ms`` is documented as *0.0 while the span is still
        open*, and the record carries the same zero — so an operation
        that never reported (a process killed mid-span) reads as "no
        time accounted" in the record, not as an error and not as a
        negative number. A span cannot be ended twice through the
        public API (the context manager's ``finally`` ends it exactly
        once), so the never-ended shape is the one a reader can
        actually meet.
        """
        tracer = Tracer()

        async with tracer.span("held") as span:
            assert span.duration_ms == 0.0
            assert span.to_dict()["duration_ms"] == 0.0

    async def test_the_child_names_the_parent_in_the_record(self) -> None:
        tracer = Tracer()

        async with tracer.span("parent") as parent, tracer.span("child") as child:
            pass

        assert child.to_dict()["parent_span_id"] == parent.span_id
        assert parent.to_dict()["parent_span_id"] is None


class TestTheMetricsIntegration:
    """*"When supplied, every finished span's duration is also observed into
    ``span_duration_ms``"* — the Tracer's second output, beside the ring.
    """

    async def test_a_finished_span_observes_its_duration_into_the_registry(self) -> None:
        registry = MetricsRegistry()
        tracer = Tracer(metrics=registry)

        async with tracer.span("work"):
            pass

        histogram = registry.get_histogram("span_duration_ms", span="work")
        assert histogram is not None
        assert histogram.count == 1
        assert set(registry.snapshot()["histograms"]) == {"span_duration_ms{span=work}"}

    async def test_a_failed_span_observes_its_duration_too(self) -> None:
        """A failure that took 30 seconds is exactly the observation worth having.

        Dropping it would make an error path look free — the same reason
        :meth:`MetricsRegistry.timer` records on the way out of a raising
        block.
        """
        registry = MetricsRegistry()
        tracer = Tracer(metrics=registry)

        with pytest.raises(ValueError, match="kaboom"):
            async with tracer.span("failing"):
                raise ValueError("kaboom")

        histogram = registry.get_histogram("span_duration_ms", span="failing")
        assert histogram is not None
        assert histogram.count == 1


class TestTheExporter:
    """The seam the OTLP bridge plugs into.

    An exporter is documented as *"called with each finished Span"* — which
    one, when, and how often is the bridge's entire contract, and a wrong
    answer here is a collector that receives half a trace.
    """

    async def test_the_exporter_receives_the_finished_span(self) -> None:
        exported: list[Span] = []
        tracer = Tracer(exporter=exported.append)

        async with tracer.span("work") as span:
            pass

        assert exported == [span]
        assert exported[0].is_finished

    async def test_a_disabled_tracer_does_not_export(self) -> None:
        """``enabled=False`` stops the export, not the bookkeeping.

        The span is still timed and still finished (pinned above); what
        the switch turns off is everything *downstream* of the record —
        the ring, the metrics, the exporter.
        """
        exported: list[Span] = []
        tracer = Tracer(ObservabilitySettings(enabled=False), exporter=exported.append)

        assert tracer.enabled is False

        async with tracer.span("work"):
            pass

        assert exported == []


class TestTheServiceName:
    async def test_the_service_name_comes_from_the_settings(self) -> None:
        """The attribute on every span is the configured service, not a constant."""
        tracer = Tracer(ObservabilitySettings(service_name="paa-doctor"))

        assert tracer.service_name == "paa-doctor"
        async with tracer.span("work") as span:
            pass

        assert span.attributes["service.name"] == "paa-doctor"
