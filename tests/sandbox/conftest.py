"""Shared machinery for the sandbox suites.

The one thing the sandbox tests have to be able to see is the **budget** a
backend hands to a wait. A budget *is* the timeout argument of
``asyncio.wait_for``, and it is the whole contract: a step's work budget and
a container's start budget are separate numbers, and a backend that adds
them together hands the caller a budget the caller did not ask for.

There is no fake clock in this repository and none is introduced here. The
budget is read off the call, not off a clock: the test asserts on the number
the backend passed, and the *behaviour* of that number is checked by letting
it expire against a real (very short) sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest


class WaitForRecorder:
    """Records the timeout of every ``asyncio.wait_for`` in one module.

    The interception is on the *module's* ``asyncio`` attribute, not on the
    global one. Patching ``asyncio.wait_for`` globally would change the
    behaviour of every coroutine in the process — the same mistake the
    ``_exec`` seam exists to avoid, and the reason the seam is a method on
    the backend rather than a patch of ``asyncio.create_subprocess_exec``.

    ``label`` names the call each timeout belongs to, so a test can ask for
    "the budget the wait got" rather than for "the third timeout", which is
    a number that moves whenever the protocol gains a step.
    """

    def __init__(self, real: Any, label: Callable[[], str] | None = None) -> None:
        self._real = real
        self._label = label
        #: Every timeout, in the order the calls were made.
        self.timeouts: list[float | None] = []
        #: Every timeout, paired with the label of the call it belonged to.
        self.calls: list[tuple[str, float | None]] = []

    def __getattr__(self, name: str) -> Any:
        """Everything that is not ``wait_for`` is the real ``asyncio``."""
        return getattr(self._real, name)

    async def wait_for(self, awaitable: Any, **kwargs: Any) -> Any:
        # The signature is mirrored with ``**kwargs`` rather than spelled out
        # as ``timeout=...``: this is a *proxy* for ``asyncio.wait_for`` and
        # the callers pass the budget by keyword, so the keyword has to be
        # accepted verbatim.
        budget = kwargs.get("timeout")
        self.timeouts.append(budget)
        self.calls.append((self._label() if self._label is not None else "", budget))
        return await self._real.wait_for(awaitable, **kwargs)


@pytest.fixture
def record_wait_for(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Any, Callable[[], str] | None], WaitForRecorder]:
    """Install a :class:`WaitForRecorder` over one module's ``asyncio``.

    Usage::

        recorder = record_wait_for(docker_backend, lambda: daemon.current)
    """

    def install(module: Any, label: Callable[[], str] | None = None) -> WaitForRecorder:
        recorder = WaitForRecorder(asyncio, label)
        monkeypatch.setattr(module, "asyncio", recorder)
        return recorder

    return install
