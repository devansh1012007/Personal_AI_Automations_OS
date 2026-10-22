"""Local-first routing with explicit, ledger-logged escalation.

This is ADR-0015 decision 2 made executable. Cheap local models handle routing,
classification, extraction and summarisation — work where a small model is
genuinely adequate and where the volume makes API calls wasteful. Tasks
classified ``COMPLEX`` or ``MAX`` may escalate to a frontier model, and every
escalation writes a ``MODEL_ESCALATED`` event so the privacy boundary is
*auditable* rather than implicit.

The escalation policy is evaluated in a fixed order, and the order is the
design:

1. **Is escalation forbidden by the permission mode?** ``LOCKDOWN`` is an
   air-gap promise. It is checked first, before capability, before failure
   handling, before anything — because every other rule in this module is about
   *whether escalating is worthwhile* and this one is about whether it is
   *permitted*. A system that escalates from ``LOCKDOWN`` because the local
   model happened to fail has broken a security guarantee, silently, at exactly
   the moment the user was least able to notice. Local failure under
   ``LOCKDOWN`` raises. That is the correct outcome and it is not a degradation.

2. **Is the task complex enough to be worth it?** Below
   ``escalate_at_or_above`` the local model handles it or the call fails.
   Escalating a ``SIMPLE`` classification to a frontier model is how a
   local-first runtime quietly becomes an expensive API client.

3. **Only then**: try local, escalate on failure.

Concurrency is capped by an :class:`asyncio.Semaphore` sized from
``max_concurrent_streams`` (RFC §6.2 — 2 on the target hardware). That cap is a
memory bound, not a politeness setting: a second resident copy of a local
model's weights is what pushes a 3.5 GB machine into swap, and a swapping
machine does not recover on its own.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from paa.config import ModelSettings, Settings
from paa.core.types import (
    ComplexityModality,
    CorrelationId,
    EventType,
    PermissionMode,
    SessionId,
    new_correlation_id,
)
from paa.models.base import (
    CompletionRequest,
    CompletionResponse,
    ModelProvider,
    ModelTier,
    ModelUnavailableError,
    StructuredOutputError,
    redact,
)

if TYPE_CHECKING:
    from paa.ledger.store import LedgerStore

__all__ = [
    "EscalatingModelRouter",
    "EscalationDecision",
    "ProviderUsage",
    "resolve_model_settings",
]

log = structlog.get_logger(__name__)

#: Rank for the ``escalate_at_or_above`` comparison.
#:
#: ``ComplexityModality`` is a ``str`` enum, so ``COMPLEX > STANDARD`` would
#: compare *alphabetically* and quietly evaluate to ``False``. Ordering it
#: explicitly here is what stops a threshold check from being wrong in a way
#: that still runs.
_MODALITY_RANK: Final[dict[ComplexityModality, int]] = {
    ComplexityModality.SIMPLE: 0,
    ComplexityModality.STANDARD: 1,
    ComplexityModality.COMPLEX: 2,
    ComplexityModality.MAX: 3,
}


def resolve_model_settings(settings: ModelSettings | Settings) -> ModelSettings:
    """Accept either the full :class:`~paa.config.Settings` or its model half.

    The mirror of :func:`paa.storage.vector.embeddings.resolve_model_settings`,
    and for the same reason: half the call sites hold one and half hold the
    other, and an ``isinstance`` test beats a ``getattr`` because it actually
    narrows for the type checker. ``getattr(settings, "models", settings)``
    looks equivalent and is not — the ``models`` attribute exists on one
    member of the union and not on the other, so the fallback is the union
    itself, and every attribute read off the result stops being checked.

    Anything that is neither is **refused** rather than replaced by the
    defaults. The old ``resolved or _ModelSettings()`` fallback could not
    tell "no configuration" from "a value that happens to be falsy", and
    answered both with the defaults — and the defaults are not a neutral
    answer here, because what this configuration carries is the *escalation
    policy*: the modality floor and the permission modes that may never
    escalate. A deployment that configured "never escalate" and handed over
    a value the old fallback read as falsy would have run on the default
    policy instead, and escalated. Reasoning material would have left the
    machine under a policy that forbade it, which is the failure ADR-0015
    exists to prevent. Refusing is also the kinder error: a caller who
    passed the wrong object learns which object was wanted, instead of
    discovering it as an ``AttributeError`` several frames later.
    """
    if isinstance(settings, Settings):
        return settings.models
    if isinstance(settings, ModelSettings):
        return settings
    raise TypeError(
        "settings must be a Settings or a ModelSettings, got "
        f"{type(settings).__name__}"
    )


def _redact_detail(value: Any) -> Any:
    """Recursively scrub strings out of a detail value.

    A detail is not always a string: ``StructuredOutputError.schema_errors`` is
    a list of strings that embed the model's own output, which is exactly where
    a provider-echoed credential lands. Redacting only the top-level string
    left that list verbatim — and ``PaaError.details`` is merged into ledger
    event payloads *verbatim*, so a leak there becomes a permanent record.

    Keys are deliberately **not** scrubbed. They come from the exception's own
    detail names (``schema_errors``, ``status_code``, ...), fixed by the code
    that raised the error rather than by the model, so a key is an identifier
    callers and ledger payloads are written against — rewriting one would break
    the contract without removing a leak. Scrubbing the values is sufficient.

    Anything that is not a string, a list/tuple or a dict is passed through
    unchanged: numbers and booleans cannot carry a credential, and the detail
    values are required to stay JSON-serialisable for the ledger.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_detail(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_detail(item) for key, item in value.items()}
    return value


@dataclass
class ProviderUsage:
    """Per-provider counters consumed by the observability layer."""

    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    escalations_into: int = 0
    """Times this provider was reached *by escalation* rather than as first
    choice. The number that answers "how often did work leave the machine?"."""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class EscalationDecision:
    """The outcome of evaluating the policy, kept as data.

    Returned from a pure function so the policy can be unit-tested and quoted
    into a ledger payload without performing an inference call to observe it.
    """

    allowed: bool
    reason: str
    modality: ComplexityModality
    permission_mode: PermissionMode

    def to_payload(self) -> dict[str, Any]:
        return {
            "escalation_allowed": self.allowed,
            "policy_reason": self.reason,
            "modality": self.modality.value,
            "permission_mode": self.permission_mode.value,
        }


class EscalatingModelRouter:
    """Routes one request to the local provider, or escalates it.

    ``ledger_store`` is optional so the router is usable in a unit test, a
    migration script or a CLI one-shot without a database. It is optional, not
    ignorable: escalating without a store logs a warning naming the fact that
    the privacy boundary was crossed unaudited, because a silent unlogged
    escalation is the failure ADR-0015 exists to prevent.
    """

    def __init__(
        self,
        local: ModelProvider,
        frontier: ModelProvider | None = None,
        *,
        settings: ModelSettings | Settings | None = None,
        ledger_store: LedgerStore | None = None,
    ) -> None:
        # ``settings`` may be the whole runtime configuration or just its
        # ``models`` sub-model, and the two are told apart by their type
        # rather than by a duck-typed attribute lookup — see
        # :func:`resolve_model_settings` for why the ``getattr`` form could
        # not be type-checked and what the silent fallback it permitted cost.
        #
        # ``None`` means "no configuration at all", which is the defaults.
        # Anything that is neither a ``Settings`` nor a ``ModelSettings`` is
        # refused outright, so that a misconfigured deployment stops at the
        # boundary instead of quietly running on the default escalation
        # policy.
        self._settings: ModelSettings = (
            ModelSettings() if settings is None else resolve_model_settings(settings)
        )

        # **The deployment's permission mode, when the router was given one.**
        #
        # The mode lives on ``Settings.policy``; ``ModelSettings`` — the model
        # half, which is what half the call sites hold — carries no mode at
        # all. So the mode is available at construction *only* when the whole
        # ``Settings`` was handed over, and it used to be thrown away: the
        # constructor narrowed the argument to its model half and kept
        # nothing else. A router built by a LOCKDOWN deployment therefore
        # probed the frontier like any other, because the one method whose
        # whole purpose is to look safe defaulted to the permissive mode.
        #
        # When the mode is **not** available the answer is the most
        # restrictive one, not the most permissive one. LOCKDOWN forbids
        # escalation; a router that does not know its mode has no business
        # assuming escalation is permitted, and "no" is the answer that
        # cannot leak. A caller that knows the mode passes it — see
        # :meth:`healthcheck` — and a caller that does not gets the safe
        # default, which is the entire point of a default.
        self._permission_mode: PermissionMode = (
            settings.policy.mode
            if isinstance(settings, Settings)
            else PermissionMode.LOCKDOWN
        )

        self._local = local
        self._frontier = frontier
        self._ledger = ledger_store

        self._semaphore = asyncio.Semaphore(self._settings.max_concurrent_streams)
        self._usage: dict[str, ProviderUsage] = {}
        # No escalation counter lives here on purpose. The discriminator that
        # keeps repeated escalations apart is the *event's own id*, which is
        # unique across processes and restarts — a counter on the instance
        # would be a counter on the *process*, and a restarted router would
        # re-use the ordinals its predecessor spent, which the ledger's
        # idempotency key would then suppress: the audit trail would
        # under-count, silently, across a restart. See
        # :meth:`_record_escalation`.

    # -- introspection ------------------------------------------------------

    @property
    def local(self) -> ModelProvider:
        return self._local

    @property
    def frontier(self) -> ModelProvider | None:
        return self._frontier

    @property
    def can_escalate(self) -> bool:
        """Whether a frontier provider is wired at all."""
        return self._frontier is not None

    @property
    def max_concurrent_streams(self) -> int:
        return self._settings.max_concurrent_streams

    def token_usage(self) -> dict[str, dict[str, int]]:
        """Per-provider counters as plain dicts, for the metrics registry."""
        return {name: asdict(usage) for name, usage in sorted(self._usage.items())}

    def reset_usage(self) -> None:
        self._usage.clear()

    async def healthcheck(
        self, *, permission_mode: PermissionMode | None = None
    ) -> dict[str, bool]:
        """Probe the tiers the active permission mode permits. Never raises.

        The frontier is probed **only when escalation is permitted in
        ``permission_mode``**, and the probe is a network call: a hosted
        provider's ``healthcheck`` issues an authenticated HTTP GET, so
        probing the frontier under ``LOCKDOWN`` breaks the air-gap promise
        the mode exists to keep — silently, because a healthcheck looks like
        the safest call in the system.

        Rule 1 of the policy is the only rule that applies here: a
        healthcheck has no task, so there is no modality to weigh and the
        modality floor must not be what decides the answer. ``MAX`` is
        therefore the modality passed to :meth:`evaluate`, and only the
        permission-mode rule can block the probe.

        The frontier's key is **absent** from the report when it was not
        probed. Absent means "not asked", which is not the same as
        ``False`` ("asked, and it cannot serve") — a frontier that is
        perfectly healthy is still not probed under ``LOCKDOWN``.

        **The mode is the one the router was constructed with.** The
        argument is optional, and when it is omitted the router uses the
        mode it was given at construction: the deployment's own
        ``Settings.policy.mode`` if the whole ``Settings`` was handed over,
        and otherwise the most restrictive mode there is (``LOCKDOWN``),
        because a router that does not know its mode must not assume that
        escalation is permitted. The default used to be ``ASK`` — the
        *permissive* mode — which made the safe behaviour opt-in by
        accident: a router built by a LOCKDOWN deployment probed the
        frontier unless the caller remembered to pass the mode. The
        permission mode a caller passes explicitly still wins; this is only
        about what happens when nobody passes one.
        """
        mode = self._permission_mode if permission_mode is None else permission_mode
        report = {self._local.name: await self._local.healthcheck()}
        permitted = self.evaluate(ComplexityModality.MAX, mode).allowed
        if self._frontier is not None and permitted:
            report[self._frontier.name] = await self._frontier.healthcheck()
        return report

    def describe(self) -> dict[str, Any]:
        return {
            "local": self._local.describe(),
            "frontier": self._frontier.describe() if self._frontier else None,
            "escalate_at_or_above": self._settings.escalate_at_or_above.value,
            "escalation_forbidden_modes": [
                m.value for m in self._settings.escalation_forbidden_modes
            ],
            "max_concurrent_streams": self._settings.max_concurrent_streams,
            "ledger_attached": self._ledger is not None,
        }

    # -- policy -------------------------------------------------------------

    def block_reasons(
        self, modality: ComplexityModality, permission_mode: PermissionMode
    ) -> list[str]:
        """Every escalation rule this request violates, in evaluation order.

        Returns the module's rules 1 and 2 evaluated to completion — security
        first (``permission_mode_forbids_escalation``), then cost
        (``modality_below_escalation_floor:<floor>``) — and is empty when
        escalation is permitted.

        :meth:`evaluate` deliberately reports only the *first* violated rule:
        its first-reason-wins contract is what ledger payloads and existing
        callers are written against, and it does not change. This method is
        the same two rules gathered into a list for **error reporting**
        instead. An operator told only "escalation is not permitted: modality
        below floor" fixes the floor, re-runs, and hits the permission-mode
        rule as if it were a brand-new bug; showing every obstacle at once is
        the difference between a diagnostic and a map.
        """
        reasons: list[str] = []
        if permission_mode in self._settings.escalation_forbidden_modes:
            reasons.append("permission_mode_forbids_escalation")

        floor = self._settings.escalate_at_or_above
        if _MODALITY_RANK[modality] < _MODALITY_RANK[floor]:
            reasons.append(f"modality_below_escalation_floor:{floor.value}")

        return reasons

    def evaluate(
        self, modality: ComplexityModality, permission_mode: PermissionMode
    ) -> EscalationDecision:
        """Decide whether escalation is permitted. Pure; no I/O.

        Rules 1 and 2 of the module docstring, in that order. The order is
        load-bearing: rule 1 is a security boundary and rule 2 is a cost
        heuristic, and a cost heuristic must never be in a position to override
        a security boundary.

        When several rules are violated only the first is reported as
        ``reason``; :meth:`block_reasons` returns all of them.
        """
        if (reasons := self.block_reasons(modality, permission_mode)):
            return EscalationDecision(
                allowed=False,
                reason=reasons[0],
                modality=modality,
                permission_mode=permission_mode,
            )

        return EscalationDecision(
            allowed=True,
            reason="permitted",
            modality=modality,
            permission_mode=permission_mode,
        )

    # -- entry points -------------------------------------------------------

    async def complete(
        self,
        request: CompletionRequest,
        *,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        permission_mode: PermissionMode = PermissionMode.ASK,
        correlation_id: CorrelationId | uuid.UUID | None = None,
        reason: str | None = None,
        force_escalate: bool = False,
        session_id: SessionId | uuid.UUID | None = None,
    ) -> CompletionResponse:
        """Complete ``request``, escalating if policy and outcome allow it.

        :param force_escalate: skip the local attempt entirely. Still subject to
            rules 1 and 2 — a caller cannot opt out of ``LOCKDOWN``, and a
            ``SIMPLE`` task cannot buy its way to a frontier model. The local
            call is skipped rather than made-and-discarded because the caller
            has already concluded local output is unusable, so spending the
            latency and the RAM to prove it again is pure waste.
        :param reason: free text recorded in the ``MODEL_ESCALATED`` payload.
            Answers "why did this leave the machine?" months later.
        """
        result = await self._route(
            request,
            schema=None,
            modality=modality,
            permission_mode=permission_mode,
            correlation_id=correlation_id,
            reason=reason,
            force_escalate=force_escalate,
            session_id=session_id,
        )
        assert isinstance(result, CompletionResponse)
        return result

    async def complete_structured(
        self,
        request: CompletionRequest,
        schema: dict[str, Any],
        *,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        permission_mode: PermissionMode = PermissionMode.ASK,
        correlation_id: CorrelationId | uuid.UUID | None = None,
        reason: str | None = None,
        force_escalate: bool = False,
        session_id: SessionId | uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """As :meth:`complete`, but the result must satisfy ``schema``.

        Schema-validation failure on the local provider is a first-class
        escalation trigger, not merely an error. Reliable structured output
        under a schema is precisely the capability ADR-0015 identifies as
        missing from small models, so "the local model cannot express this
        answer in the required shape" is the clearest possible signal that the
        task has outgrown its tier.
        """
        result = await self._route(
            request,
            schema=schema,
            modality=modality,
            permission_mode=permission_mode,
            correlation_id=correlation_id,
            reason=reason,
            force_escalate=force_escalate,
            session_id=session_id,
        )
        assert isinstance(result, dict)
        return result

    # -- routing ------------------------------------------------------------

    async def _route(
        self,
        request: CompletionRequest,
        *,
        schema: dict[str, Any] | None,
        modality: ComplexityModality,
        permission_mode: PermissionMode,
        correlation_id: CorrelationId | uuid.UUID | None,
        reason: str | None,
        force_escalate: bool,
        session_id: SessionId | uuid.UUID | None,
    ) -> CompletionResponse | dict[str, Any]:
        decision = self.evaluate(modality, permission_mode)

        if not decision.allowed:
            return await self._local_only(request, schema, decision)

        trigger = "force_escalate"
        local_error: Exception | None = None

        if not force_escalate:
            try:
                return await self._invoke(self._local, request, schema)
            except StructuredOutputError as exc:
                trigger, local_error = "local_structured_output_invalid", exc
            except ModelUnavailableError as exc:
                trigger, local_error = "local_provider_unavailable", exc

        if self._frontier is None:
            # Raised with no chain: the provider's own exception is carried in
            # the details (scrubbed), never attached to this one. See
            # :meth:`_local_only_error` for why an exception that may hold a
            # credential is never chained.
            raise self._no_frontier_error(trigger, decision, local_error) from None

        await self._record_escalation(
            trigger=trigger,
            reason=reason,
            decision=decision,
            correlation_id=correlation_id,
            session_id=session_id,
            local_error=local_error,
        )
        return await self._invoke(self._frontier, request, schema, escalated=True)

    async def _local_only(
        self,
        request: CompletionRequest,
        schema: dict[str, Any] | None,
        decision: EscalationDecision,
    ) -> CompletionResponse | dict[str, Any]:
        """Rule 1 / rule 2 path: the local provider is the only option.

        A failure here is raised, never quietly escalated. Under ``LOCKDOWN``
        that is the air-gap promise being kept; below the modality floor it is
        the cost ceiling being kept. Both are guarantees, and a guarantee that
        bends under load is not one.

        The policy decision itself is unchanged — only its *reporting* is
        richer, because the original error showed one violated rule and the
        exception type's name, which told an operator neither how many
        obstacles stood in the way nor why the provider failed. The details
        carried on the raised error are:

        ``escalation_block_reason``
            ``decision.reason`` — the *first* violated rule, byte-identical to
            the original contract so existing readers keep working.
        ``escalation_block_reasons``
            **every** violated rule from :meth:`block_reasons`, in rule order
            (security before cost). Always a list, even for a single rule, so
            callers parse one shape instead of branching on type.
        ``underlying_error`` / ``underlying_message``
            the provider failure's type name (as before) plus its own message
            — ``"model endpoint returned HTTP 429"`` rather than just
            ``ModelUnavailableError``. The type name alone discarded the
            diagnosis the provider had already built.
        ``underlying_<key>``
            each of the underlying error's ``details`` entries (``status_code``,
            ``body_excerpt``, ...), prefixed, with every string value — at any
            depth, including inside lists, tuples and nested dicts — passed
            through :func:`~paa.models.base.redact`. A detail is not always a
            string: ``StructuredOutputError.schema_errors`` is a list of
            strings that embed the model's own output, which is where a
            provider-echoed credential lands, so a top-level-only scrub is not
            enough. :class:`PaaError` redacts only the message on the way in
            and merges details into ledger payloads *verbatim*, so the scrub
            happens here; explicit keys listed above win on a name collision,
            ``underlying_*`` never overwrite them.
        ``frontier_configured`` / ``escalation_unavailable_reason``
            whether a frontier provider is wired, as a bool and as a stable
            string key (``"no_frontier_configured"`` or ``None``). Frontier
            presence is **irrelevant to this decision**: ``evaluate`` blocked
            escalation on policy before any wiring was consulted, so a missing
            frontier neither caused nor could have prevented the block and it
            is deliberately *not* appended to ``escalation_block_reasons``. It
            surfaces anyway because it is the fact the operator needs next —
            "would escalation even help if I raised the modality or left
            ``LOCKDOWN``?" — and without it the error reads as if configuring
            a frontier provider would have changed the outcome.

        **The provider's exception is not chained to the one this path
        raises.** See :meth:`_local_only_error`.
        """
        failure: ModelUnavailableError | StructuredOutputError | None = None
        try:
            return await self._invoke(self._local, request, schema)
        except (ModelUnavailableError, StructuredOutputError) as exc:
            failure = exc
        # The ``try`` above returns; the only way to reach this line is the
        # handler, which always assigns. The assert is the narrowing the type
        # checker needs, not a runtime check.
        assert failure is not None
        # Raised **outside** the handler, and with no chain. Both matter:
        # inside the handler the provider's exception is the one being
        # handled, so raising there would attach it as the new error's
        # ``__context__`` even without ``from``; and the provider's
        # exception may carry a credential this router cannot recognise.
        raise self._local_only_error(failure, decision) from None

    def _local_only_error(
        self,
        exc: ModelUnavailableError | StructuredOutputError,
        decision: EscalationDecision,
    ) -> ModelUnavailableError:
        """The error the local-only path raises, with the provider's diagnosis.

        **Why the provider's exception is not chained.** The raised error used
        to be ``raise ... from exc``, which attaches the provider's own
        exception as the ``__cause__`` — and the provider's exception is the
        one object in this story that can hold a credential the router has no
        way to recognise. The router scrubs the *copy* of the diagnosis it
        attaches (``underlying_*``, every string value at any depth), but a
        chained exception is not a copy: it is the provider's object, with
        whatever the provider put in it, and it is rendered by
        ``traceback.format_exception`` — which is the text an operator pastes
        into a bug report. Scrubbing the copy and chaining the original
        cancels out: the key that was removed from the details is still in the
        traceback.

        So the router's error carries the diagnosis as *data* (scrubbed, in
        the details, and therefore safe in a ledger payload) and carries no
        exception at all. The provider's own exception stays with the
        provider, which is where its scrubbing belongs — the router cannot
        promise that an arbitrary provider's exception is clean, and a
        guarantee it cannot keep is not a guarantee.

        What is lost is the provider's *stack*, not its diagnosis: the type
        name, the message and every detail are still on the raised error.
        What is gained is that no credential can reach a traceback through
        this path, whatever shape the provider's key takes.
        """
        frontier_ok = self._frontier is not None
        details: dict[str, Any] = {
            f"underlying_{key}": _redact_detail(value)
            for key, value in exc.details.items()
        }
        details.update(
            {
                "escalation_block_reason": decision.reason,
                "escalation_block_reasons": self.block_reasons(
                    decision.modality, decision.permission_mode
                ),
                "underlying_error": type(exc).__name__,
                "underlying_message": redact(getattr(exc, "message", str(exc))),
                "frontier_configured": frontier_ok,
                "escalation_unavailable_reason": (
                    None if frontier_ok else "no_frontier_configured"
                ),
                **decision.to_payload(),
            }
        )
        return ModelUnavailableError(
            "local provider failed and escalation is not permitted",
            provider=self._local.name,
            **details,
        )

    def _no_frontier_error(
        self,
        trigger: str,
        decision: EscalationDecision,
        local_error: Exception | None,
    ) -> ModelUnavailableError:
        """The error raised when escalation is permitted but nothing can serve it.

        The provider's own failure — if there was one — is carried in the
        details, scrubbed, and **not chained**: the same rule as
        :meth:`_local_only_error`, for the same reason. The chain used to be
        the only place the provider's diagnosis appeared on this path, so
        dropping it without adding the details would have thrown the
        diagnosis away; with the details, the operator reads the same facts
        from a surface that cannot carry a credential.
        """
        details: dict[str, Any] = {
            "escalation_trigger": trigger,
            # Always False inside this branch; carried for parity with the
            # _local_only error so a caller reads one wiring fact from one
            # stable key on either path.
            "frontier_configured": False,
            **decision.to_payload(),
        }
        if local_error is not None:
            details.update(
                {
                    f"underlying_{key}": _redact_detail(value)
                    for key, value in getattr(local_error, "details", {}).items()
                }
            )
            details["underlying_error"] = type(local_error).__name__
            details["underlying_message"] = redact(
                getattr(local_error, "message", str(local_error))
            )
        return ModelUnavailableError(
            "escalation is permitted but no frontier provider is configured",
            provider=self._local.name,
            **details,
        )

    async def _invoke(
        self,
        provider: ModelProvider,
        request: CompletionRequest,
        schema: dict[str, Any] | None,
        *,
        escalated: bool = False,
    ) -> CompletionResponse | dict[str, Any]:
        """Call a provider under the concurrency cap, accounting for tokens.

        The semaphore wraps the *call*, not the routing decision: a request
        waiting on the cap holds no model memory, and blocking the policy
        evaluation behind it would serialise work that never touches a model.
        """
        usage = self._usage.setdefault(provider.name, ProviderUsage())
        if escalated:
            usage.escalations_into += 1

        async with self._semaphore:
            try:
                if schema is None:
                    response = await provider.complete(request)
                    usage.calls += 1
                    usage.prompt_tokens += response.prompt_tokens
                    usage.completion_tokens += response.completion_tokens
                    return response

                structured = await provider.complete_structured(request, schema)
            except Exception:
                usage.calls += 1
                usage.failures += 1
                raise
            usage.calls += 1
            return structured

    # -- audit --------------------------------------------------------------

    async def _record_escalation(
        self,
        *,
        trigger: str,
        reason: str | None,
        decision: EscalationDecision,
        correlation_id: CorrelationId | uuid.UUID | None,
        session_id: SessionId | uuid.UUID | None,
        local_error: Exception | None,
    ) -> None:
        """Append exactly one ``MODEL_ESCALATED`` event per escalation.

        Written **before** the frontier call, not after. The auditable fact is
        that the boundary was crossed — a request that leaves the machine and
        then times out has still left the machine, and logging on success would
        make exactly the failed escalations invisible.

        The event's ``discriminator`` is the **event's own uuid4**, written as
        ``escalation:<nonce>``.

        It used to be a counter on the router instance, which made it *per
        process*: a restarted router began again at ``escalation:1``, re-used
        the key its predecessor had already spent, and the ledger **silently
        suppressed** the second audit record. The audit trail then
        under-counted exactly when the system was least able to notice — and a
        restart is not a rare event. Two escalations are two crossings of the
        privacy boundary, so they are two records.

        For a while the discriminator also carried a **per-correlation
        escalation ordinal** — the number of ``MODEL_ESCALATED`` events the
        lineage already held, plus one — read from the ledger at write time.
        That was the wave-2 fix for the per-process counter, and it was
        *correct about the property and wasteful about the mechanism*: the
        ordinal was a denormalised copy of a fact the ledger already holds
        exactly, and it was copied badly.

        * **The ledger already numbers the lineage.** ``state_version`` is
          dense, per-correlation, and assigned inside the store's per-lineage
          lock, so "the Nth crossing of this boundary in this lineage" is a
          fact the ledger answers *deterministically* — including under
          concurrency, where two writers are serialised and get consecutive
          versions. The ordinal in the discriminator was a second, racy copy
          of a number the ledger had already computed.
        * **The copy was racy where the original was not.** Two writers that
          read the lineage at the same moment took the same ordinal. Both
          records survived (the nonce did that job) but the label repeated,
          so the "per-correlation ordinal" was a sequence only in the
          sequential case.
        * **The copy cost a read of the whole lineage** — O(lineage) rows
          parsed to count a number — and made the audit write depend on that
          read succeeding. A store whose read failed aborted the escalation
          before the frontier call, so a *label* could cost a crossing of the
          privacy boundary.

        So the ordinal is gone, and the discriminator is the nonce alone:
        ``escalation:<the event's uuid4 hex>``. The uniqueness argument is
        the ledger's own — "the event id is unique" — and it holds across a
        restart, across processes, and under concurrency, with **no read at
        all** before the write. The sequence the ordinal used to carry is
        still there, and it is still per-correlation: it is the lineage's
        ``state_version`` order, which is the ledger's own truth and does not
        need a second number to be derived from it.
        """
        assert self._frontier is not None
        ledger = self._ledger

        payload = {
            "from_model": self._local.model,
            "from_provider": self._local.name,
            "from_tier": self._local.tier.name,
            "to_model": self._frontier.model,
            "to_provider": self._frontier.name,
            "to_tier": self._frontier.tier.name,
            "reason": reason or trigger,
            "trigger": trigger,
            "modality": decision.modality.value,
            "permission_mode": decision.permission_mode.value,
            "leaves_machine": self._frontier.tier >= ModelTier.FRONTIER,
        }
        if local_error is not None:
            # Redacted, exactly as the local-only path redacts the details it
            # copies. ``str(error)`` renders the provider's own diagnosis —
            # its message *and* every detail it carried — and a detail is not
            # always a string: ``StructuredOutputError.schema_errors`` is a
            # list of strings built from the model's own output, which is
            # where a provider-echoed credential lands. ``PaaError`` merges
            # details into a ledger payload *verbatim*, so an unscrubbed copy
            # here becomes a permanent record of the credential. The local
            # path was given this treatment; the escalation path never was.
            payload["local_error"] = redact(str(local_error))
            payload["local_error_type"] = type(local_error).__name__

        if ledger is None:
            log.warning(
                "model.escalation_unlogged",
                unaudited=True,
                impact=(
                    "reasoning material left the machine without a MODEL_ESCALATED "
                    "ledger event; the privacy boundary is not auditable for this call"
                ),
                **payload,
            )
            return

        if correlation_id is None:
            attributed = False
            # Recording an orphan lineage beats dropping the audit record: the
            # event that must never go missing is the one that says work left
            # the machine.
            correlation_id = new_correlation_id()
            log.warning(
                "model.escalation_unattributed",
                correlation_id=str(correlation_id),
                impact="escalation recorded against a synthetic correlation id",
            )
        else:
            attributed = True

        from paa.ledger.events import LedgerEvent

        # The nonce is the event's *own* id, so "the discriminator is unique"
        # is exactly as strong as "the event id is unique" — the ledger's
        # own argument — and there is no second source of uniqueness to
        # justify, and **nothing to read before the write**. The lineage's
        # escalation sequence is the ledger's own ordering (see the method
        # docstring), not a number this router has to go and count.
        event_id = uuid.uuid4()
        event = LedgerEvent.create(
            correlation_id,
            EventType.MODEL_ESCALATED,
            event_id=event_id,
            session_id=session_id,
            payload=payload,
            execution_mode=decision.modality,
            discriminator=f"escalation:{event_id.hex}",
        )
        stored = await ledger.append(event)
        if stored.event_id != event.event_id:
            # The store returned the *existing* event: this escalation's
            # idempotency key was already spent. With the nonce in the
            # discriminator that means a uuid4 collision — the same order of
            # improbability the ledger's own ``event_id`` relies on. Say so
            # loudly rather than let the boundary be crossed unrecorded.
            log.error(
                "model.escalation_unaudited",
                unaudited=True,
                correlation_id=str(correlation_id),
                impact=(
                    "reasoning material left the machine and the MODEL_ESCALATED "
                    "ledger event was suppressed as a duplicate; the privacy "
                    "boundary is not auditable for this call"
                ),
                **payload,
            )
            return

        log.info(
            "model.escalated",
            correlation_id=str(correlation_id),
            attributed=attributed,
            **payload,
        )
