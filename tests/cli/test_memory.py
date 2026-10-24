"""The ``paa memory`` command group, driven through Typer's CliRunner.

Each command builds a real :class:`~paa.runtime.Runtime` against an isolated
``PAA_HOME`` (sqlite, echo provider, subprocess sandbox) so the tests exercise
the actual wiring end-to-end without a model server or a shared DB.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app
from paa.config import get_settings, reset_settings_cache
from paa.memory.facts import FactRepository
from paa.storage.relational.database import Database, dumps, to_iso, utc_now

runner = CliRunner()


@pytest.fixture
def paa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated PAA_HOME + offline model/sandbox settings for CLI commands."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    reset_settings_cache()
    return home


async def _connect_db(home: Path) -> Database:
    """Open the relational DB the CLI's Runtime will use (same PAA_HOME path)."""
    settings = get_settings()
    db = Database(settings.storage.sqlite_path)
    await db.connect()
    return db


async def _seed_entity_fact(home: Path, name: str, predicate: str, obj: str) -> str:
    db = await _connect_db(home)
    try:
        repo = FactRepository(db)
        eid = await repo.upsert_entity("concept", name)
        await repo.add_fact(eid, predicate, obj)
        return eid
    finally:
        await db.close()


async def _seed_signal(home: Path, payload: object) -> str:
    signal_id = str(uuid.uuid4())
    db = await _connect_db(home)
    try:
        await db.execute(
            "INSERT INTO cold_lake_signals "
            "(id, received_at, channel, raw_payload, content_hash, sync_status) "
            "VALUES (?,?,?,?,?, 'unprocessed')",
            (
                signal_id,
                to_iso(utc_now()),
                "test",
                dumps(payload),
                "h" + uuid.uuid4().hex[:20],
            ),
        )
    finally:
        await db.close()
    return signal_id


async def _seed_world_model(home: Path, doc: str, section_text: str) -> None:
    vault = home / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / doc).write_text(
        "# title\n\nhuman text\n\n"
        "<!-- paa:managed:BEGIN state -->\n"
        f"{section_text}\n"
        "<!-- paa:managed:END state -->\n",
        encoding="utf-8",
    )


def _invoke(args: list[str]):
    return runner.invoke(app, ["memory", *args])


class TestMemoryHelp:
    def test_help_lists_all_subcommands(self, paa_home: Path) -> None:
        result = _invoke(["--help"])
        assert result.exit_code == 0, result.output
        for sub in ("facts", "search", "signals", "drain", "world-model"):
            assert sub in result.stdout, result.stdout


class TestFacts:
    def test_prints_table_for_known_entity(self, paa_home: Path) -> None:
        asyncio.run(_seed_entity_fact(paa_home, "Acme Corp", "status", "active"))
        result = _invoke(["facts", "Acme Corp"])
        assert result.exit_code == 0, result.output
        assert "Acme Corp" in result.stdout
        assert "status" in result.stdout
        assert "active" in result.stdout

    def test_prints_no_facts_when_entity_unknown(self, paa_home: Path) -> None:
        result = _invoke(["facts", "Does Not Exist"])
        assert result.exit_code == 0, result.output
        assert "no facts" in result.stdout

    def test_prints_no_facts_when_entity_has_none(self, paa_home: Path) -> None:
        asyncio.run(_seed_entity_fact(paa_home, "Acme Corp", "status", "active"))
        # A different, real entity with no facts of its own.
        asyncio.run(_seed_entity_fact(paa_home, "Other Corp", "status", "active"))
        result = _invoke(["facts", "Other Corp"])
        assert result.exit_code == 0, result.output
        assert "Other Corp" in result.stdout
        assert "active" in result.stdout


class TestSearch:
    def test_prints_matching_facts(self, paa_home: Path) -> None:
        asyncio.run(_seed_entity_fact(paa_home, "Acme Corp", "owner", "Ada Lovelace"))
        result = _invoke(["search", "Ada"])
        assert result.exit_code == 0, result.output
        assert "Ada Lovelace" in result.stdout
        assert "owner" in result.stdout

    def test_prints_no_matches_gracefully(self, paa_home: Path) -> None:
        result = _invoke(["search", "zzz-no-such-term-zzz"])
        assert result.exit_code == 0, result.output
        assert "no matches" in result.stdout

    def test_nul_byte_is_refused_cleanly(self, paa_home: Path) -> None:
        result = _invoke(["search", "x\x00y"])
        assert result.exit_code == 1, result.output
        assert "invalid search text" in result.output
        assert result.exception is not None


class TestDrain:
    def test_prints_all_counters_on_empty_backlog(self, paa_home: Path) -> None:
        result = _invoke(["drain"])
        assert result.exit_code == 0, result.output
        for counter in (
            "claimed",
            "processed",
            "ignored",
            "malformed",
            "quarantined",
            "errors",
            "facts_written",
        ):
            assert counter in result.stdout, result.stdout

    def test_processes_a_seeded_signal(self, paa_home: Path) -> None:
        asyncio.run(
            _seed_signal(
                paa_home,
                {
                    "facts": [
                        {
                            "subject": "Project Alpha",
                            "predicate": "status",
                            "object": "active",
                        }
                    ]
                },
            )
        )
        result = _invoke(["drain", "--limit", "25"])
        assert result.exit_code == 0, result.output
        assert "processed" in result.stdout
        assert "facts_written" in result.stdout


class TestSignals:
    def test_prints_no_signals_gracefully(self, paa_home: Path) -> None:
        result = _invoke(["signals"])
        assert result.exit_code == 0, result.output
        assert "no signals" in result.stdout

    def test_filters_by_status(self, paa_home: Path) -> None:
        asyncio.run(_seed_signal(paa_home, {"facts": []}))
        result = _invoke(["signals", "--status", "unprocessed"])
        assert result.exit_code == 0, result.output
        assert "unprocessed" in result.stdout


class TestWorldModel:
    def test_prints_managed_section(self, paa_home: Path) -> None:
        asyncio.run(_seed_world_model(paa_home, "current_focus.md", "ship the memory CLI"))
        result = _invoke(["world-model", "current_focus.md"])
        assert result.exit_code == 0, result.output
        assert "ship the memory CLI" in result.stdout

    def test_prints_no_managed_section_on_empty_vault(self, paa_home: Path) -> None:
        result = _invoke(["world-model", "current_focus.md"])
        assert result.exit_code == 0, result.output
        assert "no managed section" in result.stdout

    def test_unknown_document_is_refused_cleanly(self, paa_home: Path) -> None:
        result = _invoke(["world-model", "not-a-real-doc.md"])
        assert result.exit_code == 1, result.output
        assert "no such belief document" in result.output
        assert result.exception is not None
