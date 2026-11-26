"""The installed-pipeline adapter — sandboxed execution behind the call journal.

SPEC 2 Wave 8, row C8.6 (the pipeline spec's P5-lite), and this module is
where the loading contract is **pinned**:

An installed pipeline's code is never imported into this process. The
five-gate installer, the capability review and the conformance run clear
*bytes*, and those bytes execute as a **sandboxed subprocess** — the same
containment the smoke gate used to clear them, the same entrypoint, the
same ``--paa-args`` JSON-in / JSON-on-stdout contract a Claw-Hub skill
speaks. The registry row the install leaves behind is runnable *through
this adapter*, never through an import: the tree's own rule that an AST
scan is a screen, not a boundary (pipeline spec §10) is why, and the
honest gap it leaves — an installed pipeline cannot call ``ctx.think``
or any other facade capability, because a subprocess holds no tokens —
is the P3 RPC transport's work, exactly where the spec's P5 ("this
phase needs executable package loading") always placed it.

What the host *does* give an installed run is the machinery every
built-in run gets: a :class:`~paa.pipelines.journal.CallJournal` lineage
(``PIPELINE_RUN_*`` / ``PIPELINE_CALL_*``, so ``paa pipeline trace``
reads it), the fingerprint/replay discipline (a re-drive serves the
recorded result without re-running the child; a changed call diverges,
never resyncs; an interrupted call is the unknown outcome the host
refuses to auto-repeat — ADR-0050's D-6, real here because executing
the entrypoint is a mutating journaled call), and the sandbox's own
budgets (wall clock, memory) as the run's limits.

The entrypoint contract, v1 (pinned):

* ``python <staged>/pipeline.py --paa-args <args.json>``;
* the args file holds ``{"goal": str, "params": {...}}`` (the smoke
  gate's probe holds ``{}`` — an entrypoint must survive both);
* stdout must end with one JSON object — the whole payload, then the
  last non-empty line, the skill contract's own tolerance — shaped
  ``{"status": "completed"|"failed"|"needs_human", "summary": str,
  "metrics": {...}}``; anything else is a failed run by rule, never a
  parse error the child chooses.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

from paa.core.errors import PaaError
from paa.pipelines.base import PipelineResult, PipelineStatus
from paa.pipelines.journal import CallJournal
from paa.pipelines.replay import ReplayAction, ReplayedCallFailedError, ReplayView
from paa.pipelines.slots import PipelineDeclaration
from paa.sandbox.base import Sandbox, SandboxSpec

__all__ = [
    "ENTRYPOINT_METHOD",
    "PIPELINE_ENTRYPOINT",
    "SandboxedPipelineRunner",
    "run_result_shape",
]

log = structlog.get_logger(__name__)

#: The one file a pipeline package's entrypoint must provide, at the
#: package root. A fixed name is the contract's cheapest possible form:
#: there is no field to validate, no indirection to get wrong, and the
#: smoke gate, the conformance runner and this adapter all resolve the
#: same file by the same rule.
PIPELINE_ENTRYPOINT = "pipeline.py"

#: The call-journal method name for one entrypoint execution. The method
#: is part of the fingerprint, so a run's identity is
#: ``sha256({method, params})`` over exactly what the host passed the
#: child — goal and typed params, nothing else.
ENTRYPOINT_METHOD = "sandbox.run_entrypoint"

#: The wall-clock budget one installed run grants the child, when the
#: caller does not name one. The declaration's own knobs ride inside the
#: args file; this is the *host's* containment clock, the same role the
#: smoke gate's 20 s plays at install time, sized for a pipeline rather
#: than a probe.
_RUN_TIMEOUT_SECONDS = 120.0

#: The child's memory ceiling, matching the smoke gate's own default so
#: "what install tested" and "what a run grants" stay one number.
_RUN_MEMORY_MB = 256

#: The closed status vocabulary a child's result may carry — the
#: ``PipelineStatus`` values the host itself uses, so a run's terminal
#: state is never a string the child invented.
_RESULT_STATUSES = frozenset({status.value for status in PipelineStatus})

#: The child's summary as the host carries it, capped at the journal's
#: reason budget — a longer summary carries risk, not information, and
#: an unbounded one would ride every event payload that names the run's
#: outcome. The child's metrics are context, not a document: nested
#: containers and oversized values are dropped rather than blobbed,
#: because the run's own artifacts are the place for detail and the
#: metrics row is the place for numbers (see :func:`_metrics_of`).
_SUMMARY_MAX_CHARS = 200


class SandboxedPipelineError(PaaError):
    """The installed pipeline's execution could not be driven as contracted."""

    wire_code = -32014


def run_result_shape(stdout: str) -> dict[str, Any] | None:
    """The JSON result object a run must leave on stdout, or ``None``.

    The skill contract's tolerance, stated once for this surface too:
    the whole payload first, then the last non-empty line — a
    well-behaved entrypoint may print diagnostics before its result
    line. Anything else is the caller's failed-by-rule, never a parse
    error the child chooses.
    """
    text = stdout.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1].strip()):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class SandboxedPipelineRunner:
    """Drives one installed pipeline's entrypoint under the sandbox.

    One instance per run lineage, constructed with the journal the
    driver minted — the same construction shape the coder and observe
    facades take. The runner owns the *call* rows (and the divergence
    row, when a replay finds one); the driver owns the run's lifecycle
    rows, so both doors (``paa pipeline run <installed>`` and any host
    that mints a journal) leave identical shapes.
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        journal: CallJournal,
        staged: Path,
        declaration: PipelineDeclaration,
        timeout_s: float | None = _RUN_TIMEOUT_SECONDS,
    ) -> None:
        entrypoint = Path(staged) / PIPELINE_ENTRYPOINT
        if not entrypoint.is_file():
            raise SandboxedPipelineError(
                "the installed pipeline's entrypoint is missing",
                staged=str(Path(staged)),
                entrypoint=PIPELINE_ENTRYPOINT,
            )
        self._sandbox = sandbox
        self._journal = journal
        self._staged = Path(staged).resolve()
        self._declaration = declaration
        self._timeout_s = timeout_s

    @property
    def journal(self) -> CallJournal:
        return self._journal

    async def run(
        self,
        goal: str,
        params: Mapping[str, Any] | None = None,
        *,
        replay: ReplayView | None = None,
    ) -> PipelineResult:
        """One run: replay check, PROPOSED, sandboxed execution, outcome.

        The ledger-before-effect order is the house invariant: the
        PROPOSED row lands before the child is ever launched, so a crash
        between them leaves exactly the unknown outcome the D-6 rule
        refuses to auto-repeat.
        """
        call_params: dict[str, Any] = {"goal": goal, "params": dict(params or {})}
        if replay is not None:
            decision = replay.check(1, ENTRYPOINT_METHOD, call_params)
            if decision.action is ReplayAction.SERVE:
                return _result_from_recorded(decision.result)
            if decision.action is ReplayAction.DIVERGED:
                await self._journal.run_diverged(
                    1,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
                from paa.pipelines.base import PipelineDivergedError

                raise PipelineDivergedError(
                    1,
                    decision.expected_fingerprint or "",
                    decision.actual_fingerprint or "",
                )
            if decision.action is ReplayAction.OUTCOME_UNKNOWN:
                from paa.pipelines.base import OutcomeUnknownError

                raise OutcomeUnknownError(
                    "the recorded entrypoint call's outcome cannot be "
                    "classified; executing it again could repeat an effect "
                    "whose result nobody saw — the operator re-drives the "
                    "run, never the host",
                    seq=1,
                )
            if decision.action is ReplayAction.FAIL:
                raise ReplayedCallFailedError(
                    "the recorded outcome of this entrypoint call is a real "
                    f"failure and it stands: {decision.reason}",
                )

        await self._journal.call_proposed(1, ENTRYPOINT_METHOD, call_params)
        try:
            outcome = await self._execute(call_params)
        except asyncio.CancelledError:
            # The cancel is the one outcome whose row must land even while
            # the exception unwinds: a cancelled run closes its call, or
            # the sweep would read a dangling PROPOSED. The sandbox has
            # already killed the tree by the time the cancellation reaches
            # here (its own contract), so the row tells the truth.
            await asyncio.shield(
                self._journal.call_failed(1, ENTRYPOINT_METHOD, "cancelled")
            )
            raise
        except BaseException as exc:
            # The failed row is shielded, and its reason names the type
            # and never the text: a sandbox start failure can carry the
            # child's argv, and argv may hold a secret meant for the
            # child alone (the observe driver's rule, restated).
            await asyncio.shield(
                self._journal.call_failed(
                    1,
                    ENTRYPOINT_METHOD,
                    f"entrypoint did not resolve ({type(exc).__name__})",
                )
            )
            raise

        # The call resolved — the outcome (including a failed child) is a
        # classified fact, journalled as the recorded result replay serves
        # back. A run whose child failed is a failed run, not a crashed
        # one: the difference is exactly what the D-6 classification reads.
        await self._journal.call_completed(1, ENTRYPOINT_METHOD, outcome)
        return _result_from_recorded(outcome)

    async def _execute(self, call_params: Mapping[str, Any]) -> dict[str, Any]:
        """Run the entrypoint under the sandbox and classify the answer."""
        with tempfile.TemporaryDirectory(prefix="paa-installed-") as scratch:
            args_file = Path(scratch) / "arguments.json"
            args_file.write_text(json.dumps(dict(call_params)), encoding="utf-8")
            spec = SandboxSpec(
                command=(
                    sys.executable,
                    str(self._staged / PIPELINE_ENTRYPOINT),
                    "--paa-args",
                    str(args_file),
                ),
                workspace_path=Path(scratch),
                read_only_mounts=(self._staged,),
                timeout_seconds=self._timeout_s,
                memory_mb=_RUN_MEMORY_MB,
            )
            result = await self._sandbox.run(spec)

        if not result.ok:
            reason = result.killed_reason or (
                "timeout" if result.timed_out else f"exit {result.exit_code}"
            )
            return {
                "status": PipelineStatus.FAILED.value,
                "summary": f"entrypoint run failed ({reason})",
                "metrics": {"state": reason},
            }
        parsed = run_result_shape(result.stdout)
        if parsed is None:
            return {
                "status": PipelineStatus.FAILED.value,
                "summary": "entrypoint produced no JSON object on stdout",
                "metrics": {"state": "no-json"},
            }
        status = parsed.get("status")
        if status not in _RESULT_STATUSES:
            return {
                "status": PipelineStatus.FAILED.value,
                "summary": (
                    "entrypoint result carries an unknown status; the "
                    "vocabulary is closed"
                ),
                "metrics": {"state": "bad-status"},
            }
        return {
            "status": status,
            "summary": _bounded_summary(parsed.get("summary")),
            "metrics": _metrics_of(parsed.get("metrics")),
        }


def _result_from_recorded(recorded: Any) -> PipelineResult:
    """A ``PipelineResult`` from a recorded (or fresh) result object.

    Total over a malformed record: the fold's own defensive rule, so a
    replay serving a damaged row degrades to a named failure rather than
    raising inside the driver that asked for it.
    """
    if not isinstance(recorded, Mapping):
        return PipelineResult(
            status=PipelineStatus.FAILED,
            summary="recorded result is not an object",
            metrics={},
        )
    status = recorded.get("status")
    try:
        resolved = PipelineStatus(str(status))
    except ValueError:
        resolved = PipelineStatus.FAILED
    return PipelineResult(
        status=resolved,
        summary=_bounded_summary(recorded.get("summary")),
        metrics=_metrics_of(recorded.get("metrics")),
    )


def _bounded_summary(raw: Any) -> str:
    """The summary as the host carries it: a string, bounded.

    The child's own words, capped at the journal's reason budget — a
    longer summary carries risk, not information, and an unbounded one
    would ride every event payload that names the run's outcome.
    """
    text = raw if isinstance(raw, str) else ""
    return text[:_SUMMARY_MAX_CHARS]


def _metrics_of(raw: Any) -> dict[str, Any]:
    """The metrics the host carries: a mapping of scalars, bounded.

    The child's metrics are context, not a document; nested containers
    and oversized values are dropped rather than blobbed, because the
    run's own artifacts are the place for detail and the metrics row is
    the place for numbers.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or len(out) >= 32:
            continue
        if isinstance(value, str | int | float | bool) or value is None:
            if isinstance(value, str) and len(value) > 200:
                continue
            out[key] = value
    return out
