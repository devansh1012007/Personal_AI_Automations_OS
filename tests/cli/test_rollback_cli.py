"""The ``paa rollback`` verb, driven through Typer's CliRunner (C9.2).

The service's own behaviour is pinned by ``tests/effects``; what these
tests pin is the **door**: the exit contract the operator's shell sees
(0 rolled back / 1 nothing-or-not-found / 2 usage), the ``--json``
machine surface, and the refusal wording. The seeding writes real
effect rows into the isolated home's real ledger and real cold-lake
store — the same files the CLI's own ``Runtime`` then opens — so the
verb is exercised end-to-end over the same substrates a deployment
uses, not over fakes.

Help assertions read through ``_plain()`` (ANSI stripped, line breaks
removed): the runner's Rich-styled help folds long tokens across lines
— the measured W8-CI2/C13 class, fourth instance — and the assertions
are about content, not rendering.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

#: Rich escape sequences, stripped before content assertions (the
#: ``tests/coder/test_agent.py`` precedent).
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(output: str) -> str:
    """Rendered CLI output as content: no escape sequences, no line breaks."""
    return _ANSI.sub("", output).replace("\r", "").replace("\n", "")


def _invoke(args: list[str]) -> Any:
    """Run the CLI with structlog captured, so stdout is the command's alone.

    Under the runner the runtime's structlog renderers print into the
    same redirected stdout the assertions read — a JSON report would
    arrive prefixed by log lines. ``capture_logs`` is the suite's own
    convention for exactly this; the rollback path logs nothing it
    depends on, so capturing is safe.
    """
    import structlog

    with structlog.testing.capture_logs():
        return runner.invoke(app, args)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every rollback CLI test gets a throwaway home + offline echo provider."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()


def _seed_file_scope(workspace: Path) -> uuid.UUID:
    """Journal one modification + one creation into the home's own ledger.

    Writes through the real ``CallJournal`` against the database and the
    cold-lake store the CLI's runtime will open — the prior bytes must
    land in *that* store, because the rollback fetches them through the
    store it builds itself. Returns the scope lineage's id.
    """
    workspace.mkdir(parents=True, exist_ok=True)

    async def _seed() -> uuid.UUID:
        from paa.config import get_settings
        from paa.effects.files import capture_prior
        from paa.effects.spec import EffectKind, EffectSpec
        from paa.ledger.store import LedgerStore
        from paa.pipelines.journal import CallJournal
        from paa.storage.coldlake import get_blob_store
        from paa.storage.relational import get_database

        settings = get_settings()
        settings.ensure_directories()
        db = get_database(settings)
        await db.connect()
        try:
            ledger = LedgerStore(db)
            blobs = get_blob_store(settings.storage)
            run_id = uuid.uuid4()
            journal = CallJournal(ledger, run_id, blob_store=blobs)
            await journal.run_started("demo.writer", "1.0.0")

            # A modification: prior bytes exist, so compensating restores.
            (workspace / "notes.txt").write_bytes(b"prior notes")
            restore = capture_prior(workspace, "notes.txt", blobs)
            await journal.call_proposed(1, "fs.write", {"path": "notes.txt"})
            (workspace / "notes.txt").write_bytes(b"scope's notes")
            await journal.call_completed(
                1,
                "fs.write",
                {"written": "notes.txt"},
                effect=EffectSpec.for_call(run_id, 1, EffectKind.FILE_EDIT, restore),
            )

            # A creation: no prior bytes, so compensating removes.
            (workspace / "out").mkdir(exist_ok=True)
            remove = capture_prior(workspace, "out/created.txt", blobs)
            await journal.call_proposed(2, "fs.write", {"path": "out/created.txt"})
            (workspace / "out" / "created.txt").write_bytes(b"created by scope")
            await journal.call_completed(
                2,
                "fs.write",
                {"written": "out/created.txt"},
                effect=EffectSpec.for_call(run_id, 2, EffectKind.FILE_WRITE, remove),
            )
            return run_id
        finally:
            await db.close()

    return asyncio.run(_seed())


def _seed_irreversible_scope() -> uuid.UUID:
    """Journal one external call recorded as the irreversible marker."""

    async def _seed() -> uuid.UUID:
        from paa.config import get_settings
        from paa.effects.spec import EffectKind, EffectSpec
        from paa.ledger.store import LedgerStore
        from paa.pipelines.journal import CallJournal
        from paa.storage.relational import get_database

        settings = get_settings()
        settings.ensure_directories()
        db = get_database(settings)
        await db.connect()
        try:
            ledger = LedgerStore(db)
            run_id = uuid.uuid4()
            journal = CallJournal(ledger, run_id)
            await journal.run_started("demo.mailer", "1.0.0")
            await journal.call_proposed(1, "mail.send", {"to": "world"})
            effect = EffectSpec.for_call(run_id, 1, EffectKind.IRREVERSIBLE)
            await journal.call_completed(1, "mail.send", {"sent": True}, effect=effect)
            return run_id
        finally:
            await db.close()

    return asyncio.run(_seed())


class TestTheHelp:
    def test_the_verb_names_its_flags(self) -> None:
        result = _invoke(["rollback", "--help"])

        assert result.exit_code == 0
        plain = _plain(result.stdout)
        assert "--workspace" in plain
        assert "--json" in plain

    def test_the_top_level_help_lists_rollback(self) -> None:
        result = _invoke(["--help"])

        assert result.exit_code == 0
        assert "rollback" in _plain(result.stdout)


class TestUsageErrors:
    def test_a_scope_that_is_not_an_id_exits_two(self) -> None:
        """A scope that cannot name a lineage is a usage error, not a miss.

        The check runs before the runtime is built — a validation that
        happens after it has already paid for the thing it was meant to
        prevent (the ``tests/coder/test_agent.py`` rule).
        """
        result = _invoke(["rollback", "not-a-uuid"])

        assert result.exit_code == 2
        assert "not a task or lineage id" in result.stdout

    def test_compensable_effects_without_a_workspace_refuse(self, tmp_path: Path) -> None:
        scope = _seed_file_scope(tmp_path / "ws")

        result = _invoke(["rollback", str(scope)])

        # The named refusal is a usage error: the operator can act on it
        # by naming the jail their effects live under.
        assert result.exit_code == 2
        assert "rollback refused" in result.stdout
        assert "--workspace" in result.stdout


class TestNotFound:
    def test_an_unknown_scope_exits_one(self) -> None:
        result = _invoke(["rollback", str(uuid.uuid4())])

        assert result.exit_code == 1
        assert "no such task or lineage" in result.stdout


class TestTheWalk:
    def test_file_effects_are_settled_and_reported(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        scope = _seed_file_scope(ws)

        result = _invoke(["rollback", str(scope), "--workspace", str(ws)])

        assert result.exit_code == 0
        # The world: the modification's prior bytes are back, the
        # creation is gone — nothing the scope did to files survives.
        assert (ws / "notes.txt").read_bytes() == b"prior notes"
        assert not (ws / "out" / "created.txt").exists()
        plain = _plain(result.stdout)
        assert "2 compensated, 0 already, 0 irreversible, 0 failed" in plain
        assert "rollback lineage" in plain

    def test_a_second_run_is_an_honest_no_op(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        scope = _seed_file_scope(ws)
        first = _invoke(["rollback", str(scope), "--workspace", str(ws)])
        assert first.exit_code == 0

        second = _invoke(
            ["rollback", str(scope), "--workspace", str(ws), "--json"]
        )

        # Nothing rolled back THIS run: exit 1, every effect reported as
        # already settled — the CLI maps the report, it does not restate
        # it. Read through --json because the human table *truncates* the
        # status cell (Rich's ellipsis overflow), and the assertion is
        # about the vocabulary, not the rendering.
        assert second.exit_code == 1
        report: dict[str, Any] = json.loads(second.stdout)
        assert report["summary"] == {
            "effects": 2,
            "compensated": 0,
            "already_compensated": 2,
            "irreversible": 0,
            "failed": 0,
        }
        assert {entry["status"] for entry in report["entries"]} == {
            "already_compensated"
        }
        # The workspace is untouched by the second run.
        assert (ws / "notes.txt").read_bytes() == b"prior notes"
        assert not (ws / "out" / "created.txt").exists()

    def test_the_json_report_is_machine_readable(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        scope = _seed_file_scope(ws)

        result = _invoke(
            ["rollback", str(scope), "--workspace", str(ws), "--json"]
        )

        assert result.exit_code == 0
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["scope"] == str(scope)
        assert report["summary"] == {
            "effects": 2,
            "compensated": 2,
            "already_compensated": 0,
            "irreversible": 0,
            "failed": 0,
        }
        assert {entry["status"] for entry in report["entries"]} == {"compensated"}
        # The irreversible contract travels in the JSON too: an owner
        # call is named for every entry, so a report consumer never has
        # to guess which journalled call an effect rode.
        for entry in report["entries"]:
            assert entry["owner_call"].endswith(("/call:1", "/call:2"))


class TestIrreversibleBoundary:
    def test_an_irreversible_only_scope_rolls_back_nothing_and_says_so(
        self, tmp_path: Path
    ) -> None:
        scope = _seed_irreversible_scope()

        result = _invoke(["rollback", str(scope), "--json"])

        # Nothing was compensable, so nothing rolled back: exit 1, with
        # the irreversible effect reported rather than "fixed" — a
        # report that overstated recovery would be worse than none.
        # The detail and owner call are asserted through --json: the
        # human table truncates long cells, and this contract is about
        # the words, not the rendering.
        assert result.exit_code == 1
        report: dict[str, Any] = json.loads(result.stdout)
        assert report["summary"]["irreversible"] == 1
        (entry,) = report["entries"]
        assert entry["status"] == "irreversible"
        assert entry["owner_call"] == f"{scope}/call:1"
        assert "no compensation exists" in entry["detail"]
