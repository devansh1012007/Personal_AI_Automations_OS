"""The patch proposal — the deterministic middle artifact (C8.1 stage 2).

What these tests pin:

* **the artifact is described change, never applied** — constructing a
  proposal writes nothing; only :meth:`PatchProposal.apply` writes, and
  it writes exactly the named files;
* **the jail** — a proposal target must be a relative, non-``.git``,
  non-escaping file path, and an existing target must still hash to the
  content the proposal was synthesised against (a proposal cannot
  silently overwrite a file that changed after the loop looked at it);
* **the fence** — the managed marker keeps human playbook text
  verbatim, accumulates a second rule inside the fence, and creates the
  fence at the end of a fence-less file;
* **the artifact's identity** — the digest is canonical-JSON stable, a
  foreign schema is refused at load, and an empty change set is not a
  proposal;
* **the honesty rule** — :func:`proposal_from_report` refuses a domain
  reflection did not flag: the guarded loop proposes only for domains the
  engine's own numbers selected.

The engine runs over the suite's real ledger fixture — the same class
the pipeline's reflection stage drives — so the rule template these
tests read is the template the loop would write.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paa.core.errors import PaaError
from paa.improve.proposal import (
    DEFAULT_TARGET_FILE,
    FileChange,
    PatchProposal,
    proposal_from_report,
    read_proposal,
    write_proposal,
)
from paa.improvement.reflection import DomainFriction, ReflectionEngine
from paa.ledger.store import LedgerStore


def _high_friction(domain: str = "deploy") -> DomainFriction:
    # corrections=1, rollbacks=1, successes=0 -> friction 4.5, well over
    # the engine's 0.40 threshold: the shape the guarded loop feeds its
    # proposal stage in every suite that drives it.
    return DomainFriction(domain=domain, corrections=1, rollbacks=1, successes=0)


def _low_friction(domain: str = "deploy") -> DomainFriction:
    # One clean commit, no friction: score 0.0 — the engine's own "not
    # high-friction" answer, which the proposal stage must respect.
    return DomainFriction(domain=domain, corrections=0, rollbacks=0, successes=1)


def _proposal(*, intent: str = "record the lesson", content: str = "new\n") -> PatchProposal:
    return PatchProposal(
        intent=intent,
        rationale="friction was high",
        changes=(FileChange(path="playbooks.md", content=content, prior_sha256=""),),
        domain="deploy",
    )


class TestTheArtifact:
    def test_a_proposal_is_a_digest_over_canonical_json(self) -> None:
        proposal = _proposal()
        same = _proposal()
        assert proposal.sha256 == same.sha256

    def test_the_digest_covers_every_field(self) -> None:
        base = _proposal()
        other_intent = _proposal(intent="different intent")
        other_content = _proposal(content="other\n")
        assert base.sha256 != other_intent.sha256
        assert base.sha256 != other_content.sha256

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        proposal = _proposal()
        path = write_proposal(proposal, tmp_path / "artifacts" / "proposal.json")
        assert path.is_file()
        assert read_proposal(path) == proposal

    def test_a_foreign_schema_is_refused_by_name(self, tmp_path: Path) -> None:
        path = tmp_path / "proposal.json"
        path.write_text('{"schema": "something-else/1"}', encoding="utf-8")
        with pytest.raises(PaaError, match="paa-improve-proposal/1"):
            read_proposal(path)

    def test_an_empty_change_set_is_not_a_proposal(self, tmp_path: Path) -> None:
        path = write_proposal(
            PatchProposal(intent="i", rationale="r", changes=()), tmp_path / "p.json"
        )
        with pytest.raises(PaaError, match="carries no changes"):
            read_proposal(path)

    def test_unreadable_artifacts_are_named_not_crashed(self, tmp_path: Path) -> None:
        with pytest.raises(PaaError, match="not a readable proposal"):
            read_proposal(tmp_path / "missing.json")
        binary = tmp_path / "binary.json"
        binary.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(PaaError, match="not a readable proposal"):
            read_proposal(binary)


class TestTheJail:
    def test_apply_writes_the_named_file_and_creates_parents(
        self, tmp_path: Path
    ) -> None:
        prior = "# human text\n"
        prior_sha = hashlib.sha256(prior.encode("utf-8")).hexdigest()
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(
                FileChange(
                    path="docs/ops/playbooks.md", content="new\n", prior_sha256=prior_sha
                ),
            ),
        )
        written = proposal.apply(tmp_path)
        assert written == [tmp_path / "docs" / "ops" / "playbooks.md"]
        assert (tmp_path / "docs" / "ops" / "playbooks.md").read_text(
            encoding="utf-8"
        ) == "new\n"

    def test_apply_refuses_a_target_that_changed_under_it(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "playbooks.md").write_text("changed since\n", encoding="utf-8")
        proposal = _proposal()
        with pytest.raises(PaaError, match="changed since the proposal"):
            proposal.apply(tmp_path)
        # All-or-nothing validation: the refusal happens before any write.
        assert (tmp_path / "playbooks.md").read_text(encoding="utf-8") == "changed since\n"

    def test_apply_refuses_an_absolute_target(self, tmp_path: Path) -> None:
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(FileChange(path="/etc/passwd", content="x", prior_sha256=""),),
        )
        with pytest.raises(PaaError, match="relative file path"):
            proposal.apply(tmp_path)

    def test_apply_refuses_a_target_that_escapes_the_root(self, tmp_path: Path) -> None:
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(FileChange(path="../outside.md", content="x", prior_sha256=""),),
        )
        with pytest.raises(PaaError, match="escapes the repository root"):
            proposal.apply(tmp_path)

    def test_apply_refuses_the_git_directory(self, tmp_path: Path) -> None:
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(
                FileChange(path=".git/hooks/pre-commit", content="x", prior_sha256=""),
            ),
        )
        with pytest.raises(PaaError, match="inside the git directory"):
            proposal.apply(tmp_path)

    def test_apply_refuses_a_directory_target(self, tmp_path: Path) -> None:
        (tmp_path / "adir").mkdir()
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(FileChange(path="adir", content="x", prior_sha256=""),),
        )
        with pytest.raises(PaaError, match="is a directory"):
            proposal.apply(tmp_path)

    def test_reapplying_a_landed_proposal_refuses(self, tmp_path: Path) -> None:
        """The prior-hash guard makes a landed change its own obstacle.

        After one apply, the file hashes to the proposal's *new* content,
        so the same proposal applied again is refused — the loop cannot
        double-apply even if a host bug asks it to.
        """
        (tmp_path / "playbooks.md").write_text("old\n", encoding="utf-8")
        prior_sha = hashlib.sha256(b"old\n").hexdigest()
        proposal = PatchProposal(
            intent="i",
            rationale="r",
            changes=(
                FileChange(path="playbooks.md", content="new\n", prior_sha256=prior_sha),
            ),
        )
        assert len(proposal.apply(tmp_path)) == 1
        with pytest.raises(PaaError, match="changed since the proposal"):
            proposal.apply(tmp_path)


class TestTheFence:
    def test_human_text_outside_the_fence_is_preserved_verbatim(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text(
            "# ops playbooks\n\nhuman words\n", encoding="utf-8"
        )
        engine = ReflectionEngine(ledger)
        proposal = proposal_from_report(engine, _high_friction(), repo_root=tmp_path)
        content = proposal.changes[0].content
        assert "# ops playbooks\n\nhuman words" in content
        assert "<!-- managed:improve-reflection-loop begin -->" in content
        assert "<!-- managed:improve-reflection-loop end -->" in content

    def test_a_fenceless_file_gains_the_fence_at_the_end(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
        engine = ReflectionEngine(ledger)
        proposal = proposal_from_report(engine, _high_friction(), repo_root=tmp_path)
        content = proposal.changes[0].content
        # The human header is still first, the fence is appended after it,
        # and the file ends inside the fence's close.
        assert content.startswith("# ops playbooks\n")
        assert "<!-- managed:improve-reflection-loop begin -->" in content
        assert content.endswith("end -->\n")

    def test_a_second_rule_accumulates_inside_the_fence(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
        engine = ReflectionEngine(ledger)
        first = proposal_from_report(engine, _high_friction(), repo_root=tmp_path)
        first.apply(tmp_path)
        second = proposal_from_report(
            engine, _high_friction("deploy"), repo_root=tmp_path
        )
        content = second.changes[0].content
        # The fence now holds two anti-pattern entries, and the human
        # header is still first.
        assert content.count("### Anti-pattern: deploy") == 2
        assert content.startswith("# ops playbooks")

    def test_the_rule_text_is_the_engines_own_template(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text("", encoding="utf-8")
        engine = ReflectionEngine(ledger)
        friction = _high_friction()
        proposal = proposal_from_report(engine, friction, repo_root=tmp_path)
        rule = engine.synthesize_rule(friction)
        assert rule.strip() in proposal.changes[0].content


class TestTheSelectionRule:
    def test_a_domain_reflection_did_not_flag_is_refused(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
        engine = ReflectionEngine(ledger)
        with pytest.raises(PaaError, match="not high-friction"):
            proposal_from_report(engine, _low_friction(), repo_root=tmp_path)

    def test_the_rationale_restates_the_friction_numbers(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        (tmp_path / "playbooks.md").write_text("", encoding="utf-8")
        engine = ReflectionEngine(ledger)
        proposal = proposal_from_report(
            engine, _high_friction("deploy"), repo_root=tmp_path
        )
        # A reviewer reading only the proposal can audit the selection.
        assert "1 correction(s)" in proposal.rationale
        assert "1 rollback(s)" in proposal.rationale
        assert "'deploy'" in proposal.rationale

    def test_the_prior_digest_attests_the_file_it_was_built_against(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        prior = "# ops playbooks\n"
        (tmp_path / "playbooks.md").write_text(prior, encoding="utf-8")
        engine = ReflectionEngine(ledger)
        proposal = proposal_from_report(engine, _high_friction(), repo_root=tmp_path)
        assert proposal.changes[0].prior_sha256 == hashlib.sha256(
            prior.encode("utf-8")
        ).hexdigest()

    def test_a_missing_target_file_starts_from_empty(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        engine = ReflectionEngine(ledger)
        proposal = proposal_from_report(engine, _high_friction(), repo_root=tmp_path)
        assert proposal.changes[0].prior_sha256 == hashlib.sha256(b"").hexdigest()

    def test_the_default_target_is_the_playbook_surface(self) -> None:
        assert DEFAULT_TARGET_FILE == "playbooks.md"
