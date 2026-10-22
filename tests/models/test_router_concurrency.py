"""The concurrency cap is a memory bound, and it is asserted as one.

``max_concurrent_streams`` is not a politeness setting. RFC §6.2 sizes it at
2 because a second resident copy of a local model's weights is what pushes a
3.5 GB machine into swap, and a swapping machine does not recover on its own.
The *configured number* was asserted; the **behaviour** was not. Nothing in
the suite observed how many calls were in flight at once, so a semaphore
that serialised everything — the opposite failure, and just as bad, because
the run would take three times as long and the memory bound would be met by
accident — would have passed every test there was.

The assertion is ``max_in_flight == the cap``, not ``<=``: a cap of two that
admits one is not a cap of two, and ``<= 2`` cannot tell the two apart.

The cap is **one semaphore for the whole router**, so it gates the local and
the frontier tiers alike. That is the second half of the claim, and it is
the half a per-tier implementation would break: two semaphores of two admit
four calls at once, and the memory bound is exactly what the extra
concurrency would spend.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ModelTier,
    ModelUnavailableError,
)
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("diagnose this"),))


class _InFlightSpy:
    """Counts provider calls in flight, across every tier.

    One spy is shared by the local and the frontier provider, so the number
    it reports is the number of calls the *router* has in flight — which is
    the number the cap is about. A spy per provider would report each tier
    separately and could not see a router that ran both at once.

    It also *holds* calls: a provider named ``n`` waits at ``hold(n)`` until
    the test releases it. Holding is what makes the overlap observable —
    without it a call that awaits nothing has a zero-width window, and two
    calls could never be seen at the same time.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.entered = 0
        self.in_flight_by_name: dict[str, int] = {}
        self._holds: dict[str, asyncio.Event] = {}

    def hold(self, name: str) -> asyncio.Event:
        """The gate a provider named ``name`` waits at."""
        return self._holds.setdefault(name, asyncio.Event())

    async def enter(self, name: str) -> None:
        self.in_flight += 1
        self.entered += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.in_flight_by_name[name] = self.in_flight_by_name.get(name, 0) + 1
        # One yield point inside every call's window, so a call that is not
        # held still has a window wide enough for another call to be seen
        # inside it.
        await asyncio.sleep(0)
        await self.hold(name).wait()

    def leave(self, name: str) -> None:
        self.in_flight -= 1
        self.in_flight_by_name[name] -= 1

    def release(self, name: str) -> None:
        """Let every held call for ``name`` proceed."""
        self.hold(name).set()


class _InFlightEcho(EchoProvider):
    """``EchoProvider`` that reports how many calls overlap.

    ``fail`` makes every call raise the way an unreachable provider does.
    That is what drives the escalation path, and therefore the frontier
    tier — which is where the "one semaphore for the whole router" claim is
    decided.
    """

    def __init__(
        self,
        spy: _InFlightSpy,
        *,
        name: str = "echo",
        tier: ModelTier = ModelTier.LOCAL,
        fail: bool = False,
    ) -> None:
        super().__init__(name=name, tier=tier)
        self._spy = spy
        self._fail = fail

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        await self._spy.enter(self.name)
        try:
            if self._fail:
                raise ModelUnavailableError(
                    "the provider is unreachable", provider=self.name
                )
            return await super().complete(request)
        finally:
            self._spy.leave(self.name)


async def _wait_for(predicate: Callable[[], bool], *, attempts: int = 500) -> None:
    """Let the event loop run until ``predicate`` holds, or give up.

    A bounded drain rather than a sleep: each iteration yields, so every
    ready task takes a step. The test never guesses how long the tasks need
    — it waits for the state it is interested in, and the assertion that
    follows is what fails when the state never arrives.
    """
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)


async def test_the_cap_bounds_the_calls_in_flight() -> None:
    """More calls than the cap, and the cap is what is reached.

    Six calls against a cap of two. The calls are *held* until the test
    releases them, so the number that overlaps is the number the router
    admitted, not a number that happened by scheduling.
    """
    cap = 2
    calls = 6
    spy = _InFlightSpy()
    local = _InFlightEcho(spy)
    router = EscalatingModelRouter(
        local, settings=ModelSettings(max_concurrent_streams=cap)
    )

    tasks = [asyncio.create_task(router.complete(_request())) for _ in range(calls)]
    await _wait_for(lambda: spy.in_flight >= cap)

    # ``==`` the cap, not ``<=``: a semaphore that admitted one call at a
    # time would satisfy "<= 2" and break the memory bound it exists for.
    assert spy.max_in_flight == cap

    spy.release(local.name)
    await asyncio.gather(*tasks)

    assert local.call_count == calls
    assert spy.max_in_flight == cap


async def test_the_cap_is_one_semaphore_for_the_whole_router() -> None:
    """Both tiers share one cap, so the router never exceeds it in total.

    The local fails, so every call escalates, and the frontier's calls are
    the ones that stay in flight — holding the cap while they wait. If the
    router had a semaphore per tier, the second wave of local calls would
    run *beside* the frontier's, and the shared count would climb past the
    cap: four concurrent calls, four resident copies of the model, and the
    memory bound gone.
    """
    cap = 2
    calls = 6
    spy = _InFlightSpy()
    local = _InFlightEcho(spy, name="echo", fail=True)
    frontier = _InFlightEcho(spy, name="frontier-echo", tier=ModelTier.FRONTIER)
    router = EscalatingModelRouter(
        local, frontier, settings=ModelSettings(max_concurrent_streams=cap)
    )

    # The local is not held: it has to fail for the escalation to happen.
    spy.release(local.name)

    tasks = [
        asyncio.create_task(
            router.complete(
                _request(),
                modality=ComplexityModality.COMPLEX,
                permission_mode=PermissionMode.ASK,
            )
        )
        for _ in range(calls)
    ]
    await _wait_for(lambda: spy.in_flight_by_name.get("frontier-echo", 0) >= cap)

    assert spy.in_flight_by_name.get("frontier-echo", 0) == cap
    assert spy.max_in_flight == cap

    spy.release(frontier.name)
    await asyncio.gather(*tasks)

    assert frontier.call_count == calls
    assert spy.max_in_flight == cap


async def test_the_usage_counters_account_for_every_concurrent_call() -> None:
    """The counters are the observability half of the cap.

    ``token_usage`` answers "how much did this cost?", and it is read after
    a run in which the calls overlapped. A counter that lost a call under
    concurrency would report a smaller bill than the one that was paid, and
    the discrepancy would only show up on the busiest runs.
    """
    calls = 6
    spy = _InFlightSpy()
    local = _InFlightEcho(spy)
    router = EscalatingModelRouter(
        local, settings=ModelSettings(max_concurrent_streams=2)
    )

    tasks = [asyncio.create_task(router.complete(_request())) for _ in range(calls)]
    await _wait_for(lambda: spy.in_flight >= 2)
    spy.release(local.name)
    await asyncio.gather(*tasks)

    usage = router.token_usage()
    assert usage["echo"]["calls"] == calls
    assert usage["echo"]["failures"] == 0
    assert usage["echo"]["prompt_tokens"] > 0

    router.reset_usage()
    assert router.token_usage() == {}


async def test_two_concurrent_escalations_are_two_records(ledger: LedgerStore) -> None:
    """Two writers, one lineage, two crossings — and two audit records.

    The concurrency case for the escalation audit, and the one the wave-2
    tests did not have: they escalated twice, **sequentially**, and read the
    ordinal out of the discriminator, so nothing said what two writers
    racing on one lineage would do.

    What is guaranteed, and what this test pins:

    * **both records survive.** The discriminator is the event's own id, so
      the two idempotency keys differ and the ledger suppresses neither.
    * **the two records are ordered.** The lineage's own numbering of them
      is 1, 2 — the ledger's ``state_version`` order, which is dense and
      assigned under the lineage's lock, so it is a total order even when
      two writers arrive together. The router's old read-then-count ordinal
      could not promise that: two writers that read the lineage at the same
      moment took the *same* ordinal, so the label tied.
    * **both say the same thing.** Two crossings of the boundary are two
      records with the same trigger and the same providers, not one record
      and a copy of it.

    The *order* of the two writers is not asserted, and must not be: which
    writer gets the lock first is the scheduler's business, not the
    ledger's, and a test that asserted it would be asserting the event
    loop's mood.
    """
    correlation_id = uuid.uuid4()
    routers = [
        EscalatingModelRouter(
            EchoProvider(fail_times=1),
            EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER),
            settings=ModelSettings(),
            ledger_store=ledger,
        )
        for _ in range(2)
    ]

    await asyncio.gather(
        *(
            router.complete(
                _request(),
                modality=ComplexityModality.COMPLEX,
                permission_mode=PermissionMode.ASK,
                correlation_id=correlation_id,
            )
            for router in routers
        )
    )

    events = await ledger.read_correlation(correlation_id)
    escalations = [e for e in events if e.event_type is EventType.MODEL_ESCALATED]
    assert len(escalations) == 2
    # Two distinct discriminators: neither write was suppressed as a
    # duplicate of the other.
    assert len({e.discriminator for e in escalations}) == 2
    # The ledger's own order is a total order — no tie — so the lineage's
    # numbering of the two crossings is 1, 2.
    assert len({e.state_version for e in escalations}) == 2
    assert {e.payload["trigger"] for e in escalations} == {"local_provider_unavailable"}
    assert {e.payload["to_provider"] for e in escalations} == {"frontier-echo"}
