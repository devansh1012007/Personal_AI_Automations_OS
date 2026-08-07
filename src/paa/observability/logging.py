"""structlog configuration and correlation propagation. RFC §10.

Every log line in this runtime is structured. That is not a style preference:
the ledger records *what* happened and the log records *how it went*, and
joining the two after the fact requires the correlation id to be a field rather
than a substring of a sentence.

Which is what the contextvar processor is for. Threading ``correlation_id``
through every call signature down to the log statement would be invasive and
would be forgotten at exactly the depth where it mattered. Instead
:func:`bind_correlation` sets it once per task and
:func:`_inject_correlation` stamps it onto everything logged inside — including
by modules that know nothing about correlation ids.

The module is named ``logging`` inside its package and still imports the
standard library's ``logging`` without incident: Python 3 resolves imports
absolutely, so ``import logging`` here is unambiguous.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from paa.config import ObservabilitySettings, Settings

__all__ = [
    "bind_correlation",
    "clear_correlation",
    "configure_logging",
    "get_correlation_id",
    "get_trace_id",
    "set_trace_id",
]

_CORRELATION_ID: ContextVar[str | None] = ContextVar("paa_correlation_id", default=None)
_TRACE_ID: ContextVar[str | None] = ContextVar("paa_trace_id", default=None)

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def set_trace_id(trace_id: str | None) -> None:
    """Stamp the active trace id onto subsequent log lines in this task."""
    _TRACE_ID.set(trace_id)


def _inject_correlation(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add ``correlation_id`` / ``trace_id`` from the ambient context.

    An explicit keyword at the call site always wins. A caller logging about a
    *different* lineage than the one it is running inside — the recovery sweep
    reporting on the tasks it is repairing, for instance — means it, and having
    the ambient value silently overwrite it would make those lines lie.
    """
    if (correlation_id := _CORRELATION_ID.get()) and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = correlation_id
    if (trace_id := _TRACE_ID.get()) and "trace_id" not in event_dict:
        event_dict["trace_id"] = trace_id
    return event_dict


def configure_logging(
    settings: ObservabilitySettings | Settings | None = None,
    *,
    stream: Any = None,
) -> None:
    """Configure structlog from ``settings``. Idempotent.

    Console renderer when ``log_json`` is false, JSON when true. The default is
    console because this runtime's primary operator is a person at a terminal;
    ``log_json`` is what a log shipper wants, and it is opt-in for the same
    zero-egress reason the OTLP endpoint is.

    Timestamps are ISO-8601 UTC to match every other timestamp the runtime
    persists, so a log line and a ledger row can be ordered against each other
    without a timezone conversion in the middle.
    """
    resolved = getattr(settings, "observability", settings)
    level_name = str(getattr(resolved, "log_level", "INFO")).upper()
    level = _LEVELS.get(level_name, logging.INFO)
    as_json = bool(getattr(resolved, "log_json", False))

    processors: list[Any] = [
        # merge_contextvars first, so anything bound with
        # structlog.contextvars.bind_contextvars is present before the
        # correlation processor decides whether a key is already set.
        structlog.contextvars.merge_contextvars,
        _inject_correlation,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer(sort_keys=True)
        if as_json
        else structlog.dev.ConsoleRenderer(colors=not as_json and _supports_colour(stream))
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        # False so a later reconfigure actually takes effect. The CLI reads
        # flags after the first modules have already imported and bound loggers,
        # and cached bound loggers would silently keep the boot-time level.
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=level, format="%(message)s", stream=stream or sys.stderr)


def _supports_colour(stream: Any) -> bool:
    target = stream or sys.stderr
    try:
        return bool(target.isatty())
    except Exception:  # pragma: no cover - exotic stream objects
        return False


@contextmanager
def bind_correlation(
    correlation_id: Any, trace_id: str | None = None
) -> Iterator[None]:
    """Stamp ``correlation_id`` onto every log line emitted inside the block.

    Restores the previous value on exit rather than clearing it, so nesting
    works: a sub-task that binds its own id does not orphan its parent's when
    it returns.

    Accepts anything stringifiable — the runtime passes
    :class:`~paa.core.types.CorrelationId` (a ``UUID``), while tests and the CLI
    pass strings.
    """
    correlation_token = _CORRELATION_ID.set(str(correlation_id) if correlation_id else None)
    trace_token = _TRACE_ID.set(trace_id) if trace_id is not None else None
    try:
        yield
    finally:
        _CORRELATION_ID.reset(correlation_token)
        if trace_token is not None:
            _TRACE_ID.reset(trace_token)


def clear_correlation() -> None:
    """Drop both ids. For a worker returning to an idle pool."""
    _CORRELATION_ID.set(None)
    _TRACE_ID.set(None)
