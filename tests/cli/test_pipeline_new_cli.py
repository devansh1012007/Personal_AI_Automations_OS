"""``paa pipeline new`` through Typer's CliRunner (P7, W9-C9.5).

The same throwaway-home discipline ``tests/cli/test_pipeline.py`` applies
to the registry doors: an isolated ``PAA_HOME``, a wide ``COLUMNS`` (Rich
wraps tokens to the runner's narrow default — assertions are about
content, not wrapping), and the offline echo provider. What these tests
prove is the door as a door: the exit contract (0 created, 1 refused
with the reason named, 2 usage), the default target (the registry's
local dir, so ``list`` sees the row), ``--path`` and ``--force``, and
the help surface an author reads first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every scaffold-CLI test gets a throwaway home + wide Rich columns."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "220")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    return home


class TestTheExitContract:
    def test_created_is_exit_0_and_lists_the_files(self, home: Path) -> None:
        result = runner.invoke(app, ["pipeline", "new", "my-loop"])
        assert result.exit_code == 0
        assert "scaffolded my-loop" in result.stdout
        for filename in ("pipeline.toml", "pipeline.py", "README.md", "test_pipeline.py"):
            assert filename in result.stdout
        assert (home / "pipelines" / "my-loop" / "pipeline.toml").is_file()

    def test_refused_is_exit_1_with_the_reason(self, home: Path) -> None:
        runner.invoke(app, ["pipeline", "new", "my-loop"])
        again = runner.invoke(app, ["pipeline", "new", "my-loop"])
        assert again.exit_code == 1
        assert "scaffold refused" in again.stdout
        assert "already exists" in again.stdout
        assert "holds" in again.stdout  # the refusal names what is there

    def test_usage_is_exit_2(self) -> None:
        # Typer's usage errors (a missing argument) exit 2 and write the
        # usage text to stderr — the door keeps the standard contract
        # rather than re-implementing it with a custom message.
        missing_name = runner.invoke(app, ["pipeline", "new"])
        assert missing_name.exit_code == 2
        assert "Missing argument" in missing_name.stderr
        assert "Usage: paa pipeline new" in missing_name.stderr

    def test_the_reserved_name_refuses_at_the_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "new", "installed"])
        assert result.exit_code == 1
        assert "reserved" in result.stdout

    def test_an_unscaffoldable_name_refuses_at_the_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "new", "../escape"])
        assert result.exit_code == 1
        assert "not scaffoldable" in result.stdout


class TestTheDefaultTarget:
    def test_list_sees_the_row_the_moment_it_lands(self, home: Path) -> None:
        created = runner.invoke(app, ["pipeline", "new", "my-loop"])
        assert created.exit_code == 0

        listing = runner.invoke(app, ["pipeline", "list"])
        assert listing.exit_code == 0
        assert "my-loop" in listing.stdout
        assert "local" in listing.stdout

    def test_inspect_reads_the_scaffolded_declaration(self, home: Path) -> None:
        runner.invoke(app, ["pipeline", "new", "my-loop"])
        result = runner.invoke(app, ["pipeline", "inspect", "my-loop"])
        assert result.exit_code == 0
        assert "my-loop" in result.stdout
        assert "unrated" in result.stdout
        assert "journal" in result.stdout

    def test_the_next_steps_name_the_doors(self, home: Path) -> None:
        result = runner.invoke(app, ["pipeline", "new", "my-loop"])
        assert result.exit_code == 0
        assert "paa pipeline list" in result.stdout
        assert "paa pipeline install" in result.stdout


class TestPathAndForce:
    def test_path_scaffolds_elsewhere(self, tmp_path: Path, home: Path) -> None:
        elsewhere = tmp_path / "repo"
        result = runner.invoke(
            app, ["pipeline", "new", "my-loop", "--path", str(elsewhere)]
        )
        assert result.exit_code == 0
        assert (elsewhere / "my-loop" / "pipeline.toml").is_file()
        # And the local dir does NOT see it: the registry reads one dir.
        listing = runner.invoke(app, ["pipeline", "list"])
        assert "my-loop" not in listing.stdout

    def test_force_overwrites_the_scaffolds_own_files(self, home: Path) -> None:
        runner.invoke(app, ["pipeline", "new", "my-loop"])
        target = home / "pipelines" / "my-loop"
        (target / "pipeline.py").write_text("# the author's edits", encoding="utf-8")

        forced = runner.invoke(app, ["pipeline", "new", "my-loop", "--force"])
        assert forced.exit_code == 0
        entrypoint = (target / "pipeline.py").read_text(encoding="utf-8")
        assert "# the author's edits" not in entrypoint
        assert "THE CONTRACT" in entrypoint

    def test_force_refuses_a_foreign_pipeline(self, home: Path) -> None:
        runner.invoke(app, ["pipeline", "new", "my-loop"])
        target = home / "pipelines" / "my-loop"
        (target / "pipeline.toml").write_text(
            '[pipeline]\nname = "someone-else"\nversion = "1.0.0"\n'
            'protocol = "paa-pipeline/1"\nrequires_runtime = ">=4.1,<5"\n\n'
            "[limits]\nmax_tokens = 1\nmax_wall_s = 1\nmax_calls = 1\n"
            "max_sub_runs = 1\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["pipeline", "new", "my-loop", "--force"])
        assert result.exit_code == 1
        assert "someone-else" in result.stdout


class TestHelp:
    def test_the_help_reads_as_the_authoring_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "new", "--help"])
        assert result.exit_code == 0
        assert "Scaffold a new pipeline" in result.stdout
        assert "--path" in result.stdout
        assert "--force" in result.stdout

    def test_the_pipeline_group_lists_new(self) -> None:
        result = runner.invoke(app, ["pipeline", "--help"])
        assert result.exit_code == 0
        assert "new" in result.stdout
