"""Resilience at the chat model seam: inline retry with backoff, circuit breaker.

SPEC 2 §3.2's error taxonomy: ``ModelError`` (retryable: timeout / rate-limit)
gets inline retry with backoff and a circuit breaker after three consecutive
failures. The right layer for that is the *model seam*, not the turn loop:
:func:`paa.chat.loop.ChatLoop` records one ledger row per turn, so a retry
above the seam would write a second ``PROPOSED`` for the same operator
action, while a retry *inside* :meth:`ChatModel.reply` re-runs only the model
call — the recall consults and the signal writes already landed stay put, and
the turn's ``COMPLETED`` row attests the final reply.

Only failures :func:`paa.tui.chat.errors.classify` calls ``model_retryable``
are retried. A partial stream is never retried (the operator already saw
those deltas; a second stream would duplicate them) — the failure surfaces
instead, attested on the ledger by the loop as a failed or cancelled turn.

The breaker counts consecutive retryable failures *across* turns; any success
resets it, and ``reset()`` (wired to ``/doctor`` and ``/model``) clears it
even while open. When open, ``reply`` fails immediately with
:class:`CircuitOpenError` — a :class:`~paa.core.errors.PaaError`, so the loop
records it as the turn's failure reason and the UI's microcopy explains the
next action.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from paa.core.errors import PaaError
from paa.tui.chat.errors import ErrorKind, classify

if TYPE_CHECKING:
    from paa.chat.loop import ChatModel, ChatReply

__all__ = ["DEFAULT_ATTEMPTS", "CircuitOpenError", "ResilientChatModel"]

#: The streaming surface, re-declared from the chat loop's own type alias.
DeltaSink = Callable[[str], Awaitable[None]]

#: Attempts per turn (1 initial + 2 retries). The spec's breaker counts three
#: consecutive *turns*, which is the counter below at this default.
DEFAULT_ATTEMPTS: int = 3

#: Backoff schedule between attempts, seconds. Real-world defaults; tests
#: pass their own.
DEFAULT_BACKOFF: tuple[float, ...] = (0.5, 1.0, 2.0)


class CircuitOpenError(PaaError):
    """The breaker is open: the model has failed too many times in a row."""

    def __init__(self, *, failures: int, cooldown: float) -> None:
        super().__init__(
            f"model circuit open after {failures} consecutive failures; "
            f"retry blocked for {cooldown:.0f}s",
            failures=failures,
            cooldown_seconds=cooldown,
        )


class ResilientChatModel:
    """A :class:`~paa.chat.loop.ChatModel` wrapper: retry, backoff, breaker.

    Satisfies the chat seam's protocol, so it composes with any inner model —
    the offline echo, a registry provider, or a test double.
    """

    def __init__(
        self,
        inner: ChatModel,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 30.0,
    ) -> None:
        self._inner = inner
        self._attempts = max(1, attempts)
        self._backoff = backoff
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown = breaker_cooldown
        self._consecutive_failures = 0
        self._open_until = 0.0

    @property
    def breaker_open(self) -> bool:
        """True while the breaker refuses calls (cooldown window active).

        ``time.monotonic()``, not ``asyncio.get_running_loop().time()``: the
        ``--plain`` REPL reads this flag **on the main thread, where no asyncio
        loop is running** (the loop lives on the plain surface's daemon
        thread). A loop-clock read raises ``RuntimeError`` there — and the
        main-thread read is the read that decides whether a turn is submitted
        at all. ``time.monotonic()`` is the same clock ``loop.time()`` is built
        on, so the window means the same thing from either thread.
        """
        return time.monotonic() < self._open_until

    @property
    def attempts(self) -> int:
        """The retry budget per turn (1 initial call + retries)."""
        return self._attempts

    @property
    def backoff(self) -> tuple[float, ...]:
        """The backoff schedule between attempts, in seconds."""
        return self._backoff

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def reset(self) -> None:
        """Close the breaker and zero the counter (``/doctor``, ``/model``)."""
        self._consecutive_failures = 0
        self._open_until = 0.0

    async def reply(
        self, prompt: str, *, on_delta: DeltaSink | None = None
    ) -> ChatReply:
        """One reply, retried on retryable failures, breaker-gated.

        A failure after deltas were emitted is *not* retried, because the
        caller already holds those bytes; a second stream would duplicate
        them.
        """
        if self.breaker_open:
            raise CircuitOpenError(
                failures=self._consecutive_failures,
                cooldown=self._breaker_cooldown,
            )

        last_attempt = self._attempts - 1
        for attempt in range(self._attempts):
            emitted = False

            async def sink(delta: str) -> None:
                nonlocal emitted
                emitted = True
                if on_delta is not None:
                    await on_delta(delta)

            try:
                reply = await self._inner.reply(
                    prompt,
                    on_delta=sink if on_delta is not None else None,
                )
            except Exception as exc:
                retryable = classify(exc) is ErrorKind.MODEL_RETRYABLE and not emitted
                if not retryable or attempt == last_attempt:
                    self._count_failure()
                    raise
                await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                continue
            self._consecutive_failures = 0
            return reply
        raise AssertionError("unreachable")  # pragma: no cover

    def _count_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            # ``time.monotonic()`` for the same cross-thread reason as
            # :attr:`breaker_open` — this may be counted on the loop thread and
            # read from the REPL thread, or vice versa after a ``reset()``.
            self._open_until = time.monotonic() + self._breaker_cooldown
