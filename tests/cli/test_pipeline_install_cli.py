"""The ``paa pipeline install`` door, driven through Typer's CliRunner (C8.6).

The same discipline ``tests/cli/test_skills_cli.py`` applies to the skills
door: a real ``Runtime`` built against an isolated ``PAA_HOME``, the offline
echo provider and the subprocess sandbox, so the install really runs the five
gates, the conformance standard and the review prompt — no doubles. What
these tests pin is the operator-facing contract: exit codes, the output lines
a human reads (the review included — the ask and its v1 limits are on the
surface the decision is made on), and the zero-trace refusal.

The git sources are local repositories created in ``tmp_path`` — no network,
the brief's own rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

_DECLARATION = """\
[pipeline]
name = "demo.echo"
version = "1.2.3"
protocol = "paa-pipeline/1"
description = "A fixture pipeline that echoes its goal."
requires_runtime = ">=4.1,<5"

[run]
resume = "journal"

[evidence]
declares_tier = "unrated"

[capabilities]
think = { roles = ["worker"] }

[limits]
max_tokens = 100000
max_wall_s = 600
max_calls = 100
max_sub_runs = 1
"""

_ENTRYPOINT = """\
import json, sys

args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
goal = args.get("goal") or ""
print(json.dumps({
    "status": "completed",
    "summary": "echo: " + goal,
    "metrics": {},
}))
"""


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every install-door test gets a throwaway home + offline stack."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    # Rich sizes its tables to the terminal; the assertions are about
    # content, not wrapping.
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    monkeypatch.setenv("PAA_OBSERVABILITY__LOG_LEVEL", "WARNING")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    yield home
    reset_settings_cache()


def _package_dir(
    root: Path,
    *,
    declaration: str = _DECLARATION,
    entrypoint: str = _ENTRYPOINT,
) -> Path:
    source = root / "pkg"
    source.mkdir(parents=True)
    (source / "pipeline.toml").write_text(declaration, encoding="utf-8")
    (source / "pipeline.py").write_text(entrypoint, encoding="utf-8")
    return source


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _git_repo(root: Path) -> tuple[Path, str]:
    """A local git repository whose HEAD is the fixture package."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "pipeline.toml").write_text(_DECLARATION, encoding="utf-8")
    (repo / "pipeline.py").write_text(_ENTRYPOINT, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "the fixture package")
    return repo, _git(repo, "rev-parse", "HEAD")


class TestTheDoorItself:
    def test_help_names_the_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "install", "--help"])
        assert result.exit_code == 0
        assert "pinned" in result.stdout
        assert "capability review" in result.stdout


class TestLocalDirectoryInstall:
    def test_an_approved_install_stages_and_registers(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", str(source)],
            input="y\n",
        )

        assert result.exit_code == 0, result.stdout
        assert "installed demo.echo v1.2.3" in result.stdout
        # The gates are named in the operator's face — token by token,
        # because the line wraps at the terminal width CliRunner pins.
        for gate in (
            "signature", "content_hash", "ast_scan", "permissions", "smoke_test"
        ):
            assert gate in result.stdout
        # The review the human approved is on the surface too — the ask,
        # and the honest v1 limit beside it.
        assert "capabilities asked" in result.stdout
        assert "NOT minted in v1" in result.stdout
        # The gated bytes (and the review artifact) are staged.
        staged = _isolated_home / "pipelines" / "installed" / "demo.echo" / "1.2.3"
        assert (staged / "pipeline.toml").is_file()
        assert (staged / "pipeline.py").is_file()
        assert (staged / "capability-review.json").is_file()

        # Staging is registration: the registry's own door reads the row.
        listed = runner.invoke(app, ["pipeline", "list"])
        assert listed.exit_code == 0, listed.stdout
        assert "demo.echo" in listed.stdout
        assert "installed" in listed.stdout

    def test_a_declined_review_refuses_with_zero_trace(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", str(source)],
            input="n\n",
        )

        assert result.exit_code == 1
        assert "refused demo.echo at the capability_review stage" in result.stdout
        assert "refused by human:cli" in result.stdout
        staged = _isolated_home / "pipelines" / "installed" / "demo.echo"
        assert not staged.exists()
        listed = runner.invoke(app, ["pipeline", "list"])
        assert "demo.echo" not in listed.stdout

    def test_no_opt_in_refuses_the_unsigned_source(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)

        result = runner.invoke(app, ["pipeline", "install", str(source)], input="y\n")

        assert result.exit_code == 1
        assert "refused demo.echo at the signature stage" in result.stdout
        assert "unsigned" in result.stdout

    def test_a_gate_refusal_names_the_gate(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(
            tmp_path, entrypoint="import os\nos.system('rm -rf /')\n"
        )

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", str(source)],
            input="y\n",
        )

        assert result.exit_code == 1
        assert "refused demo.echo at the ast_scan stage" in result.stdout
        assert "AST scan" in result.stdout


class TestGitInstall:
    def test_a_pinned_sha_installs(self, tmp_path: Path, _isolated_home: Path) -> None:
        repo, sha = _git_repo(tmp_path)

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", f"git+{repo.as_uri()}",
             "--ref", sha],
            input="y\n",
        )

        assert result.exit_code == 0, result.stdout
        assert "installed demo.echo v1.2.3" in result.stdout
        assert "git_pinned" in result.stdout
        assert sha[:12] in result.stdout

    def test_a_moving_branch_is_refused(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        repo, _ = _git_repo(tmp_path)

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", f"git+{repo.as_uri()}",
             "--ref", "main"],
            input="y\n",
        )

        assert result.exit_code == 1
        # The refusal lands before the declaration is read, so the name is
        # the source's own basename — the honest best-known name, never a
        # fabrication.
        assert "refused repo at the source stage" in result.stdout
        assert "moving branch" in result.stdout

    def test_a_missing_ref_is_refused(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        repo, _ = _git_repo(tmp_path)

        result = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", f"git+{repo.as_uri()}"],
            input="y\n",
        )

        assert result.exit_code == 1
        assert "at the source stage" in result.stdout
        assert "pinned ref" in result.stdout
