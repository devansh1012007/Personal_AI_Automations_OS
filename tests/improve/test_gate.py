"""The human gate — the one decision the loop refuses to make itself (C8.1 stage 4).

What these tests pin:

* **the decision vocabulary is closed** — a verdict outside
  approved/rejected cannot be constructed, so no reader of a decision
  artifact ever meets a verdict it cannot interpret;
* **an anonymous approval is an absent one** — a decision without an
  approver kind and id is refused, the same asserted-identity rule the
  attested bundle applies to its approvals;
* **the unwired gate refuses** — :func:`refuse_all` is the default, and
  a loop that cannot ask must not silently approve (a gate decided by
  *nobody* is the one thing worse than a gate decided by a model);
* **the terminal seam is a human's answer** — ``typer.confirm`` is the
  whole mechanism, a declined prompt and a closed stdin are both
  refusals, and the artifact names the human and the moment.
"""

from __future__ import annotations

import pytest

from paa.core.errors import PaaError
from paa.improve.gate import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    HumanDecision,
    refuse_all,
    terminal_approver,
)
from paa.improve.proposal import FileChange, PatchProposal
from paa.improve.validation import CommandOutcome, ValidationVerdict


def _decision(**overrides: str) -> HumanDecision:
    fields = {
        "decision": DECISION_APPROVED,
        "approver_kind": "human",
        "approver_id": "test-approver",
        "note": "looks right",
        "recorded_at": "2026-11-26T00:00:00+00:00",
    }
    fields.update(overrides)
    return HumanDecision(**fields)


class TestTheDecisionArtifact:
    def test_an_approval_is_approved(self) -> None:
        assert _decision().approved
        assert _decision().to_json()["decision"] == DECISION_APPROVED

    def test_the_vocabulary_is_closed(self) -> None:
        with pytest.raises(PaaError, match=r"approved.*or.*rejected"):
            _decision(decision="changes_requested")

    def test_an_anonymous_approval_is_refused(self) -> None:
        with pytest.raises(PaaError, match="non-empty approver kind"):
            _decision(approver_id="")
        with pytest.raises(PaaError, match="non-empty approver kind"):
            _decision(approver_kind="   ")

    def test_a_note_is_bounded_at_200_characters(self) -> None:
        decision = _decision(note="x" * 500)
        assert len(decision.note) == 200

    def test_a_missing_timestamp_is_stamped_not_invented(self) -> None:
        decision = _decision(recorded_at="")
        assert decision.recorded_at  # the moment it was made, never empty
        assert decision.recorded_at != "2026-11-26T00:00:00+00:00"

    def test_a_given_timestamp_is_preserved(self) -> None:
        decision = _decision(recorded_at="2026-11-26T12:00:00+00:00")
        assert decision.recorded_at == "2026-11-26T12:00:00+00:00"


class TestTheUnwiredGate:
    async def test_an_absent_approver_refuses_naming_the_absence(self) -> None:
        proposal = PatchProposal(intent="i", rationale="r")
        verdict = ValidationVerdict(commands=())
        decision = await refuse_all(proposal, verdict)
        assert decision.decision == DECISION_REJECTED
        assert not decision.approved
        assert "no approver was wired" in decision.note


class TestTheTerminalSeam:
    def _proposal(self) -> PatchProposal:
        return PatchProposal(
            intent="record the deploy lesson",
            rationale="friction 4.5",
            changes=(
                FileChange(path="playbooks.md", content="x\n", prior_sha256="00"),
            ),
            domain="deploy",
        )

    def _verdict(self) -> ValidationVerdict:
        return ValidationVerdict(
            commands=(
                CommandOutcome(
                    argv=("ruff", "check", "src"),
                    exit_code=0,
                    timed_out=False,
                    duration_ms=1.0,
                    stdout="",
                    stderr="",
                    stdout_sha256="00",
                    stderr_sha256="00",
                    stdout_bytes=0,
                    stderr_bytes=0,
                    truncated=False,
                ),
            )
        )

    async def test_a_yes_at_the_terminal_is_an_approval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import typer

        echoed: list[str] = []

        def fake_echo(message: object = None, **kwargs: object) -> None:
            echoed.append(str(message))

        monkeypatch.setattr(typer, "echo", fake_echo)
        monkeypatch.setattr(typer, "confirm", lambda message, default=False: True)
        decision = await terminal_approver(who="operator-at-the-desk")(
            self._proposal(), self._verdict()
        )
        assert decision.approved
        assert decision.approver_id == "operator-at-the-desk"
        assert decision.approver_kind == "human"
        # The prompt showed the evidence the decision was made on: the
        # intent, the file with its digest, and the gate's exit code.
        assert any("record the deploy lesson" in line for line in echoed)
        assert any("playbooks.md" in line and "sha256" in line for line in echoed)
        assert any("ruff check src" in line and "exit 0" in line for line in echoed)

    async def test_a_no_at_the_terminal_is_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import typer

        monkeypatch.setattr(typer, "confirm", lambda message, default=False: False)
        decision = await terminal_approver(who="operator")(
            self._proposal(), self._verdict()
        )
        assert decision.decision == DECISION_REJECTED

    async def test_a_closed_stdin_is_a_refusal_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``typer.Abort`` is what a closed stdin actually raises."""
        import typer

        def aborting_confirm(message: str, default: bool = False) -> bool:
            raise typer.Abort()

        monkeypatch.setattr(typer, "confirm", aborting_confirm)
        decision = await terminal_approver(who="operator")(
            self._proposal(), self._verdict()
        )
        assert decision.decision == DECISION_REJECTED
        assert "declined" in decision.note
