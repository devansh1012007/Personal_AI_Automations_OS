"""``builtin.observe`` — the T1 adapter as pipeline #4 (C7.1 / P1.4).

The external-harness spec's §6 rule, made concrete: an observed CLI is a
first-class pipeline in the registry, runs on a host-minted facade like
the other three, and differs from them in exactly two declared facts —
``resume = "none"`` and ``evidence_tier = "T1"``. The facade exposes one
typed operation (``observe.run``) where the coder's exposes ``think``
and ``use_skill``: the observer asks for **no** capabilities, because
the observer's mind is the only one involved and the host mints nothing
for it. The call journal still wraps the run — the PROPOSED row lands
before the child is spawned (ledger-before-effect, the one invariant),
and the COMPLETED row carries the bundle's hash, never its text.

What the pipeline is *not* is equally declared: it cannot replay. A
re-run is a new observation of a new child, not a resume of the old
one, and the crash classifier exists precisely because the honest
answer to "what now?" after a crash is a named state, not a retry.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.core.types import ComplexityModality, Permission, PermissionMode
from paa.observe.observer import ObserveOutcome, ObserverCore
from paa.pipelines.base import (
    Capability,
    CapabilityFacade,
    CapabilityNotGrantedError,
    CapabilitySet,
    PipelineArtifact,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.journal import CallJournal
from paa.sandbox.base import Sandbox

__all__ = [
    "OBSERVE_CAPABILITIES",
    "ObserveFacade",
    "ObservePipeline",
]

#: The empty ask. The observer grants itself nothing because there is
#: nothing to grant: the host runs the child and records what it sees
#: (the spec §6 adapters "cannot honour the replay contract" and their
#: declaration says so). An observer that someday wants ``remember`` —
#: say, to file the classification as a fact — must widen this set and
#: re-run conformance; the grant computation will still narrow it.
OBSERVE_CAPABILITIES: frozenset[Capability] = frozenset()


class ObserveFacade:
    """The host-minted door ``builtin.observe`` runs through.

    One typed operation, journalled as ``observe.run`` at seq 1: the
    command, its knobs and the workspace are the call's identity (the
    fingerprint covers them), and the result the journal serves back is
    the bounded summary — state, exit code, the bundle's SHA-256 and
    where it landed. The transcript never enters a payload; it lives in
    the bundle, attested by hash, exactly as chat messages and tool
    results are attested in their vocabularies.

    The rest of the :class:`~paa.pipelines.base.CapabilityFacade` surface
    is implemented and refused, not absent: the observer asks for **no**
    capabilities (``OBSERVE_CAPABILITIES`` is empty), so every capability
    method is a door the empty grant set closes — the same shape the
    coder's facade gives its ungranted reaches, and the reason a
    third-party facade can be told apart from the host's by behaviour,
    not just by ``isinstance``.
    """

    required_permissions: frozenset[Permission] = frozenset()
    capabilities = OBSERVE_CAPABILITIES

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        journal: CallJournal,
        workspace: Path,
        bundle_home: Path,
        child_env: Mapping[str, str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._journal = journal
        self._workspace = Path(workspace)
        self._bundle_home = Path(bundle_home)
        self._child_env = dict(child_env) if child_env else None
        self._core = ObserverCore(sandbox, bundle_home=self._bundle_home)
        # The empty ask, minted: every capability door below is closed by
        # this set, exactly as ADR-0051's only door demands.
        self._grants = CapabilitySet.mint(
            OBSERVE_CAPABILITIES, run_id=journal.run_id
        )
        # Seeded from the run id (see ``random``): per-run determinism, the
        # coder facade's own discipline, at zero cost to the observer.
        self._rng_state = int(journal.run_id.hex[:16], 16)

    @property
    def workspace(self) -> Path:
        return self._workspace

    # -- run scope (the facade contract's own carrier) ---------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._journal.run_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return None  # an observation is not a conversation

    @property
    def modality(self) -> ComplexityModality:
        return ComplexityModality.STANDARD

    @property
    def permission_mode(self) -> PermissionMode:
        return PermissionMode.AUTO  # the operator ran the command on purpose

    @property
    def tokens_budget(self) -> int:
        return 0  # no model is called; a budget of nothing, stated

    async def observe_command(
        self,
        command: Sequence[str],
        *,
        timeout_s: int = 600,
        memory_mb: int = 512,
        transcript_cap_kb: int = 2048,
    ) -> ObserveOutcome:
        """Run the observed CLI and return the classified outcome.

        Journals ``observe.run`` PROPOSED → COMPLETED/FAILED around
        :meth:`ObserverCore.observe`. The bundle run id is the ledger's
        run id, so a lineage, its trace and its artifact share one name.
        """
        if not command:
            raise PaaError("builtin.observe needs a non-empty command")
        params: dict[str, Any] = {
            "command": list(command),
            "workspace": str(self._workspace),
            "timeout_s": timeout_s,
            "memory_mb": memory_mb,
            "transcript_cap_kb": transcript_cap_kb,
        }
        await self._journal.call_proposed(1, "observe.run", params)
        try:
            outcome = await self._core.observe(
                command,
                workspace=self._workspace,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
                transcript_cap_kb=transcript_cap_kb,
                run_id=str(self._journal.run_id),
                env=self._child_env,
            )
        except BaseException as exc:
            # The FAILED row is shielded: an observer killed mid-run must
            # still leave the audit that the call never resolved — the
            # sweep later closes it as interrupted, never re-drives it.
            # The exception's text is deliberately NOT journalled: a
            # sandbox start failure carries the child's argv, which may
            # contain a secret meant only for the observed process.
            await asyncio.shield(
                self._journal.call_failed(
                    1,
                    "observe.run",
                    f"observer did not resolve ({type(exc).__name__})",
                )
            )
            raise
        result = {
            "state": outcome.classification.state.value,
            "exit_code": outcome.classification.exit_code,
            "duration_ms": round(outcome.result.duration_ms, 3),
            "bundle_run_id": outcome.run_id,
            "bundle_path": str(outcome.bundle_path),
            "bundle_sha256": outcome.bundle.sha256(),
            "files_created": outcome.classification.files_created,
            "files_modified": outcome.classification.files_modified,
            "files_deleted": outcome.classification.files_deleted,
            "git_dirty": outcome.classification.git_dirty,
            "commits": outcome.classification.commits,
            "evidence_tier": outcome.evidence_tier,
            "resume": "none",
        }
        await self._journal.call_completed(1, "observe.run", result)
        return outcome

    # -- refused capabilities (protocol shape; the empty grant refuses) ----
    #
    # The observer's mind is the only one involved: it thinks nothing,
    # recalls nothing, commits nothing. Each door below is the facade
    # contract's own shape, closed by the empty grant set — so a caller
    # gets the vocabulary's refusal (``-32006``, with the granted set
    # named as empty) rather than an AttributeError.

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        self._grants.require(Capability.THINK)
        raise CapabilityNotGrantedError(Capability.THINK.value)  # pragma: no cover

    async def call_agent(
        self, role: Any, message: Any, agent_ctx: Any
    ) -> Any:
        self._grants.require(Capability.CALL_AGENT)
        raise CapabilityNotGrantedError(Capability.CALL_AGENT.value)  # pragma: no cover

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> str:
        self._grants.require(Capability.USE_SKILL)
        raise CapabilityNotGrantedError(Capability.USE_SKILL.value)  # pragma: no cover

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        self._grants.require(Capability.RECALL)
        raise CapabilityNotGrantedError(Capability.RECALL.value)  # pragma: no cover

    async def remember(self, fact: Mapping[str, Any]) -> None:
        self._grants.require(Capability.REMEMBER)
        raise CapabilityNotGrantedError(Capability.REMEMBER.value)  # pragma: no cover

    async def gate(self, role: Any, payload: Mapping[str, Any]) -> Any:
        self._grants.require(Capability.GATE)
        raise CapabilityNotGrantedError(Capability.GATE.value)  # pragma: no cover

    async def submit_plan(
        self,
        steps: Sequence[Mapping[str, Any]],
        *,
        requirements: Mapping[str, Any] | None = None,
        bypassed_planner: bool = False,
    ) -> None:
        self._grants.require(Capability.PLAN_SUBMIT)
        raise CapabilityNotGrantedError(Capability.PLAN_SUBMIT.value)  # pragma: no cover

    async def check_policy(self, state: Any) -> Any:
        self._grants.require(Capability.POLICY_CHECK)
        raise CapabilityNotGrantedError(Capability.POLICY_CHECK.value)  # pragma: no cover

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        self._grants.require(Capability.COMMIT)
        raise CapabilityNotGrantedError(Capability.COMMIT.value)  # pragma: no cover

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        self._grants.require(Capability.SUB_RUN)
        raise CapabilityNotGrantedError(Capability.SUB_RUN.value)  # pragma: no cover

    # -- determinism aids and heartbeat --------------------------------------

    async def now(self) -> datetime:
        """Recorded time. The observer reads it for the bundle, not for replay."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """The door exists because the contract has it; the observer never draws.

        Seeded from the run id with the coder's tiny LCG anyway: a door on
        the facade contract that behaves differently per run would be a
        determinism hole the next adapter could fall into.
        """
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) % (1 << 64)
        return self._rng_state / (1 << 64)

    async def progress(self, status: str) -> None:
        """Heartbeat; never journalled."""

    # -- in-process transport reads -------------------------------------------

    @property
    def agent_ctx(self) -> Any:
        raise PaaError(
            "builtin.observe drives no agent lineage; there is no agent context",
            actual="builtin.observe",
        )

    async def stage_question(self, role: Any) -> None:
        return None  # in-process transport read; the observer asks no gates

    async def project(self) -> Any:
        raise PaaError(
            "builtin.observe drives no task lineage; there is no projection to read",
            actual="builtin.observe",
        )


class ObservePipeline:
    """``builtin.observe``: run any CLI, record the T1 evidence, classify.

    The control flow is one call and a verdict: the command comes from
    the task's goal (``shlex`` — a goal is a command line here; shell
    metacharacters are inert because no shell is involved, and an
    unbalanced quote is refused at parse time), the knobs
    from the task's params against the declaration's defaults, and the
    result maps the child's fate onto the pipeline's — a clean exit is
    ``completed``, everything else is ``failed`` *with the
    classification riding the metrics*, because a failed harness is a
    successful observation only in the sense that the evidence exists.
    """

    name = "builtin.observe"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = OBSERVE_CAPABILITIES
    limits = RunLimits()

    async def run(self, task: PipelineTask, ctx: CapabilityFacade) -> PipelineResult:
        facade = self._narrow(ctx)
        command = self._command_from(task)
        outcome = await facade.observe_command(
            command,
            timeout_s=_int_param(task, "timeout_s", 600),
            memory_mb=_int_param(task, "memory_mb", 512),
            transcript_cap_kb=_int_param(task, "transcript_cap_kb", 2048),
        )
        return self._result(outcome)

    def _command_from(self, task: PipelineTask) -> list[str]:
        """The goal as argv: no shell, so metacharacters stay literal data."""
        try:
            parts = shlex.split(task.goal)
        except ValueError as exc:
            raise PaaError(
                "builtin.observe takes a command line as its goal and could "
                f"not parse it: {exc}",
                goal=task.goal,
            ) from exc
        if not parts:
            raise PaaError(
                "builtin.observe needs a non-empty command as its goal",
                goal=task.goal,
            )
        return parts

    def _result(self, outcome: ObserveOutcome) -> PipelineResult:
        c = outcome.classification
        clean = c.state.value == "clean_exit"
        status = PipelineStatus.COMPLETED if clean else PipelineStatus.FAILED
        metrics: dict[str, Any] = {
            "state": c.state.value,
            "exit_code": c.exit_code,
            "signal": c.signal,
            "duration_ms": round(c.duration_ms, 3),
            "files_created": c.files_created,
            "files_modified": c.files_modified,
            "files_deleted": c.files_deleted,
            "git_dirty": c.git_dirty,
            "commits": c.commits,
            "evidence_tier": outcome.evidence_tier,
            "resume": "none",
            "bundle_run_id": outcome.run_id,
            "bundle_path": str(outcome.bundle_path),
        }
        artifacts = [
            PipelineArtifact(
                kind="observe_bundle", sha256=outcome.bundle.sha256()
            )
        ]
        return PipelineResult(
            status=status,
            summary=(
                "clean exit"
                if clean
                else f"{c.state.value}: {c.notes}"
            ),
            metrics=metrics,
            artifacts=artifacts,
        )

    def _narrow(self, ctx: CapabilityFacade) -> ObserveFacade:
        """Narrow the protocol to the facade the observe host mints.

        The coder's rule, restated: a foreign facade is a host bug worth
        naming, not a duck type to tolerate.
        """
        if not isinstance(ctx, ObserveFacade):
            raise PaaError(
                "builtin.observe requires the host-minted ObserveFacade",
                actual=type(ctx).__name__,
            )
        return ctx


def _int_param(task: PipelineTask, key: str, default: int) -> int:
    """A positive int param, or the default. ``True`` is not 1."""
    value = task.params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
