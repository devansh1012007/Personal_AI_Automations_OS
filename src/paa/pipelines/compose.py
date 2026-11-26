"""Layer-4 composition: ``ctx.sub_run`` with budget halving and a recursion
guard (SPEC 2 Wave 8, C8.2 — docs/plans/2026-11-21-pipeline-platform-spec.md
§4 layer 4 and §3.2's ``ctx.sub_run`` row).

What composition is here: a **manager** pipeline decomposes one task into
delegations and runs each on another registered pipeline — a **worker** —
through the same facade door every capability goes through. The host, not
the manager, owns everything that makes delegation safe:

* **budget halving** — a child's limits are half of the parent's
  *remaining* budget, per limit key (:func:`halve_budget`), and the grant
  leaves the parent's wallet (grant accounting: the sum of everything
  granted down a branch never exceeds the root's budget);
* **the recursion guard** — a delegation cycle (A → B → A) is refused by
  name-stack membership, the composition depth is capped by
  :data:`MAX_COMPOSITION_DEPTH`, and each run's ``max_sub_runs`` counts
  the sub-runs it may initiate, halving into the child the same way;
* **capability discipline** — the child's grants are computed from *its
  own declaration* via :func:`declared_capabilities` (ADR-0051's grant
  computation, manifest as the only source): a parent cannot launder a
  capability into a child that did not ask for it;
* **journalling and replay** — every ``sub_run`` is one call on the
  composition lineage's :class:`~paa.pipelines.journal.CallJournal`
  (``PIPELINE_CALL_*`` with ``method="sub_run"``, ADR-0046's golden v1
  shapes, no new event type), so a re-run is *served* the recorded child
  result with zero re-execution, a changed call **diverges** the run
  (ADR-0050: the host never resyncs), and a call the crash left without
  an outcome is the pipeline's to decide (D-6) — ``sub_run`` can mutate,
  so it is never auto-repeated. This is the row's "delegation **with
  attestation**": the PROPOSED row carries the call's fingerprint and a
  shape-only preview, the COMPLETED row carries the child result's
  canonical JSON and its SHA-256.

**Child isolation.** A worker's own failure — a failed
:class:`~paa.pipelines.base.PipelineResult` or an exception raised by its
runner — is *the child's* outcome, returned to the parent as a failed
child result; the parent run does not crash, and the aggregate says which
child failed and why. The exceptions that are **not** isolated are the
composition's own control vocabulary (:data:`CONTROL_ERRORS` — the guard,
budget and selection errors, the replay halts): those propagate to the
root, because a manager that could silently absorb a recursion-guard
refusal would keep delegating in circles while the ledger paid for each
round. ``asyncio.CancelledError`` is the host's word, never a worker
failure: it is journalled as a cancelled call and re-raised.

What runs a child: a **host-minted runner** (:data:`ChildRunner`), one
per worker name. The registry answers "which pipelines exist and what do
they ask for"; the runner is the host's act of trusting one of them with
in-process execution — the built-in workers' runners mint their own
facades (the task pipeline's orchestrator, the coder's facade), exactly
as the one-shot CLI drivers do. A local manifest with a minted runner
therefore *runs* only host code under the manifest's declared ask; the
manifest's own ``[run].command`` is never executed, which is why this is
not the P3 boundary being crossed.

The manager mind (:class:`ManagerPipeline`) is deliberately
deterministic: the decomposition is the **operator's word** — the
request's ``delegations`` list — not a model's guess, keeping with the
house rule that no generative reasoning sits in a control path. A
model-driven manager is a ``think``-granted pipeline composing this one,
not a change to this one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.core.errors import BudgetExceededError, PaaError, RecursionGuardError
from paa.core.types import (
    AgentRole,
    ComplexityModality,
    EventType,
    Permission,
)
from paa.ledger.events import LedgerEvent
from paa.ledger.replay import TaskProjection
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    Capability,
    CapabilityFacade,
    InvalidCallParamsError,
    OutcomeUnknownError,
    PipelineArtifact,
    PipelineDivergedError,
    PipelineError,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
    compute_grants,
)
from paa.pipelines.builtin import (
    BuiltinPipelineFacade,
    StageAgents,
    SubRunExecutor,
)
from paa.pipelines.journal import CallJournal
from paa.pipelines.registry import PipelineRegistry, PipelineSelectionError, select
from paa.pipelines.replay import (
    ReplayAction,
    ReplayedCallFailedError,
    ReplayView,
)
from paa.pipelines.slots import PipelineDeclaration

__all__ = [
    "CONTROL_ERRORS",
    "MAX_COMPOSITION_DEPTH",
    "ChildRun",
    "CompositionHost",
    "ManagerPipeline",
    "SubRunComposer",
    "declared_capabilities",
    "halve_budget",
    "run_composition",
]

#: The platform's composition-depth ceiling: the length of the pipeline-name
#: chain (root included) may not exceed it. Name-stack cycle detection
#: already bounds depth by the registry's size; this cap bounds the
#: unbounded-``max_sub_runs`` case (``RunLimits`` keys may be ``None``) with
#: a number that does not depend on how many pipelines happen to be
#: registered. Eight is deliberately shallow: the spec's own example
#: manifest ships ``max_sub_runs = 2``, and a composition deeper than a
#: handful of levels is a decomposition smell, not a workload.
MAX_COMPOSITION_DEPTH = 8

#: The manifest's declarable capability keys (ADR-0052's closed set), mapped
#: to the facade vocabulary. ``gate``/``plan_submit``/``policy_check`` are
#: deliberately absent: they are host-side typed operations, not
#: manifest-requestable (base.Capability's own rule).
_DECLARABLE_CAPABILITIES: dict[str, Capability] = {
    "think": Capability.THINK,
    "use_skill": Capability.USE_SKILL,
    "recall": Capability.RECALL,
    "remember": Capability.REMEMBER,
    "call_agent": Capability.CALL_AGENT,
    "sub_run": Capability.SUB_RUN,
    "commit": Capability.COMMIT,
}

#: The exceptions that are the composition *contract's*, not a worker's:
#: raised by the guards, the selection, or the replay table, they propagate
#: to the root run instead of being isolated into a failed child result.
#: Every one is a :class:`PaaError`; the tuple exists so the isolation
#: ``except`` clause can name exactly the contract and let the rest be the
#: child's own crash.
CONTROL_ERRORS: tuple[type[Exception], ...] = (
    RecursionGuardError,
    BudgetExceededError,
    PipelineError,
)

#: The metered resource keys, in the order RunLimits declares them.
_RESOURCE_KEYS = ("max_tokens", "max_wall_s", "max_calls")


# ---------------------------------------------------------------------------
# The child a runner receives
# ---------------------------------------------------------------------------


class ChildRunner(Protocol):
    """What a host mints to execute one composed child.

    The runner receives the fully resolved child — declaration, grants,
    halved limits, its own composer for nested delegation — and drives it
    to a terminal :class:`PipelineResult`. The runner is host code and is
    trusted accordingly (ADR-0051: in-process grants are correlation, not
    containment); its contract is to honour the child's ``grants`` and
    ``limits`` when it mints the child's facade, and to return a result
    whose ``metrics`` are canonical-JSON serialisable — the journal serves
    the recorded result back on replay, and an unrecordable result is a
    result it cannot serve.
    """

    async def __call__(self, run: ChildRun) -> PipelineResult: ...


@dataclass(frozen=True, slots=True)
class ChildRun:
    """One resolved child delegation, as the host hands it to its runner.

    ``grants`` came from the child's own declaration (never the parent's —
    the anti-laundering rule), ``limits`` are the halved grant, and
    ``composer`` is the door the child's own ``sub_run`` calls would go
    through: handed over unconditionally, because the door without the
    grant is exactly the discipline the facade enforces.
    """

    name: str
    declaration: PipelineDeclaration
    grants: frozenset[Capability]
    limits: RunLimits
    task_input: Mapping[str, Any]
    #: The pipeline-name chain from the composition root to this child,
    #: inclusive — the cycle guard's stack.
    stack: tuple[str, ...]
    #: This child's depth: 1 for the root's direct children.
    depth: int
    composer: SubRunExecutor


# ---------------------------------------------------------------------------
# The grant computation and the budget arithmetic
# ---------------------------------------------------------------------------


def declared_capabilities(declaration: PipelineDeclaration) -> frozenset[Capability]:
    """The grants a declaration asks for, in facade vocabulary.

    A manifest asks with its keys (``think``, ``use_skill``, ``sub_run``,
    …); a key absent, or present with a falsy ask (``false``, ``[]``), is
    not an ask. The result is :func:`compute_grants` over the ask alone —
    the declaration *is* the request, and the composition deliberately
    adds no constraint of its own: tighten-only is the mandate's and the
    overlay's job, and a child that did not ask for a capability does not
    get it, whoever its parent is.
    """
    asked = [
        capability
        for key, ask in declaration.capabilities.items()
        if ask and (capability := _DECLARABLE_CAPABILITIES.get(key)) is not None
    ]
    return compute_grants(asked)


def halve_budget(remaining: RunLimits) -> tuple[RunLimits, RunLimits]:
    """Split one run's remaining budget into the child's grant and the rest.

    Per metered resource key (``max_tokens``, ``max_wall_s``,
    ``max_calls``): the child receives ``max(1, remaining // 2)`` — floor
    at one, because a child granted zero of a metered key could not run at
    all and a parent holding one unit still owes its child the minimum
    that makes a run possible — and the parent keeps the remainder, so
    grants *leave the wallet*: the total granted down any branch (and
    across siblings, in delegation order) never exceeds the root's
    budget. Integer halving rounds the grant down (``7 → 3``); a parent
    holding an odd number keeps the richer half, which keeps every grant
    an exact integer and every wallet non-negative.

    ``max_sub_runs`` halves with **no floor**: the child receives
    ``remaining // 2``, because zero is the meaningful statement "this
    child may not compose" — the arithmetic form of the depth guard. A
    parent whose remaining count is 1 delegates to a leaf; a parent at 0
    has exhausted its count and is refused before this function runs.

    ``None`` (unbounded) keys stay ``None`` in both halves: the absence of
    a meter is not a zero, and halving an unbounded key into a bounded one
    would invent a number nobody declared. The depth ceiling and the cycle
    guard bound the unbounded case.
    """
    child: dict[str, int | None] = {}
    rest: dict[str, int | None] = {}
    for key in _RESOURCE_KEYS:
        value = getattr(remaining, key)
        if value is None:
            child[key] = None
            rest[key] = None
            continue
        grant = max(1, value // 2)
        child[key] = grant
        rest[key] = value - grant
    count = remaining.max_sub_runs
    child["max_sub_runs"] = count // 2 if count is not None else None
    rest["max_sub_runs"] = count
    return RunLimits(**child), RunLimits(**rest)


# ---------------------------------------------------------------------------
# The composition state and the composer
# ---------------------------------------------------------------------------


class _CompositionState:
    """What one composition shares across every level of its tree.

    The journal and its ``seq`` counter (monotonic over the whole
    composition, because every ``sub_run`` at any depth is a call on the
    same lineage), the registry and the mandate scope the children are
    selected under, the host-minted runners, and the replay view a re-run
    is served from. Per-run facts — the name stack, the wallet, the
    sub-run count — belong to the :class:`SubRunComposer` frames, not
    here.
    """

    def __init__(
        self,
        *,
        journal: CallJournal,
        registry: PipelineRegistry,
        runners: Mapping[str, ChildRunner],
        replay: ReplayView | None = None,
        allowed: tuple[str, ...] | None = None,
    ) -> None:
        self.journal = journal
        self.registry = registry
        self.runners = dict(runners)
        self.replay = replay
        self.allowed = allowed
        self._seq = 0

    def next_seq(self) -> int:
        """The next call's sequence number, monotonic within the run."""
        self._seq += 1
        return self._seq


class SubRunComposer:
    """The host-side executor behind one run's ``ctx.sub_run`` (C8.2).

    One frame per run in the composition: the frame carries the run's
    name, its stack (the cycle guard's evidence), its remaining wallet
    and sub-run count, all sharing one :class:`_CompositionState`.
    :meth:`run_child` is the whole layer-4 contract in order — validate,
    guard, resolve, halve, journal, replay-check, execute, isolate,
    attest — and the order is the contract: guards before the ledger, the
    PROPOSED row before the effect, the COMPLETED row with the result the
    re-run will be served.
    """

    def __init__(
        self,
        state: _CompositionState,
        *,
        name: str,
        stack: tuple[str, ...],
        limits: RunLimits,
        root_limits: RunLimits | None = None,
        sub_runs_left: int | None = None,
    ) -> None:
        self._state = state
        self._name = name
        self._stack = stack
        self._wallet = limits
        self._root = root_limits if root_limits is not None else limits
        self._sub_runs_limit = sub_runs_left
        self._sub_runs_left = sub_runs_left

    @property
    def name(self) -> str:
        return self._name

    async def run_child(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        """Run one child pipeline to a terminal result.

        Raises the composition's control vocabulary (:data:`CONTROL_ERRORS`)
        for a refused delegation — a cycle, the depth ceiling, an
        exhausted budget, an unknown or out-of-scope pipeline, a missing
        runner, a replay halt — and returns the child's own outcome
        (including its failure) as a :class:`PipelineResult` otherwise.
        """
        if not isinstance(name, str) or not name.strip():
            raise InvalidCallParamsError("sub_run needs a non-empty pipeline name")
        if name in self._stack:
            raise RecursionGuardError(
                "sub_run would close a delegation cycle",
                depth=len(self._stack),
                cycle=[*self._stack, name],
            )
        if len(self._stack) + 1 > MAX_COMPOSITION_DEPTH:
            raise RecursionGuardError(
                "composition depth exceeds the platform ceiling",
                depth=len(self._stack) + 1,
                ceiling=MAX_COMPOSITION_DEPTH,
            )
        if self._sub_runs_left is not None and self._sub_runs_left <= 0:
            raise BudgetExceededError(
                "sub_run",
                float(self._sub_runs_limit or 0),
                float(self._sub_runs_limit or 0),
                pipeline=self._name,
            )
        for key in _RESOURCE_KEYS:
            value = getattr(self._wallet, key)
            if value is not None and value <= 0:
                root = getattr(self._root, key) or 0
                raise BudgetExceededError(
                    key,
                    float(root),
                    float(root - value),
                    pipeline=self._name,
                )

        # Selection is explicit and mandate-scoped, the C6.5 rule: the
        # registry answers existence, the scope (when one bounds the run)
        # answers permission, and neither is the manager's to widen.
        row = select(name, registry=self._state.registry, allowed=self._state.allowed)
        runner = self._state.runners.get(name)
        if runner is None:
            raise PipelineSelectionError(
                f"no runner is minted for {name!r} in this host — composition "
                "runs host-minted in-process runners; the manifest's own "
                "command is never executed (that is P3's boundary)",
                pipeline=name,
            )

        # The child's grants: its own declaration, never the parent's.
        grants = declared_capabilities(row.declaration)

        # The halving, and the wallet it leaves behind. This happens
        # before the replay check on purpose: the guards above read the
        # wallet, so a re-run must mutate it exactly as the recorded run
        # did at the same point, or its own later guards would diverge
        # from the decisions being replayed.
        grant, rest = halve_budget(self._wallet)
        self._wallet = rest
        child_sub_runs = grant.max_sub_runs
        if self._sub_runs_left is not None:
            self._sub_runs_left -= 1

        params: dict[str, Any] = {"pipeline": name, "task_input": dict(task_input)}
        try:
            from paa.ledger.events import canonical_json

            canonical_json(dict(task_input))
        except (TypeError, ValueError) as exc:
            raise InvalidCallParamsError(
                "sub_run task_input must be canonical-JSON serialisable — "
                "the journal's fingerprint and the replay's served value "
                "both depend on it"
            ) from exc

        seq = self._state.next_seq()
        if (replay := self._state.replay) is not None:
            decision = replay.check(seq, "sub_run", params)
            if decision.action is ReplayAction.SERVE:
                return _result_of(decision.result)
            if decision.action is ReplayAction.DIVERGED:
                await self._state.journal.run_diverged(
                    seq,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
                raise PipelineDivergedError(
                    seq,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
            if decision.action is ReplayAction.OUTCOME_UNKNOWN:
                raise OutcomeUnknownError(
                    "the recorded sub_run's outcome cannot be classified; "
                    "the pipeline decides (D-6)",
                    seq=seq,
                )
            if decision.action is ReplayAction.FAIL:
                raise ReplayedCallFailedError(
                    f"the recorded sub_run call failed: {decision.reason}"
                )
            # EXECUTE: a call beyond the record. Fall through.

        await self._state.journal.call_proposed(seq, "sub_run", params)

        child_stack = (*self._stack, name)
        child = ChildRun(
            name=name,
            declaration=row.declaration,
            grants=grants,
            limits=grant,
            task_input=dict(task_input),
            stack=child_stack,
            depth=len(child_stack),
            composer=SubRunComposer(
                self._state,
                name=name,
                stack=child_stack,
                limits=grant,
                root_limits=grant,
                sub_runs_left=child_sub_runs,
            ),
        )
        try:
            result = await runner(child)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._state.journal.call_failed(seq, "sub_run", "cancelled")
            )
            raise
        except CONTROL_ERRORS:
            await self._state.journal.call_failed(
                seq, "sub_run", _reason_of_current()
            )
            raise
        except Exception as exc:
            # The child's own crash: an outcome of the child, not of the
            # composition. Isolated, named, and journalled as the call's
            # recorded result — the parent keeps going and the aggregate
            # says which delegation died and how.
            result = PipelineResult(
                status=PipelineStatus.FAILED,
                summary=f"child {name} crashed: {type(exc).__name__}: {exc}",
                metrics={"crashed": True, "error_type": type(exc).__name__},
            )

        await self._state.journal.call_completed(seq, "sub_run", _result_payload(result))
        return result


def _reason_of_current() -> str:
    """The current exception's name-and-message, for a FAILED row's reason."""
    import sys

    exc = sys.exc_info()[1]
    if isinstance(exc, PaaError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


def _result_payload(result: PipelineResult) -> dict[str, Any]:
    """The child result as the journal records — and replay serves — it."""
    return {
        "status": result.status.value,
        "summary": result.summary,
        "metrics": dict(result.metrics),
        "artifacts": [
            {"kind": artifact.kind, "sha256": artifact.sha256}
            for artifact in result.artifacts
        ],
    }


def _result_of(recorded: Any) -> PipelineResult:
    """Rebuild a :class:`PipelineResult` from its recorded payload.

    Defensive by rule: the fold is total over malformed rows, so the
    rebuild is too — an unrecognised status or a non-table metrics value
    degrades to a failed result that says so, never to a crash wearing a
    result costume.
    """
    data = recorded if isinstance(recorded, dict) else {}
    raw_status = data.get("status")
    try:
        status = PipelineStatus(str(raw_status))
    except ValueError:
        status = PipelineStatus.FAILED
    metrics = data.get("metrics")
    raw_artifacts = data.get("artifacts")
    artifacts = [
        PipelineArtifact(
            kind=str(entry.get("kind") or "artifact"),
            sha256=entry.get("sha256") if isinstance(entry.get("sha256"), str) else None,
        )
        for entry in (raw_artifacts if isinstance(raw_artifacts, list) else [])
        if isinstance(entry, dict)
    ]
    summary = data.get("summary")
    return PipelineResult(
        status=status,
        summary=summary if isinstance(summary, str) else "",
        metrics=dict(metrics) if isinstance(metrics, dict) else {},
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# The manager mind
# ---------------------------------------------------------------------------


class ManagerPipeline:
    """``builtin.manager`` — the composition manager (C8.2, layer 4).

    The mind half of the split: it owns the delegation loop and the
    aggregation, and touches nothing but the facade it is handed. The
    decomposition is the **operator's word** — the request's
    ``delegations`` list of ``{"pipeline": str, "goal": str,
    "input"?: table}`` entries — because a manager that decomposed by
    model would put generative reasoning in the delegation control path,
    and the house rule is that control paths stay deterministic. A
    model-driven manager composes *this* pipeline with ``think`` granted
    rather than changing it.

    Every delegation runs: a parked or failed sibling does not stop the
    others, because the decomposition named them all and independence is
    the operator's claim to make. The aggregate surfaces the strongest
    need — ``needs_human`` over ``failed`` over ``completed`` — with
    per-child statuses in metrics and the children's artifacts carried
    under prefixed kinds (provenance kept).
    """

    name = "builtin.manager"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = frozenset({Capability.SUB_RUN})
    limits = RunLimits(max_tokens=200_000, max_wall_s=1800, max_calls=400, max_sub_runs=2)

    async def run(self, task: PipelineTask, ctx: CapabilityFacade) -> PipelineResult:
        delegations = _delegations_of(task.request)
        if isinstance(delegations, str):
            return PipelineResult(
                status=PipelineStatus.FAILED,
                summary=delegations,
                metrics={"delegations": 0, "completed": 0, "failed": 0, "needs_human": 0},
            )

        counts = {"completed": 0, "failed": 0, "needs_human": 0}
        children: list[dict[str, Any]] = []
        artifacts: list[PipelineArtifact] = []
        for index, entry in enumerate(delegations, start=1):
            await ctx.progress(
                f"delegating {index}/{len(delegations)} to {entry['pipeline']}"
            )
            child_input = {"goal": entry["goal"], **entry["input"]}
            result = await ctx.sub_run(entry["pipeline"], child_input)
            counts[result.status.value] += 1
            children.append(
                {
                    "pipeline": entry["pipeline"],
                    "status": result.status.value,
                    "summary": result.summary,
                }
            )
            artifacts.extend(
                PipelineArtifact(
                    kind=f"{entry['pipeline']}:{artifact.kind}", sha256=artifact.sha256
                )
                for artifact in result.artifacts
            )

        if counts["needs_human"]:
            status = PipelineStatus.NEEDS_HUMAN
        elif counts["failed"]:
            status = PipelineStatus.FAILED
        else:
            status = PipelineStatus.COMPLETED
        return PipelineResult(
            status=status,
            summary=(
                f"{len(delegations)} delegation(s): {counts['completed']} completed, "
                f"{counts['failed']} failed, {counts['needs_human']} needs human"
            ),
            metrics={"delegations": len(delegations), **counts, "children": children},
            artifacts=artifacts,
        )


def _delegations_of(request: Mapping[str, Any]) -> list[dict[str, Any]] | str:
    """Validate the operator's decomposition, or name the refusal.

    Returns the parsed entries (``pipeline``/``goal``/``input`` with
    ``input`` defaulted to an empty table), or a human-readable refusal as
    a ``str`` — the caller turns that into the run's failed result, so a
    malformed decomposition is a named, journalled outcome rather than a
    crash.
    """
    raw = request.get("delegations")
    if not isinstance(raw, list):
        return "request['delegations'] must be a list of {pipeline, goal} tables"
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            return f"delegation {index} must be a table"
        pipeline = entry.get("pipeline")
        goal = entry.get("goal")
        if not isinstance(pipeline, str) or not pipeline.strip():
            return f"delegation {index} needs a non-empty 'pipeline'"
        if not isinstance(goal, str):
            return f"delegation {index} needs a string 'goal'"
        extra = entry.get("input")
        if extra is not None and not isinstance(extra, Mapping):
            return f"delegation {index} 'input' must be a table"
        parsed.append(
            {"pipeline": pipeline, "goal": goal, "input": dict(extra) if extra else {}}
        )
    return parsed


# ---------------------------------------------------------------------------
# The host and the driver
# ---------------------------------------------------------------------------


class CompositionHost:
    """The :class:`~paa.pipelines.builtin.PipelineHost` a composition mints on.

    A composition run is a **pipeline lineage**, not a task lineage: the
    manager's only granted capability is ``sub_run``, so every host
    service the facade's other capabilities reach for — event emission,
    gates, projections — is unreachable through the door, and each member
    below refuses rather than pretends (the
    :class:`~paa.observe.pipeline.ObserveFacade` precedent for a host
    surface a pipeline family genuinely does not have). The gate members
    answer honestly instead of refusing: no gate was ever opened on this
    lineage, so "no questions" is a fact, not a stub.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def emit(
        self,
        correlation_id: uuid.UUID,
        event_type: EventType,
        *,
        session_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        modality: ComplexityModality = ComplexityModality.STANDARD,
        role: AgentRole | None = None,
        attempt: int = 0,
        discriminator: str | None = None,
        causation_id: uuid.UUID | None = None,
    ) -> LedgerEvent:
        raise PaaError(
            "a composition run emits no task-lineage events; its vocabulary "
            "is the call journal's (PIPELINE_RUN_* / PIPELINE_CALL_*)",
            actual=self._name,
        )

    def message(
        self,
        state: TaskProjection,
        ctx: AgentContext,
        intent: MessageType,
        payload: dict[str, Any],
    ) -> AgentMessage:
        raise PaaError(
            "a composition run drives no task lineage; there is no stage "
            "message to build",
            actual=self._name,
        )

    async def project(self, correlation_id: uuid.UUID) -> TaskProjection:
        raise PaaError(
            "a composition run drives no task lineage; there is no projection "
            "to read",
            actual=self._name,
        )

    async def open_gate(
        self, ctx: AgentContext, role: AgentRole, payload: dict[str, Any]
    ) -> TaskProjection:
        raise PaaError(
            "a composition run opens no human gates; a child that parks "
            "returns needs_human as its result",
            actual=self._name,
        )

    async def gate_history(self, correlation_id: uuid.UUID) -> list[Any]:
        return []

    def stage_question(self, questions: list[Any], role: AgentRole) -> Any:
        return None


async def run_composition(
    ledger: LedgerStore,
    task: PipelineTask,
    *,
    runners: Mapping[str, ChildRunner],
    registry: PipelineRegistry | None = None,
    limits: RunLimits | None = None,
    replay: ReplayView | None = None,
    allowed: tuple[str, ...] | None = None,
) -> tuple[uuid.UUID, PipelineResult]:
    """Drive one ``builtin.manager`` run over host-minted worker runners.

    The composition's one-shot driver, the shape the coder and observe
    one-shots established: a fresh lineage (the task's ``correlation_id``
    — a re-run passes the *same* task with a ``replay`` view folded from
    that lineage), a :class:`CallJournal` writing
    ``PIPELINE_RUN_STARTED`` → ``PIPELINE_CALL_*`` (one ``sub_run`` row
    per delegation) → ``PIPELINE_RUN_COMPLETED``/``_FAILED``. A returned
    result lands on ``run_completed`` with its terminal status; an
    exception (the guards, a replay halt, cancellation) lands on
    ``run_failed`` and re-raises — the caller decides how that prints.

    ``limits`` overrides the declaration's (the caller owns tighten-only:
    the overlay loader's rule, applied before this call). ``allowed`` is
    the mandate's pipeline scope, enforced on every child selection the
    same way :func:`~paa.pipelines.registry.select` enforces it at the
    top level.
    """
    run_id = task.correlation_id
    resolved_registry = registry if registry is not None else PipelineRegistry.default()
    row = select(ManagerPipeline.name, registry=resolved_registry, allowed=allowed)
    grants = declared_capabilities(row.declaration)
    effective_limits = limits if limits is not None else row.declaration.limits

    journal = CallJournal(ledger, run_id)
    await journal.run_started(
        ManagerPipeline.name, ManagerPipeline.version, run_kind="composition"
    )
    state = _CompositionState(
        journal=journal,
        registry=resolved_registry,
        runners=runners,
        replay=replay,
        allowed=allowed,
    )
    composer = SubRunComposer(
        state,
        name=ManagerPipeline.name,
        stack=(ManagerPipeline.name,),
        limits=effective_limits,
        root_limits=effective_limits,
        sub_runs_left=effective_limits.max_sub_runs,
    )
    agent_ctx = AgentContext(
        correlation_id=run_id,
        session_id=task.session_id,
        modality=task.modality,
        permission_mode=task.permission_mode,
        tokens_budget=effective_limits.max_tokens or 0,
    )
    facade = BuiltinPipelineFacade(
        CompositionHost(ManagerPipeline.name),
        StageAgents(),
        agent_ctx,
        capabilities=grants,
        sub_runs=composer,
    )
    try:
        result = await ManagerPipeline().run(task, facade)
    except asyncio.CancelledError:
        await asyncio.shield(journal.run_failed("cancelled"))
        raise
    except PaaError as exc:
        await journal.run_failed(exc.message)
        raise
    except Exception as exc:
        await journal.run_failed(f"{type(exc).__name__}: {exc}")
        raise
    await journal.run_completed(result.status.value, metrics=dict(result.metrics))
    return run_id, result
