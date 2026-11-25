"""The attested bundle's format and builder mechanics (C7.2).

Unit-level: the six sections, the CAS blob layout, the chain's
determinism, the two-phase write's crash marker, the key file's
lifecycle, and the refusal paths (closed approval vocabulary, unresolvable
``--diff-since``, non-final evidence, empty goal, insecure signing).
The end-to-end flow — observe, attest, verify, tamper — lives in the
sibling suites; this one pins the parts.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from paa.attest.bundle import (
    ATTEST_SCHEMA,
    SECTION_ORDER,
    Approval,
    build_bundle,
    load_or_generate_key,
    read_manifest,
)
from paa.attest.format import chain_steps
from paa.attest.verify import read_section, verify_bundle
from paa.core.errors import PaaError
from paa.observe.bundle import ObserveBundle

pytestmark = pytest.mark.requires_attest


def _git(ws: Path, *args: str) -> None:
    """A sync git call for fixture-building (the coder suite's pattern)."""
    subprocess.run(["git", *args], cwd=ws, check=True)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    return home


@pytest.fixture()
def git_ws(tmp_path: Path) -> Path:
    """A committed workspace with one dirty change."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("hello\n", encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.invalid")
    _git(ws, "config", "user.name", "T")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "initial")
    (ws / "feature.txt").write_text("the change\n", encoding="utf-8")
    return ws


@pytest.fixture()
def key(tmp_path: Path) -> Path:
    return tmp_path / "home" / "attest" / "attest-key.json"


def _fake_observe_bundle(
    ws: Path, *, run_id: str = "run-0001", exit_code: int = 0
) -> Path:
    """A complete T1 bundle from a scripted fake harness — the same shape
    ``paa observe`` writes, built directly so the unit suite stays fast."""
    bundle = ObserveBundle(
        run_id=run_id,
        command=("pytest", "-q"),
        workspace=str(ws),
        child={"exit_code": exit_code},
        classification={"state": "clean_exit" if exit_code == 0 else "error_exit"},
    )
    bundle.finalise()
    path = ws.parent / f"observe-{run_id}" / "bundle.json"
    bundle.write(path)
    return path


def _build(
    ws: Path,
    out: Path,
    key_path: Path,
    *,
    goal: str = "ship the feature",
    approvals: list[Approval] | None = None,
    test_bundles: list[Path] | None = None,
    asserted_commands: list[list[str]] | None = None,
    diff_since: str | None = None,
):
    return build_bundle(
        out_dir=out,
        workspace=ws,
        goal=goal,
        diff_since=diff_since,
        asserted_commands=asserted_commands or [],
        test_bundles=test_bundles or [],
        approvals=approvals
        or [
            Approval(
                approver_kind="cli",
                approver_id="operator",
                decision="approved",
                recorded_at="2026-11-25T00:00:00+00:00",
            )
        ],
        key=load_or_generate_key(key_path),
    )


class TestTheFormat:
    def test_six_sections_blobs_and_a_signed_final_manifest(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        out = tmp_path / "bundle"
        test_bundle = _fake_observe_bundle(git_ws)
        result = _build(
            git_ws, out, key, test_bundles=[test_bundle],
            asserted_commands=[["ruff", "check", "src"]],
        )

        manifest = read_manifest(out / "manifest.json")
        assert manifest["phase"] == "final"
        assert manifest["schema"] == ATTEST_SCHEMA
        assert [s["name"] for s in manifest["sections"]] == list(SECTION_ORDER)
        assert manifest["signature"]["algorithm"] == "ed25519"
        assert manifest["signature"]["key_id"] == result.key_id
        assert manifest["chain_root"] == result.chain_root
        # The evidence row and the observed test row cite the same blob.
        evidence_blob = next(
            b for b in manifest["blobs"] if b["role"].startswith("evidence:")
        )
        assert evidence_blob["bytes"] > 0
        assert result.observed_tests == 1
        assert result.asserted_tests == 0
        assert result.evidence_count == 1
        # Every blob sits at the CAS layout, raw.
        for entry in manifest["blobs"]:
            digest = entry["sha256"]
            blob = out / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"
            assert blob.is_file(), f"{digest} not at the CAS layout"

    def test_the_chain_root_is_deterministic_across_rebuilds(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        """Same evidence, same policy, same approvals -> same chain root,
        whatever the wall clock says: the chain covers payloads, and none
        of the payloads carry a timestamp here (the approval's
        ``recorded_at`` is pinned by the fixture for exactly this)."""
        test_bundle = _fake_observe_bundle(git_ws)
        first = _build(git_ws, tmp_path / "b1", key, test_bundles=[test_bundle])
        second = _build(git_ws, tmp_path / "b2", key, test_bundles=[test_bundle])
        assert first.chain_root == second.chain_root
        assert first.bundle_id != second.bundle_id  # ids differ; roots do not

    def test_a_valid_diff_since_is_resolved_to_real_commits(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        _git(git_ws, "add", "-A")
        _git(git_ws, "commit", "-qm", "second commit")
        build_bundle(
            out_dir=tmp_path / "bundle",
            workspace=git_ws,
            goal="ship the feature",
            diff_since="HEAD~1",
            key=load_or_generate_key(key),
        )
        manifest = read_manifest(tmp_path / "bundle" / "manifest.json")
        digest = next(
            str(s["sha256"]) for s in manifest["sections"] if s["name"] == "diff"
        )
        diff_section = read_section(tmp_path / "bundle", digest)
        assert diff_section is not None
        commits = diff_section["git"]["commits_since"]
        assert len(commits) == 1
        assert "second commit" in commits[0]["line"]

    def test_chain_steps_fold_name_digest_and_predecessor(self) -> None:
        steps = chain_steps([("a", "d1"), ("b", "d2")])
        assert len(steps) == 2
        # Swapping the order changes the root: order is load-bearing.
        reordered = chain_steps([("b", "d2"), ("a", "d1")])
        assert steps[-1] != reordered[-1]

    def test_a_foreign_schema_manifest_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({"schema": "something-else/1"}), encoding="utf-8")
        with pytest.raises(ValueError, match="paa-attest-bundle/1"):
            read_manifest(path)

    def test_the_same_witness_can_be_test_and_general_evidence(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        """The repeatable CLI flags may name one observe bundle twice; the
        index records the content-addressed witness once, so the verifier's
        exact-set check still holds."""
        witness = _fake_observe_bundle(git_ws)
        out = tmp_path / "bundle"
        build_bundle(
            out_dir=out,
            workspace=git_ws,
            goal="ship the feature",
            test_bundles=[witness],
            evidence_bundles=[witness],
            key=load_or_generate_key(key),
        )

        manifest = read_manifest(out / "manifest.json")
        indexed = [b for b in manifest["blobs"] if b["role"].startswith("evidence:")]
        assert len(indexed) == 1
        result = verify_bundle(out)
        assert result.ok, [c.detail for c in result.checks]
        assert result.evidence_count == 2


class TestTwoPhaseWrite:
    def test_a_build_that_dies_leaves_the_started_skeleton(
        self, git_ws: Path, tmp_path: Path, key: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The crash marker: evidence gathering blows up mid-build and the
        manifest still says ``started`` — nothing silently upgrades it."""
        from paa.attest import bundle as attest_bundle

        def _explode(workspace: Path, since: str | None) -> dict[str, object]:
            raise RuntimeError("the producer died")

        monkeypatch.setattr(attest_bundle, "_git_facts", _explode)
        with pytest.raises(RuntimeError):
            _build(git_ws, tmp_path / "bundle", key)
        manifest = json.loads(
            (tmp_path / "bundle" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["phase"] == "started"
        assert manifest["signature"] is None


class TestRefusals:
    def test_an_empty_goal_is_refused(self, git_ws: Path, tmp_path: Path, key: Path) -> None:
        with pytest.raises(PaaError, match="goal"):
            _build(git_ws, tmp_path / "bundle", key, goal="   ")

    def test_the_approval_vocabulary_is_closed(self) -> None:
        with pytest.raises(PaaError, match=r"vocabulary|decision"):
            Approval(approver_kind="cli", approver_id="o", decision="maybe")

    def test_an_anonymous_approval_is_refused(self) -> None:
        with pytest.raises(PaaError, match="anonymous"):
            Approval(approver_kind=" ", approver_id="o", decision="approved")

    def test_an_unresolvable_diff_since_is_refused(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        with pytest.raises(PaaError, match="does not resolve"):
            _build(git_ws, tmp_path / "bundle", key, diff_since="no-such-rev")

    def test_an_option_looking_diff_since_is_refused_without_writing(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        """The old ``git log`` argv let ``--output=…`` become a git option
        and write a producer-host file while the bundle said 'no commits'."""
        target = tmp_path / "injected"
        with pytest.raises(PaaError, match="git option, not a revision"):
            _build(
                git_ws,
                tmp_path / "bundle",
                key,
                diff_since=f"--output={target}",
            )
        assert not target.exists()
        assert not (tmp_path / "injected..HEAD").exists()

    def test_a_failed_git_status_is_not_recorded_as_a_clean_diff(
        self, git_ws: Path, tmp_path: Path, key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from paa.attest import bundle as attest_bundle

        def _git_without_status(
            workspace: Path, *args: str, **kwargs: object
        ) -> str | None:
            if args and args[0] == "rev-parse":
                return "abc123\n"
            return None

        monkeypatch.setattr(attest_bundle, "_git", _git_without_status)
        with pytest.raises(PaaError, match="git status failed"):
            _build(git_ws, tmp_path / "bundle", key)

    def test_an_empty_diff_is_reported_not_presented_as_silent(
        self, git_ws: Path, tmp_path: Path, key: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from paa.attest import bundle as attest_bundle

        def _clean_git(
            workspace: Path, *args: str, **kwargs: object
        ) -> str | None:
            if args and args[0] == "rev-parse":
                return "abc123\n"
            if args and args[0] in {"status", "diff"}:
                return ""
            return None

        monkeypatch.setattr(attest_bundle, "_git", _clean_git)
        result = _build(git_ws, tmp_path / "bundle", key)
        assert any("empty working-tree diff" in w for w in result.warnings)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("evidence_tier", "T2", "not T1"),
            ("resume", "hot", "not 'none'"),
        ],
    )
    def test_a_non_t1_or_resumed_observe_bundle_is_not_evidence(
        self,
        git_ws: Path,
        tmp_path: Path,
        key: Path,
        field: str,
        value: str,
        message: str,
    ) -> None:
        evidence = ObserveBundle(
            run_id="claim",
            command=("pytest", "-q"),
            workspace=str(git_ws),
            **{field: value},
        )
        evidence.finalise()
        path = tmp_path / "claim" / "bundle.json"
        evidence.write(path)
        with pytest.raises(PaaError, match=message):
            _build(git_ws, tmp_path / "bundle", key, test_bundles=[path])

    def test_secret_looking_asserted_commands_are_refused(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        with pytest.raises(PaaError, match="AWS access key"):
            _build(
                git_ws,
                tmp_path / "bundle",
                key,
                asserted_commands=[["echo", "AKIA1234567890ABCDEF"]],
            )

    def test_a_secret_in_an_approval_identity_is_refused(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        """Approver kind/id are operator argv too; they are signed into a
        portable section, so them being secrets is a leak, not metadata."""
        with pytest.raises(PaaError, match="AWS access key"):
            _build(
                git_ws,
                tmp_path / "bundle",
                key,
                approvals=[
                    Approval(
                        approver_kind="AKIA1234567890ABCDEF",
                        approver_id="operator",
                        decision="approved",
                    )
                ],
            )

    def test_a_secret_in_an_embedded_transcript_is_refused(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        evidence = ObserveBundle(
            run_id="leaky",
            command=("echo", "AKIA1234567890ABCDEF"),
            workspace=str(git_ws),
        )
        evidence.finalise()
        path = tmp_path / "leaky" / "bundle.json"
        evidence.write(path)
        with pytest.raises(PaaError, match="AWS access key"):
            _build(git_ws, tmp_path / "bundle", key, test_bundles=[path])

    def test_a_started_observe_bundle_is_not_evidence(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        """The observer's own crash marker must not be embedded as
        evidence of a completed run."""
        stuck = ObserveBundle(
            run_id="stuck", command=("pytest", "-q"), workspace=str(git_ws)
        )  # phase stays "started"
        path = tmp_path / "stuck" / "bundle.json"
        stuck.write(path)
        with pytest.raises(PaaError, match="phase='started'"):
            _build(git_ws, tmp_path / "bundle", key, test_bundles=[path])

    def test_a_foreign_observe_bundle_is_refused(
        self, git_ws: Path, tmp_path: Path, key: Path
    ) -> None:
        path = tmp_path / "not-a-bundle.json"
        path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
        with pytest.raises(PaaError, match="not a readable"):
            _build(git_ws, tmp_path / "bundle", key, test_bundles=[path])


class TestTheKeyFile:
    def test_generated_once_then_reused(
        self, tmp_path: Path, key: Path
    ) -> None:
        first = load_or_generate_key(key)
        assert first.secure is True
        assert first.algorithm == "ed25519"
        again = load_or_generate_key(key)
        assert again.key_id == first.key_id
        # The private half is mode 600 from the first write.
        mode = key.stat().st_mode & 0o777
        assert mode == 0o600

    def test_a_group_or_world_readable_key_is_refused(
        self, tmp_path: Path, key: Path
    ) -> None:
        load_or_generate_key(key)
        os.chmod(key, 0o644)
        with pytest.raises(PaaError, match="group/world accessible"):
            load_or_generate_key(key)

    def test_insecure_signing_is_refused_without_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import paa.marketplace.signing as marketplace_signing

        # Simulate a bare install: no cryptography package, so both the
        # security probe and the key generator take the fallback path.
        monkeypatch.setattr(
            marketplace_signing, "_cryptography_available", lambda: False
        )
        with pytest.raises(PaaError, match="asymmetric signing is unavailable"):
            load_or_generate_key(tmp_path / "k.json")

    def test_insecure_signing_is_allowed_and_labelled_with_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import paa.marketplace.signing as marketplace_signing

        monkeypatch.setattr(
            marketplace_signing, "_cryptography_available", lambda: False
        )
        key = load_or_generate_key(tmp_path / "k.json", allow_insecure=True)
        assert key.secure is False
        assert key.algorithm == "hmac-dev-fallback"
        # And reloading the insecure key still needs the flag.
        with pytest.raises(PaaError, match="forgeable"):
            load_or_generate_key(tmp_path / "k.json")
