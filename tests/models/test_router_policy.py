"""Direct coverage of the escalation policy and the errors it produces.

``evaluate`` was written as a contract-tested policy primitive and then, until
now, never tested directly: every existing assertion reaches it only through a
full ``complete`` call, so the exact reason strings, their rule ordering, and
the diagnostic details attached when a blocked local call *fails* could all
drift unnoticed. These tests pin all three.

The split they enforce mirrors the module's own design:

- :meth:`~paa.models.router.EscalatingModelRouter.evaluate` is first-reason-wins
  — its single ``reason`` is what ledger payloads are written against.
- :meth:`~paa.models.router.EscalatingModelRouter.block_reasons` reports *every*
  violated rule, because an operator fixing escalation failures one obstacle at
  a time is the failure mode this exists to prevent.
- The error raised from the local-only path must carry the whole picture: all
  block reasons, whether a frontier provider is even wired, and the underlying
  provider's own diagnosis (status code, body) — redacted.
"""

from __future__ import annotations

from typing import Any

import pytest

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, PermissionMode
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
)
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter

#: Credential-shaped token long enough to trip the generic redactor in
#: :func:`paa.models.base.redact` (``sk-`` + 12+ chars).
FAKE_KEY = "sk-test-abcdef0123456789ABCDEF"


def _router(
    *,
    frontier: ModelProvider | None = None,
    fail_times: int = 0,
) -> EscalatingModelRouter:
    """A router with an echo local provider, default policy settings."""
    local = EchoProvider(fail_times=fail_times)
    return EscalatingModelRouter(local, frontier, settings=ModelSettings())


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("diagnose this"),))


class _RaisingProvider(ModelProvider):
    """Local double that fails the way a real HTTP provider fails.

    :class:`~paa.models.base.ModelUnavailableError` takes arbitrary ``**details``,
    so the test hands it exactly what ``_http.py`` builds on a 429 — a status
    code and a body excerpt — and asserts both survive the router's wrap.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__()
        self._message = message
        self._details = details

    @property
    def name(self) -> str:
        return "fake"

    @property
    def tier(self) -> ModelTier:
        return ModelTier.LOCAL

    @property
    def model(self) -> str:
        return "fake-model"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ModelUnavailableError(self._message, provider=self.name, **self._details)

    async def healthcheck(self) -> bool:
        return False


# -- evaluate(): the policy matrix ------------------------------------------


def test_evaluate_standard_below_floor_blocked_by_modality() -> None:
    router = _router()
    decision = router.evaluate(ComplexityModality.STANDARD, PermissionMode.ASK)
    assert decision.allowed is False
    assert decision.reason == "modality_below_escalation_floor:COMPLEX"


def test_evaluate_complex_at_floor_is_permitted() -> None:
    router = _router()
    decision = router.evaluate(ComplexityModality.COMPLEX, PermissionMode.ASK)
    assert decision.allowed is True
    assert decision.reason == "permitted"


def test_evaluate_lockdown_blocks_every_modality() -> None:
    router = _router()
    for modality in ComplexityModality:
        decision = router.evaluate(modality, PermissionMode.LOCKDOWN)
        assert decision.allowed is False
        assert decision.reason == "permission_mode_forbids_escalation"


def test_evaluate_ask_mode_never_triggers_permission_rule() -> None:
    router = _router()
    for modality in ComplexityModality:
        decision = router.evaluate(modality, PermissionMode.ASK)
        assert decision.reason != "permission_mode_forbids_escalation"


def test_evaluate_permitted_reason_contract() -> None:
    """The allowed case keeps returning ``reason="permitted"`` — guard, not feat."""
    router = _router()
    decision = router.evaluate(ComplexityModality.MAX, PermissionMode.ASK)
    assert decision.allowed is True
    assert decision.reason == "permitted"
    assert decision.to_payload()["policy_reason"] == "permitted"


# -- block_reasons(): the full list ------------------------------------------


def test_block_reasons_lists_security_rule_before_cost_rule() -> None:
    router = _router()
    reasons = router.block_reasons(ComplexityModality.STANDARD, PermissionMode.LOCKDOWN)
    assert reasons == [
        "permission_mode_forbids_escalation",
        "modality_below_escalation_floor:COMPLEX",
    ]
    # evaluate still reports only the first — first-reason-wins is its contract.
    blocked = router.evaluate(ComplexityModality.STANDARD, PermissionMode.LOCKDOWN)
    assert blocked.reason == "permission_mode_forbids_escalation"


def test_block_reasons_single_and_empty_shapes() -> None:
    router = _router()
    # Single rule violated is still a one-element list — one shape to parse.
    only_cost = router.block_reasons(ComplexityModality.STANDARD, PermissionMode.ASK)
    assert only_cost == ["modality_below_escalation_floor:COMPLEX"]
    only_security = router.block_reasons(ComplexityModality.MAX, PermissionMode.LOCKDOWN)
    assert only_security == ["permission_mode_forbids_escalation"]
    assert router.block_reasons(ComplexityModality.COMPLEX, PermissionMode.ASK) == []


# -- the local-only error: every fact the operator needs ---------------------


async def test_local_only_failure_reports_every_block_reason_and_missing_frontier() -> None:
    router = _router(fail_times=1)  # frontier defaults to None
    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
        )

    details = excinfo.value.details
    assert details["escalation_block_reasons"] == ["modality_below_escalation_floor:COMPLEX"]
    # Backward compat: the singular key still carries the first violated rule.
    assert details["escalation_block_reason"] == "modality_below_escalation_floor:COMPLEX"
    assert details["escalation_allowed"] is False
    assert details["policy_reason"] == "modality_below_escalation_floor:COMPLEX"
    assert details["modality"] == "STANDARD"
    assert details["permission_mode"] == "ASK"
    # The wiring fact that never used to surface, on both key shapes.
    assert details["frontier_configured"] is False
    assert details["escalation_unavailable_reason"] == "no_frontier_configured"
    # The underlying diagnosis, not just its type name.
    assert details["underlying_error"] == "ModelUnavailableError"
    assert "echo provider was configured to fail this call" in details["underlying_message"]


async def test_local_only_reports_frontier_configured_honestly() -> None:
    """Same blocked decision, frontier wired — ``frontier_configured`` flips."""
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    router = _router(frontier=frontier, fail_times=1)
    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
        )

    details = excinfo.value.details
    assert details["frontier_configured"] is True
    assert details["escalation_unavailable_reason"] is None
    # Wiring being fine does not unblock policy: the floor still holds.
    assert details["escalation_block_reasons"] == ["modality_below_escalation_floor:COMPLEX"]


async def test_permitted_without_frontier_names_the_missing_wiring() -> None:
    router = _router(fail_times=1)  # COMPLEX + ASK would be permitted, but no frontier
    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.ASK,
        )

    assert "no frontier provider is configured" in excinfo.value.message
    details = excinfo.value.details
    assert details["escalation_trigger"] == "local_provider_unavailable"
    assert details["frontier_configured"] is False
    assert details["escalation_allowed"] is True


async def test_underlying_provider_details_survive_the_wrap() -> None:
    local = _RaisingProvider(
        "model endpoint returned HTTP 429",
        status_code=429,
        body_excerpt="rate limited, retry in 20s",
    )
    router = EscalatingModelRouter(local, None, settings=ModelSettings())
    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
        )

    details = excinfo.value.details
    assert details["underlying_error"] == "ModelUnavailableError"
    assert details["underlying_message"] == "model endpoint returned HTTP 429"
    assert details["underlying_status_code"] == 429
    assert details["underlying_body_excerpt"] == "rate limited, retry in 20s"


async def test_underlying_secrets_are_redacted_before_attachment() -> None:
    local = _RaisingProvider(
        f"model endpoint returned HTTP 401 for key {FAKE_KEY}",
        status_code=401,
        body_excerpt=f"invalid api key: {FAKE_KEY}",
    )
    router = EscalatingModelRouter(local, None, settings=ModelSettings())
    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
        )

    details = excinfo.value.details
    message = details["underlying_message"]
    excerpt = details["underlying_body_excerpt"]
    assert message == "model endpoint returned HTTP 401 for key ***REDACTED***"
    assert excerpt == "invalid api key: ***REDACTED***"
    assert FAKE_KEY not in str(details)
