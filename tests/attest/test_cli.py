"""``paa attest`` and ``paa verify`` as commands (C7.2).

The done-when driven through the front doors: a scripted fake harness
runs under ``paa observe`` (a *real* T1 bundle, not a fixture), the
operator attests the change with it as test evidence, and a reviewer
verifies — green, then green with a trust pin, then red after one
flipped byte. The same throwaway-home discipline the observe CLI suite
applies: isolated ``PAA_HOME``, the subprocess sandbox, driven offline.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
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


def _observe_a_fake_test_run(ws: Path, home: Path) -> Path:
    """Run the fake harness under the real ``paa observe`` and return the
    complete T1 bundle's path — the C7.1 artifact C7.2 consumes."""
    harness = (
        "import sys; print('fake tests ran'); sys.exit(0)"
    )
    result = runner.invoke(
        app,
        ["observe", "--workspace", str(ws), "--", sys.executable, "-c", harness],
    )
    assert result.exit_code == 0, result.output
    bundles = list((home / "observe").rglob("bundle.json"))
    assert len(bundles) == 1
    return bundles[0]


def _attest(ws: Path, home: Path, test_bundle: Path) -> Path:
    result = runner.invoke(
        app,
        [
            "attest", "build",
            "--workspace", str(ws),
            "--goal", "ship the feature",
            "--requester", "operator",
            "--test-bundle", str(test_bundle),
            "--command", "ruff check src",
            "--approval", "cli:operator:approved:looks right",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "attested" in result.output
    # The bundle dir is printed on the "bundle:" line.
    line = next(ln for ln in result.output.splitlines() if ln.startswith("bundle:"))
    return Path(line.split("bundle:", 1)[1].strip())


class TestTheRoundTrip:
    def test_observe_then_attest_then_verify(
        self, git_ws: Path, home: Path
    ) -> None:
        """The C7.2 done-when as one flow: real observe bundle in, green
        verification out; a pin on the signer's key turns 'self-asserted'
        into 'trusted'."""
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)

        result = runner.invoke(app, ["verify", str(bundle_dir)])
        assert result.exit_code == 0, result.output
        assert "verified" in result.output
        assert "self-asserted" in result.output
        assert "tests observed/asserted: 1/0" in result.output

        key_file = home / "attest" / "attest-key.json"
        assert key_file.is_file()
        pinned = runner.invoke(
            app, ["verify", str(bundle_dir), "--trust", str(key_file)]
        )
        assert pinned.exit_code == 0, pinned.output
        assert "pinned" in pinned.output

    def test_verify_json_names_every_check(
        self, git_ws: Path, home: Path
    ) -> None:
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)
        result = runner.invoke(app, ["verify", "--json", str(bundle_dir)])
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["ok"] is True
        assert [c["name"] for c in payload["checks"]] == [
            "manifest", "blobs", "chain", "signature", "key",
        ]
        assert payload["observed_tests"] == 1
        assert payload["approvals"] == 1

    def test_one_flipped_byte_fails_the_front_door(
        self, git_ws: Path, home: Path
    ) -> None:
        """DONE-WHEN 1, as a command: alter one byte of a section blob
        and ``paa verify`` exits 1 naming the tamper."""
        from paa.attest.bundle import read_manifest

        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)

        manifest = read_manifest(bundle_dir / "manifest.json")
        digest = str(manifest["sections"][0]["sha256"])
        blob = bundle_dir / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"
        data = bytearray(blob.read_bytes())
        data[len(data) // 2] ^= 0x01
        blob.write_bytes(bytes(data))

        result = runner.invoke(app, ["verify", str(bundle_dir)])
        assert result.exit_code == 1
        assert "section:mandate" in result.output

    def test_workspace_equivalence_through_the_front_door(
        self, git_ws: Path, home: Path
    ) -> None:
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)

        clean = runner.invoke(
            app, ["verify", str(bundle_dir), "--workspace", str(git_ws)]
        )
        assert clean.exit_code == 0, clean.output
        assert "matches the attested manifest" in clean.output

        (git_ws / "feature.txt").write_text("drifted\n", encoding="utf-8")
        drifted = runner.invoke(
            app, ["verify", str(bundle_dir), "--workspace", str(git_ws)]
        )
        assert drifted.exit_code == 1
        assert "drifts" in drifted.output


class TestRefusalsAndLabels:
    def test_asserted_only_tests_are_labelled_not_blocked(
        self, git_ws: Path, home: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "attest", "build",
                "--workspace", str(git_ws),
                "--goal", "claims without evidence",
                "--test-command", "pytest -q",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "every test row is asserted" in result.output
        line = next(ln for ln in result.output.splitlines() if ln.startswith("bundle:"))
        bundle_dir = Path(line.split("bundle:", 1)[1].strip())
        verdict = runner.invoke(app, ["verify", str(bundle_dir)])
        assert verdict.exit_code == 0
        assert "tests observed/asserted: 0/1" in verdict.output

    def test_a_malformed_approval_is_refused(
        self, git_ws: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "attest", "build",
                "--workspace", str(git_ws),
                "--goal", "g",
                "--approval", "nonsense",
            ],
        )
        assert result.exit_code == 2
        assert "kind:id:decision" in result.output

    def test_an_empty_goal_is_refused(self, git_ws: Path) -> None:
        result = runner.invoke(
            app,
            ["attest", "build", "--workspace", str(git_ws), "--goal", "  "],
        )
        assert result.exit_code == 2
        assert "goal" in result.output

    def test_a_started_bundle_is_refused_by_verify(
        self, git_ws: Path, home: Path
    ) -> None:
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)
        path = bundle_dir / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["phase"] = "started"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        result = runner.invoke(app, ["verify", str(bundle_dir)])
        assert result.exit_code == 1
        assert "crash marker" in result.output


class TestThePrBody:
    def test_pr_prints_a_paste_ready_body(
        self, git_ws: Path, home: Path
    ) -> None:
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)

        result = runner.invoke(app, ["attest", "pr", str(bundle_dir)])
        assert result.exit_code == 0, result.output
        assert "## Attested change bundle" in result.output
        assert "ship the feature" in result.output
        assert f"paa verify {bundle_dir}" in result.output
        assert "chain root" in result.output.lower() or "Chain root" in result.output

    def test_pr_writes_to_a_file(
        self, git_ws: Path, home: Path, tmp_path: Path
    ) -> None:
        test_bundle = _observe_a_fake_test_run(git_ws, home)
        bundle_dir = _attest(git_ws, home, test_bundle)
        out = tmp_path / "pr-body.md"
        result = runner.invoke(
            app, ["attest", "pr", str(bundle_dir), "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        body = out.read_text(encoding="utf-8")
        assert "## Attested change bundle" in body
        assert "How to verify" in body

    def test_pr_refuses_a_foreign_directory(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["attest", "pr", str(tmp_path)])
        assert result.exit_code == 2
