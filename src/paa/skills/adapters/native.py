"""In-process callables — the fast path (RFC §8, ``provider='native'``).

Not every capability needs a sandbox. A pure function that reformats a date, a
lookup against an already-loaded table, a deterministic transform — routing those
through a subprocess would cost more in process spawn than the work itself, and
would gain nothing: there is no untrusted code to contain, because the callable
is first-party code registered in the repository, not downloaded.

So the native adapter trades containment for latency *deliberately and only for
first-party code*. The contract still exists and the output is still validated
against ``output_schema`` (by the USA, the single place that owns the schema
gate), because "we wrote it" is not "it cannot regress". What is skipped is the
sandbox, not the contract.

The one rule: a native callable takes a single ``dict`` of arguments and returns
a ``dict``. Sync or async are both accepted — an async callable is awaited, a
sync one is offloaded to a thread so a slow native skill cannot stall the event
loop.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import structlog

from paa.skills.adapters.base import SkillAdapter, SkillResult

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.skills.adapters.base import SecretProvider
    from paa.skills.contracts import SkillContract

__all__ = ["NativeAdapter", "NativeCallable"]

log = structlog.get_logger(__name__)

#: A native skill: arguments in, result mapping out. May be sync or async.
NativeCallable = Callable[[dict[str, Any]], "dict[str, Any] | Awaitable[dict[str, Any]]"]


class NativeAdapter(SkillAdapter):
    """Runs registered Python callables in-process.

    Callables are held in an instance-local table rather than a global registry
    so tests get a clean slate per adapter and two runtimes cannot clobber each
    other's registrations.
    """

    def __init__(self) -> None:
        self._callables: dict[str, NativeCallable] = {}
        self._contracts: dict[str, SkillContract] = {}

    @property
    def provider(self) -> str:
        return "native"

    def register(self, contract: SkillContract, fn: NativeCallable) -> None:
        """Bind a callable to its contract under ``contract.skill_name``.

        The contract must declare ``provider='native'`` and its invocation
        ``target`` names the callable for the ledger; the binding here is what
        makes the target resolvable. Re-registering the same name replaces the
        binding, which is what a hot-reload during development wants.
        """
        if contract.provider != "native":
            raise ValueError(
                f"NativeAdapter only serves provider='native', "
                f"got {contract.provider!r} for {contract.skill_name!r}"
            )
        self._callables[contract.skill_name] = fn
        self._contracts[contract.skill_name] = contract
        log.debug("skills.native.registered", skill=contract.skill_name)

    async def discover(self) -> list[SkillContract]:
        """The contracts of every currently registered callable."""
        return list(self._contracts.values())

    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - deliberate per-call budget
        secret_broker: SecretProvider | None = None,
    ) -> SkillResult:
        """Call the bound function and capture its outcome.

        ``sandbox`` is accepted for interface uniformity and ignored — the whole
        point of this adapter is that there isn't one. A callable that raises is
        caught and reported as ``ok=False``; the exception text is surfaced but
        the traceback is not, because a traceback can render locals and a local
        might be a credential.

        A callable that declares a ``get_secret`` parameter receives the bound
        :class:`~paa.skills.adapters.base.SecretProvider`'s ``get_secret`` — this
        is the in-process form of RFC §8.2's secret proxy. The host has already
        checked ``Permission.SECRET_READ`` when it bound the provider, so the
        callable gets a working resolver only when the mode permits it.
        """
        fn = self._callables.get(contract.skill_name)
        if fn is None:
            return SkillResult(
                ok=False,
                error=f"no native callable registered under {contract.skill_name!r}",
            )

        started = time.perf_counter()
        try:
            output = await self._call(fn, arguments, timeout, secret_broker)
        except TimeoutError:
            return SkillResult(
                ok=False,
                error=f"native skill exceeded {timeout}s timeout",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:  # a native skill may raise anything; a sample still
            return SkillResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        if not isinstance(output, dict):
            return SkillResult(
                ok=False,
                error=f"native skill must return a dict, got {type(output).__name__}",
                latency_ms=latency_ms,
            )
        return SkillResult(ok=True, output=output, latency_ms=latency_ms, exit_code=0)

    async def _call(
        self,
        fn: NativeCallable,
        arguments: dict[str, Any],
        timeout: float | None,  # noqa: ASYNC109 - deliberate per-call budget
        secret_broker: SecretProvider | None,
    ) -> Any:
        """Await an async callable or offload a sync one, under ``timeout``.

        Injects ``get_secret`` only for callables that ask for it, so the common
        pure-function skill keeps its simple ``(arguments) -> dict`` signature.
        """
        kwargs: dict[str, Any] = {}
        if secret_broker is not None and "get_secret" in inspect.signature(fn).parameters:
            kwargs["get_secret"] = secret_broker.get_secret

        if inspect.iscoroutinefunction(fn):
            coro: Awaitable[Any] = fn(arguments, **kwargs)  # type: ignore[call-arg]
        else:
            # to_thread keeps a blocking native skill off the event loop.
            coro = asyncio.to_thread(lambda: fn(arguments, **kwargs))  # type: ignore[call-arg]
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
