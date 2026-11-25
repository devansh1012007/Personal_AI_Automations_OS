"""Host-side call-journal replay for in-process pipelines (C6.3 / P2.2-P2.3).

ADR-0050's R3, the production half: a crashed run's journal is folded
into a :class:`ReplayView`, and the re-run pipeline's calls are answered
from the record — completed calls are **served**, never re-executed —
while a call whose fingerprint differs from the record **diverges** the
run (the host never resyncs) and a mutating call whose outcome cannot be
classified is reported as ``-32008 OutcomeUnknown`` (D-6: the pipeline
decides, the host never auto-repeats).

The decision table (:meth:`ReplayView.check`), one proposed call at a
time::

    seq not in the record            -> EXECUTE      (a new call)
    fingerprint differs              -> DIVERGED     (PIPELINE_RUN_DIVERGED, stop)
    outcome COMPLETED                -> SERVE        (the recorded result, no execution)
    outcome FAILED, reason interrupted
      and the method is pure         -> EXECUTE      (a model call has no effect
                                                       outside the journal; re-invoking
                                                       it is safe and costs only tokens)
    outcome FAILED, reason interrupted
      and the method is mutating     -> OUTCOME_UNKNOWN  (-32008; never auto-repeat)
    outcome FAILED, a real error     -> FAIL         (the recorded failure stands)
    outcome PROPOSED (no outcome row)
      and the method is pure         -> EXECUTE      (the sweep has not closed it
                                                       yet; same reasoning)
      and the method is mutating     -> OUTCOME_UNKNOWN  (D-6)

``interrupted`` is the sweep's stable close marker
(:data:`INTERRUPTED_REASON`, ``ledger/recovery.py``'s golden v1 shape),
not a heuristic: the sweep writes it, this module reads it, and a real
provider failure carries a different recorded reason. Purity is a
property of the *capability*, declared once
(:data:`PURE_METHODS): ``think`` writes nothing outside its journal
rows, so its unknown outcome is safe to resolve by re-execution — the
same reasoning the coder journal applies to read-only tools. A
``use_skill`` call never reaches this table: it journals through the
existing ``TOOL_CALL_*`` vocabulary (ADR-0051) and is classified by the
coder recovery engine's own decision table, unchanged.

What this module deliberately does **not** do: re-append served calls.
The ADR's cost model — "replaying N completed calls is N cheap
round-trips against the host's in-memory view" — is implemented by
answering from the folded record and touching the ledger only for new
calls, so the replay's zero-re-execution property is visible in the
journal itself: the record's row count does not grow while calls are
served.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from paa.pipelines.base import PipelineError
from paa.pipelines.journal import (
    CallOutcome,
    CallRecord,
    fingerprint,
    read_call_journal,
)

__all__ = [
    "INTERRUPTED_REASON",
    "PURE_METHODS",
    "ReplayAction",
    "ReplayDecision",
    "ReplayView",
    "ReplayedCallFailedError",
    "ServedToolResults",
    "ToolSequenceDivergedError",
    "load_replay_view",
    "serve_or_execute",
]

#: The sweep's close marker for a call the crash interrupted
#: (``ledger/recovery.py::close_interrupted_pipeline_calls`` writes this
#: exact reason; the replay table reads it back).
INTERRUPTED_REASON = "interrupted"

#: Capabilities whose execution has no effect outside the journal: an
#: unknown outcome is safe to resolve by re-execution. ``think`` is a
#: model call — it reads the window and returns a proposal; it writes
#: nothing. Everything else the facade offers can mutate (a skill, a
#: commit, a sub-run), so its unknown outcome is the pipeline's to
#: decide, never the host's to repeat.
PURE_METHODS = frozenset({"think"})


class ReplayAction(enum.Enum):
    """What the host does with a call a replaying pipeline proposed."""

    SERVE = "SERVE"
    EXECUTE = "EXECUTE"
    DIVERGED = "DIVERGED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ReplayDecision:
    """One row of the replay decision table.

    ``result`` carries the recorded value on SERVE; ``reason`` carries
    the recorded failure text on FAIL and the offending fingerprints on
    DIVERGED (for the audit row the caller appends).
    """

    action: ReplayAction
    seq: int
    result: Any = None
    reason: str | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


class ReplayView:
    """The host's folded view of one run's recorded calls.

    Built once, before the re-run starts, from the ledger — the
    in-memory map the ADR's cost model names. Immutable afterwards: a
    call the re-run executes appends to the *journal*, and the next
    replay (after the next crash) folds a fresh view. Serving therefore
    never mutates this object, which is what keeps the zero-re-execution
    property checkable from the outside (the ledger's row count for the
    served seqs does not move).
    """

    def __init__(self, records: Mapping[int, CallRecord]) -> None:
        self._records = dict(records)

    @classmethod
    async def load(
        cls,
        ledger: Any,
        run_id: uuid.UUID,
        *,
        blob_store: Any = None,
    ) -> ReplayView:
        """Fold a run's call journal into a view (one read, one map)."""
        records = await read_call_journal(ledger, run_id, blob_store=blob_store)
        return cls({record.seq: record for record in records})

    def __len__(self) -> int:
        return len(self._records)

    def record_at(self, seq: int) -> CallRecord | None:
        return self._records.get(seq)

    def check(self, seq: int, method: str, params: Mapping[str, Any]) -> ReplayDecision:
        """The decision table row for one proposed call."""
        record = self._records.get(seq)
        if record is None:
            return ReplayDecision(action=ReplayAction.EXECUTE, seq=seq)

        actual = fingerprint(method, params)
        if actual != record.fingerprint:
            return ReplayDecision(
                action=ReplayAction.DIVERGED,
                seq=seq,
                reason="fingerprint mismatch",
                expected_fingerprint=record.fingerprint,
                actual_fingerprint=actual,
            )

        if record.outcome is CallOutcome.COMPLETED:
            if record.result is None and record.result_sha256 is None:
                # A blobbed result whose store was unavailable folds to a
                # COMPLETED row with no value and no attestation. Serving
                # that as a real result would forge a null outcome; D-6
                # says the pipeline must decide instead.
                return ReplayDecision(
                    action=ReplayAction.OUTCOME_UNKNOWN,
                    seq=seq,
                    reason="recorded result unavailable",
                )
            return ReplayDecision(
                action=ReplayAction.SERVE, seq=seq, result=record.result
            )

        if record.outcome is CallOutcome.PROPOSED or (
            record.outcome is CallOutcome.FAILED and record.error == INTERRUPTED_REASON
        ):
            if method in PURE_METHODS:
                return ReplayDecision(action=ReplayAction.EXECUTE, seq=seq)
            return ReplayDecision(
                action=ReplayAction.OUTCOME_UNKNOWN,
                seq=seq,
                reason=record.error or INTERRUPTED_REASON,
            )

        # A real recorded failure: the outcome is known and it is "no".
        return ReplayDecision(
            action=ReplayAction.FAIL, seq=seq, reason=record.error or "failed"
        )


async def load_replay_view(
    ledger: Any, run_id: uuid.UUID, *, blob_store: Any = None
) -> ReplayView:
    """Convenience awaitable (:meth:`ReplayView.load`)."""
    return await ReplayView.load(ledger, run_id, blob_store=blob_store)


def serve_or_execute(
    view: ReplayView, seq: int, method: str, params: Mapping[str, Any]
) -> ReplayDecision:
    """Synchronous table lookup for callers that already hold the view."""
    return view.check(seq, method, params)


class ReplayedCallFailedError(PipelineError):
    """A replayed call's recorded outcome is a real failure; it stands.

    The re-run hits the same wall the original run hit — same call, same
    fingerprint, same recorded reason — and fails there, deterministically,
    rather than re-attempting an effect whose record already says "no".
    """


class ToolSequenceDivergedError(PipelineError):
    """The re-run's tool calls do not follow the recorded transcript's order.

    The served-tool queue is position-indexed against the crashed run's
    transcript: the determinism contract says a replaying pipeline re-issues
    the same calls in the same order. A tool name that does not match the
    next recorded result is the tool-path equivalent of a fingerprint
    mismatch — the record can no longer speak for this run — and the host
    stops rather than guess which effect to serve.
    """

    wire_code = -32004


class ServedToolResults:
    """The recorded tool results of a crashed run, in transcript order.

    Built from the crashed run's durable session transcript: every
    ``TOOL_RESULT`` turn, in order, paired with the tool name the result
    answered. On replay the facade pops the next entry for each
    redelivered ``use_skill`` and serves the recorded text without
    executing the tool — the tool journal attests hashes, never values
    (ADR-0046), so the transcript is the only place the answer text
    lives, and the deterministic re-issue order is what makes the
    position-indexed queue sound.

    An empty queue means "nothing left to serve": every call after it
    executes through the ordinary gated path.
    """

    def __init__(self, entries: list[tuple[str, str]]) -> None:
        self._entries = list(entries)
        # The served log — what this queue answered, for the caller's
        # zero-re-execution assertions.
        self.served: list[tuple[str, str]] = []

    @classmethod
    def from_transcript(cls, path: Any) -> ServedToolResults:
        """Read a session transcript (JSONL) and collect its tool results."""
        import json as _json
        from pathlib import Path as _Path

        text = _Path(path).read_text(encoding="utf-8")
        entries: list[tuple[str, str]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            row = _json.loads(line)
            if row.get("role") == "tool_result":
                entries.append((str(row.get("tool_name") or ""), str(row.get("content") or "")))
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def pop_next(self, tool: str) -> str | None:
        """Serve the next recorded result if it answers ``tool``.

        Returns the recorded text and consumes the entry, or ``None`` when
        the queue is empty. Raises :class:`ToolSequenceDivergedError` when
        the next recorded entry answers a different tool — a re-run that
        swaps call order is diverging, and serving the wrong result would
        forge history.
        """
        if not self._entries:
            return None
        name, result = self._entries[0]
        if name != tool:
            raise ToolSequenceDivergedError(
                "the replaying run's next tool call does not match the "
                "recorded transcript's next result",
                expected_tool=name,
                actual_tool=tool,
            )
        self._entries.pop(0)
        self.served.append((tool, result))
        return result
