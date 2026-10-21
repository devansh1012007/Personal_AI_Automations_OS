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
