"""``paa improve`` and its registry door through Typer's CliRunner (C8.1).

The same throwaway-home discipline ``tests/cli/test_pipeline.py`` applies
to ``paa pipeline run``: an isolated ``PAA_HOME``, the offline echo
provider, AUTO policy — a real runtime over a real ledger, driven offline.
The cycle's own fixtures are real too (the ``tests/improve`` convention):
a real git repository with identity configured, a ledger seeded with one
high-friction deploy domain, and gate commands that are real
``SystemExit`` subprocesses. The only scripted piece is the human: the
approver seam is monkeypatched, because a CLI test that waited on a
terminal prompt would not be a test.

What is proven here is the **doors**: exit codes (0 the cycle completed,
1 the cycle stopped by its own rules, 2 the door refused), the artifacts
named in the output, and that ``paa pipeline run
improve.reflection-loop`` drives the same shared driver ``paa improve``
does (same journal shape, same stopped reason) — the observe pattern's
one-driver-two-doors claim, asserted rather than asserted-by-reading.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

# The gate commands: real subprocesses with deterministic exit codes,
# spelled so no test ever depends on this repository's own toolchain.
_GREEN = [sys.executable, "-c", "raise SystemExit(0)"]
_RED = [sys.executable, "-c", "raise SystemExit(2)"]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(output: str) -> str:
    """Rendered CLI output as content: no escape sequences, no line breaks.

    The W8-CI2 measurement on the runner: Rich's styled help wraps long
    option names across lines there (``--gate`` split mid-token), intact on
    every terminal-less host — the assertions are about content, so they
    read the help the way the coder suite's ``--max-steps`` assertions
    learned to (``tests/coder/test_agent.py``).
    """
    return _ANSI.sub("", output).replace("\r", "").replace("\n", "")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every improve-CLI test gets a throwaway home + offline echo."""
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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real fixture repository, from the improve suite's own helper."""
    if sys.platform == "win32":
        pytest.skip("the improve CLI suite drives real git subprocesses")
    from tests.improve.conftest import make_repo

    return make_repo(tmp_path, name="cli-repo")


@pytest.fixture
async def seeded_ledger(tmp_path: Path) -> Path:
    """A ledger file seeded with one high-friction deploy domain.

    Returns the SQLite path; the CLI driver opens its own runtime over
    the ambient home, so the seed has to be in the home the fixture
    controls — the same store the driver will build, opened once here to
    write the events the reflection stage will read.
    """
    from paa.config import get_settings
    from paa.ledger.store import LedgerStore
    from paa.storage.relational.database import Database
    from tests.improve.conftest import seed_high_friction

    db = Database(get_settings().storage.sqlite_path)
    await db.connect()
    try:
        await seed_high_friction(LedgerStore(db))
    finally:
        await db.close()
    return get_settings().storage.sqlite_path


def _scripted(decision: str) -> Any:
    """An approver seam that answers without a terminal, both ways."""
    from paa.improve.gate import HumanDecision

    async def answer(proposal: object, verdict: object) -> HumanDecision:
        return HumanDecision(
            decision=decision,
            approver_kind="human",
            approver_id="cli-test-approver",
            note=f"scripted {decision}",
        )

    return answer


@pytest.fixture
def approve(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "paa.improve.gate.terminal_approver", lambda **_: _scripted("approved")
    )


@pytest.fixture
def refuse(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "paa.improve.gate.terminal_approver", lambda **_: _scripted("rejected")
    )


def _gate(argv: list[str]) -> str:
    """One gate command as the CLI's ``--gate`` text, shell-quoted."""
    import shlex

    return " ".join(shlex.quote(part) for part in argv)


class TestTheHelpAndTheDoor:
    def test_help_names_the_five_stages_and_the_gates(self) -> None:
        result = runner.invoke(app, ["improve", "--help"])
        assert result.exit_code == 0
        assert "guarded self-improvement cycle" in result.stdout
        assert "--gate" in _plain(result.stdout)
        assert "--repo" in _plain(result.stdout)

    def test_a_missing_repo_refuses_at_the_door(self) -> None:
        result = runner.invoke(
            app, ["improve", "--repo", "/nonexistent/repo", "--gate", _gate(_GREEN)]
        )
        assert result.exit_code == 2
        assert "was not found" in result.stdout

    def test_no_gate_commands_refuse_at_the_door(self, repo: Path) -> None:
        result = runner.invoke(app, ["improve", "--repo", str(repo)])
        assert result.exit_code == 2
        assert "no validation gate commands" in result.stdout

    def test_an_unparseable_gate_refuses_at_the_door(self, repo: Path) -> None:
        result = runner.invoke(
            app, ["improve", "--repo", str(repo), "--gate", "'unterminated"]
        )
        assert result.exit_code == 2
        assert "does not shell-split" in result.stdout


class TestTheCycle:
    def test_the_happy_path_lands_and_verifies(
        self, repo: Path, seeded_ledger: Path, approve: None, tmp_path: Path
    ) -> None:
        from tests.improve.conftest import head_of

        seed_head = head_of(repo)
        result = runner.invoke(
            app,
            [
                "improve", "--repo", str(repo),
                "--gate", _gate(_GREEN),
                "--insecure-fallback-signing",
            ],
        )
        assert result.exit_code == 0
        assert "completed" in result.stdout
        assert "bundle:" in result.stdout
        assert "stopped: committed" in result.stdout
        assert "correlation:" in result.stdout

        # The improvement is real: the playbook grew the managed fence,
        # HEAD moved, and the commit subject names the attested bundle.
        playbook = (repo / "playbooks.md").read_text(encoding="utf-8")
        assert "managed:improve-reflection-loop begin" in playbook
        assert head_of(repo) != seed_head
        correlation = next(
            line.split("correlation:")[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("correlation:")
        )
        # The printed id is the lineage UUID (dashed); the artifact
        # directory is its hex — the same id, two spellings.
        run_dir = tmp_path / "home" / "improve" / uuid.UUID(correlation).hex
        assert (run_dir / "proposal.json").is_file()
        assert (run_dir / "attest-bundle" / "manifest.json").is_file()

    def test_a_red_gate_stops_the_cycle_with_the_refusal_recorded(
        self, repo: Path, seeded_ledger: Path, approve: None
    ) -> None:
        from tests.improve.conftest import head_of

        seed_head = head_of(repo)
        result = runner.invoke(
            app,
            [
                "improve", "--repo", str(repo),
                "--gate", _gate(_RED),
                "--insecure-fallback-signing",
            ],
        )
        assert result.exit_code == 1
        assert "stopped: validation_failed" in result.stdout
        # Nothing landed, no bundle was built, the proposal is recorded.
        assert (repo / "playbooks.md").read_text(encoding="utf-8") == (
            "# ops playbooks\n"
        )
        assert head_of(repo) == seed_head

    def test_a_refusing_human_stops_the_cycle(
        self, repo: Path, seeded_ledger: Path, refuse: None
    ) -> None:
        from tests.improve.conftest import head_of

        seed_head = head_of(repo)
        result = runner.invoke(
            app,
            [
                "improve", "--repo", str(repo),
                "--gate", _gate(_GREEN),
                "--insecure-fallback-signing",
            ],
        )
        assert result.exit_code == 1
        assert "stopped: human_gate_refused" in result.stdout
        assert head_of(repo) == seed_head

    def test_the_json_flag_prints_the_metrics(
        self, repo: Path, seeded_ledger: Path, approve: None
    ) -> None:
        import json

        result = runner.invoke(
            app,
            [
                "improve", "--repo", str(repo),
                "--gate", _gate(_GREEN),
                "--insecure-fallback-signing", "--json",
            ],
        )
        assert result.exit_code == 0
        # ``console.print_json`` wraps the JSON in Rich markup-free output;
        # the metrics are the last JSON object printed, parsed from the
        # first ``{`` onwards.
        start = result.stdout.index("{")
        metrics = json.loads(result.stdout[start:])
        assert metrics["stopped_reason"] == "committed"
        assert metrics["git_commit"]


class TestTheRegistryDoor:
    def test_the_pipeline_door_drives_the_same_cycle(
        self, repo: Path, seeded_ledger: Path, approve: None
    ) -> None:
        """``paa pipeline run improve.reflection-loop`` is the same driver.

        The observe pattern's claim, asserted: the registry's door journals
        the same lifecycle and stops with the same reason the direct verb
        does, and the goal argument is the repository. The registry door
        carries no insecure-signing fallback by design (see
        ``docs/modules/improve.md`` — the shared runner never grows a flag
        that lowers an attestation guarantee), so the commit stage attests
        with the real backend and the test needs the same extra the attest
        suite needs, skipping the same way on a shape without it.
        """
        pytest.importorskip(
            "cryptography", reason="the registry door commits via real Ed25519"
        )
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "improve.reflection-loop",
                "--goal", str(repo),
                "--gate", _gate(_GREEN),
            ],
        )
        assert result.exit_code == 0
        assert "completed" in result.stdout
        assert "stopped: committed" in result.stdout
        assert (repo / "playbooks.md").read_text(encoding="utf-8") != (
            "# ops playbooks\n"
        )

    def test_the_gate_option_is_refused_for_a_foreign_pipeline(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--gate", _gate(_GREEN),
            ],
        )
        assert result.exit_code == 1
        assert "the improve loop's validation gate list" in result.stdout
        assert "builtin.chat" in result.stdout

    def test_the_pipeline_door_without_gates_refuses(
        self, repo: Path, seeded_ledger: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "improve.reflection-loop",
                "--goal", str(repo),
            ],
        )
        assert result.exit_code == 2
        assert "no validation gate commands" in result.stdout

    def test_the_shared_runner_offers_no_insecure_signing_flag(self) -> None:
        """Door parity, pinned: the registry door keeps the strict default.

        ``paa improve`` carries ``--insecure-fallback-signing``; the shared
        ``pipeline run`` surface deliberately does not — a flag whose only
        effect is to lower an attestation guarantee belongs on the
        operator's direct door, not on the one surface every pipeline runs
        through. This pins the help text so the asymmetry cannot drift
        back into silence (the decision is recorded in
        ``docs/modules/improve.md``).
        """
        result = runner.invoke(app, ["pipeline", "run", "--help"])
        assert result.exit_code == 0
        assert "--gate" in _plain(result.stdout)  # the improve-only option IS named
        assert "insecure" not in _plain(result.stdout)  # the fallback flag is NOT
