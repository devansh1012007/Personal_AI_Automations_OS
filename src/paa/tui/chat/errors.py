"""The chat error taxonomy: classify, then microcopy.

SPEC 2 §3.2's rule: **microcopy is what happened + why + next action**, and
no raw traceback ever reaches the screen. The service layer raises whatever
the substrate raises; this module is where the chat surface decides what the
operator sees. Four categories, one function each:

* ``model_retryable`` — timeout / rate-limit / provider unavailable. The app
  retries inline with backoff and opens a circuit breaker after three
  consecutive failures.
* ``model_permanent`` — configuration (unknown model name, credential shape).
  A doctor hint, never a retry.
* ``policy`` — a named rule refused the action. The microcopy carries the
  rule name and how clearance is granted.
* ``storage`` / ``session`` — substrate trouble and unknown-session trouble.

The classification is structural (exception *type*), not string sniffing —
one exception type per category is the substrate's own vocabulary:
:class:`~paa.models.base.ModelUnavailableError` is already "the routable
failure" by its own docstring, :class:`~paa.core.errors.PolicyViolation`
carries ``.rule``, and :class:`~paa.core.errors.StorageError` /
:class:`SessionNotFoundError <paa.chat.session.SessionNotFoundError>` are
the ledger/substrate pair. Cancellation is not an error at all — the caller
asks for it — but it is classified so the same microcopy path renders it.
"""

from __future__ import annotations

import asyncio
import enum

from paa.chat.session import SessionNotFoundError
from paa.core.errors import PolicyViolation, StorageError
from paa.models.base import ModelUnavailableError
from paa.pipelines.base import CapabilityNotGrantedError

__all__ = ["ErrorKind", "classify", "microcopy"]


class ErrorKind(str, enum.Enum):
    """The four taxonomy categories plus the two non-error outcomes."""

    MODEL_RETRYABLE = "model_retryable"
    MODEL_PERMANENT = "model_permanent"
    POLICY = "policy"
    STORAGE = "storage"
    SESSION = "session"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def classify(exc: BaseException) -> ErrorKind:
    """Map one exception onto the taxonomy. Total over any exception."""
    if isinstance(exc, asyncio.CancelledError):
        return ErrorKind.CANCELLED
    if isinstance(exc, (PolicyViolation, CapabilityNotGrantedError)):
        return ErrorKind.POLICY
    if isinstance(exc, (StorageError, SessionNotFoundError)):
        return ErrorKind.STORAGE if isinstance(exc, StorageError) else ErrorKind.SESSION
    if isinstance(exc, ModelUnavailableError):
        return ErrorKind.MODEL_RETRYABLE
    # Transport-shaped failures (timeout, connection, rate limit) are the
    # provider layer's retryable set; httpx is a core dependency but its
    # class names are checked lazily so this module stays cheap to import.
    for name in ("TimeoutException", "ConnectError", "ReadError", "WriteError"):
        candidate = _httpx_error(name)
        if candidate is not None and isinstance(exc, candidate):
            return ErrorKind.MODEL_RETRYABLE
    if isinstance(exc, TimeoutError):
        return ErrorKind.MODEL_RETRYABLE
    return ErrorKind.UNKNOWN


def _httpx_error(name: str) -> type[Exception] | None:
    """One httpx transport error class, or ``None`` (httpx never imported here)."""
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a core dependency
        return None
    return getattr(httpx, name, None)


def _reason(exc: BaseException) -> str:
    """A one-line, bounded reason. Redaction is the raise site's job (the
    ledger and :mod:`paa.models.base` already scrub); here it is only bounded
    so the message stream stays one line."""
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= 160 else text[:157] + "…"


def microcopy(kind: ErrorKind, exc: BaseException) -> str:
    """What happened + why + next action, per the spec's microcopy rule."""
    reason = _reason(exc)
    if kind is ErrorKind.CANCELLED:
        return (
            "turn stopped — you cancelled it (Esc); the ledger recorded "
            "CHAT_TURN_CANCELLED, so a replay shows the stop honestly"
        )
    if kind is ErrorKind.MODEL_RETRYABLE:
        return (
            f"model error, retrying — {reason}. The model seam retries with "
            "backoff; after 3 straight failures it opens a circuit and asks "
            "you to run /doctor"
        )
    if kind is ErrorKind.MODEL_PERMANENT:
        return (
            f"model configuration error — {reason}. Next: /model echo to fall "
            "back offline, or check the provider row in PAA_MODELS__*"
        )
    if kind is ErrorKind.POLICY:
        rule = getattr(exc, "rule", None) or getattr(exc, "capability", "policy")
        return (
            f"denied by policy — rule {rule!r} refused the action ({reason}). "
            "Next: ask the operator to grant it (PAA_POLICY__MODE / the "
            "session mandate); the denial is on the ledger either way"
        )
    if kind is ErrorKind.STORAGE:
        return (
            f"storage error — {reason}. The turn was not lost: the ledger "
            "holds every row written before the failure. Next: /doctor, or "
            "check the relational backend"
        )
    if kind is ErrorKind.SESSION:
        return (
            f"session error — {reason}. Next: /sessions to pick a live one, "
            "or /new to start over"
        )
    return (
        f"unexpected error — {reason}. Next: F12 opens the log drawer; the "
        "structured trace of this turn is in PAA_HOME/logs/chat.log"
    )
