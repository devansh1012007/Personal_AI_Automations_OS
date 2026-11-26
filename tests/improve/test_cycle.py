"""The guarded cycle end to end — C8.1's done-when, as tests.

A full cycle on a **fixture git repository** with a **real ledger** whose
events are real reflection input: a ``deploy`` task with one correction
and one rollback (friction 4.5), the same seed the daemon's own tests
use. The stages are driven through the shared ``run_cycle`` door (the
one the conformance runner and the CLI's driver use), so a cycle driven
from a test and a cycle driven from the front door leave the same ledger
shape.

The scenarios, in the row's own order:

* **the happy path** — reflection finding → deterministic proposal →
  validation green → human approves → the change lands **as an attested
  change bundle** (C7.2) behind the host-git integrity gate, and the
  bundle verifies offline through the existing verify path;
* **validation failure** — a red gate command stops the cycle before the
  human is even asked: no commit, no bundle, an intact journal;
* **human refusal** — the proposal is recorded, the decision is
  recorded, and nothing is applied;
* **clean history** — a week with no friction is the loop working, not
  the loop failing;
* **the approval guard** — the commit stage without a carried approval
  (or with a carried *rejection*) refuses before touching the tree;
* **replay** — a re-drive of a completed lineage serves every call from
  the record and lands nothing twice.

Real git and real gate-command subprocesses throughout: ``slow``, the
``tests/coder`` convention, and the machinery under test is never its own
fixture.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from paa.core.errors import PaaError
from paa.core.types import EventType
from paa.improve.gate import DECISION_REJECTED
from paa.improve.pipeline import ImproveFacade, ImproveReflectionLoop
from paa.ledger.store import LedgerStore
from paa.pipelines.base import PipelineStatus
from paa.pipelines.journal import CallJournal, CallOutcome, read_call_journal
from paa.pipelines.replay import ReplayView

from .conftest import (
    approve,
    head_of,
    make_repo,
    refuse,
    run_cycle,
    seed_clean_history,
    subject_of,
)

pytestmark = pytest.mark.slow


def _green() -> list[list[str]]:
    return [[sys.executable, "-c", "raise SystemExit(0)"]]


def _red() -> list[list[str]]:
    return [[sys.executable, "-c", "raise SystemExit(2)"]]


def _section(bundle_dir: Path, name: str) -> dict[str, Any]:
    """One signed section of an attested bundle, read from its blob."""
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    digest = next(str(s["sha256"]) for s in manifest["sections"] if s["name"] == name)
    blob = bundle_dir / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"
    payload: Any = json.loads(blob.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


async def _fold(ledger: LedgerStore, run_id: uuid.UUID) -> list[tuple[str, str]]:
    records = await read_call_journal(ledger, run_id)
    return [(record.method, record.outcome.value) for record in records]


class TestTheHappyPath:
    async def test_a_full_guarded_cycle_lands_an_verifiable_attested_commit(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        """The done-when: finding → proposal → validation → approval → commit."""
        from paa.attest.verify import verify_bundle

        seed_head = head_of(git_repo)
        run_id, result = await run_cycle(
            improve_ledger, git_repo, tmp_path, validators=_green(), approver=approve
        )

        # The cycle completed and names its evidence.
        assert result.status is PipelineStatus.COMPLETED
        assert result.metrics["stopped_reason"] == "committed"
        bundle_dir = Path(result.metrics["bundle_dir"])
        bundle_id = result.metrics["bundle_id"]

        # 1. The reflection finding drove a real proposal artifact.
        playbook = (git_repo / "playbooks.md").read_text(encoding="utf-8")
        assert "### Anti-pattern: deploy" in playbook
        assert "<!-- managed:improve-reflection-loop begin -->" in playbook
        assert playbook.startswith("# ops playbooks")  # human text preserved
        artifacts = tmp_path / "improve-artifacts" / run_id.hex
        assert (artifacts / "proposal.json").is_file()
        assert (artifacts / "evidence" / "validation-01.json").is_file()

        # 2. The commit is real, and its subject names the bundle.
        assert result.metrics["git_commit"] is not None
        assert result.metrics["git_commit"] != seed_head
        assert head_of(git_repo) == result.metrics["git_commit"]
        last_subject = subject_of(git_repo)
        assert last_subject.startswith("improve: ")
        assert bundle_id in last_subject

        # 3. The improvement inherited the evidence chain: the bundle
        #    verifies offline through the existing C7.2 verify path.
        verification = verify_bundle(bundle_dir)
        assert verification.ok, [c.detail for c in verification.checks]
        assert verification.approvals == 1
        assert verification.evidence_count >= 1

        # 4. The approval row is the human decision the gate recorded,
        #    and the observed command rows are the validation gates.
        approvals = _section(bundle_dir, "approvals")
        assert approvals["v"] == 1
        (approval_row,) = approvals["entries"]
        # The bundle's asserted-identity shape: the record says who the
        # producer says decided (C7.2's honesty rule), verbatim from the
        # gate's own decision artifact.
        assert approval_row["approver_asserted"]["id"] == "test-approver"
        assert approval_row["approver_asserted"]["kind"] == "human"
        assert approval_row["decision"] == "approved"
        commands = _section(bundle_dir, "commands")
        observed = [row for row in commands["entries"] if row["source"] == "observed"]
        assert len(observed) == 1
        assert observed[0]["exit_code"] == 0
        assert observed[0]["command_green"] is True

        # 5. The journal folded to the five stages, all completed.
        assert await _fold(improve_ledger, run_id) == [
            ("improve.reflect", "COMPLETED"),
            ("improve.propose", "COMPLETED"),
            ("improve.validate", "COMPLETED"),
            ("improve.gate", "COMPLETED"),
            ("improve.commit", "COMPLETED"),
        ]
        events = await improve_ledger.read_correlation(run_id)
        started = [e for e in events if e.event_type is EventType.PIPELINE_RUN_STARTED]
        assert started and started[0].payload["pipeline"] == "improve.reflection-loop"
        completed = [
            e for e in events if e.event_type is EventType.PIPELINE_RUN_COMPLETED
        ]
        assert completed and completed[0].payload["status"] == "completed"


class TestTheRefusalPaths:
    async def test_a_red_gate_stops_the_cycle_before_the_human_is_asked(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        seed_head = head_of(git_repo)
        run_id, result = await run_cycle(
            improve_ledger, git_repo, tmp_path, validators=_red(), approver=approve
        )

        assert result.status is PipelineStatus.FAILED
        assert result.metrics["stopped_reason"] == "validation_failed"
        failed = result.metrics["failed_commands"]
        assert failed == [{"command_index": 1, "exit_code": 2, "timed_out": False}]

        # Nothing was applied, nothing was committed, no bundle exists.
        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )
        assert head_of(git_repo) == seed_head
        artifacts = tmp_path / "improve-artifacts" / run_id.hex
        assert (artifacts / "proposal.json").is_file()  # the proposal IS recorded
        assert not (artifacts / "attest-bundle").exists()

        # The journal is intact and coherent: three completed calls, the
        # human was never asked, no broken trace.
        assert await _fold(improve_ledger, run_id) == [
            ("improve.reflect", "COMPLETED"),
            ("improve.propose", "COMPLETED"),
            ("improve.validate", "COMPLETED"),
        ]
        events = await improve_ledger.read_correlation(run_id)
        assert not [e for e in events if e.event_type is EventType.PIPELINE_CALL_FAILED]
        completed = [
            e for e in events if e.event_type is EventType.PIPELINE_RUN_COMPLETED
        ]
        assert completed and completed[0].payload["status"] == "failed"

    async def test_a_refusing_human_records_the_proposal_and_applies_nothing(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        seed_head = head_of(git_repo)
        run_id, result = await run_cycle(
            improve_ledger, git_repo, tmp_path, validators=_green(), approver=refuse
        )

        assert result.status is PipelineStatus.FAILED
        assert result.metrics["stopped_reason"] == "human_gate_refused"
        assert result.metrics["decision"] == DECISION_REJECTED
        assert result.metrics["approver"] == "test-approver"

        # The proposal and the decision are recorded; nothing is applied.
        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )
        assert head_of(git_repo) == seed_head
        artifacts = tmp_path / "improve-artifacts" / run_id.hex
        assert (artifacts / "proposal.json").is_file()
        assert not (artifacts / "attest-bundle").exists()

        records = await read_call_journal(improve_ledger, run_id)
        assert [r.method for r in records] == [
            "improve.reflect",
            "improve.propose",
            "improve.validate",
            "improve.gate",
        ]
        gate_record = records[3]
        assert gate_record.outcome is CallOutcome.COMPLETED
        decision = gate_record.result
        assert isinstance(decision, dict)
        assert decision["decision"] == "rejected"
        assert decision["approver_id"] == "test-approver"
        assert decision["recorded_at"]  # identity and time, both recorded

    async def test_an_unwired_gate_refuses_by_default(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        """No approver injected: the cycle must not silently approve."""
        _, result = await run_cycle(
            improve_ledger, git_repo, tmp_path, validators=_green()
        )
        assert result.status is PipelineStatus.FAILED
        assert result.metrics["stopped_reason"] == "human_gate_refused"
        assert result.metrics["approver"] == "unwired-human-gate"
        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )


class TestTheCleanHistory:
    async def test_a_week_without_friction_completes_and_proposes_nothing(
        self, db, tmp_path: Path
    ) -> None:
        ledger = LedgerStore(db)
        await seed_clean_history(ledger)
        repo = make_repo(tmp_path, name="clean-repo")
        run_id, result = await run_cycle(
            ledger, repo, tmp_path, validators=_green(), approver=approve
        )

        assert result.status is PipelineStatus.COMPLETED
        assert result.metrics["stopped_reason"] == "clean_history"
        assert result.metrics["high_friction"] == 0
        # Only the reflection ran: one call, no artifacts, no commit.
        assert await _fold(ledger, run_id) == [("improve.reflect", "COMPLETED")]
        assert not (
            tmp_path / "improve-artifacts" / run_id.hex / "proposal.json"
        ).exists()
        assert (repo / "playbooks.md").read_text(encoding="utf-8") == "# ops playbooks\n"


class TestTheApprovalGuard:
    async def test_the_commit_stage_refuses_without_a_carried_approval(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        """A host bug that forgets the bridge lands nothing — the guard is
        before any effect, so the tree stays byte-identical."""
        seed_head = head_of(git_repo)
        run_id = uuid.uuid4()
        journal = CallJournal(improve_ledger, run_id)
        await journal.run_started(
            ImproveReflectionLoop.name, ImproveReflectionLoop.version
        )
        facade = ImproveFacade(
            ledger=improve_ledger,
            repo_root=git_repo,
            artifact_home=tmp_path / "improve-artifacts",
            journal=journal,
            validators=_green(),
            approver=approve,
            allow_insecure_signing=True,
        )
        report = await facade.reflect(7)
        proposal = await facade.propose(report.high_friction_domains[0])
        await facade.validate(proposal)
        await facade.ask_human(proposal, _served_verdict())

        with pytest.raises(PaaError, match="requires the recorded human approval"):
            await facade.commit_proposal(proposal)

        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )
        assert head_of(git_repo) == seed_head

    async def test_a_carried_rejection_never_opens_the_commit_door(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        seed_head = head_of(git_repo)
        run_id = uuid.uuid4()
        journal = CallJournal(improve_ledger, run_id)
        await journal.run_started(
            ImproveReflectionLoop.name, ImproveReflectionLoop.version
        )
        facade = ImproveFacade(
            ledger=improve_ledger,
            repo_root=git_repo,
            artifact_home=tmp_path / "improve-artifacts",
            journal=journal,
            validators=_green(),
            approver=refuse,
            allow_insecure_signing=True,
        )
        report = await facade.reflect(7)
        proposal = await facade.propose(report.high_friction_domains[0])
        verdict = await facade.validate(proposal)
        decision = await facade.ask_human(proposal, verdict)
        assert not decision.approved
        facade.carry_approval(decision)  # the bridge carries a rejection

        with pytest.raises(PaaError, match="not an approval"):
            await facade.commit_proposal(proposal)

        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )
        assert head_of(git_repo) == seed_head


class TestTheReplay:
    async def test_a_completed_cycle_re_driven_from_the_record_lands_nothing_twice(
        self, improve_ledger: LedgerStore, git_repo: Path, tmp_path: Path
    ) -> None:
        run_id, first = await run_cycle(
            improve_ledger, git_repo, tmp_path, validators=_green(), approver=approve
        )
        assert first.status is PipelineStatus.COMPLETED
        head_after_first = head_of(git_repo)
        playbook_after_first = (git_repo / "playbooks.md").read_text(encoding="utf-8")

        # The re-drive: same lineage, the record answering every call.
        replay = await ReplayView.load(improve_ledger, run_id)
        _, second = await run_cycle(
            improve_ledger,
            git_repo,
            tmp_path,
            validators=_green(),
            approver=approve,
            replay=replay,
            run_id=run_id,
        )

        assert second.status is PipelineStatus.COMPLETED
        assert second.metrics["stopped_reason"] == "committed"
        assert second.metrics["git_commit"] == first.metrics["git_commit"]
        # Nothing landed twice: same HEAD, same playbook, same fold.
        assert head_of(git_repo) == head_after_first
        assert (git_repo / "playbooks.md").read_text(encoding="utf-8") == (
            playbook_after_first
        )
        assert len(await read_call_journal(improve_ledger, run_id)) == 5


def _served_verdict() -> Any:
    """A minimal green verdict for driving the facade by hand."""
    from paa.improve.validation import CommandOutcome, ValidationVerdict

    return ValidationVerdict(
        commands=(
            CommandOutcome(
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
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
