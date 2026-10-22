"""No API key ever reaches a log line, a ledger payload, or an exception.

The escalation boundary is the privacy promise, and a credential that
crosses it is not a near miss: it is a copy of the key in the ledger, in the
log aggregator, and in whatever the operator pastes into a bug report.

The leak this file exists to close was on the **escalation** path. The
local-only path already scrubbed the provider's diagnosis before attaching
it to the error it raised; the escalation path copied ``str(error)`` into
the ``MODEL_ESCALATED`` ledger payload and into the ``model.escalated`` log
line with no scrub at all — and ``str(error)`` renders the exception's
``details``, which for a schema failure include the model's *own output*,
because ``validate_against_schema`` quotes the offending instance into its
message. A provider that echoed a credential back put it straight into
``schema_errors``, and the same exception redacted its ``raw_excerpt`` while
leaving that list verbatim.

Every assertion here goes through the house helper
``assert_no_key_leak`` — there is one, and a fourth copy of it would be a
fourth thing to keep in step.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import structlog.testing

from paa.config import ModelSettings
from paa.core.types import ComplexityModality, EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
    StructuredOutputError,
)
from paa.models.echo_provider import EchoProvider
from paa.models.router import EscalatingModelRouter

from .conftest import FAKE_KEY, assert_no_key_leak

#: A schema the model's output cannot satisfy when it carries the key: the
#: ``enum`` check is what quotes the offending instance into the error.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"token": {"type": "string", "enum": ["ok"]}},
    "required": ["token"],
}

#: The model's own output, carrying the credential.
_MODEL_OUTPUT = json.dumps({"token": FAKE_KEY})


def _request() -> CompletionRequest:
    return CompletionRequest(messages=(Message.user("give me the token"),))


class _FixedTextProvider(ModelProvider):
    """Answers with one fixed string — the model's own output, verbatim.

    The point of driving the *real* ``complete_structured`` is that the
    error message is then built by ``validate_against_schema`` out of the
    model's own output, which is how a credential reaches ``schema_errors``
    in production. A hand-written ``StructuredOutputError`` would exercise
    the exception's constructor and not the path that fills it.
    """

    def __init__(
        self,
        text: str,
        *,
        name: str = "local",
        tier: ModelTier = ModelTier.LOCAL,
    ) -> None:
        # ``max_retries=0``: one attempt, so the failure is the schema
        # failure and not a repair attempt's.
        super().__init__(max_retries=0)
        self._text = text
        self._name = name
        self._tier = tier
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> ModelTier:
        return self._tier

    @property
    def model(self) -> str:
        return "fixed-text"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(
            text=self._text,
            model=self.model,
            provider=self.name,
            finish_reason="stop",
        )

    async def healthcheck(self) -> bool:
        return True


class _UnscrubbedDetailProvider(ModelProvider):
    """Fails the way a provider that forgot to scrub its own detail does.

    :class:`~paa.models.base.ModelUnavailableError` redacts the *message* it
    is given and carries its ``**details`` verbatim, so a provider that
    puts a credential in a detail leaves it in ``str(error)``. The HTTP
    provider scrubs its own body excerpt; the router must not *depend* on
    every provider doing so, because the local-only path does not.
    """

    @property
    def name(self) -> str:
        return "local"

    @property
    def tier(self) -> ModelTier:
        return ModelTier.LOCAL

    @property
    def model(self) -> str:
        return "unscrubbed"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise ModelUnavailableError(
            "model endpoint returned HTTP 401",
            provider=self.name,
            status_code=401,
            body_excerpt=f"invalid api key: {FAKE_KEY}",
        )

    async def healthcheck(self) -> bool:
        return False


def _router(
    local: ModelProvider,
    *,
    frontier: ModelProvider | None = None,
    ledger: LedgerStore | None = None,
) -> EscalatingModelRouter:
    return EscalatingModelRouter(
        local, frontier, settings=ModelSettings(), ledger_store=ledger
    )


# -- the exception itself ----------------------------------------------------


async def test_the_schema_failure_carries_no_key() -> None:
    """The exception's own text and details are clean.

    ``PaaError.__str__`` renders every detail verbatim, and ``to_payload``
    merges them into the ledger payload, so a key in ``details`` is a key in
    every message and every record the exception touches.
    """
    provider = _FixedTextProvider(_MODEL_OUTPUT)

    with pytest.raises(StructuredOutputError) as excinfo:
        await provider.complete_structured(_request(), _SCHEMA)

    error = excinfo.value
    assert_no_key_leak(str(error), error.details, error.schema_errors)
    # Scrubbed, not dropped: the operator still needs the diagnosis.
    assert error.schema_errors == ["$.token: '***REDACTED***' is not one of ['ok']"]


# -- the escalation path -----------------------------------------------------


async def test_the_escalation_payload_carries_no_key(ledger: LedgerStore) -> None:
    """The ``MODEL_ESCALATED`` payload is clean.

    This is the record that says reasoning material left the machine, and it
    is the record that is kept forever. A credential in it is a credential
    in the ledger.
    """
    local = _FixedTextProvider(_MODEL_OUTPUT)
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    router = _router(local, frontier=frontier, ledger=ledger)
    correlation_id = uuid.uuid4()

    await router.complete_structured(
        _request(),
        _SCHEMA,
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    events = await ledger.read_correlation(correlation_id)
    escalations = [e for e in events if e.event_type is EventType.MODEL_ESCALATED]
    assert len(escalations) == 1
    payload = escalations[0].payload
    assert_no_key_leak(payload, payload.get("local_error"))
    # The diagnosis survives the scrub — a payload that says nothing is not
    # an audit record.
    assert "REDACTED" in str(payload["local_error"])


async def test_no_log_line_carries_no_key(ledger: LedgerStore) -> None:
    """Every captured log line is clean.

    The escalation log line carries the payload, so the payload's scrub is
    the log line's scrub — but the assertion is made on the *logs*, because
    the logs are what an operator pastes into a bug report.
    """
    local = _FixedTextProvider(_MODEL_OUTPUT)
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    router = _router(local, frontier=frontier, ledger=ledger)

    with structlog.testing.capture_logs() as logs:
        await router.complete_structured(
            _request(),
            _SCHEMA,
            modality=ComplexityModality.COMPLEX,
            permission_mode=PermissionMode.ASK,
            correlation_id=uuid.uuid4(),
        )

    escalated = [entry for entry in logs if entry.get("event") == "model.escalated"]
    assert len(escalated) == 1
    for entry in logs:
        assert_no_key_leak(entry)


async def test_the_escalation_payload_scrubs_the_provider_details(
    ledger: LedgerStore,
) -> None:
    """A detail the provider did not scrub is scrubbed on the way in.

    The escalation path copies the provider's diagnosis — its message *and*
    its details — into the payload. The local-only path scrubs what it
    copies; this path did not, so a provider that left a credential in a
    detail put it in the ledger.
    """
    local = _UnscrubbedDetailProvider()
    frontier = EchoProvider(name="frontier-echo", tier=ModelTier.FRONTIER)
    router = _router(local, frontier=frontier, ledger=ledger)
    correlation_id = uuid.uuid4()

    await router.complete(
        _request(),
        modality=ComplexityModality.COMPLEX,
        permission_mode=PermissionMode.ASK,
        correlation_id=correlation_id,
    )

    events = await ledger.read_correlation(correlation_id)
    escalations = [e for e in events if e.event_type is EventType.MODEL_ESCALATED]
    assert len(escalations) == 1
    payload = escalations[0].payload
    assert_no_key_leak(payload, payload.get("local_error"))
    assert "REDACTED" in str(payload["local_error"])


async def test_the_local_only_path_still_carries_no_key() -> None:
    """The path that already scrubbed must keep scrubbing.

    A guard, not a claim about the fix: the local-only path attaches the
    provider's details as ``underlying_*`` and scrubs them. The escalation
    path now does the same, and this test is what says the two agree.
    """
    local = _UnscrubbedDetailProvider()
    router = _router(local)

    with pytest.raises(ModelUnavailableError) as excinfo:
        await router.complete(
            _request(),
            modality=ComplexityModality.STANDARD,
            permission_mode=PermissionMode.ASK,
        )

    error = excinfo.value
    assert_no_key_leak(str(error), error.details)
    assert error.details["underlying_body_excerpt"] == "invalid api key: ***REDACTED***"
