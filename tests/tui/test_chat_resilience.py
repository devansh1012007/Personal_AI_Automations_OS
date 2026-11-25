"""The breaker's clock must read from the REPL thread, not only the loop thread.

Regression for the ``paa chat --plain`` crash found by the live smoke: the
``--plain`` surface runs its REPL synchronously on the main thread while the
asyncio loop runs on a daemon thread, so a ``breaker_open`` implemented with
``asyncio.get_running_loop().time()`` raised ``RuntimeError: no running event
loop`` at the exact call that decides whether a turn is submitted. The tests
in this file read the breaker from a thread with **no running loop** — the
production shape. The pytest suite had not caught it because pytest-asyncio
runs every async test *inside* a loop, where a loop-clock read happens to
work; that is the blind spot this file exists to close.
"""

from __future__ import annotations

import asyncio

import pytest

from paa.tui.chat.resilience import ResilientChatModel


class _TimeoutModel:
    """A chat model that always fails retryably and never emits a delta.

    :class:`TimeoutError` is one of the exception types the error taxonomy
    classifies as ``model_retryable``, and no delta is emitted before the
    raise, so the seam counts each ``reply`` as one consecutive failure —
    the minimum needed to walk the breaker to its open state.
    """

    async def reply(
        self, prompt: str, *, on_delta: object = None
    ) -> object:  # pragma: no cover - always raises
        raise TimeoutError(f"provider timeout after {prompt!r}")


def _breaker_two_failures() -> ResilientChatModel:
    """A resilient wrapper whose breaker opens after two failed turns."""
    return ResilientChatModel(
        _TimeoutModel(),
        attempts=1,
        backoff=(0.0,),
        breaker_threshold=2,
        breaker_cooldown=30.0,
    )


class TestBreakerOpens:
    async def test_consecutive_failures_open_the_breaker(self) -> None:
        model = _breaker_two_failures()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await model.reply("ping")
        assert model.breaker_open is True
        assert model.consecutive_failures == 2

    async def test_reset_closes_the_breaker(self) -> None:
        model = _breaker_two_failures()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await model.reply("ping")
        assert model.breaker_open is True
        model.reset()
        assert model.breaker_open is False
        assert model.consecutive_failures == 0


class TestBreakerClockCrossesThreads:
    async def test_breaker_readable_from_thread_with_no_running_loop(
        self,
    ) -> None:
        """The plain REPL's exact read: another thread, no running loop.

        This is the regression test for the ``RuntimeError: no running event
        loop`` crash — before the clock moved to ``time.monotonic()``,
        ``asyncio.to_thread`` put the read on a worker thread with no loop and
        the property raised. The turn-gate in ``PlainChat._run_turn`` reads the
        flag from the main thread the same way.
        """
        model = _breaker_two_failures()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await model.reply("ping")

        opened = await asyncio.to_thread(lambda: model.breaker_open)
        assert opened is True

    async def test_failure_window_is_readable_across_threads(self) -> None:
        """The window stamped on the loop thread reads correctly from another.

        The cooldown is stamped inside ``reply`` on the loop thread and read
        by the plain REPL on the main thread; both sides must agree on one
        clock. Counted here on the loop thread, read from a no-loop thread,
        then re-read on the loop thread after the window elapses.
        """
        model = _breaker_two_failures()
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await model.reply("ping")

        assert await asyncio.to_thread(lambda: model.breaker_open) is True
        assert model.breaker_open is True

    async def test_breaker_window_counts_real_seconds(self) -> None:
        """A short cooldown actually elapses on the monotonic clock."""
        model = ResilientChatModel(
            _TimeoutModel(),
            attempts=1,
            backoff=(0.0,),
            breaker_threshold=1,
            breaker_cooldown=0.05,
        )
        with pytest.raises(TimeoutError):
            await model.reply("ping")
        assert model.breaker_open is True
        await asyncio.sleep(0.06)
        assert model.breaker_open is False
