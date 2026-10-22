"""The structlog configuration and the correlation id it stamps on.

Why this is tested at all
-------------------------
``paa.observability.logging`` had **no** coverage at all — 169 lines, and
not one of them exercised — while its docstring makes five claims that the
whole observability story rests on:

* the correlation id is *"a field rather than a substring of a
  sentence"*;
* *"An explicit keyword at the call site always wins"*;
* *"Restores the previous value on exit rather than clearing it"*;
* the timestamps are **ISO-8601 UTC**;
* JSON rendering is **opt-in**.

None of them was checked, and all of them are things a reader of a log
would assume without asking. The first one in particular is the reason the
module exists: joining a log line to a ledger row *after the fact* needs
the correlation id to be a field, because a substring is a thing you grep
for and a field is a thing you query by.

The name collision
------------------
The module is named ``logging`` inside its package and still imports the
standard library's ``logging`` without incident: Python 3 resolves imports
absolutely, so ``import logging`` here is unambiguous.

That is also the trap. The import system consults ``sys.modules`` *before*
it consults the path, so a test that shadows ``sys.modules["logging"]``
breaks this module — it hands the module a fake ``logging`` and the
module's own ``logging.DEBUG`` lookups then fail. So the suite never
shadows it in this interpreter; the demonstration of the trap runs in a
child, where a broken import costs nothing.
"""

from __future__ import annotations

import io
import json
import logging as stdlib_logging
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import structlog

from paa.config import ObservabilitySettings
from paa.observability import logging as paa_logging
from paa.observability.logging import (
    bind_correlation,
    clear_correlation,
    configure_logging,
    get_correlation_id,
    get_trace_id,
    set_trace_id,
)

#: The logger the tests log through. A lazy proxy, exactly like every
#: other module's — so it picks up whatever configuration is in force
#: when the call is made, which is the point of the fixture below.
log = structlog.get_logger(__name__)


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Put structlog's global configuration back after every test.

    :func:`configure_logging` reconfigures structlog **globally** — that
    is structlog's API, and there is no other way to configure it. A
    test that called it without restoring would leave every later test
    in the session logging through the renderer and the level it chose.
    So the configuration is saved on the way in and put back on the way
    out, whatever the test did in between.
    """
    saved = structlog.get_config().copy()
    root_level = stdlib_logging.getLogger().level
    try:
        yield
    finally:
        structlog.configure(**saved)
        stdlib_logging.getLogger().setLevel(root_level)
        structlog.contextvars.clear_contextvars()
        clear_correlation()


def _json_lines(buffer: io.StringIO) -> list[dict[str, object]]:
    """The lines a JSON renderer wrote, parsed."""
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


class TestTheCorrelationIdIsAField:
    """*"The correlation id [is] a field rather than a substring of a
    sentence."*"""

    def test_the_id_is_a_field_of_the_record(self) -> None:
        """The claim, as it appears to a reader of the log.

        The id is a **key** in the record, beside the message — not
        text inside the message. A reader who wants every line of one
        lineage asks for the field; a reader who wants the sentence gets
        the sentence.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        with bind_correlation("correlation-123"):
            log.info("the task finished")

        record = _json_lines(buffer)[0]
        assert record["correlation_id"] == "correlation-123"
        assert "correlation-123" not in str(record["event"])

    def test_a_line_logged_outside_a_binding_carries_no_id(self) -> None:
        """No ambient id, no field.

        The field is *stamped*, not defaulted: an id that was never
        bound is absent from the record rather than present and empty,
        so "this line belongs to no lineage" and "this line belongs to
        the lineage with the empty id" stay different.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        log.info("nobody's line")

        record = _json_lines(buffer)[0]
        assert "correlation_id" not in record

    def test_the_trace_id_is_stamped_the_same_way(self) -> None:
        """Both ids, both as fields."""
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        with bind_correlation("correlation-123", "trace-456"):
            log.info("a span finished")

        record = _json_lines(buffer)[0]
        assert record["correlation_id"] == "correlation-123"
        assert record["trace_id"] == "trace-456"


class TestTheCallSiteWins:
    """*"An explicit keyword at the call site always wins."*"""

    def test_an_explicit_correlation_id_is_not_overwritten(self) -> None:
        """A caller reporting on a *different* lineage means it.

        The recovery sweep reporting on the tasks it is repairing is the
        case the docstring names: it is running inside one lineage and
        talking about another, and having the ambient value silently
        overwrite the explicit one would make those lines lie.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        with bind_correlation("ambient"):
            log.info("about another lineage", correlation_id="explicit")

        record = _json_lines(buffer)[0]
        assert record["correlation_id"] == "explicit"

    def test_an_explicit_trace_id_is_not_overwritten(self) -> None:
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        with bind_correlation("ambient", "ambient-trace"):
            log.info("about another span", trace_id="explicit-trace")

        record = _json_lines(buffer)[0]
        assert record["trace_id"] == "explicit-trace"

    def test_a_contextvar_binding_also_wins(self) -> None:
        """The processor order is part of the contract.

        ``merge_contextvars`` runs first, so a ``correlation_id`` bound
        through structlog's own context variables is already in the
        record by the time the correlation processor decides whether the
        key is taken — and it is taken. Reordering the two processors
        would silently hand the ambient id the field instead.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        structlog.contextvars.bind_contextvars(correlation_id="from-contextvars")
        with bind_correlation("ambient"):
            log.info("a line")

        record = _json_lines(buffer)[0]
        assert record["correlation_id"] == "from-contextvars"


class TestTheBindingIsRestored:
    """*"Restores the previous value on exit rather than clearing it."*"""

    def test_the_previous_correlation_id_comes_back(self) -> None:
        """Nesting works.

        A sub-task that binds its own id must not orphan its parent's
        when it returns — the parent is still running, and its next log
        line belongs to the parent's lineage.
        """
        with bind_correlation("outer"):
            assert get_correlation_id() == "outer"

            with bind_correlation("inner"):
                assert get_correlation_id() == "inner"

            assert get_correlation_id() == "outer"

        assert get_correlation_id() is None

    def test_the_previous_trace_id_comes_back(self) -> None:
        """The same rule, for the second id."""
        with bind_correlation("outer", "outer-trace"):
            with bind_correlation("inner", "inner-trace"):
                assert get_trace_id() == "inner-trace"

            assert get_trace_id() == "outer-trace"

        assert get_trace_id() is None

    def test_a_binding_that_mentions_no_trace_id_leaves_the_ambient_one_alone(
        self,
    ) -> None:
        """Omission is not a value, here too.

        A nested binding that says nothing about the trace is not saying
        "there is no trace" — it is saying nothing, and the trace the
        outer scope established still belongs to the lines inside.
        """
        with bind_correlation("outer", "outer-trace"):
            with bind_correlation("inner"):
                assert get_trace_id() == "outer-trace"

            assert get_trace_id() == "outer-trace"

    def test_a_uuid_is_accepted_and_becomes_its_canonical_text(self) -> None:
        """The runtime passes a ``CorrelationId``; the CLI passes a string.

        Both are accepted, and both come back as text — which is what
        the record carries.
        """
        correlation = uuid.uuid4()

        with bind_correlation(correlation):
            assert get_correlation_id() == str(correlation)

    def test_clear_correlation_drops_both_ids(self) -> None:
        """A worker returning to an idle pool.

        Not a context manager, and not a restore: it drops both ids, so
        the next line the worker writes belongs to nobody until it binds
        again.
        """
        with bind_correlation("correlation-123", "trace-456"):
            clear_correlation()

            assert get_correlation_id() is None
            assert get_trace_id() is None

    def test_set_trace_id_stamps_the_trace_without_a_correlation(self) -> None:
        """The trace id can be set on its own.

        The two ids are independent: a tracer knows the trace long
        before it knows the lineage, and the reverse is true of the
        recovery sweep.
        """
        set_trace_id("trace-456")

        assert get_trace_id() == "trace-456"
        assert get_correlation_id() is None


class TestTheRendering:
    """ISO-8601 UTC timestamps, and JSON that is opt-in."""

    def test_the_timestamp_is_iso_8601_utc(self) -> None:
        """Every other timestamp the runtime persists is ISO-8601 UTC.

        So a log line and a ledger row can be ordered against each other
        without a timezone conversion in the middle — and the ``Z`` is
        what says "UTC" rather than leaving a reader to guess the
        offset.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        log.info("a line")

        timestamp = str(_json_lines(buffer)[0]["timestamp"])
        assert timestamp.endswith("Z")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == UTC.utcoffset(None)

    def test_json_rendering_is_opt_in(self) -> None:
        """The default is the console.

        This runtime's primary operator is a person at a terminal, and
        ``log_json`` is what a log shipper wants — so it is opt-in for
        the same zero-egress reason the OTLP endpoint is.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(), stream=buffer)

        log.info("a line")

        output = buffer.getvalue().strip()
        assert output
        with pytest.raises(json.JSONDecodeError):
            json.loads(output)

    def test_json_rendering_is_turned_on_by_the_setting(self) -> None:
        """And it is one setting away."""
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        log.info("a line", extra="value")

        record = _json_lines(buffer)[0]
        assert record["event"] == "a line"
        assert record["extra"] == "value"
        assert record["level"] == "info"

    def test_the_level_is_the_one_that_was_configured(self) -> None:
        """A line below the level is not written at all.

        Not written down to the level and filtered by the reader: the
        record is not there, which is the difference between a log that
        is quiet and a log that is large.
        """
        buffer = io.StringIO()
        configure_logging(
            ObservabilitySettings(log_level="WARNING", log_json=True), stream=buffer
        )

        log.info("below the level")
        log.warning("at the level")

        records = _json_lines(buffer)
        assert [record["event"] for record in records] == ["at the level"]

    def test_configuring_twice_leaves_the_second_configuration_in_force(self) -> None:
        """The function is idempotent, and the later call is the one that counts.

        The CLI reads its flags after the first modules have already
        imported and bound loggers, so a reconfiguration has to actually
        take effect rather than be swallowed by a cache.
        """
        buffer = io.StringIO()
        configure_logging(ObservabilitySettings(log_json=False), stream=buffer)
        configure_logging(ObservabilitySettings(log_json=True), stream=buffer)

        log.info("a line")

        assert _json_lines(buffer)[0]["event"] == "a line"


class TestTheNameCollision:
    """The module is named ``logging`` and imports the standard library's.

    Python 3 resolves imports absolutely, so the import inside the
    package is unambiguous — the two modules are different objects with
    different keys in ``sys.modules``.
    """

    def test_the_module_is_not_the_standard_library(self) -> None:
        assert paa_logging is not stdlib_logging
        assert sys.modules["paa.observability.logging"] is paa_logging
        assert sys.modules["logging"] is stdlib_logging

    def test_the_module_resolved_the_standard_library(self) -> None:
        """The name inside the module is the standard library's.

        Not a relative import, not a copy, not a shim: the same object
        the rest of the process has.
        """
        assert paa_logging.logging is stdlib_logging

    def test_shadowing_the_standard_library_breaks_the_module(self) -> None:
        """The trap, demonstrated in a child interpreter.

        The import system consults ``sys.modules`` before it consults
        the path, so a fake ``logging`` installed under that name is
        what ``import logging`` finds. The module's own
        ``logging.DEBUG`` lookups then fail, and the failure is an
        ``AttributeError`` about a module attribute — which is exactly
        the kind of confusing failure that makes the trap worth
        writing down.

        In a child, because doing it in this interpreter would poison
        the module for every later test in the session.
        """
        script = textwrap.dedent(
            """
            import sys
            import types

            sys.modules["logging"] = types.ModuleType("logging")
            try:
                import paa.observability.logging
            except BaseException as exc:
                print("BROKE", type(exc).__name__)
            else:
                print("SURVIVED")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert "BROKE" in completed.stdout, (
            f"a shadowed 'logging' did not break the module: {completed.stdout!r} "
            f"{completed.stderr!r}"
        )

    def test_a_fresh_interpreter_resolves_the_standard_library(self) -> None:
        """The other half, also in a child.

        Nothing shadowed, the module imports and binds the standard
        library — which is the property the shadowing test above takes
        away.
        """
        script = textwrap.dedent(
            """
            import importlib
            import sys

            module = importlib.import_module("paa.observability.logging")
            print("OK" if module.logging is sys.modules["logging"] else "WRONG")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert "OK" in completed.stdout, completed.stdout + completed.stderr
