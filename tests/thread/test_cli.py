"""``paa thread`` through Typer's CliRunner (C7.5) — the front doors.

The machine doors (``--json``) are the ones asserted on: they print the
same facts the engine holds, so the tests read the CLI's output the way an
operator's script would. The verbs run against a real Runtime — real
ledger over a throwaway home, the real subprocess sandbox — with the
offline echo provider, exactly the wiring the pipeline CLI tests use, so
what is pinned here is the wiring, not a mock of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from paa.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every thread-CLI test gets a throwaway home + offline providers."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()


def _new_thread(tmp_path: Path) -> dict:
    """Create one thread over a fresh workspace; returns the JSON door's reply."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    result = runner.invoke(
        app,
        ["thread", "new", "the cli thread", "--workspace", str(workspace),
         "--harness", "fake-alpha", "--json"],
    )
    assert result.exit_code == 0, result.output
    return _json_of(result)


def _json_of(result: Result) -> dict:
    """Parse the JSON a ``--json`` verb printed.

    The observe CLI tests' own rule, hardened one step: structlog's info
    lines share the stream and one of them can even *start* with a bracket
    (``[info     ] …``), so the payload is the first bracket-leading line
    from which the whole remainder parses as JSON — the noise is the
    runtime's, the JSON is the verb's, and the verb speaks last.
    """
    lines = result.output.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith(("{", "[")):
            continue
        try:
            return json.loads("\n".join(lines[index:]))
        except json.JSONDecodeError:
            continue
    raise AssertionError(f"no JSON payload in CLI output:\n{result.output}")


class TestNew:
    def test_new_prints_the_thread_and_its_mandate(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        assert record["title"] == "the cli thread"
        assert record["status"] == "active"
        assert record["default_harness"] == "fake-alpha"
        assert record["mandate"].startswith("basis=explicit")
        assert record["id"]

    def test_new_writes_the_record_under_the_home(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        home = tmp_path / "home"
        assert (home / "threads" / f"{record['id']}.json").is_file()

    def test_the_human_door_names_the_ledger_walk(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = runner.invoke(
            app,
            ["thread", "new", "human door", "--workspace", str(workspace)],
        )
        assert result.exit_code == 0, result.stdout
        assert "thread:" in result.stdout
        assert "paa ledger " in result.stdout

    def test_new_refuses_a_blank_title(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["thread", "new", "   "])
        assert result.exit_code == 2
        assert "refused" in result.stdout


class TestListAndShow:
    def test_list_shows_the_created_thread(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        result = runner.invoke(app, ["thread", "list", "--json"])
        assert result.exit_code == 0
        listed = _json_of(result)
        assert [t["id"] for t in listed] == [record["id"]]
        assert listed[0]["segments"] == 0

    def test_list_with_no_threads_says_so(self) -> None:
        result = runner.invoke(app, ["thread", "list"])
        assert result.exit_code == 0
        assert "no threads" in result.stdout

    def test_show_prints_the_full_record(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        result = runner.invoke(app, ["thread", "show", record["id"], "--json"])
        assert result.exit_code == 0
        shown = _json_of(result)
        assert shown["schema"] == "paa-thread/1"
        assert shown["id"] == record["id"]
        assert shown["segments"] == []

    def test_show_refuses_an_unknown_thread(self) -> None:
        result = runner.invoke(app, ["thread", "show", "nope"])
        assert result.exit_code == 2
        assert "refused" in result.stdout


class TestRunAndSwap:
    def test_run_then_swap_through_the_cli(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        workspace = tmp_path / "ws"

        run = runner.invoke(
            app, ["thread", "run", record["id"], "--json", "--timeout-s", "60"]
        )
        assert run.exit_code == 0, run.output
        segment = _json_of(run)
        assert segment["harness"] == "fake-alpha"
        assert segment["exit_code"] == 0

        swap = runner.invoke(
            app,
            ["thread", "swap", record["id"], "--harness", "fake-beta",
             "--json", "--timeout-s", "60"],
        )
        assert swap.exit_code == 0, swap.output
        swapped = _json_of(swap)
        assert swapped["harness"] == "fake-beta"
        assert swapped["thread_id"] == record["id"]

        # The show door now renders both stints and their packet hashes.
        shown = _json_of(runner.invoke(app, ["thread", "show", record["id"], "--json"]))
        assert [s["harness"] for s in shown["segments"]] == ["fake-alpha", "fake-beta"]
        assert all(len(s["packet_sha256"]) == 64 for s in shown["segments"])

        # The workspace carries the swap's own evidence: beta continued alpha.
        notes = (workspace / "thread-notes.md").read_text(encoding="utf-8")
        assert "fake-alpha:" in notes
        assert "fake-beta:" in notes and "1 note line" in notes

    def test_swap_refuses_an_unknown_harness(self, tmp_path: Path) -> None:
        record = _new_thread(tmp_path)
        result = runner.invoke(
            app, ["thread", "swap", record["id"], "--harness", "cursor"]
        )
        assert result.exit_code == 2
        assert "refused" in result.stdout

    def test_run_refuses_an_unknown_thread(self) -> None:
        result = runner.invoke(app, ["thread", "run", "nope", "--json"])
        assert result.exit_code == 2
