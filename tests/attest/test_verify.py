"""The offline verifier (C7.2) — the tamper matrix and the two acceptance lines.

The spec's done-when for the attested bundle is two sentences, and both
are pinned here:

* **"A bundle altered by one byte fails verification"** — the tamper
  matrix flips a byte in a section blob, in an evidence blob, in the
  manifest's chain root, and swaps and deletes blobs; every variant must
  fail, naming which door was breached.
* **"A reviewer can verify one on a machine without PAA's database"** —
  the clean-checkout test points ``PAA_HOME`` at a path that does not
  exist, verifies, and asserts the path is still nonexistent afterwards:
  the verifier read the bundle and nothing else.

Plus the property the honesty design lives on: a bundle re-signed by an
attacker's key is *intact* (the signature over the tampered-with header
verifies against the attacker's embedded key) and *untrusted* (it fails
``--trust``) at the same time — integrity and origin are reported as
the separate facts they are, never collapsed into one green light.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paa.attest.bundle import (
    Approval,
    build_bundle,
    load_or_generate_key,
    read_manifest,
)
from paa.attest.verify import verify_bundle
from paa.observe.bundle import ObserveBundle


def _git(ws: Path, *args: str) -> None:
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


def _fake_observe_bundle(ws: Path, *, run_id: str = "run-0001") -> Path:
    bundle = ObserveBundle(
        run_id=run_id,
        command=("pytest", "-q"),
        workspace=str(ws),
        child={"exit_code": 0},
        classification={"state": "clean_exit"},
    )
    bundle.finalise()
    path = ws.parent / f"observe-{run_id}" / "bundle.json"
    bundle.write(path)
    return path


@pytest.fixture()
def built(git_ws: Path, tmp_path: Path) -> Path:
    """A complete, honestly-built bundle with one observed test."""
    out = tmp_path / "bundle"
    build_bundle(
        out_dir=out,
        workspace=git_ws,
        goal="ship the feature",
        test_bundles=[_fake_observe_bundle(git_ws)],
        approvals=[
            Approval(
                approver_kind="cli",
                approver_id="operator",
                decision="approved",
                recorded_at="2026-11-25T00:00:00+00:00",
            )
        ],
        key=load_or_generate_key(tmp_path / "home" / "attest" / "attest-key.json"),
    )
    return out


def _blob_path(bundle: Path, digest: str) -> Path:
    return bundle / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"


def _section_digest(bundle: Path, name: str) -> str:
    manifest = read_manifest(bundle / "manifest.json")
    return next(
        str(s["sha256"]) for s in manifest["sections"] if s["name"] == name
    )


def _flip_one_byte(path: Path) -> None:
    """The spec's own attack: alter the artifact by exactly one byte."""
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


def _check(result, name: str) -> object:
    return next(c for c in result.checks if c.name == name)


class TestRoundTrip:
    def test_a_honestly_built_bundle_verifies_green(self, built: Path) -> None:
        result = verify_bundle(built)
        assert result.ok, [c.detail for c in result.checks]
        assert [c.name for c in result.checks] == [
            "manifest", "blobs", "chain", "signature", "key",
        ]
        assert result.algorithm == "ed25519"
        assert result.observed_tests == 1
        assert result.asserted_tests == 0
        assert result.approvals == 1
        assert result.evidence_count == 1

    def test_without_a_pin_the_key_is_self_asserted_never_trusted(
        self, built: Path
    ) -> None:
        result = verify_bundle(built)
        key = _check(result, "key")
        assert key.ok is True
        assert "self-asserted" in key.detail


class TestTheTamperMatrix:
    def test_one_flipped_byte_in_a_section_blob_fails(
        self, built: Path
    ) -> None:
        """DONE-WHEN 1a: the spec's own acceptance line, on a section."""
        _flip_one_byte(_blob_path(built, _section_digest(built, "mandate")))
        result = verify_bundle(built)
        assert result.ok is False
        blobs = _check(result, "blobs")
        assert blobs.ok is False
        assert "section:mandate" in blobs.detail

    def test_one_flipped_byte_in_an_evidence_blob_fails(
        self, built: Path
    ) -> None:
        """DONE-WHEN 1b: the same, on an embedded T1 observe bundle."""
        manifest = read_manifest(built / "manifest.json")
        evidence = next(
            b for b in manifest["blobs"] if b["role"].startswith("evidence:")
        )
        _flip_one_byte(_blob_path(built, str(evidence["sha256"])))
        result = verify_bundle(built)
        assert result.ok is False
        assert _check(result, "blobs").ok is False

    def test_one_flipped_byte_in_the_manifest_fails(
        self, built: Path
    ) -> None:
        """DONE-WHEN 1c: the manifest is covered too — tamper the chain
        root and both the chain and the signature go red."""
        path = built / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["chain_root"] = ("0" * 64) if manifest["chain_root"][0] != "0" else "1" * 64
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = verify_bundle(built)
        assert result.ok is False
        assert _check(result, "chain").ok is False
        assert _check(result, "signature").ok is False

    def test_two_swapped_section_payloads_fail(self, built: Path) -> None:
        """Swapping two blobs' contents keeps each blob's name wrong for
        the other's bytes — the content address is the checksum."""
        a = _blob_path(built, _section_digest(built, "mandate"))
        b = _blob_path(built, _section_digest(built, "diff"))
        a_bytes, b_bytes = a.read_bytes(), b.read_bytes()
        a.write_bytes(b_bytes)
        b.write_bytes(a_bytes)
        result = verify_bundle(built)
        assert result.ok is False
        assert _check(result, "blobs").ok is False

    def test_a_missing_blob_fails(self, built: Path) -> None:
        _blob_path(built, _section_digest(built, "approvals")).unlink()
        result = verify_bundle(built)
        assert result.ok is False
        assert "missing" in _check(result, "blobs").detail

    def test_a_started_manifest_is_refused(self, built: Path) -> None:
        path = built / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["phase"] = "started"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = verify_bundle(built)
        assert result.ok is False
        assert "crash marker" in _check(result, "manifest").detail

    def test_a_foreign_schema_directory_is_refused(self, built: Path) -> None:
        path = built / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["schema"] = "someone-elses/7"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = verify_bundle(built)
        assert result.ok is False
        assert "not a paa-attest-bundle/1" in _check(result, "manifest").detail

    def test_a_reordered_section_list_fails(self, built: Path) -> None:
        """The chain walks a fixed order; reordering the declared list
        (with digests intact) breaks both the order check and the root."""
        path = built / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        sections = manifest["sections"]
        manifest["sections"] = [sections[1], sections[0], *sections[2:]]
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = verify_bundle(built)
        assert result.ok is False


class TestTheCleanCheckout:
    def test_verify_works_with_no_paa_database_and_creates_none(
        self, built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DONE-WHEN 2: a machine without PAA's database. PAA_HOME points
        at a path that does not exist; the verifier works and leaves it
        nonexistent — no home, no ledger, no store was ever built."""
        nowhere = tmp_path / "nowhere-home"
        monkeypatch.setenv("PAA_HOME", str(nowhere))
        result = verify_bundle(built)
        assert result.ok, [c.detail for c in result.checks]
        assert not nowhere.exists()

    def test_the_verifier_never_mutates_the_bundle(
        self, built: Path, tmp_path: Path
    ) -> None:
        """Read-only proof: the CAS's own writer creates a ``staging/``
        directory on construction; the verifier must not. Strip it, verify,
        and it stays stripped."""
        staging = built / "staging"
        staging.rmdir()  # empty after the build
        result = verify_bundle(built)
        assert result.ok
        assert not staging.exists()

    def test_a_bundle_moved_to_another_machine_verifies(
        self, built: Path, tmp_path: Path
    ) -> None:
        """Portability: copy the directory elsewhere (the reviewer's
        checkout), verify there."""
        import shutil

        moved = tmp_path / "reviewer" / "bundle"
        shutil.copytree(built, moved)
        result = verify_bundle(moved)
        assert result.ok
        assert result.bundle_id == verify_bundle(built).bundle_id


class TestIntegrityVersusOrigin:
    def test_a_trust_pin_on_the_signer_key_passes(self, built: Path) -> None:
        manifest = read_manifest(built / "manifest.json")
        pinned = manifest["signature"]["public_key_b64"]
        result = verify_bundle(built, trust_key=pinned)
        assert result.ok
        assert "pinned" in _check(result, "key").detail

    def test_a_trust_pin_on_a_file_passes(
        self, built: Path, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "home" / "attest" / "attest-key.json"
        result = verify_bundle(built, trust_key=str(key_file))
        assert result.ok
        assert "pinned" in _check(result, "key").detail

    def test_a_wrong_pin_fails_the_key_check(
        self, built: Path, tmp_path: Path
    ) -> None:
        wrong = load_or_generate_key(tmp_path / "wrong-key.json")
        result = verify_bundle(built, trust_key=wrong.public_b64)
        assert result.ok is False
        key = _check(result, "key")
        assert key.ok is False
        assert "wrong signer" in key.detail

    def test_an_unreadable_pin_fails_rather_than_skips(self, built: Path) -> None:
        result = verify_bundle(built, trust_key=str(Path("/no/such/key")))
        assert result.ok is False
        assert "could not read" in _check(result, "key").detail

    def test_a_forged_resignation_is_intact_but_untrusted(
        self, built: Path, tmp_path: Path
    ) -> None:
        """The honesty split, as an attack: an attacker re-signs the
        *same* manifest with their own key and embeds their own public
        key. Integrity holds — the signature covers what was signed —
        and origin fails the pin. Without a pin the verdict is honestly
        'self-asserted': a reviewer who pinned nothing was told so."""
        from paa.ledger.events import canonical_json
        from paa.marketplace.signing import sign

        attacker = load_or_generate_key(tmp_path / "attacker-key.json")
        path = built / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        signed_header = {
            "schema": manifest["schema"],
            "bundle_id": manifest["bundle_id"],
            "created_at": manifest["created_at"],
            "tool_version": manifest["tool_version"],
            "sections": manifest["sections"],
            "chain_root": manifest["chain_root"],
        }
        signed_bytes = canonical_json(signed_header).encode("utf-8")
        manifest["signature"] = {
            "algorithm": "ed25519",
            "key_id": attacker.key_id,
            "public_key_b64": attacker.public_b64,
            "value_b64": sign(signed_bytes, attacker.private_b64),
            "signed_over": __import__("hashlib").sha256(signed_bytes).hexdigest(),
        }
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        # Intact: every check passes without a pin…
        unpinned = verify_bundle(built)
        assert unpinned.ok, [c.detail for c in unpinned.checks]
        assert "self-asserted" in _check(unpinned, "key").detail
        # …and untrusted: the original signer's pin catches the swap.
        original = json.loads(
            (tmp_path / "home" / "attest" / "attest-key.json").read_text(
                encoding="utf-8"
            )
        )
        pinned = verify_bundle(built, trust_key=original["public_b64"])
        assert pinned.ok is False
        assert "wrong signer" in _check(pinned, "key").detail


class TestWorkspaceEquivalence:
    def test_an_unchanged_checkout_matches(self, built: Path, git_ws: Path) -> None:
        result = verify_bundle(built, workspace=git_ws)
        assert result.ok, [c.detail for c in result.checks]
        assert "matches the attested manifest" in _check(result, "workspace").detail

    def test_drift_is_named_not_just_reported(
        self, built: Path, git_ws: Path
    ) -> None:
        (git_ws / "feature.txt").write_text("silently changed\n", encoding="utf-8")
        (git_ws / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
        (git_ws / "README.md").unlink()
        result = verify_bundle(built, workspace=git_ws)
        assert result.ok is False
        detail = str(_check(result, "workspace").detail)
        assert "drifts" in detail
        assert "modified" in detail and "feature.txt" in detail
        assert "created" in detail and "sneaky.py" in detail
        assert "deleted" in detail and "README.md" in detail


class TestSummary:
    def test_asserted_only_tests_warn(self, git_ws: Path, tmp_path: Path) -> None:
        out = tmp_path / "asserted-only"
        build_bundle(
            out_dir=out,
            workspace=git_ws,
            goal="claims without evidence",
            asserted_tests=[("pytest", "-q")],
            key=load_or_generate_key(tmp_path / "home" / "attest" / "attest-key.json"),
        )
        result = verify_bundle(out)
        assert result.ok
        assert result.observed_tests == 0
        assert result.asserted_tests == 1
        assert any("asserted" in w for w in result.warnings)
