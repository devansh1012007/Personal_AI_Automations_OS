"""P-013 weekly reflection wiring (T14 + INV-05 Fix A).

The reflection engine is implemented and tested, but nothing schedules it, so
the RFC "once a week" claim is false until the daemon fires it. These tests pin
the W3-delegated Fix A: a supervised daemon 4th loop gated by
``ReflectionSettings.enabled``, a file-based last-run marker under ``PAA_HOME``
(no ``REFLECTION_*`` EventType — inventing one would make this SURFACE task
DEEP), and ``/doctor`` rows (CLI + TUI mirror) reporting
``reflection.last_run_age_days`` and due/disabled state.

Loop timing is driven by an injected ``now`` plus the state file — no real
sleeps. Reversal: reverting the daemon loop / helpers / doctor rows makes
every test below fail (no marker, no row, no single-append guarantee).
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from paa.cli.main import app
from paa.config import Settings, reset_settings_cache
from paa.core.types import PermissionMode
from paa.daemon import Daemon
from paa.improvement.reflection import (
    format_reflection_health,
    read_reflection_last_run,
    reflection_health,
    write_reflection_last_run,
)
from paa.runtime import Runtime
from paa.storage.relational.database import utc_now
from paa.tui.dashboard import render_doctor

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


class _ScriptedModel:
    """Stand-in model so Runtime.build never touches a provider."""

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        return {}


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real Runtime against an isolated tmp_path SQLite DB, no backends."""
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=_ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


async def _seed_high_friction(rt: Runtime) -> None:
    """One deploy task with a correction + a rollback: friction 4.5, rule written."""
    from paa.core.types import EventType, new_correlation_id
    from paa.ledger.events import LedgerEvent

    cid = new_correlation_id()
    await rt.ledger.append(
        LedgerEvent.create(
            cid, EventType.TASK_REQUESTED, payload={"request": {"goal": "deploy the service"}}
        )
    )
    await rt.ledger.append(LedgerEvent.create(cid, EventType.USER_CORRECTION))
    await rt.ledger.append(
        LedgerEvent.create(cid, EventType.STATE_ROLLBACK_TRIGGERED, discriminator="r1")
    )


def _playbook(rt: Runtime) -> Path:
    return Path(rt.settings.storage.vault_path) / "playbooks.md"


def _anti_pattern_count(rt: Runtime) -> int:
    path = _playbook(rt)
    if not path.exists():
        return 0
    return path.read_text(encoding="utf-8").count("Anti-pattern: deploy")


# ---------------------------------------------------------------------------
# Last-run marker helpers (pure, fixed clocks)
# ---------------------------------------------------------------------------


class TestLastRunMarker:
    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        assert read_reflection_last_run(tmp_path) == T0
        payload = json.loads((tmp_path / "reflection_last_run.json").read_text(encoding="utf-8"))
        assert payload["last_run_at"] == T0.astimezone(UTC).isoformat()

    def test_second_write_wins(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        write_reflection_last_run(tmp_path, T0 + DAY)
        assert read_reflection_last_run(tmp_path) == T0 + DAY

    def test_missing_file_is_unknown_not_a_crash(self, tmp_path: Path) -> None:
        assert read_reflection_last_run(tmp_path) is None
        health = reflection_health(tmp_path, enabled=True, now=T0)
        assert health["last_run_age_days"] is None
        assert health["due"] is True
        assert health["stale"] is False
        assert format_reflection_health(health) == "unknown (due)"

    def test_corrupt_file_is_unknown_not_a_crash(self, tmp_path: Path) -> None:
        (tmp_path / "reflection_last_run.json").write_text("not json{{{", encoding="utf-8")
        assert read_reflection_last_run(tmp_path) is None
        health = reflection_health(tmp_path, enabled=True, now=T0)
        assert health["due"] is True
        assert format_reflection_health(health) == "unknown (due)"

    @pytest.mark.parametrize(
        "content",
        [
            '{"last_run_at": 123}',
            '{"other": "2026-01-01T00:00:00+00:00"}',
            '{"last_run_at": "bogus"}',
            "[]",
        ],
    )
    def test_wrong_shaped_file_is_unknown(self, tmp_path: Path, content: str) -> None:
        (tmp_path / "reflection_last_run.json").write_text(content, encoding="utf-8")
        assert read_reflection_last_run(tmp_path) is None
        assert reflection_health(tmp_path, enabled=True, now=T0)["due"] is True

    def test_naive_now_is_treated_as_utc(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        health = reflection_health(tmp_path, enabled=True, now=datetime(2026, 1, 2))
        assert health["last_run_age_days"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T14: staleness + 7d boundary pair
# ---------------------------------------------------------------------------


class TestT14Staleness:
    def test_due_eight_days_ago_reports_stale(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        health = reflection_health(tmp_path, enabled=True, now=T0 + 8 * DAY)
        assert health["stale"] is True
        assert health["due"] is True
        assert health["last_run_age_days"] == pytest.approx(8.0)
        assert format_reflection_health(health) == "8.0d (stale)"

    def test_exactly_seven_days_is_not_stale(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        health = reflection_health(tmp_path, enabled=True, now=T0 + 7 * DAY)
        assert health["last_run_age_days"] == pytest.approx(7.0)
        assert health["stale"] is False
        assert health["due"] is False
        assert format_reflection_health(health) == "7.0d"

    def test_seven_days_plus_one_second_is_stale(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        health = reflection_health(tmp_path, enabled=True, now=T0 + 7 * DAY + timedelta(seconds=1))
        assert health["last_run_age_days"] > 7.0
        assert health["stale"] is True
        assert health["due"] is True
        assert "(stale)" in format_reflection_health(health)

    def test_disabled_is_never_due_but_keeps_age(self, tmp_path: Path) -> None:
        write_reflection_last_run(tmp_path, T0)
        health = reflection_health(tmp_path, enabled=False, now=T0 + 30 * DAY)
        assert health["due"] is False
        assert health["last_run_age_days"] == pytest.approx(30.0)
        assert format_reflection_health(health) == "disabled"


# ---------------------------------------------------------------------------
# Daemon tick: due gating, run clears, concurrency, failure + retry
# ---------------------------------------------------------------------------


class TestReflectionTick:
    async def test_due_run_clears_staleness(self, runtime: Runtime) -> None:
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        stale = reflection_health(runtime.settings.home, enabled=True, now=now)
        assert stale["stale"] is True

        ran = await Daemon(runtime)._run_reflection_tick(now=now)

        assert ran is True
        fresh = reflection_health(runtime.settings.home, enabled=True, now=now)
        assert fresh["last_run_age_days"] is not None
        assert fresh["last_run_age_days"] <= 7.0
        assert fresh["due"] is False
        assert _anti_pattern_count(runtime) == 1

    async def test_not_due_tick_is_a_noop(self, runtime: Runtime) -> None:
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - DAY)

        ran = await Daemon(runtime)._run_reflection_tick(now=now)

        assert ran is False
        assert _anti_pattern_count(runtime) == 0
        assert read_reflection_last_run(runtime.settings.home) == now - DAY

    async def test_first_boot_with_no_marker_is_due_once(self, runtime: Runtime) -> None:
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        assert read_reflection_last_run(runtime.settings.home) is None

        ran = await Daemon(runtime)._run_reflection_tick(now=now)

        assert ran is True
        assert _anti_pattern_count(runtime) == 1

    async def test_two_ticks_append_once(self, runtime: Runtime) -> None:
        """Concurrency/idempotency: the due gate, not a rebuild, sheds the second tick."""
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        daemon = Daemon(runtime)

        first = await daemon._run_reflection_tick(now=now)
        second = await daemon._run_reflection_tick(now=now)

        assert first is True
        assert second is False
        assert _anti_pattern_count(runtime) == 1

    async def test_empty_ledger_tick_runs_without_playbook_write(
        self, runtime: Runtime
    ) -> None:
        """Hostile empty ledger: the pass still runs + marks, writes no playbook, no crash."""
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        assert _playbook(runtime).exists() is False

        ran = await Daemon(runtime)._run_reflection_tick(now=now)

        assert ran is True
        assert _playbook(runtime).exists() is False
        assert reflection_health(runtime.settings.home, enabled=True, now=now)["due"] is False

    async def test_failed_tick_writes_no_marker_and_retry_succeeds_once(
        self, runtime: Runtime, tmp_path: Path
    ) -> None:
        """Hostile vault (a file where the dir must be): tick fails, no marker, retry lands once."""
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        real_vault = Path(runtime.settings.storage.vault_path)
        blocker = tmp_path / "vault_is_a_file"
        blocker.write_text("not a directory", encoding="utf-8")
        runtime.settings.storage.vault_path = blocker
        daemon = Daemon(runtime)

        with pytest.raises(OSError):
            await daemon._run_reflection_tick(now=now)
        # A crash/failure must not mark the run done: the next tick retries.
        assert read_reflection_last_run(runtime.settings.home) == now - 8 * DAY

        runtime.settings.storage.vault_path = real_vault
        ran = await daemon._run_reflection_tick(now=now)

        assert ran is True
        assert _anti_pattern_count(runtime) == 1
        fresh = reflection_health(runtime.settings.home, enabled=True, now=now)
        assert fresh["due"] is False

    async def test_disabled_tick_never_fires_but_engine_stays_callable(
        self, runtime: Runtime
    ) -> None:
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = False
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        health = reflection_health(runtime.settings.home, enabled=False, now=now)
        assert format_reflection_health(health) == "disabled"

        ran = await Daemon(runtime)._run_reflection_tick(now=now)

        assert ran is False
        assert _anti_pattern_count(runtime) == 0
        assert read_reflection_last_run(runtime.settings.home) == now - 8 * DAY

        # Flag-off observability is not engine-off: a manual run still works.
        from paa.improvement.reflection import ReflectionEngine

        report = await ReflectionEngine(
            runtime.ledger, vault_path=runtime.settings.storage.vault_path
        ).run_weekly(now=now)
        assert report.rules_written
        assert _anti_pattern_count(runtime) == 1

    async def test_tick_waits_for_the_curation_lock(self, runtime: Runtime) -> None:
        """Ordering: a reflection tick never runs parallel to a curator prune tick."""
        await _seed_high_friction(runtime)
        runtime.settings.reflection.enabled = True
        now = utc_now()
        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        daemon = Daemon(runtime)
        done = asyncio.Event()

        async def _tick() -> bool:
            result = await daemon._run_reflection_tick(now=now)
            done.set()
            return result

        await daemon._curation_lock.acquire()
        try:
            task = asyncio.create_task(_tick())
            await asyncio.sleep(0)  # yield so the tick reaches the lock acquisition
            assert task.done() is False
            assert done.is_set() is False
        finally:
            daemon._curation_lock.release()
        result = await asyncio.wait_for(task, timeout=10)

        assert result is True
        assert _anti_pattern_count(runtime) == 1

    async def test_start_launches_five_supervised_loops(self, runtime: Runtime) -> None:
        daemon = Daemon(runtime)
        await daemon.start()
        try:
            assert len(daemon._tasks) == 5  # 5th loop = allocator tick (W5 T-007b)
        finally:
            await daemon.stop()

    async def test_delay_polls_when_disabled_or_due_and_sleeps_until_due_otherwise(
        self, runtime: Runtime
    ) -> None:
        now = utc_now()
        daemon = Daemon(runtime, reflection_poll_interval=123.0)
        writing = write_reflection_last_run(runtime.settings.home, now - DAY)

        runtime.settings.reflection.enabled = True
        assert writing is not None
        remaining = daemon._reflection_delay_seconds(now=now)
        assert remaining == pytest.approx(6 * 86400, abs=120)

        write_reflection_last_run(runtime.settings.home, now - 8 * DAY)
        assert daemon._reflection_delay_seconds(now=now) == 123.0

        runtime.settings.reflection.enabled = False
        assert daemon._reflection_delay_seconds(now=now) == 123.0


# ---------------------------------------------------------------------------
# Hostile: empty home — health is read-only, no vault write, no crash
# ---------------------------------------------------------------------------


class TestHostileEmptyHome:
    def test_health_on_empty_home_produces_due_unknown_without_writing(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "empty_home"
        home.mkdir()
        health = reflection_health(home, enabled=True, now=T0)
        assert health["due"] is True
        assert health["last_run_age_days"] is None
        assert format_reflection_health(health) == "unknown (due)"
        # `/doctor` must never write: no marker, no vault, no playbook.
        assert list(home.iterdir()) == []


# ---------------------------------------------------------------------------
# TUI mirror: same label, same value strings as the CLI health format
# ---------------------------------------------------------------------------


def _render_doctor(info: dict[str, Any]) -> str:
    buf = io.StringIO()
    Console(file=buf, width=100, force_terminal=False).print(render_doctor(info))
    return buf.getvalue()


class TestTuiDoctorMirror:
    def test_stale_row_matches_cli_format(self) -> None:
        out = _render_doctor(
            {
                "home": "/tmp/paa",
                "policy_mode": "ask",
                "relational": "sqlite",
                "sandbox": None,
                "open_tasks": [],
                "reflection_enabled": True,
                "reflection_last_run_age_days": 8.0,
                "reflection_stale": True,
            }
        )
        assert "reflection.last_run_age_days" in out
        assert "8.0d (stale)" in out

    def test_disabled_and_unknown_rows(self) -> None:
        disabled = _render_doctor(
            {
                "home": "/tmp/paa",
                "policy_mode": "ask",
                "relational": "sqlite",
                "sandbox": None,
                "open_tasks": [],
                "reflection_enabled": False,
            }
        )
        assert "reflection.last_run_age_days" in disabled
        assert "disabled" in disabled

        unknown = _render_doctor(
            {
                "home": "/tmp/paa",
                "policy_mode": "ask",
                "relational": "sqlite",
                "sandbox": None,
                "open_tasks": [],
                "reflection_enabled": True,
                "reflection_last_run_age_days": None,
            }
        )
        assert "unknown (due)" in unknown

    def test_missing_reflection_keys_do_not_crash(self) -> None:
        out = _render_doctor(
            {
                "home": "/tmp/paa",
                "policy_mode": "ask",
                "relational": "sqlite",
                "sandbox": None,
                "open_tasks": [],
            }
        )
        assert "reflection.last_run_age_days" in out


# ---------------------------------------------------------------------------
# CLI /doctor rows (real Runtime through CliRunner, like tests/cli/test_cli.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    reset_settings_cache()
    return home


@pytest.mark.slow
class TestCliDoctorRows:
    def test_doctor_reports_stale_when_enabled_and_old(
        self, _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PAA_REFLECTION__ENABLED", "true")
        reset_settings_cache()
        write_reflection_last_run(_isolated_home, utc_now() - 8 * DAY)
        result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "reflection.last_run_age_days" in result.stdout
        assert "stale" in result.stdout

    def test_doctor_reports_disabled_by_default(self, _isolated_home: Path) -> None:
        result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "reflection.last_run_age_days" in result.stdout
        assert "disabled" in result.stdout
