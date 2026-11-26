"""``improve.reflection-loop`` — the guarded self-improvement cycle (C8.1).

SPEC 2 Wave 8's flagship row, as a first-class pipeline: **reflection →
patch proposal → deterministic validation → human gate → commit**, wired
from the existing :mod:`paa.improvement` primitives (the Weekly
Reflection Engine above all) instead of forking them. The loop is a
cycle of five typed, journalled operations on one facade:

===============  ==========================================================
seq  method             what it does
===============  ==========================================================
1  ``improve.reflect``   the reflection engine reads the real ledger and
                         reports domain friction
2  ``improve.propose``   a deterministic :class:`PatchProposal` is
                         synthesised and written as an artifact — never
                         applied
3  ``improve.validate``  the proposal is applied to a scratch copy and
                         the repository's own gate commands run as
                         subprocesses; exit codes are the verdict
4  ``improve.gate``      a human rules; the decision is recorded
                         (approval *and* refusal)
5  ``improve.commit``    the proposal is applied to the real repository,
                         landed as an attested change bundle (C7.2), and
                         committed behind the host-git integrity gate
===============  ==========================================================

The two lines this module holds at all costs:

* **No gate is ever model-decided.** The declaration asks for no
  ``think`` and no ``use_skill``; there is no model anywhere in the
  loop, so validation is exit codes, and the only approval that exists
  is the one a human gave through :data:`~paa.improve.gate.Approver`.
  The facade's ``think`` door is closed by the grant set — the refusal
  is the machine-readable half of the rule.
* **Self-modification inherits the evidence chain.** The commit stage
  has exactly one door — the C7.2 attested bundle — carrying the
  mandate, the diff, the observed validation commands and the human
  approval. There is no "commit without a bundle" path to bypass it
  with, and the host-git integrity gate (ADR-0047) fronts the git call.

The honesty line (ADR-0017), stated where a reader will meet it:
**prototypical few-shot classification ≠ gradient meta-learning.** This
loop is not meta-learning at all — it is a deterministic function of
the ledger's friction numbers driving a guarded change process, the
same thing the reflection engine's weekly analysis always was, now with
a commit at the end that has to survive its own evidence chain.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from paa.core.errors import PaaError
from paa.core.types import ComplexityModality, Permission, PermissionMode
from paa.improve.gate import Approver, HumanDecision, refuse_all
from paa.improve.proposal import (
    DEFAULT_TARGET_FILE,
    PatchProposal,
    proposal_from_report,
    read_proposal,
    write_proposal,
)
from paa.improve.validation import CommandOutcome, ValidationVerdict, run_validation
from paa.improvement.reflection import (
    DomainFriction,
    ReflectionEngine,
    ReflectionReport,
)
from paa.pipelines.base import (
    Capability,
    CapabilityFacade,
    CapabilityNotGrantedError,
    CapabilitySet,
    OutcomeUnknownError,
    PipelineArtifact,
    PipelineDivergedError,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.journal import CallJournal
from paa.pipelines.replay import ReplayAction, ReplayedCallFailedError, ReplayView

__all__ = [
    "IMPROVE_CAPABILITIES",
    "ImproveFacade",
    "ImproveReflectionLoop",
]

log = structlog.get_logger(__name__)

#: The one capability the loop asks for: the final commit. Everything
#: else it does is its own typed operations over host services it was
#: handed (the ledger to reflect from, the repository to improve, the
#: gate commands to run, the approver to ask). An improve loop that
#: someday wants ``think`` — richer rule prose, say — must widen this
#: set and re-run conformance, and the determinism rule says the gates
#: stay model-free regardless.
IMPROVE_CAPABILITIES: frozenset[Capability] = frozenset({Capability.COMMIT})


class ImproveFacade:
    """The host-minted door ``improve.reflection-loop`` runs through.

    Five typed operations, each journalled through the call journal
    (PROPOSED before any effect — the ledger-before-effect invariant —
    then COMPLETED or FAILED), each replay-aware: a re-run of a crashed
    lineage is answered from the record (a completed gate decision is
    *served*, never re-asked; a completed commit is served too, so a
    crash after the bundle landed cannot double-apply it), and a call
    whose outcome the crash left unclassifiable refuses with ``-32008``
    — the pipeline decides, the host never auto-repeats (ADR-0050 D-6).

    The rest of the :class:`~paa.pipelines.base.CapabilityFacade`
    surface is implemented and refused, not absent: the grant set holds
    exactly ``commit``, so every other door closes with the vocabulary's
    own refusal — including ``think``, which is the machine-readable
    half of "no gate is ever model-decided".
    """

    required_permissions: frozenset[Permission] = frozenset()
    capabilities = IMPROVE_CAPABILITIES

    def __init__(
        self,
        *,
        ledger: Any,
        repo_root: Path,
        artifact_home: Path,
        journal: CallJournal,
        validators: Sequence[Sequence[str]] = (),
        approver: Approver | None = None,
        replay: ReplayView | None = None,
        target_file: str = DEFAULT_TARGET_FILE,
        allow_insecure_signing: bool = False,
    ) -> None:
        self._ledger = ledger
        self._engine = ReflectionEngine(ledger)
        self._repo_root = Path(repo_root).resolve()
        self._artifact_home = Path(artifact_home)
        self._journal = journal
        self._validators = tuple(tuple(argv) for argv in validators)
        self._approver: Approver = approver or refuse_all
        self._replay = replay
        self._target_file = target_file
        self._allow_insecure_signing = allow_insecure_signing
        self._grants = CapabilitySet.mint(IMPROVE_CAPABILITIES, run_id=journal.run_id)
        self._rng_state = int(journal.run_id.hex[:16], 16)
        self._seq = 0
        # The gate's decision, carried to the commit stage between the
        # two calls by the pipeline. Set right after ``ask_human``
        # returns; the commit stage refuses without it.
        self._pending_approval: Any = None

    # -- run scope (the facade contract's own carrier) ---------------------

    @property
    def correlation_id(self) -> uuid.UUID:
        return self._journal.run_id

    @property
    def session_id(self) -> uuid.UUID | None:
        return None  # an improvement cycle is not a conversation

    @property
    def modality(self) -> ComplexityModality:
        return ComplexityModality.STANDARD

    @property
    def permission_mode(self) -> PermissionMode:
        # The operator ran the loop on purpose, and the loop's own human
        # gate is stricter than any permission mode: it asks regardless.
        return PermissionMode.AUTO

    @property
    def tokens_budget(self) -> int:
        return 0  # no model is called; a budget of nothing, stated

    @property
    def run_dir(self) -> Path:
        """This run's artifact directory: proposal, evidence, bundle."""
        return self._artifact_home / self._journal.run_id.hex

    # -- the journalling spine ----------------------------------------------

    async def _begin(self, method: str, params: dict[str, Any]) -> Any:
        """Seq up, replay-check, then the PROPOSED row — or the served value.

        Returns the recorded result when the replay answers this call
        (the journal is untouched: the served call's rows already exist
        on the lineage), or ``None`` after appending the PROPOSED row
        for a call that will execute. The three stop rows of the replay
        table (ADR-0050 R3) raise here: divergence (with the audit row
        appended first), outcome-unknown, and replayed-failure.
        """
        self._seq += 1
        if self._replay is not None:
            decision = self._replay.check(self._seq, method, params)
            if decision.action is ReplayAction.SERVE:
                return decision.result
            if decision.action is ReplayAction.DIVERGED:
                await self._journal.run_diverged(
                    self._seq,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
                raise PipelineDivergedError(
                    self._seq,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
            if decision.action is ReplayAction.OUTCOME_UNKNOWN:
                raise OutcomeUnknownError(
                    f"the recorded {method} call's outcome cannot be "
                    "classified; the pipeline decides (D-6)",
                    seq=self._seq,
                )
            if decision.action is ReplayAction.FAIL:
                raise ReplayedCallFailedError(
                    f"the recorded {method} call failed: {decision.reason}"
                )
        await self._journal.call_proposed(self._seq, method, params)
        return None

    async def _complete(self, method: str, result: Mapping[str, Any]) -> None:
        await self._journal.call_completed(self._seq, method, dict(result))

    async def _fail(self, method: str, reason: str) -> None:
        await self._journal.call_failed(self._seq, method, reason[:200])

    # -- the five typed operations -------------------------------------------

    async def reflect(self, window_days: int) -> ReflectionReport:
        """Stage 1: the reflection engine reads the real ledger.

        Not a fork, not a summary — the engine's own ``analyze_window``
        over the events that are actually there, which is what makes the
        finding a *reflection finding* rather than a prompt someone
        wrote.
        """
        params = {"repo_root": str(self._repo_root), "window_days": window_days}
        served = await self._begin("improve.reflect", params)
        if served is not None:
            return _report_from_record(served)
        try:
            until = datetime.now(UTC)
            report = await self._engine.analyze_window(
                since=until - timedelta(days=window_days), until=until
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("improve.reflect", "cancelled"))
            raise
        except Exception as exc:
            await self._fail("improve.reflect", f"{type(exc).__name__}: {exc}")
            raise
        await self._complete(
            "improve.reflect",
            {
                "window_start": report.window_start,
                "window_end": report.window_end,
                "domains": [
                    {
                        "domain": d.domain,
                        "corrections": d.corrections,
                        "rollbacks": d.rollbacks,
                        "successes": d.successes,
                        "score": d.score,
                    }
                    for d in report.domains
                ],
                "high_friction": len(report.high_friction_domains),
            },
        )
        return report

    async def propose(self, friction: DomainFriction) -> PatchProposal:
        """Stage 2: synthesise the deterministic proposal, write the artifact.

        Nothing is applied. The proposal lands as a JSON artifact under
        the run's directory, and the journalled result carries its hash
        and bounded fields — the content itself never enters a payload
        (the opaque-content rule); it is attested by digest and lives in
        the artifact the digest names.
        """
        params = {
            "repo_root": str(self._repo_root),
            "domain": friction.domain,
            "target_file": self._target_file,
        }
        served = await self._begin("improve.propose", params)
        if served is not None:
            return _proposal_from_record(served)
        try:
            proposal = proposal_from_report(
                self._engine,
                friction,
                repo_root=self._repo_root,
                target_file=self._target_file,
            )
            artifact = write_proposal(proposal, self.run_dir / "proposal.json")
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("improve.propose", "cancelled"))
            raise
        except Exception as exc:
            await self._fail("improve.propose", f"{type(exc).__name__}: {exc}")
            raise
        change = proposal.changes[0] if proposal.changes else None
        await self._complete(
            "improve.propose",
            {
                "proposal_sha256": proposal.sha256,
                "domain": proposal.domain,
                "intent": proposal.intent,
                "rationale": proposal.rationale,
                "artifact_path": str(artifact),
                "content_bytes": (
                    len(change.content.encode("utf-8")) if change is not None else 0
                ),
                "content_sha256": change.content_sha256 if change is not None else None,
            },
        )
        return proposal

    async def validate(
        self,
        proposal: PatchProposal,
        *,
        timeout_s: int = 300,
    ) -> ValidationVerdict:
        """Stage 3: the proposal against a scratch copy, gates as subprocesses.

        :mod:`paa.improve.validation` carries the determinism argument;
        the facade's job is the journal discipline (outputs by digest,
        never as text) and the evidence files the commit stage's bundle
        will carry as blobs.
        """
        params = {
            "repo_root": str(self._repo_root),
            "proposal_sha256": proposal.sha256,
            "commands": [list(argv) for argv in self._validators],
            "timeout_s": timeout_s,
        }
        served = await self._begin("improve.validate", params)
        if served is not None:
            return _verdict_from_record(served, self._validators)
        try:
            verdict = await run_validation(
                proposal,
                repo_root=self._repo_root,
                scratch_root=self.run_dir / "validation",
                commands=self._validators,
                timeout_s=timeout_s,
                evidence_dir=self.run_dir / "evidence",
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("improve.validate", "cancelled"))
            raise
        except Exception as exc:
            await self._fail("improve.validate", f"{type(exc).__name__}: {exc}")
            raise
        await self._complete(
            "improve.validate",
            {
                "passed": verdict.passed,
                "reason": verdict.reason,
                "commands": [
                    {
                        # The argv stays out of the record on purpose: a
                        # gate command is operator-typed text and can carry
                        # a credential (``--token=…``) the way a prompt can;
                        # the PROPOSED row already fingerprints the whole
                        # command list, so a served call's argv is *known
                        # equal* to the live one and is rebuilt from it.
                        "command_index": index,
                        "exit_code": command.exit_code,
                        "timed_out": command.timed_out,
                        "stdout_sha256": command.stdout_sha256,
                        "stdout_bytes": command.stdout_bytes,
                        "stderr_sha256": command.stderr_sha256,
                        "stderr_bytes": command.stderr_bytes,
                    }
                    for index, command in enumerate(verdict.commands, start=1)
                ],
            },
        )
        return verdict

    async def ask_human(self, proposal: PatchProposal, verdict: ValidationVerdict) -> HumanDecision:
        """Stage 4: the human gate. The decision is recorded, both ways.

        On replay a completed decision is served back — the human is not
        asked twice (the ADR-0034 rule). The journalled result carries
        the full decision artifact: the record that a human, not a
        model, ruled.
        """
        params = {
            "repo_root": str(self._repo_root),
            "proposal_sha256": proposal.sha256,
            "validation_passed": verdict.passed,
            "failed_commands": len(verdict.failed_commands),
        }
        served = await self._begin("improve.gate", params)
        if served is not None:
            return _decision_from_record(served)
        try:
            decision = await self._approver(proposal, verdict)
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("improve.gate", "cancelled"))
            raise
        except Exception as exc:
            await self._fail("improve.gate", f"{type(exc).__name__}: {exc}")
            raise
        await self._complete("improve.gate", decision.to_json())
        return decision

    async def commit_proposal(self, proposal: PatchProposal) -> dict[str, Any]:
        """Stage 5: apply, attest (C7.2), and commit behind the git gate.

        The order is the crash discipline: the PROPOSED row is already
        on the ledger (``_begin`` above), then the proposal is applied
        to the real repository, then the attested bundle is built over
        the changed tree (so its diff section shows exactly what
        landed), and only then does git commit — through the integrity
        gate. A crash between apply and commit leaves the change in the
        working tree and a bundle that describes it: recoverable by
        inspection, never by silent re-application.
        """
        params = {
            "repo_root": str(self._repo_root),
            "proposal_sha256": proposal.sha256,
        }
        served = await self._begin("improve.commit", params)
        if served is not None:
            return _mapping_from_record(served)
        try:
            result = await self._commit_effects(proposal)
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("improve.commit", "cancelled"))
            raise
        except Exception as exc:
            await self._fail("improve.commit", f"{type(exc).__name__}: {exc}")
            raise
        await self._complete("improve.commit", result)
        return result

    async def _commit_effects(self, proposal: PatchProposal) -> dict[str, Any]:
        """The commit stage's effects: apply, attest, git-commit."""
        import hashlib

        from paa.attest.bundle import build_bundle, load_or_generate_key
        from paa.improve.gitcommit import guarded_git_commit, is_git_repository

        written = proposal.apply(self._repo_root)

        evidence_dir = self.run_dir / "evidence"
        extra_evidence: list[tuple[str, Path, dict[str, Any]]] = []
        observed_command_rows: list[dict[str, Any]] = []
        if evidence_dir.is_dir():
            for path in sorted(evidence_dir.glob("validation-*.json")):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                extra_evidence.append(
                    (
                        "improve-validation",
                        path,
                        {
                            "kind": "improve-validation/1",
                            "run_id": self._journal.run_id.hex,
                            "command_index": len(extra_evidence) + 1,
                        },
                    )
                )
                payload = _load_json_object(path)
                outcome = payload.get("command") or {}
                observed_command_rows.append(
                    {
                        "command": [str(part) for part in (outcome.get("argv") or [])],
                        "exit_code": outcome.get("exit_code"),
                        "evidence_sha256": digest,
                    }
                )

        if self._pending_approval is None:
            raise PaaError(
                "the commit stage requires the recorded human approval; none "
                "was carried — this is a guarded loop, not a conveyor"
            )

        key = load_or_generate_key(
            self.run_dir / "attest-key.json",
            allow_insecure=self._allow_insecure_signing,
        )
        bundle_dir = self.run_dir / "attest-bundle"
        built = build_bundle(
            out_dir=bundle_dir,
            workspace=self._repo_root,
            goal=f"self-improvement: {proposal.intent}",
            requester="improve.reflection-loop",
            requester_kind="pipeline",
            asserted_commands=(),
            asserted_tests=(),
            approvals=(self._pending_approval,),
            extra_evidence=extra_evidence,
            observed_command_rows=observed_command_rows,
            key=key,
        )

        git_commit: str | None = None
        if await is_git_repository(self._repo_root):
            commit_result = await guarded_git_commit(
                self._repo_root,
                [change.path for change in proposal.changes],
                f"improve: {proposal.intent} (attested {built.bundle_id})",
            )
            git_commit = commit_result.head

        return {
            "applied_files": [str(path) for path in written],
            "bundle_dir": str(built.bundle_dir),
            "bundle_id": built.bundle_id,
            "chain_root": built.chain_root,
            "git_commit": git_commit,
        }

    def carry_approval(self, decision: HumanDecision) -> None:
        """Bridge the gate's decision to the bundle's approval row.

        The bundle's approvals are the C7.2 ``Approval`` shape; the
        gate's decision is the improve vocabulary. This is the one place
        the two meet, so they cannot drift: the decision's identity,
        verdict and note become the approval row verbatim. The attest
        import is lazy — a minimal install (without ``paa[attest]``)
        still imports this package; only the commit stage needs the kit.
        """
        from paa.attest.bundle import Approval

        self._pending_approval = Approval(
            approver_kind=decision.approver_kind,
            approver_id=decision.approver_id,
            decision=decision.decision,
            note=decision.note,
            recorded_at=decision.recorded_at,
        )

    # -- refused capabilities (protocol shape; the grant set closes them) --

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        """Refused: the guarded loop is model-free by declaration.

        This closed door is half of the determinism rule — a loop that
        could ask a model "is this change good?" would be a loop whose
        gate was model-decided, which SPEC 2 §9 forbids.
        """
        self._grants.require(Capability.THINK)
        raise CapabilityNotGrantedError(Capability.THINK.value)  # pragma: no cover

    async def call_agent(self, role: Any, message: Any, agent_ctx: Any) -> Any:
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
        """Refused: this loop's human gate is its own typed operation."""
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
        """The facade protocol's ``commit`` — refused with a pointer.

        The protocol verb carries the task pipeline's vocabulary; this
        loop commits through its own typed ``commit_proposal``
        operation (the guarded cycle's fifth stage), which is the only
        path that builds the attested bundle first.
        """
        self._grants.require(Capability.COMMIT)
        raise PaaError(
            "improve.reflection-loop commits through its own typed "
            "commit_proposal operation (the guarded cycle's fifth stage), "
            "not the task pipeline's commit verb"
        )

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult:
        self._grants.require(Capability.SUB_RUN)
        raise CapabilityNotGrantedError(Capability.SUB_RUN.value)  # pragma: no cover

    # -- determinism aids and heartbeat --------------------------------------

    async def now(self) -> datetime:
        """Recorded time — the clock a replayable pipeline is allowed to read."""
        return datetime.now(UTC)

    async def random(self) -> float:
        """Seeded per run (the run id seeds it); the loop never draws."""
        self._rng_state = (
            self._rng_state * 6364136223846793005 + 1442695040888963407
        ) % (1 << 64)
        return self._rng_state / (1 << 64)

    async def progress(self, status: str) -> None:
        """Heartbeat; never journalled."""
        log.debug(
            "improve.progress",
            correlation_id=str(self._journal.run_id),
            status=status,
        )

    # -- in-process transport reads -------------------------------------------

    @property
    def agent_ctx(self) -> Any:
        raise PaaError(
            "improve.reflection-loop drives no agent lineage; there is no "
            "agent context",
            actual="improve.reflection-loop",
        )

    async def stage_question(self, role: Any) -> None:
        return None  # in-process transport read; the loop asks no task gates

    async def project(self) -> Any:
        raise PaaError(
            "improve.reflection-loop drives no task lineage; there is no "
            "projection to read",
            actual="improve.reflection-loop",
        )


# ---------------------------------------------------------------------------
# Served-result reconstruction: the record is the value
# ---------------------------------------------------------------------------


def _mapping_from_record(record: Any) -> dict[str, Any]:
    return dict(record) if isinstance(record, Mapping) else {}


def _report_from_record(record: Any) -> ReflectionReport:
    """Rebuild a reflection report a replay served back.

    The domains' friction counts are the record; the scores are
    recomputed from them the way the engine computes them (the property
    is the same function), so a served report answers with the same
    numbers a live one would.
    """
    payload = _mapping_from_record(record)
    domains: list[DomainFriction] = []
    for row in payload.get("domains") or []:
        if not isinstance(row, Mapping):
            continue
        domains.append(
            DomainFriction(
                domain=str(row.get("domain") or "unknown"),
                corrections=_int_or(row.get("corrections")),
                rollbacks=_int_or(row.get("rollbacks")),
                successes=_int_or(row.get("successes")),
            )
        )
    return ReflectionReport(
        window_start=_dt_or(payload.get("window_start")),
        window_end=_dt_or(payload.get("window_end")),
        domains=domains,
    )


def _verdict_from_record(
    record: Any, validators: Sequence[Sequence[str]]
) -> ValidationVerdict:
    """Rebuild a validation verdict a replay served back.

    The per-command stdout/stderr texts are not in the record (digests
    are), and neither is the argv (the operator's own text — the record
    carries the command's *index*). The argv is rebuilt from the live
    validator list the fingerprint already proved identical to the
    recorded one, so a served verdict answers the pipeline's question —
    did the gates pass — with the same commands a live one would name.
    """
    payload = _mapping_from_record(record)
    commands: list[CommandOutcome] = []
    for row in payload.get("commands") or []:
        if not isinstance(row, Mapping):
            continue
        index = row.get("command_index")
        position = (
            index - 1
            if isinstance(index, int) and not isinstance(index, bool) and index >= 1
            else len(commands)
        )
        # The live list the fingerprint proved identical to the recorded
        # one; out of range (a malformed record) degrades to an empty
        # argv rather than inventing a command.
        argv = tuple(validators[position]) if 0 <= position < len(validators) else ()
        commands.append(
            CommandOutcome(
                argv=argv,
                exit_code=row.get("exit_code") if isinstance(row.get("exit_code"), int) else None,
                timed_out=bool(row.get("timed_out")),
                duration_ms=0.0,
                stdout="",
                stderr="",
                stdout_sha256=str(row.get("stdout_sha256") or ""),
                stderr_sha256=str(row.get("stderr_sha256") or ""),
                stdout_bytes=_int_or(row.get("stdout_bytes")),
                stderr_bytes=_int_or(row.get("stderr_bytes")),
                truncated=False,
            )
        )
    return ValidationVerdict(
        commands=tuple(commands),
        scratch_root="",
        reason=str(payload.get("reason") or ""),
    )


def _decision_from_record(record: Any) -> HumanDecision:
    payload = _mapping_from_record(record)
    return HumanDecision(
        decision=str(payload.get("decision") or ""),
        approver_kind=str(payload.get("approver_kind") or ""),
        approver_id=str(payload.get("approver_id") or ""),
        note=str(payload.get("note") or ""),
        recorded_at=str(payload.get("recorded_at") or ""),
    )


def _proposal_from_record(record: Any) -> PatchProposal:
    """Reload the proposal artifact a replay served back by pointer.

    The journalled result carries the artifact's path and the
    proposal's digest; the artifact is the content. A missing artifact
    is a named refusal — replay cannot invent the proposal the digest
    attests, and serving anything else would forge history.
    """
    payload = _mapping_from_record(record)
    path = Path(str(payload.get("artifact_path") or ""))
    if not path.is_file():
        raise PaaError(
            f"the recorded proposal artifact {path} is gone; the served "
            "improve.propose call cannot be answered without it"
        )
    proposal = read_proposal(path)
    recorded = str(payload.get("proposal_sha256") or "")
    if recorded and proposal.sha256 != recorded:
        raise PaaError(
            f"the proposal artifact {path} no longer hashes to the recorded "
            f"digest {recorded[:12]}…; the served call's record and its "
            "artifact have diverged"
        )
    return proposal


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _int_or(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _dt_or(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(UTC)


class ImproveReflectionLoop:
    """The pipeline: five stages, two first-class refusal paths.

    The stage order is the row's own order — reflection → proposal →
    validation → human gate → commit — and the two ways the cycle stops
    without landing anything are outcomes, not errors:

    * **validation failed** → the run ends ``failed`` with the failing
      commands in the metrics; no human was asked, no bundle was built,
      nothing was applied to the real repository;
    * **the human refused** → the run ends ``failed`` with the refusal
      in the metrics; the proposal artifact and its journal rows exist
      (the proposal is *recorded*), and nothing was applied.

    A clean history is a completed run that proposes nothing: "no
    high-friction domains" is the loop working, not the loop failing.
    """

    name = "improve.reflection-loop"
    version = "4.1.0"
    requires_runtime = ">=4.1,<5"
    required_permissions: frozenset[Permission] = frozenset()
    capabilities = IMPROVE_CAPABILITIES
    limits = RunLimits()

    async def run(self, task: PipelineTask, ctx: CapabilityFacade) -> PipelineResult:
        facade = self._narrow(ctx)
        window_days = _int_param(task, "window_days", 7)
        timeout_s = _int_param(task, "validation_timeout_s", 300)

        await facade.progress("reflecting over the ledger window")
        report = await facade.reflect(window_days)
        hot = report.high_friction_domains
        if not hot:
            return PipelineResult(
                status=PipelineStatus.COMPLETED,
                summary=(
                    f"reflection found {len(report.domains)} domain(s), none "
                    "high-friction; nothing to propose"
                ),
                metrics={
                    "domains": len(report.domains),
                    "high_friction": 0,
                    "stopped_reason": "clean_history",
                },
            )

        await facade.progress("proposing the deterministic patch")
        proposal = await facade.propose(hot[0])

        await facade.progress("validating the proposal against a scratch copy")
        verdict = await facade.validate(proposal, timeout_s=timeout_s)
        if not verdict.passed:
            failed = [
                {
                    # Index, not argv: the failing command's text is the
                    # operator's own configuration (it can carry a
                    # credential), and the evidence file names it in full.
                    "command_index": index,
                    "exit_code": command.exit_code,
                    "timed_out": command.timed_out,
                }
                for index, command in enumerate(verdict.commands, start=1)
                if not command.green
            ]
            metrics: dict[str, Any] = {
                "stopped_reason": "validation_failed",
                "proposal_sha256": proposal.sha256,
                "failed_commands": failed,
            }
            if not verdict.commands:
                # The zero-validators refusal: nothing ran, and the reason
                # is the whole verdict.
                metrics["validation_reason"] = verdict.reason
            return PipelineResult(
                status=PipelineStatus.FAILED,
                summary=(
                    "deterministic validation refused the proposal "
                    f"({len(verdict.failed_commands)} of {len(verdict.commands)} "
                    "gate command(s) red); nothing was applied"
                ),
                metrics=metrics,
            )

        await facade.progress("asking the human gate")
        decision = await facade.ask_human(proposal, verdict)
        if not decision.approved:
            return PipelineResult(
                status=PipelineStatus.FAILED,
                summary=(
                    f"the human gate refused the proposal ({decision.approver_id}); "
                    "the proposal is recorded, nothing was applied"
                ),
                metrics={
                    "stopped_reason": "human_gate_refused",
                    "proposal_sha256": proposal.sha256,
                    "decision": decision.decision,
                    "approver": decision.approver_id,
                },
            )

        await facade.progress("committing the attested change")
        facade.carry_approval(decision)
        result = await facade.commit_proposal(proposal)
        git_commit = result.get("git_commit")
        return PipelineResult(
            status=PipelineStatus.COMPLETED,
            summary=(
                f"improvement committed as attested bundle {result['bundle_id']}"
                + (f" (git {str(git_commit)[:12]}…)" if git_commit else "")
            ),
            metrics={
                "stopped_reason": "committed",
                "proposal_sha256": proposal.sha256,
                "bundle_dir": result["bundle_dir"],
                "bundle_id": result["bundle_id"],
                "chain_root": result["chain_root"],
                "git_commit": git_commit,
            },
            artifacts=[
                PipelineArtifact(kind="attest_bundle", sha256=result["chain_root"]),
                PipelineArtifact(kind="improve_proposal", sha256=proposal.sha256),
            ],
        )

    def _narrow(self, ctx: CapabilityFacade) -> ImproveFacade:
        """Narrow the protocol to the facade the improve host mints.

        The coder's rule: a foreign facade is a host bug worth naming,
        not a duck type to tolerate.
        """
        if not isinstance(ctx, ImproveFacade):
            raise PaaError(
                "improve.reflection-loop requires the host-minted ImproveFacade",
                actual=type(ctx).__name__,
            )
        return ctx


def _int_param(task: PipelineTask, key: str, default: int) -> int:
    """A positive int param, or the default. ``True`` is not 1."""
    value = task.params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
