"""The human gate — the one decision the loop refuses to make itself.

C8.1's fourth stage. After deterministic validation and before anything
touches the real repository, a human rules on the proposal. Two rules
make this gate real rather than decorative:

* **No gate is ever model-decided** (SPEC 2 §9 rule 2). The decision
  arrives through :data:`Approver` — a callable the *host* injects (a
  terminal prompt in the CLI, a scripted answer in tests). The pipeline
  holds no model at all: its declaration asks for no ``think``, and the
  facade's ``think`` door is closed by the grant set, so there is nothing
  in the loop that could answer the question even by accident.
* **Refusal is first-class.** A rejected proposal is recorded — the
  decision artifact names the approver, the time and the note — and
  nothing is applied. An absent approver is not an approval either: a
  loop with no decision wired in refuses, mirroring the coder loop's
  "with no confirm callback the loop auto-denies".

The decision vocabulary is the attested bundle's own
(:data:`paa.attest.format.APPROVAL_DECISIONS`), so the approval that
rides the C7.2 bundle is the same artifact the gate produced — one
vocabulary, not two that can drift.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from paa.core.errors import PaaError

if TYPE_CHECKING:
    from paa.improve.proposal import PatchProposal
    from paa.improve.validation import ValidationVerdict

__all__ = [
    "DECISION_APPROVED",
    "DECISION_REJECTED",
    "Approver",
    "HumanDecision",
    "refuse_all",
    "terminal_approver",
]

#: The two decisions a human can make (the bundle's closed vocabulary uses
#: a third, ``changes_requested``, which for this loop is a refusal with
#: a note — the loop has no revision pass yet, so it records the note and
#: stops).
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

#: The decision seam: something that can rule on a proposal. The host
#: owns what this is — a human at a terminal, a scripted answer in a
#: test — and the loop only ever *calls* it, never implements it.
Approver = Callable[["PatchProposal", "ValidationVerdict"], Awaitable["HumanDecision"]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class HumanDecision:
    """One ruling on one proposal: who, when, what, why.

    ``recorded_at`` is the wall-clock moment the decision was made — not
    the run's journalled time — because a human's answer happens in real
    time and the artifact should say so. The identity is *asserted*, the
    same honesty rule the attested bundle applies to its approvals: the
    record says who the producer says decided, and a reviewer who needs
    more than that asks out of band.
    """

    decision: str
    approver_kind: str
    approver_id: str
    note: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.decision not in (DECISION_APPROVED, DECISION_REJECTED):
            raise PaaError(
                f"a human-gate decision must be {DECISION_APPROVED!r} or "
                f"{DECISION_REJECTED!r}, not {self.decision!r}; the "
                "vocabulary is closed so no reader ever meets a verdict "
                "it cannot interpret"
            )
        if not self.approver_kind.strip() or not self.approver_id.strip():
            raise PaaError(
                "a human-gate decision needs a non-empty approver kind and "
                "id; an anonymous approval is an absent one"
            )
        # ``note`` is bounded the way every journal reason is: a longer
        # note carries no information, only risk. ``recorded_at`` is
        # stamped here so a decision constructed without one still carries
        # the moment it was made, never an invented earlier one.
        object.__setattr__(self, "note", self.note[:200])
        object.__setattr__(self, "recorded_at", self.recorded_at or _now_iso())

    @property
    def approved(self) -> bool:
        return self.decision == DECISION_APPROVED

    def to_json(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "approver_kind": self.approver_kind,
            "approver_id": self.approver_id,
            "note": self.note,
            "recorded_at": self.recorded_at,
        }


async def refuse_all(
    proposal: PatchProposal, verdict: ValidationVerdict
) -> HumanDecision:
    """The absent-approver answer: refuse, naming the absence.

    The default when a host wires no decision seam. A loop that cannot
    ask must not silently approve — that would be a gate decided by
    *nobody*, which is the one thing worse than a gate decided by a
    model.
    """
    return HumanDecision(
        decision=DECISION_REJECTED,
        approver_kind="loop",
        approver_id="unwired-human-gate",
        note="no approver was wired; the guarded loop refuses by default",
    )


def terminal_approver(*, who: str) -> Approver:
    """The CLI's decision seam: a human at a terminal, reading the evidence.

    Returns the approver callable (the prompt happens when the pipeline
    calls it, mid-run, exactly when the gate is reached). The prompt shows
    the intent, the rationale, every file with its content digest, and
    each validation command with its exit code — the evidence the
    decision is being made on. A ``KeyboardInterrupt`` or EOF is a
    refusal, not an error: a human who walked away did not approve.
    """
    import typer

    async def approve(proposal: PatchProposal, verdict: ValidationVerdict) -> HumanDecision:
        typer.echo(f"proposal: {proposal.intent}")
        typer.echo(f"rationale: {proposal.rationale}")
        for change in proposal.changes:
            typer.echo(
                f"file: {change.path} "
                f"({len(change.content.encode('utf-8'))} bytes, "
                f"sha256 {change.content_sha256[:12]}…)"
            )
        for command in verdict.commands:
            state = "timed out" if command.timed_out else f"exit {command.exit_code}"
            typer.echo(f"gate: {' '.join(command.argv)} → {state}")
        try:
            answer = typer.confirm("apply this improvement?", default=False)
        except (KeyboardInterrupt, EOFError):
            answer = False
        return HumanDecision(
            decision=DECISION_APPROVED if answer else DECISION_REJECTED,
            approver_kind="human",
            approver_id=who,
            note="terminal prompt" if answer else "terminal prompt declined",
        )

    return approve
