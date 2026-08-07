"""The adapter contract — one execution modality per implementation.

RFC §8 names four ways a capability can reach the runtime: a Claw-Hub skill
directory, a self-hosted MCP server, an in-process native callable, and a
marketplace package. Each has a wholly different execution mechanic — a
subprocess over a mounted directory, a JSON-RPC stdio session, a direct function
call — but the *runtime* wants one shape to drive them all. This module is that
shape.

Two responsibilities, deliberately split from the registry:

* :meth:`SkillAdapter.discover` turns a source (a directory, a running server, a
  registration table) into :class:`~paa.skills.contracts.SkillContract`\\ s. The
  registry persists them; the adapter only *finds* them.
* :meth:`SkillAdapter.invoke` runs one skill and returns a :class:`SkillResult`.

Why a second result type
------------------------
:class:`paa.skills.contracts.SkillResult` is the runtime's rich, ledger-facing
outcome (it carries ``output_valid``, ``schema_errors``, the adapter name and
the reliability-relevant verdict). :class:`SkillResult` *here* is the thin,
mechanical thing an adapter can honestly produce: did the mechanism run, what did
it emit, how long did it take. The Unified Skill Adapter (:mod:`paa.skills.usa`)
is the only place that knows about the output schema and the reliability model,
so it — not the adapter — mints the rich result. Keeping the adapter result
narrow stops every adapter from having to re-implement schema validation, and
stops it from *pretending* to a verdict it cannot reach.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.skills.contracts import SkillContract
    from paa.skills.secrets import SecretValue

__all__ = ["SecretProvider", "SkillAdapter", "SkillResult"]


@runtime_checkable
class SecretProvider(Protocol):
    """The one method an adapter may use to reach a credential.

    Deliberately *not* the raw :class:`~paa.skills.secrets.SecretBroker`: the
    broker needs the active permission grants and a requester identity to make an
    authorization decision, and an adapter has neither. The Unified Skill Adapter
    binds those in and hands the adapter this narrowed surface instead — so the
    permission check (RFC §8.2, ``Permission.SECRET_READ``) and the requester
    audit trail happen host-side, exactly once, at a layer the skill cannot
    forge. The returned :class:`~paa.skills.secrets.SecretValue` redacts itself
    everywhere except an explicit ``.reveal()``.
    """

    def get_secret(self, name: str) -> SecretValue:
        """Resolve ``name`` to a live secret handle, or raise if not permitted."""
        ...


class SkillResult(BaseModel):
    """The mechanical outcome of one adapter invocation.

    Total by construction: a skill that crashed, timed out or returned garbage
    still yields a ``SkillResult`` (with ``ok=False`` and ``error`` set) rather
    than raising, because the layer above needs a *sample* even from a failure to
    feed the reliability optimiser. Adapters raise only when they cannot invoke
    at all — a missing entrypoint, an unspeakable contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    """The mechanism completed and produced a usable ``output``. Not a statement
    about whether the output is *schema-valid* — that verdict belongs to the USA
    and lives on :class:`paa.skills.contracts.SkillResult.output_valid`."""

    output: dict[str, Any] = Field(default_factory=dict)
    """The skill's structured return value. Empty on failure."""

    stdout: str = ""
    stderr: str = ""

    error: str | None = None
    """Human-readable failure summary when ``ok`` is ``False``. Never carries a
    secret value — adapters that handle credentials route them through the
    :class:`~paa.skills.secrets.SecretBroker`, which redacts by construction."""

    latency_ms: float = 0.0
    exit_code: int | None = None


class SkillAdapter(abc.ABC):
    """One execution modality. Stateless across calls, safe to reuse.

    Concrete adapters must not hold per-invocation state on the instance: the
    runtime keeps one adapter per provider and drives it concurrently.
    """

    @property
    @abc.abstractmethod
    def provider(self) -> str:
        """The ``SkillContract.provider`` value this adapter services."""

    @abc.abstractmethod
    async def discover(self) -> list[SkillContract]:
        """Enumerate the skills this adapter's source currently exposes.

        Returns validated contracts ready to hand to the registry. Discovery is
        read-only and must be safe to call repeatedly — the marketplace refresh
        loop does exactly that.
        """

    @abc.abstractmethod
    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None,
        timeout: float | None,  # noqa: ASYNC109 - deliberate per-call budget in the adapter API
        secret_broker: SecretProvider | None,
    ) -> SkillResult:
        """Run one skill and return its mechanical outcome.

        :param contract: the skill to run. The adapter reads
            ``contract.invocation`` for the recipe.
        :param arguments: the caller's arguments. The adapter is responsible for
            delivering them *without* placing them in the child environment (see
            :class:`paa.sandbox.base.SandboxSpec`).
        :param sandbox: the containment backend, or ``None`` for adapters that
            run in-process (:class:`~paa.skills.adapters.native.NativeAdapter`).
        :param timeout: wall-clock budget in seconds, or ``None`` to defer to the
            modality profile / adapter default.
        :param secret_broker: the bound secret proxy for this dispatch, or
            ``None`` when the skill declares no secret access. Named
            ``secret_broker`` for RFC §8.2 continuity; it is the narrowed
            :class:`SecretProvider`, not the raw broker.
        """
