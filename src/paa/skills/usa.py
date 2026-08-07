"""The Unified Skill Adapter — RFC §8.2's dispatch state machine.

Every skill invocation the runtime makes flows through :meth:`UnifiedSkillAdapter.dispatch`,
which walks the RFC §8.2 pipeline in a fixed order:

1. **discovery** — resolve the skill from the registry (or, for exploratory
   callers, :meth:`discover` runs a semantic/trigram search first);
2. **intent compilation** — bind the caller's arguments to the contract and
   check them against ``input_schema`` before anything runs;
3. **security authorization** — the contract's ``required_permissions`` must be a
   subset of the active mode's grants, or dispatch is refused *before a sandbox
   boots*. A skill needing ``NET_EGRESS`` under ``LOCKDOWN`` is a hard stop, not
   a downgrade;
4. **sandbox mount + invoke** — hand off to the adapter for the contract's
   provider;
5. **secret proxy** — the skill reaches credentials only through a host-bound
   :class:`~paa.skills.adapters.base.SecretProvider`; the host resolves each via
   the :class:`~paa.skills.secrets.SecretBroker` after checking
   ``Permission.SECRET_READ``, and the value is never written to the child's
   environment or to any log;
6. **output validation** — the return value is checked against ``output_schema``;
7. **reliability adjustment** — the skill's weight rises on a clean, schema-valid
   success and falls on a crash or malformed output, so the planner's future
   choices are informed by observed behaviour.

The ordering is the security property. Authorization precedes execution;
validation precedes the reliability signal. Reordering any step would let a
skill do something the mode forbids, or let malformed output be scored as a win.

What raises vs. what returns
----------------------------
A *refusal before execution* — unknown skill, invalid contract, permission
denied — raises, because it produces no evidence about the skill and must not
drag its reliability down. Everything from step 4 onward returns a
:class:`~paa.skills.contracts.SkillResult`: a crash, a timeout and garbage output
are all *samples* the optimiser needs, so they come back as data, not exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from paa.core.errors import PermissionDeniedError, SkillContractError
from paa.core.types import Permission, PermissionMode
from paa.skills.contracts import SkillResult
from paa.validation.schema_validator import SchemaValidator

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.skills.adapters.base import SkillAdapter
    from paa.skills.adapters.base import SkillResult as AdapterResult
    from paa.skills.contracts import SkillContract
    from paa.skills.registry import SkillRegistry, SkillVectorStore
    from paa.skills.secrets import SecretBroker, SecretValue

__all__ = ["UnifiedSkillAdapter"]

log = structlog.get_logger(__name__)

#: Default reliability nudges. Down is larger than up (a 2:1 ratio): a skill that
#: lies about its output is more costly than one clean run is reassuring, so the
#: EWMA should react faster to failure than to success. Both feed the CHECK-
#: constrained ``reliability_weight`` column, which the registry clamps to [0,1].
_RELIABILITY_UP = 0.05
_RELIABILITY_DOWN = 0.10


class _BoundSecretProxy:
    """A :class:`~paa.skills.adapters.base.SecretProvider` with the authorization
    context baked in.

    The USA constructs this once per dispatch, binding the active mode's grants
    and a requester identity, and hands it to the adapter. The adapter — and the
    skill behind it — can therefore *ask* for a secret but cannot *decide* whether
    it is allowed one: that decision lives in :meth:`SecretBroker.get_secret`,
    which this proxy always calls with the bound grants. The returned
    :class:`~paa.skills.secrets.SecretValue` redacts itself everywhere but an
    explicit ``.reveal()``.
    """

    __slots__ = ("_broker", "_correlation_id", "_granted", "_requester")

    def __init__(
        self,
        broker: SecretBroker,
        granted: frozenset[Permission],
        requester: str,
        correlation_id: str | None,
    ) -> None:
        self._broker = broker
        self._granted = granted
        self._requester = requester
        self._correlation_id = correlation_id

    def get_secret(self, name: str) -> SecretValue:
        return self._broker.get_secret(
            name,
            granted=self._granted,
            requester=self._requester,
            correlation_id=self._correlation_id,
        )


class UnifiedSkillAdapter:
    """Drives one skill dispatch through the RFC §8.2 pipeline.

    Holds the registry, the per-provider adapters and the output validator. One
    instance serves the whole runtime; it carries no per-dispatch state.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        adapters: list[SkillAdapter],
        *,
        validator: SchemaValidator | None = None,
        reliability_up: float = _RELIABILITY_UP,
        reliability_down: float = _RELIABILITY_DOWN,
    ) -> None:
        self._registry = registry
        self._adapters: dict[str, SkillAdapter] = {a.provider: a for a in adapters}
        self._validator = validator or SchemaValidator()
        self._reliability_up = reliability_up
        self._reliability_down = reliability_down

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    async def discover(
        self,
        intent: str,
        *,
        vector_store: SkillVectorStore | None = None,
        limit: int = 10,
    ) -> list[SkillContract]:
        """RFC §8.2 step 1: find skills relevant to ``intent``."""
        return await self._registry.search(intent, vector_store=vector_store, limit=limit)

    async def dispatch(
        self,
        skill_name: str,
        arguments: dict[str, Any],
        *,
        mode: PermissionMode,
        sandbox: Sandbox | None = None,
        secret_broker: SecretBroker | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - deliberate per-dispatch budget
        correlation_id: str | None = None,
    ) -> SkillResult:
        """Run ``skill_name`` end to end. The runtime's single skill entry point.

        :raises SkillContractError: the skill is unknown/inactive, its contract
            is invalid, or no adapter services its provider.
        :raises PermissionDeniedError: the contract requires a permission the
            active ``mode`` does not grant.
        """
        # -- step 1: discovery (resolve the concrete contract) ---------------
        contract = await self._registry.get(skill_name)
        if contract is None:
            raise SkillContractError(
                "skill is unknown or inactive", skill_name=skill_name
            )

        # -- step 2: intent compilation (bind + validate arguments) ----------
        input_errors = contract.validate_input(arguments)
        if input_errors:
            # A refusal before execution: the skill produced no evidence, so it
            # is raised rather than scored.
            raise SkillContractError(
                "arguments do not satisfy the skill's input_schema",
                skill_name=skill_name,
                findings=[{"field": "arguments", "problem": e} for e in input_errors],
            )

        # -- step 3: security authorization ---------------------------------
        granted = mode.granted
        missing = contract.missing_permissions(granted)
        if missing:
            log.warning(
                "skills.usa.permission_denied",
                skill=skill_name,
                mode=mode.value,
                missing=[p.value for p in missing],
            )
            raise PermissionDeniedError(
                missing=[p.value for p in missing], mode=mode.value
            )

        adapter = self._adapters.get(contract.provider)
        if adapter is None:
            raise SkillContractError(
                "no adapter is registered for this provider",
                skill_name=skill_name,
                findings=[{"field": "provider", "problem": contract.provider}],
            )

        # -- step 5 (prepared): the bound secret proxy ----------------------
        # Built before invoke so the adapter can pass it to the skill. The
        # permission gate lives inside the broker, invoked with these grants —
        # the proxy cannot widen them.
        proxy = (
            _BoundSecretProxy(
                secret_broker,
                granted,
                requester=f"skill:{skill_name}",
                correlation_id=correlation_id,
            )
            if secret_broker is not None
            else None
        )

        log.info(
            "skills.usa.dispatch",
            skill=skill_name,
            provider=contract.provider,
            mode=mode.value,
            correlation_id=correlation_id,
        )

        # -- step 4: sandbox mount + invoke ---------------------------------
        adapter_result = await adapter.invoke(
            contract,
            arguments,
            sandbox=sandbox,
            timeout=timeout,
            secret_broker=proxy,
        )

        # -- steps 6 + 7: validate output, then adjust reliability ----------
        return await self._finalise(skill_name, contract, adapter_result)

    async def _finalise(
        self,
        skill_name: str,
        contract: SkillContract,
        result: AdapterResult,
    ) -> SkillResult:
        """RFC §8.2 steps 6-7: schema-gate the output and score the run."""
        if not result.ok:
            # The mechanism failed (crash, timeout, non-JSON). A failure is a
            # sample: nudge reliability down, but return rather than raise.
            weight = await self._registry.update_reliability(
                skill_name, -self._reliability_down
            )
            log.info(
                "skills.usa.execution_failed",
                skill=skill_name,
                error=result.error,
                reliability=round(weight, 4),
            )
            return SkillResult(
                skill_name=skill_name,
                ok=False,
                output={},
                error=result.error,
                duration_ms=result.latency_ms,
                output_valid=False,
                exit_code=result.exit_code,
                adapter=contract.provider,
            )

        # The SchemaValidator is the runtime's single output gate (RFC §13); use
        # it rather than re-deriving validation so skill output is judged by the
        # exact same subset every other host-side check uses.
        raw_errors = self._validator.check(result.output, contract.output_schema)
        schema_errors = [str(e) for e in raw_errors]
        output_valid = not schema_errors

        delta = self._reliability_up if output_valid else -self._reliability_down
        weight = await self._registry.update_reliability(skill_name, delta)

        if not output_valid:
            log.warning(
                "skills.usa.output_invalid",
                skill=skill_name,
                error_count=len(schema_errors),
                reliability=round(weight, 4),
            )
        else:
            log.info(
                "skills.usa.execution_completed",
                skill=skill_name,
                reliability=round(weight, 4),
            )

        return SkillResult(
            skill_name=skill_name,
            # ``ok`` reports that the mechanism ran; ``output_valid`` is the
            # separate verdict on whether what it returned is believable.
            ok=True,
            output=result.output,
            error=None if output_valid else "output failed schema validation",
            duration_ms=result.latency_ms,
            output_valid=output_valid,
            schema_errors=tuple(schema_errors),
            exit_code=result.exit_code,
            adapter=contract.provider,
        )
