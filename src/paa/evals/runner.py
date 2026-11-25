"""The eval runner — task x harness, scored deterministically, journalled (C7.4).

One pair (one task, one harness) runs in an isolated workspace under
``PAA_HOME/eval/<eval-id>/`` and is scored by :mod:`paa.evals.scoring`;
a matrix of pairs becomes an :class:`EvalReport` — canonical JSON under
the run dir plus the Rich table the CLI prints. The sequencing per pair:

1. lay the task's fixture and suite into a fresh workspace, and snapshot
   the manifest (the scoring baseline — the only bytes the
   forbidden-effects probe will compare against);
2. run the harness the adapter named, under the mediation the adapter
   declared — a ``mediated`` adapter under
   :class:`~paa.recorder.core.RecorderCore` (flight record, MCP policy
   proxy, observe bundle, attested bundle), an ``observed`` adapter under
   :class:`~paa.observe.observer.ObserverCore` (the T1 floor, nothing
   more). The runner enforces the mode structurally, so the report's
   mediation label is a fact about how the pair ran, not a claim;
3. snapshot the manifest again and score the workspace checks
   (intent, forbidden effects) — **before** the suite runs, because the
   suite is a scoring instrument and must not perturb the subject;
4. run the task's own pytest suite (the ``tests_pass`` check, by the
   validation engine's own :class:`~paa.validation.test_runner.PytestRunner`);
5. for a mediated pair, verify the flight record and the attested bundle
   with the reviewer's own verifiers; for an observed pair, report the
   bundle check as not applicable — stated, never scored as a pass;
6. journal one ``PIPELINE_RUN_COMPLETED`` row on the matrix's ledger
   lineage, carrying the verdict's and the task's content hashes.

Why the ledger row reuses ``PIPELINE_RUN_COMPLETED`` rather than minting
an eval-specific ``EventType``: the run-completion audit vocabulary is
the tree's existing home for "a journalled run that is not a task" —
``builtin.observe`` (C7.1) already journals arbitrary-CLI runs through
it — and, load-bearingly, the recovery sweep's pipeline-lineage
exemption (:func:`paa.ledger.recovery._is_pipeline_only_lineage`) keeps
an eval-only lineage from being re-queued as phantom task work on every
boot. A dedicated ``EVAL_*`` member would need the same exemption
welded into a package this task does not own; the reuse keeps the
corpus rows phase-inert, sweep-exempt and queryable now, with the eval
identity carried in the payload (``metrics.task`` / ``metrics.harness``
/ the ``eval:<task>:<harness>`` discriminator), and a dedicated
vocabulary stays a cheap follow-up if the corpus outgrows sharing one.

The report's determinism boundary, stated once: every scoring fact —
task, harness, mediation, check verdicts, overall, both content hashes,
the reproduction argv — is a pure function of the inputs, pinned by
``scoring_sha256`` and byte-identical across re-runs. The measured
facts (wall time, run ids, evidence paths) ride beside them under each
row's ``measured`` key and are excluded from the hash, because a
measurement that could not vary would not be one.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paa.attest.verify import verify_bundle
from paa.core.errors import PaaError
from paa.core.types import EventType, new_correlation_id
from paa.evals.harnesses import HarnessAdapter
from paa.evals.scoring import (
    CHECK_BUNDLE,
    CheckStatus,
    CheckVerdict,
    PairVerdict,
    bundle_not_applicable,
    score_bundle,
    score_forbidden,
    score_intent,
    score_tests,
)
from paa.evals.tasks import EvalTask
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.store import LedgerStore
from paa.observe.bundle import ManifestDiff, snapshot_manifest
from paa.observe.observer import ObserverCore
from paa.recorder.core import PolicySpec, RecorderCore
from paa.recorder.record import verify_record
from paa.sandbox.base import Sandbox
from paa.validation.test_runner import PytestResult, PytestRunner

__all__ = [
    "EVAL_POLICY",
    "REPORT_SCHEMA",
    "EvalReport",
    "EvalRunner",
    "PairResult",
    "eval_ledger_row",
]

#: The report format's identity. Additive changes only within ``/1``.
REPORT_SCHEMA = "paa-eval-report/1"

#: The policy a mediated eval pair runs under. ``accept_edits`` auto-
#: allows the file tools (the shipped harness's whole surface); reads
#: are pre-allowed explicitly; ``bash`` is left to the ask default, which
#: an unattended proxy resolves to a recorded denial — a harness that
#: needs commands gets a policy that names them, never a quiet allow.
EVAL_POLICY = PolicySpec(mode="accept_edits", allow=("read_file",))


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class PairResult:
    """One pair's scored outcome, plus the measured facts beside it."""

    verdict: PairVerdict
    task_sha256: str
    wall_time_ms: float
    subject_state: str
    exit_code: int | None
    tests: PytestResult
    evidence: dict[str, str] = field(default_factory=dict)
    refusal: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            **self.verdict.to_json(),
            "task_sha256": self.task_sha256,
            "verdict_sha256": self.verdict.verdict_sha256(),
            "measured": {
                "wall_time_ms": round(self.wall_time_ms, 1),
                "subject_state": self.subject_state,
                "exit_code": self.exit_code,
                "refusal": self.refusal,
                "tests": {
                    "passed": self.tests.passed,
                    "failed": self.tests.failed,
                    "errors": self.tests.errors,
                    "skipped": self.tests.skipped,
                    "collected": self.tests.collected,
                },
                "evidence": dict(self.evidence),
            },
        }


@dataclass(slots=True)
class EvalReport:
    """A matrix run's report: the rows, the summary, the reproduction line."""

    eval_id: str
    correlation_id: uuid.UUID
    argv: tuple[str, ...]
    rows: list[PairResult]
    report_dir: Path | None = None

    @property
    def report_path(self) -> Path | None:
        """Where :meth:`EvalRunner.run_matrix` wrote this report, if it did."""
        if self.report_dir is None:
            return None
        return self.report_dir / "report.json"

    @property
    def reproduce(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)

    def scoring_view(self) -> dict[str, Any]:
        """The deterministic half of the report: everything but measurement.

        Dropping ``eval_id``/``created_at`` and each row's ``measured``
        block leaves a pure function of the invocation and the task set
        — the bytes the determinism guarantee is about.
        """
        return {
            "schema": REPORT_SCHEMA,
            "reproduce": self.reproduce,
            "rows": [
                {
                    "task": row.verdict.task,
                    "harness": row.verdict.harness,
                    "mediation": row.verdict.mediation,
                    "checks": row.verdict.to_json()["checks"],
                    "overall": row.verdict.overall,
                    "verdict_sha256": row.verdict.verdict_sha256(),
                    "task_sha256": row.task_sha256,
                }
                for row in self.rows
            ],
            "summary": self._summary(),
        }

    def _summary(self) -> dict[str, Any]:
        by_harness: dict[str, dict[str, int]] = {}
        for row in self.rows:
            bucket = by_harness.setdefault(
                row.verdict.harness, {"passed": 0, "failed": 0}
            )
            bucket["passed" if row.verdict.overall is CheckStatus.HELD else "failed"] += 1
        return {
            "rows": len(self.rows),
            "passed": sum(1 for r in self.rows if r.verdict.overall is CheckStatus.HELD),
            "failed": sum(1 for r in self.rows if r.verdict.overall is not CheckStatus.HELD),
            "by_harness": by_harness,
        }

    def scoring_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json(self.scoring_view()).encode("utf-8")
        ).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "eval_id": self.eval_id,
            "correlation_id": str(self.correlation_id),
            "created_at": _utc_now_iso(),
            "reproduce": self.reproduce,
            "rows": [row.to_json() for row in self.rows],
            "summary": self._summary(),
            "scoring_sha256": self.scoring_sha256(),
        }

    def write(self, path: Path) -> None:
        """Write the canonical report, atomically (sorted keys, indented)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)


def eval_ledger_row(
    correlation_id: uuid.UUID,
    eval_id: str,
    task: EvalTask,
    adapter: HarnessAdapter,
    result: PairResult,
) -> LedgerEvent:
    """One pair's corpus row: a ``v=1`` ``PIPELINE_RUN_COMPLETED`` event.

    The matrix's correlation is the lineage and the pair is the
    discriminator (``eval:<task>:<harness>``), so one matrix appends
    exactly one row per pair, a redelivered append dedupes, and a
    re-run is a new correlation — never a second row answering to the
    first run's identity. The event type is the run-completion audit
    vocabulary rather than a new ``EventType`` member for the reasons
    the module docstring gives; the eval identity rides the payload.

    Payload discipline (ADR-0046) holds exactly: ``v`` first, and no
    free text anywhere — check *reasons* never enter the row, only the
    verdict's content hash, which covers them; the names are the task's
    and the adapter's registered ids; the statuses are the check
    vocabulary's own three strings. A row is small by construction and
    stays under the blob threshold, so the corpus stays queryable in
    one place.
    """
    payload: dict[str, Any] = {
        "v": 1,
        "run_id": eval_id,
        "status": result.verdict.overall.value,
        "metrics": {
            "kind": "eval",
            "task": task.task_id,
            "harness": adapter.harness_id,
            "mediation": adapter.mediation,
            "task_sha256": result.task_sha256,
            "verdict_sha256": result.verdict.verdict_sha256(),
            "checks": {c.name: c.status.value for c in result.verdict.checks},
        },
    }
    return LedgerEvent.create(
        correlation_id,
        EventType.PIPELINE_RUN_COMPLETED,
        attempt=0,
        discriminator=f"eval:{task.task_id}:{adapter.harness_id}",
        payload=payload,
    )


class EvalRunner:
    """Runs task x harness pairs and scores them; owns no adapter by name."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        home: Path,
        ledger: LedgerStore | None = None,
        policy: PolicySpec | None = None,
        child_env: Mapping[str, str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._home = Path(home)
        self._ledger = ledger
        self._policy = policy or EVAL_POLICY
        # Extra env every harness receives, merged over the adapter's own.
        # The test seam: a run under an interpreter whose ``paa`` resolves
        # elsewhere (an editable install pointing at another checkout)
        # forwards ``PYTHONPATH`` here so the spawned proxy grandchild
        # imports the same tree the scorer did. Production passes nothing.
        self._child_env = dict(child_env) if child_env else {}

    def eval_dir_for(self, eval_id: str) -> Path:
        return self._home / "eval" / eval_id

    # -- one pair -----------------------------------------------------------

    async def run_pair(
        self,
        task: EvalTask,
        adapter: HarnessAdapter,
        *,
        eval_id: str,
        correlation_id: uuid.UUID,
        timeout_s: float = 300.0,
    ) -> PairResult:
        """Run and score one pair in its own isolated workspace."""
        started = time.monotonic()
        workspace = self.eval_dir_for(eval_id) / "ws" / task.task_id / adapter.harness_id
        if workspace.exists() and any(workspace.iterdir()):
            raise PaaError(
                f"workspace {workspace} is not empty — one pair, one workspace; "
                "a re-run needs a new eval id"
            )
        workspace.mkdir(parents=True, exist_ok=True)
        task.layout(workspace)

        baseline = snapshot_manifest(workspace)
        baseline_sha = {f.path: f.sha256 for f in baseline.files}

        evidence: dict[str, str] = {}
        subject_state = "refused"
        exit_code: int | None = None
        refusal: str | None = None
        record_run_dir: Path | None = None
        attest_dir: Path | None = None
        attest_error: str | None = None

        try:
            # The command factory runs inside the refusal boundary: an
            # adapter whose script has gone missing is a *failed subject*
            # (a scored row, a recorded refusal), not a crash that costs
            # the matrix its other rows.
            command = adapter.command(task, workspace)
            env = dict(adapter.child_env(task, workspace))
            env.update(self._child_env)
            # The harness's children (a cooperating harness's proxy
            # grandchild, a test suite's runtime) should resolve the same
            # PAA_HOME the runner scores under — a harness that discovers
            # the boundary from its env and a scorer that reads another
            # home would be two different runs wearing one report.
            env.setdefault("PAA_HOME", str(self._home))

            if adapter.uses_recorder:
                outcome = await RecorderCore(self._sandbox, home=self._home).record(
                    command,
                    workspace=workspace,
                    timeout_s=timeout_s,
                    policy=self._policy,
                    child_env=env,
                    goal=task.intent,
                    quiet=True,
                )
                subject_state = outcome.state
                exit_code = outcome.result.exit_code
                record_run_dir = outcome.run_dir
                attest_dir = outcome.attest_bundle_dir
                attest_error = outcome.attest_error
                evidence["record_run_dir"] = str(outcome.run_dir)
                evidence["observe_bundle"] = str(outcome.observe_bundle_path)
                if outcome.attest_bundle_dir is not None:
                    evidence["attest_bundle"] = str(outcome.attest_bundle_dir)
            else:
                outcome_o = await ObserverCore(
                    self._sandbox, bundle_home=self._home
                ).observe(
                    command,
                    workspace=workspace,
                    timeout_s=int(timeout_s),
                    env=env,
                )
                subject_state = outcome_o.classification.state.value
                exit_code = outcome_o.result.exit_code
                evidence["observe_bundle"] = str(outcome_o.bundle_path)
        except PaaError as exc:
            # The adapter's run itself was refused (sandbox refused to
            # establish, a missing harness script, …). The pair still
            # scores honestly against whatever the workspace holds, and
            # the refusal is a measured fact on the row, not a crash
            # that costs the matrix its other rows.
            refusal = exc.message

        # The scoring snapshot: taken before the suite runs (the suite is
        # an instrument, not part of the subject's diff).
        after = snapshot_manifest(workspace)
        after_sha = {f.path: f.sha256 for f in after.files}
        diff = ManifestDiff.between(baseline, after)

        forbidden = score_forbidden(
            task.forbidden_created, task.forbidden_changed, after_sha, baseline_sha
        )
        intent = score_intent(task, diff, after_sha)

        tests = await PytestRunner(self._sandbox).run(
            workspace, test_paths=("tests",), timeout_seconds=timeout_s
        )
        tests_check = score_tests(tests)

        if adapter.uses_recorder:
            if record_run_dir is None:
                bundle = CheckVerdict(
                    CHECK_BUNDLE,
                    CheckStatus.BREACHED,
                    f"the recorded run never happened: {refusal or 'unknown refusal'}",
                )
            else:
                bundle = score_bundle(
                    verify_record(record_run_dir),
                    (
                        verify_bundle(attest_dir)
                        if attest_dir is not None
                        else None
                    ),
                    attest_error,
                )
        else:
            bundle = bundle_not_applicable(adapter.mediation)

        verdict = PairVerdict(
            task=task.task_id,
            harness=adapter.harness_id,
            mediation=adapter.mediation,
            checks=(tests_check, bundle, intent, forbidden),
        )
        result = PairResult(
            verdict=verdict,
            task_sha256=task.manifest_digest(),
            wall_time_ms=(time.monotonic() - started) * 1000,
            subject_state=subject_state,
            exit_code=exit_code,
            tests=tests,
            evidence=evidence,
            refusal=refusal,
        )

        if self._ledger is not None:
            await self._ledger.append(
                eval_ledger_row(correlation_id, eval_id, task, adapter, result)
            )
        return result

    # -- a matrix -------------------------------------------------------------

    async def run_matrix(
        self,
        tasks: Sequence[EvalTask],
        adapters: Sequence[HarnessAdapter],
        *,
        argv: Sequence[str],
        timeout_s: float = 300.0,
        eval_id: str | None = None,
    ) -> EvalReport:
        """Run every task x harness pair and write the report.

        Rows land in (task id, harness registration) order — the task
        set's order, not the caller's — so two invocations naming the
        same selections produce row-identical scoring views.
        """
        correlation = new_correlation_id()
        eval_id = eval_id or correlation.hex
        rows: list[PairResult] = []
        for task in tasks:
            for adapter in adapters:
                rows.append(
                    await self.run_pair(
                        task,
                        adapter,
                        eval_id=eval_id,
                        correlation_id=correlation,
                        timeout_s=timeout_s,
                    )
                )
        report = EvalReport(
            eval_id=eval_id,
            correlation_id=correlation,
            argv=tuple(argv),
            rows=rows,
            report_dir=self.eval_dir_for(eval_id),
        )
        report.write(self.eval_dir_for(eval_id) / "report.json")
        return report
