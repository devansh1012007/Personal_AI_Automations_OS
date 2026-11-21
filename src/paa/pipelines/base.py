"""The pipeline platform's base vocabulary: capabilities, tokens, protocols.

This module is the P1.1 freeze of the "narrow waist" every pipeline speaks
(docs/plans/2026-11-21-pipeline-platform-spec.md §3.2, ADR-0051). It is
deliberately transport-agnostic: the same protocol names and shapes are what
the out-of-process JSON-RPC transport (P3) serialises, so freezing them
in-process first is a rehearsal of the wire contract, not a throwaway.

What lives here:

* :class:`Capability` — the closed vocabulary of facade capabilities a grant
  can be minted for (ADR-0051). ``now``/``random``/``progress`` are
  deliberately **not** members: they are the determinism aids and heartbeat,
  always served, because gating them would push a compliant pipeline back to
  reading the clock directly — the exact behaviour the journal exists to
  prevent (ADR-0050).
* :class:`CapabilityToken` / :class:`CapabilitySet` — the grant machinery:
  the host mints one opaque, run-scoped token per granted capability, where
  ``granted = manifest ∩ mandate ∩ policy``. In-process these tokens are
  *correlation, not containment* — same-address-space code can reach anything
  the process can reach (ADR-0051 states this plainly); the boundary becomes
  real when the pipeline runs out-of-process (P3) or in a container launcher.
* :class:`Pipeline` — what a pipeline *is*: identity, a declared capability
  ask, and one ``run`` method that drives a task to a terminal result using
  only the facade it is handed.
* :class:`CapabilityFacade` — the only door a pipeline gets. Typed
  operations, host-validated, host-emitted (ADR-0051): there is no ``emit``
  and no ``checkpoint`` on this surface, and the call journal (ADR-0050, P2)
  is the checkpoint.

What deliberately does *not* live here: an implementation of the facade.
Implementations belong to hosts (the built-in's host lives in
:mod:`paa.pipelines.builtin`; the orchestrator wires it in P1.2), because a
facade is a *grant*, and the host is what grants.

SPEC DEVIATION (docs/adr/0051): the older design's ``RuntimeContext`` carried
``emit`` and ``checkpoint``. Both are removed — a pipeline that could emit
``POLICY_CLEARED`` could approve its own plan.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from paa.agents.base import AgentContext, AgentMessage, AgentResult
from paa.core.errors import PaaError
from paa.core.types import AgentRole, ComplexityModality, Permission, PermissionMode
from paa.ledger.replay import TaskProjection

__all__ = [
    "FACADE_RUNTIME_VERSION",
    "Capability",
    "CapabilityNotGrantedError",
    "CapabilitySet",
    "CapabilityToken",
    "GateQuestion",
    "GateRejectedError",
    "InvalidCallParamsError",
    "OutcomeUnknownError",
    "Pipeline",
    "PipelineCancelledError",
    "PipelineDivergedError",
    "PipelineError",
    "PipelineResult",
    "PipelineStatus",
    "PipelineTask",
    "RunLimits",
    "compute_grants",
]

#: The facade version this module's protocols describe. A pipeline's
#: ``requires_runtime`` range is checked against this (and, per ADR-0052, is
#: the facade's version, never the product's).
FACADE_RUNTIME_VERSION = "4.1"


class Capability(str, Enum):
    """The closed set of facade capabilities a grant can be minted for.

    The seven manifest-requestable members (ADR-0052's declarable subset:
    ``think``, ``use_skill``, ``recall``, ``remember``, ``call_agent``,
    ``sub_run``, ``commit``) are what a ``pipeline.toml``'s
    ``[capabilities]`` section may carry; ``gate``, ``plan_submit`` and
    ``policy_check`` are host-side typed operations of the facade itself
    and are not manifest-requestable in v1 — a pipeline may still be refused
    them when the grant computation says so.
    """

    THINK = "think"
    CALL_AGENT = "call_agent"
    USE_SKILL = "use_skill"
    RECALL = "recall"
    REMEMBER = "remember"
    GATE = "gate"
    PLAN_SUBMIT = "plan_submit"
    POLICY_CHECK = "policy_check"
    COMMIT = "commit"
    SUB_RUN = "sub_run"


# ---------------------------------------------------------------------------
# Errors — the wire table, in-process form (ADR-0051)
# ---------------------------------------------------------------------------


class PipelineError(PaaError):
    """Base of the pipeline-platform error vocabulary.

    Every member is a :class:`PaaError`, so a host's existing failure path
    turns it into a recorded ``EXECUTION_FAILED`` unchanged. ``wire_code`` is
    the JSON-RPC error code the RPC transport (P3) renders the class as; the
    two remaining codes in the table reuse core errors rather than growing a
    second vocabulary: ``-32001`` is
    :class:`paa.core.errors.BudgetExceededError` and ``-32002`` is
    :class:`paa.core.errors.PermissionDeniedError`.
    """


class GateRejectedError(PipelineError):
    """A human said no to a gate the pipeline opened (-32003)."""

    wire_code = -32003


class PipelineDivergedError(PipelineError):
    """The re-run pipeline's calls differ from the journal; the run stops (-32004).

    SPEC DEVIATION (docs/adr/0050): the host never resyncs or skips — a
    pipeline that is not replaying its own history is a pipeline the record
    can no longer speak for.
    """

    wire_code = -32004

    def __init__(self, seq: int, expected_fingerprint: str, actual_fingerprint: str) -> None:
        super().__init__(
            "pipeline call sequence diverged from the journal",
            seq=seq,
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=actual_fingerprint,
        )


class PipelineCancelledError(PipelineError):
    """The run was cancelled and the pipeline stopped within its grace period (-32005)."""

    wire_code = -32005


class CapabilityNotGrantedError(PipelineError):
    """A call arrived for a capability with no valid token (-32006).

    The refusal names the capability and the granted set, so the pipeline can
    adapt (or report) rather than guess.
    """

    wire_code = -32006

    def __init__(self, capability: str, granted: Iterable[str] = ()) -> None:
        super().__init__(
            "capability is not granted to this run",
            capability=capability,
            granted=sorted(granted),
        )
        self.capability = capability


class InvalidCallParamsError(PipelineError):
    """A capability call's parameters failed their schema (-32007)."""

    wire_code = -32007


class OutcomeUnknownError(PipelineError):
    """A prior mutating call's outcome cannot be classified; the pipeline decides (-32008).

    The D-6 rule (ADR-0050): the host never auto-repeats a mutating call
    whose outcome is unknown — repeating a shell or git effect can duplicate
    a commit, and asking is cheaper than duplicating.
    """

    wire_code = -32008


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityToken:
    """One opaque, run-scoped token for one granted capability.

    ``nonce`` makes tokens distinct per mint, so a leaked token from an old
    run cannot be replayed against a new one even for the same capability.
    In-process this is correlation, not containment — see the module
    docstring and ADR-0051.
    """

    capability: Capability
    run_id: uuid.UUID
    nonce: str


class CapabilitySet:
    """The tokens a host minted for one run: ``manifest ∩ mandate ∩ policy``.

    The host mints the set at run start from :func:`compute_grants`; a
    capability call without a valid token for that capability is refused with
    :class:`CapabilityNotGrantedError` (``-32006`` over the wire). Tokens
    expire with the run because the set is never persisted — never to the
    ledger, never to a transcript (ADR-0051).
    """

    def __init__(self, run_id: uuid.UUID, capabilities: Iterable[Capability]) -> None:
        self._run_id = run_id
        self._tokens = {
            capability: CapabilityToken(capability, run_id, uuid.uuid4().hex)
            for capability in capabilities
        }

    @classmethod
    def mint(
        cls, capabilities: Iterable[Capability], run_id: uuid.UUID | None = None
    ) -> CapabilitySet:
        """Mint a set for one run; a fresh run id is generated when none is given."""
        return cls(run_id or uuid.uuid4(), capabilities)

    def __contains__(self, capability: Capability) -> bool:
        return capability in self._tokens

    def __iter__(self) -> Iterator[Capability]:
        return iter(self._tokens)

    @property
    def run_id(self) -> uuid.UUID:
        return self._run_id

    def token_for(self, capability: Capability) -> CapabilityToken | None:
        return self._tokens.get(capability)

    def require(self, capability: Capability) -> CapabilityToken:
        """The token for a capability, or the refusal (ADR-0051's only door)."""
        token = self._tokens.get(capability)
        if token is None:
            raise CapabilityNotGrantedError(capability.value, (c.value for c in self._tokens))
        return token

    def verify(self, token: CapabilityToken) -> bool:
        """True only for a token this run minted for that capability."""
        return self._tokens.get(token.capability) == token and token.run_id == self._run_id


def compute_grants(
    requested: Iterable[Capability], *constraints: Iterable[Capability] | None
) -> frozenset[Capability]:
    """The grant computation: the request intersected with every constraint.

    Each constraint is one authority source's ceiling — the mandate's scope,
    the active permission mode's grants — and ``None`` means that source
    imposes no constraint (P1 mints from the pipeline's declared ask alone;
    the mandate and policy sources arrive with P2/P4 per ADR-0052). The
    result can only ever be *smaller* than the request: that is
    tighten-only, applied to capabilities the same way ADR-0052 applies it
    to overlays.
    """
    granted = frozenset(requested)
    for constraint in constraints:
        if constraint is not None:
            granted &= frozenset(constraint)
    return granted


# ---------------------------------------------------------------------------
# Run shapes
# ---------------------------------------------------------------------------


class PipelineStatus(str, Enum):
    """A pipeline run's terminal status (the wire result's ``status``)."""

    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True, slots=True)
class PipelineArtifact:
    """One artifact a run produced, identified by content hash."""

    kind: str
    sha256: str | None = None


@dataclass(slots=True)
class PipelineResult:
    """The final result of one pipeline run (spec §5's final-result shape)."""

    status: PipelineStatus
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[PipelineArtifact] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunLimits:
    """The manifest's ``[limits]``, as the host enforces them (P2/P3).

    Frozen because the pipeline carries it as a class-level default and a
    mutable limit would be shared state across runs; a run that wants
    different limits gets its own instance.

    ``None`` means unbounded at this layer; the host's own watchdog and the
    facade's budget checks are always live regardless.
    """

    max_tokens: int | None = None
    max_wall_s: int | None = None
    max_calls: int | None = None
    max_sub_runs: int | None = None


@dataclass(frozen=True, slots=True)
class GateQuestion:
    """One human-gate question, as a lineage records it.

    A question is *opened* by an ``AWAITING_HUMAN_ATTESTATION`` event and
    *answered* by the ``HUMAN_GATE_CLEARED`` event that names it; the role is
    the stage that asked, and ``discriminator`` names the question within
    that stage (ADR-0034's idempotency mechanism).
    """

    role: str
    discriminator: str | None
    answered: bool
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PipelineTask:
    """The task handed to one pipeline run.

    The in-process form carries the live :class:`TaskProjection` (the R2
    state carrier the built-in's stage guards read); the wire form (P3)
    serialises ``goal``/``request``/``params`` and re-derives state host-side
    between calls, which is exactly why the projection is marked here as
    transport-local rather than part of the contract.
    """

    correlation_id: uuid.UUID
    session_id: uuid.UUID | None
    goal: str
    request: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    modality: ComplexityModality = ComplexityModality.STANDARD
    permission_mode: PermissionMode = PermissionMode.ASK
    projection: TaskProjection | None = None


# ---------------------------------------------------------------------------
# The two protocols: the pipeline, and the only door it gets
# ---------------------------------------------------------------------------


class Pipeline(Protocol):
    """What a pipeline is: identity, an ask, and one ``run``.

    A pipeline is judged by two things only — it produces a terminal
    :class:`PipelineResult`, and it did everything through the
    :class:`CapabilityFacade` it was handed. Everything else (ReAct,
    tree-of-thought, a TDD loop, a wrapper around another harness) is the
    pipeline's own strategy, and deliberately opaque to the host.

    There is deliberately no ``can_handle`` here: selection is explicit and
    mandate-scoped (ADR-0052 supersedes the auto-scoring design).
    """

    name: str
    version: str
    #: Semver range of the facade the pipeline needs (ADR-0052's
    #: ``requires_runtime`` — the facade's version, never the product's).
    requires_runtime: str
    #: Core permissions the pipeline needs before it may run at all.
    required_permissions: frozenset[Permission]
    #: The capability ask; the grant computation narrows it, never widens.
    capabilities: frozenset[Capability]

    async def run(self, task: PipelineTask, ctx: CapabilityFacade) -> PipelineResult:
        """Drive one task to a terminal result, using ``ctx`` for all capabilities."""
        ...


class CapabilityFacade(Protocol):
    """The only door a pipeline gets (ADR-0051).

    Every method is a typed operation the host validates and records; the
    host emits the resulting lifecycle events itself. There is no ``emit`` —
    a pipeline that could emit ``POLICY_CLEARED`` could approve its own plan
    — and no ``checkpoint``: the call journal (ADR-0050, P2) is the
    checkpoint. Implementations provide the granted subset; a call for an
    ungranted capability raises :class:`CapabilityNotGrantedError`.

    The run-scope properties are what the wire transport's
    ``pipeline.initialize`` carries. ``agent_ctx`` and the two transport reads
    at the end (``stage_question``/``project``) are the in-process carrier's
    extras — the RPC transport replaces them and they will not survive it,
    which is why they are grouped and marked below rather than left to be
    mistaken for the contract. Three further members carry in-process-only
    *types* today and will be reshaped (not renamed) by the RPC transport:
    ``call_agent``'s message/context parameters, ``gate``'s and
    ``check_policy``'s ``TaskProjection`` — the wire forms serialise the
    request and return the verdict, host-side, which is what P3 replaces;
    ``think``/``use_skill``/``sub_run``/``now``/``random``/``progress`` are
    already wire-shaped.
    """

    # -- run scope -------------------------------------------------------
    @property
    def correlation_id(self) -> uuid.UUID: ...

    @property
    def session_id(self) -> uuid.UUID | None: ...

    @property
    def modality(self) -> ComplexityModality: ...

    @property
    def permission_mode(self) -> PermissionMode: ...

    @property
    def tokens_budget(self) -> int: ...

    # -- capabilities -----------------------------------------------------
    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        """A model call with role, schema, and budget; honours escalation and LOCKDOWN."""
        ...

    async def call_agent(
        self, role: AgentRole, message: AgentMessage, agent_ctx: AgentContext
    ) -> AgentResult[Any]:
        """Reuse a built-in agent role; the delegation graph and recursion guard apply."""
        ...

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> str:
        """A skill or tool call: policy, sandbox, validation, secret proxy."""
        ...

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        """A bounded memory read."""
        ...

    async def remember(self, fact: Mapping[str, Any]) -> None:
        """A memory write, through the memory creator."""
        ...

    async def gate(self, role: AgentRole, payload: Mapping[str, Any]) -> TaskProjection:
        """Ask a human; parks the run and returns the parked projection."""
        ...

    async def submit_plan(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        requirements: Mapping[str, Any] | None = None,
        bypassed_planner: bool = False,
    ) -> None:
        """Offer a plan; the host validates schema and size, then emits ``PLAN_COMPILED``."""
        ...

    async def check_policy(self, state: TaskProjection) -> TaskProjection:
        """Request clearance for a plan; the host runs the policy agent and records the verdict."""
        ...

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        """Commit validated changes; requires clearance and a passing validation (ADR-0051)."""
        ...

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        """Run another pipeline; the child's budget halves and recursion is guarded."""
        ...

    async def now(self) -> datetime:
        """Recorded time — the clock a replayable pipeline is allowed to read."""
        ...

    async def random(self) -> float:
        """Seeded, journalled randomness."""
        ...

    async def progress(self, status: str) -> None:
        """Heartbeat and status text; feeds the watchdog, never journalled."""
        ...

    # -- in-process transport reads (not part of the wire contract) --------
    @property
    def agent_ctx(self) -> AgentContext:
        """The run's live agent context. In-process carrier only (P1/P2)."""
        ...

    async def stage_question(self, role: AgentRole) -> GateQuestion | None:
        """This role's most recent gate question, if it asked one. In-process carrier only."""
        ...

    async def project(self) -> TaskProjection:
        """Re-read the lineage's current projection (the R2 read). In-process carrier only."""
        ...
