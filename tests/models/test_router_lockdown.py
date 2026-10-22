"""LOCKDOWN never reaches the frontier — asserted against a spy, not a policy.

The escalation *policy* was covered: every assertion in
``test_router_policy.py`` calls
:meth:`~paa.models.router.EscalatingModelRouter.evaluate` and reads its
verdict. Not one of them **made a call**. A policy can be correct and the
router can still cross the boundary, because the router has more moving parts
than the policy does — a force flag, a provider that fails, a frontier that
is wired, a healthcheck that probes. This file closes that gap by watching
the providers themselves.

``EchoProvider`` is the spy: it records every request it was handed in
``calls`` and counts them in ``call_count``. The frontier's count is the one
that matters — under ``LOCKDOWN`` it must be **zero**, and the local's must
be **one**: a request that leaves the machine under an air-gap promise is the
failure this module exists to make impossible, and it is a failure that
cannot be undone once it has happened.

The ledger is consulted too, because "the frontier was not called" and "the
boundary was not crossed" are different claims and only the second one is
the promise. A router that called the frontier and wrote no audit record
would satisfy the first and break the second.
"""

from __future__ import annotations

import uuid

import pytest

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.models.base import CompletionRequest, Message, ModelTier, ModelUnavailableError
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("diagnose this"),))


class _HealthcheckSpy(EchoProvider):
    """``EchoProvider`` that also counts the healthcheck probes it receives.

    ``EchoProvider`` records *generations*; it says nothing about
    ``healthcheck`` calls, and the frontier's healthcheck is the one call
    that can leave the machine without a completion ever being requested.
    """

    def __init__(
        self,
        *,
        name: str = "frontier-echo",
        tier: ModelTier = ModelTier.FRONTIER,
    ) -> None:
        super().__init__(name=name, tier=tier)
        self.healthcheck_count = 0

    async def healthcheck(self) -> bool:
        self.healthcheck_count += 1
        return await super().healthcheck()


def _router(
    local: EchoProvider,
    frontier: EchoProvider | None = None,
    ledger: LedgerStore | None = None,
) -> EscalatingModelRouter:
    """A router over echo providers, on the default policy settings."""
    return EscalatingModelRouter(
        local, frontier, settings=ModelSettings(), ledger_store=ledger
    )


async def _escalations(ledger: LedgerStore, correlation_id: uuid.UUID) -> list[LedgerEvent]:
    events = await ledger.read_correlation(correlation_id)
    return [e for e in events if e.event_type is EventType.MODEL_ESCALATED]


# -- the frontier is never called --------------------------------------------


@pytest.mark.parametrize("modality", list(ComplexityModality))
async def test_lockdown_never_calls_the_frontier(modality: ComplexityModality) -> None:
    """Every modality, every time: the frontier's call count is zero.

    ``MAX`` is in the table deliberately: it is the modality the escalation
    floor is satisfied by, so it is the one under which a router that
    consulted the floor before the permission mode would escalate.
    """
    local = EchoProvider()
    frontier = _HealthcheckSpy()
    router = _router(local, frontier)

    response = await router.complete(
        _request(),
        modality=modality,
        permission_mode=PermissionMode.LOCKDOWN,
    )

    assert frontier.call_count == 0
    # The local answered instead — LOCKDOWN means *local only*, not *no
    # answer*. A router that refused the call outright would satisfy the
    # first assertion and break the mode's actual contract.
    assert local.call_count == 1
    assert response.provider == "echo"


async def test_force_escalate_does_not_bypass_lockdown() -> None:
    """``force_escalate`` is a cost shortcut, not a permission override.

    The flag skips the local attempt so the caller does not pay for an
    answer it has already judged unusable. It must not skip the *policy*:
    a caller that could reach a frontier model from ``LOCKDOWN`` would make
    the air-gap promise a convention rather than a boundary.
    """
    local = EchoProvider()
    frontier = _HealthcheckSpy()
    router = _router(local, frontier)

    await router.complete(
        _request(),
        modality=ComplexityModality.MAX,
        permission_mode=PermissionMode.LOCKDOWN,
        force_escalate=True,
    )

    assert frontier.call_count == 0
    # The blocked path calls the local whatever the flag says: the decision
    # was taken before the flag was consulted, so the flag never reaches the
    # branch that would have skipped it.
    assert local.call_count == 1


async def test_a_failing_local_under_lockdown_does_not_reach_the_frontier() -> None:
    """The failure path is where an air-gap promise is usually broken.

    A router that escalates because the local model happened to fail has
    broken a security guarantee *silently*, at exactly the moment the user
    was least able to notice. The local failure is raised instead.
    """
    local = EchoProvider(fail_times=1)
    frontier = _HealthcheckSpy()
    router = _router(local, frontier)

    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.LOCKDOWN,
        )

    assert frontier.call_count == 0
    assert excinfo.value.details["escalation_block_reason"] == (
        "permission_mode_forbids_escalation"
    )


# -- the ledger records no crossing -----------------------------------------


async def test_lockdown_writes_no_escalation_event(ledger: LedgerStore) -> None:
    """No ``MODEL_ESCALATED`` event, because no boundary was crossed.

    An audit record for an escalation that did not happen is as wrong as a
    missing one: the ledger is the truth, and a reader counting escalations
    to answer "how often did work leave this machine?" must not have to
    subtract the ones that never did.
    """
    local = EchoProvider()
    frontier = _HealthcheckSpy()
    router = _router(local, frontier, ledger)
    correlation_id = uuid.uuid4()

    await router.complete(
        _request(),
        modality=ComplexityModality.MAX,
        permission_mode=PermissionMode.LOCKDOWN,
        correlation_id=correlation_id,
    )

    assert await _escalations(ledger, correlation_id) == []


async def test_a_failing_local_under_lockdown_writes_no_escalation_event(
    ledger: LedgerStore,
) -> None:
    """The failure path writes no audit record either.

    The same rule: the record exists to say "work left the machine". Nothing
    left, so nothing is written — including when the local failed, which is
    the situation in which a router is most tempted to reach for the
    frontier.
    """
    local = EchoProvider(fail_times=1)
    frontier = _HealthcheckSpy()
    router = _router(local, frontier, ledger)
    correlation_id = uuid.uuid4()

    with pytest.raises(ModelUnavailableError):
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.LOCKDOWN,
            correlation_id=correlation_id,
        )

    assert await _escalations(ledger, correlation_id) == []


# -- the healthcheck is a crossing too --------------------------------------


async def test_lockdown_does_not_probe_the_frontier() -> None:
    """``healthcheck`` is a network call, and it is gated like any other.

    A hosted provider's healthcheck issues an authenticated HTTP GET. A
    router that probed the frontier under ``LOCKDOWN`` would carry the
    credential to a hosted endpoint and leave the machine — while looking,
    from the caller's side, like the safest call in the system.

    The frontier's key is **absent** from the report rather than ``False``:
    it was not asked, which is not the same as "asked, and it cannot serve".
    """
    local = EchoProvider()
    frontier = _HealthcheckSpy()
    router = _router(local, frontier)

    report = await router.healthcheck(permission_mode=PermissionMode.LOCKDOWN)

    assert frontier.healthcheck_count == 0
    assert "frontier-echo" not in report
    assert report["echo"] is True


async def test_the_frontier_is_probed_when_escalation_is_permitted() -> None:
    """The gate must not cost the frontier its healthcheck in the normal case.

    Without this, a healthcheck that simply never probed the frontier would
    satisfy the assertion above while leaving the router unable to tell an
    operator whether the frontier is usable.
    """
    local = EchoProvider()
    frontier = _HealthcheckSpy()
    router = _router(local, frontier)

    report = await router.healthcheck(permission_mode=PermissionMode.ASK)

    assert frontier.healthcheck_count == 1
    assert report["frontier-echo"] is True
