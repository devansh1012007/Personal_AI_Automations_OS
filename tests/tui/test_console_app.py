"""The Loop Console's Textual half, driven by the pilot (C8.5).

The service suite (:mod:`tests.tui.test_console_service`) pins the
console's textual-free seam — every read and action delegating to the
surfaces that already decide. This file pins the half the operator
actually touches: the screen, its keys, and the flows rendered on it,
driven through Textual's pilot exactly as ``tests/tui/test_chat_app.py``
drives the chat surface:

* **the registry panel** is populated from the real pipeline registry —
  the table's rows are the registry's own, compared row-for-row, with
  trust, version and the evidence-tier badge visible per row;
* **install** runs the real five-gate flow from the console's own modal
  (source typed, candidate highlighted, Enter), and a gate refusal
  renders as a refusal with zero trace;
* **activate** flips a staged row live through the ``a`` key;
* **fork** drives the pinned mechanism through its modal — the written
  overlay loads against the real declaration, and a taken name is
  refused in place, in the modal, without closing it;
* **conformance** runs the six-check standard from the ``c`` key and
  renders the report's own per-check verdicts, on a ledger the
  operator's never sees written;
* the two doors to the screen — the ``ctrl+b`` binding and the
  ``paa chat --console`` start flag — each pinned by opening through it.

No doubles: the production wiring (real ledger, real database, the
subprocess sandbox the smoke gate needs), a tmp_path home, the offline
echo model, and the greeter fixture skill the extension-surface suites
install.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import DataTable, Input, ListView, Static

from paa.chat.loop import EchoChatModel
from paa.config import ChatSettings
from paa.core.types import EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.pipelines.conformance import CHECK_NAMES
from paa.pipelines.overlay import load_overlay
from paa.pipelines.registry import PipelineRegistry
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database
from paa.tui.chat.app import ChatApp
from paa.tui.chat.console import FORKS_DIR_NAME
from paa.tui.chat.screens import (
    ConformanceModal,
    ConsoleScreen,
    ForkNameModal,
    InstallSourceModal,
)
from paa.tui.chat.wiring import ChatWiring

if TYPE_CHECKING:
    from textual.testing.pilot import Pilot

#: Generous on purpose: the install flows run the smoke gate's
#: entrypoint as a real subprocess under the sandbox, and this box's two
#: cores are shared. The waits are polled, never slept.
_TIMEOUT_SECONDS = 30.0

# -- the fixture skill (the shape tests/chat/test_extensions.py installs) ----

_ENTRYPOINT = """\
import json, sys
args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
print(json.dumps({"greeting": "hello " + args.get("name", "world")}))
"""

_SKILL_MD = """\
---
name: greeter
description: Greets whoever is named in the arguments, in one short line.
version: 1.0.0
permissions: [PERM_SANDBOX_RUN]
input_schema: {"type": "object", "properties": {"name": {"type": "string"}}}
output_schema:
  type: object
  properties:
    greeting:
      type: string
  required: [greeting]
---
You are a greeter. Say hello to the named person.
"""


def _make_hub(root: Path, *, entrypoint: str = _ENTRYPOINT) -> Path:
    """A Claw-Hub-shaped source: a directory of skill directories."""
    skill_dir = root / "hub" / "greeter"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (skill_dir / "main.py").write_text(entrypoint, encoding="utf-8")
    return root / "hub"


def _wiring(
    ledger: LedgerStore,
    db: Database,
    home: Path,
    tmp_path: Path,
    *,
    start_on_console: bool = False,
) -> tuple[ChatApp, ChatWiring]:
    """The production app over the production wiring — plus the wiring."""
    wiring = ChatWiring.from_parts(
        ledger=ledger,
        db=db,
        creator=MemoryCreator(db),
        settings=ChatSettings(),
        signals=SignalRepository(db, ContentAddressedStore(tmp_path / "cold")),
        home=home,
        permission_mode=PermissionMode.ASK,
        model=EchoChatModel(),
        sandbox=SubprocessSandbox(),
    )
    return ChatApp(wiring, start_on_console=start_on_console), wiring


def _static_text(widget: Static) -> str:
    """A Static's current text, plain — the screens update with rich Text."""
    content = widget.content
    return content.plain if isinstance(content, Text) else str(content)


def _cell(table: DataTable, row: str, column: str) -> str:
    """One cell's text, plain — cells may hold rich Text (the name column)."""
    row_key = next(key for key in table.rows if key.value == row)
    column_key = next(key for key in table.columns if key.value == column)
    value: object = table.get_cell(row_key, column_key)
    return value.plain if isinstance(value, Text) else str(value)


async def _wait_for(
    pilot: Pilot, predicate: Callable[[], bool], what: str
) -> None:
    """Pump the pilot until the predicate holds, or name what never did."""
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
    raise AssertionError(f"timed out waiting for {what}")


async def _open_console(pilot: Pilot) -> ConsoleScreen:
    """Ctrl+B from the chat screen — the binding every test opens through."""
    await pilot.press("ctrl+b")
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, ConsoleScreen)
    # The mount refresh populates the panel; the registry's six builtins
    # are the contract here (a local manifest joins the same rows).
    table = screen.query_one("#console-pipelines", DataTable)
    await _wait_for(pilot, lambda: table.row_count > 0, "the registry panel")
    return screen


async def _ledger_count(db: Database, kind: EventType) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM system_state_ledger WHERE event_type = ?",
        (kind.value,),
    )
    assert row is not None
    return int(row["n"])


class TestTheDoorsToTheScreen:
    """The two ways in: the key binding, and the ``--console`` start flag."""

    async def test_ctrl_b_opens_the_console_over_the_chat(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)
            # The chat screen stays underneath — Esc climbs back to it.
            assert any(s is not screen for s in pilot.app.screen_stack)
            title = _static_text(screen.query_one("#console-title", Static))
            assert "LOOPS" in title

    async def test_the_console_start_flag_opens_it_first(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """``paa chat --console`` — loop management first, chat underneath."""
        app, _ = _wiring(ledger, db, paa_home, tmp_path, start_on_console=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, ConsoleScreen)
            table = screen.query_one("#console-pipelines", DataTable)
            await _wait_for(pilot, lambda: table.row_count > 0, "the registry panel")


class TestTheRegistryPanel:
    """The panel reads the registry — never a parallel hand-maintained list."""

    async def test_the_rows_are_the_registrys_own_with_badges(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)
            table = screen.query_one("#console-pipelines", DataTable)

            # Row-for-row the registry's own: same names, same order, no
            # parallel list to drift from it.
            registry = PipelineRegistry(local_dir=paa_home / "pipelines")
            rows = [key.value for key in table.rows]
            assert rows == [row.name for row in registry.list()]
            assert "builtin.chat" in rows
            assert "builtin.observe" in rows

            # Trust, tier and version visible per row — the declared
            # facts, restated by the registry, not by the screen.
            chat = registry.get("builtin.chat")
            assert chat is not None
            assert _cell(table, "builtin.chat", "version") == chat.version
            assert _cell(table, "builtin.chat", "trust") == chat.trust
            assert _cell(table, "builtin.chat", "runnable") == "yes"
            # The evidence-tier badge: bracketed tier, per row.
            assert (
                _cell(table, "builtin.chat", "evidence")
                == f"[{chat.declaration.evidence_tier}]"
            )
            assert _cell(table, "builtin.observe", "evidence") == "[T1]"

            # The extensions panel starts honest: no rows, no invention.
            skills = screen.query_one("#console-skills", DataTable)
            assert skills.row_count == 0


class TestInstallThroughTheGates:
    """``d`` — the install flow: source, candidate, Enter, five real gates."""

    async def test_install_from_a_source_passes_the_five_gates_and_lands(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        hub = _make_hub(tmp_path)
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)

            await pilot.press("d")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, InstallSourceModal)
            source = modal.query_one("#install-source", Input)
            source.focus()
            await pilot.pause()
            await pilot.press(*str(hub))
            await pilot.press("enter")

            # Discovery is a worker: poll for the candidate it found.
            listing = modal.query_one("#install-found", ListView)
            await _wait_for(
                pilot, lambda: len(listing.children) == 1, "the discovered skill"
            )
            assert listing.index == 0  # highlighted: Enter is the promise
            await pilot.press("enter")

            # The install rides the real gates (the smoke gate runs the
            # entrypoint under the sandbox) and renders its outcome.
            status = screen.query_one("#console-status", Static)
            await _wait_for(
                pilot,
                lambda: "installed greeter" in _static_text(status),
                "the install's outcome line",
            )
            line = _static_text(status)
            assert line.startswith("✓ installed greeter through the gates")
            for gate in (
                "signature",
                "content_hash",
                "ast_scan",
                "permissions",
                "smoke_test",
            ):
                assert gate in line
            assert "EXTENSION_INSTALLED recorded" in line

            # The extensions panel merged the row: live, signed state
            # and provider from the two registries of truth.
            skills = screen.query_one("#console-skills", DataTable)
            await _wait_for(
                pilot, lambda: skills.row_count == 1, "the extensions row"
            )
            assert _cell(skills, "greeter", "version") == "1.0.0"
            assert _cell(skills, "greeter", "state") == "live"
            assert _cell(skills, "greeter", "signature") == "unverified"
            assert _cell(skills, "greeter", "provider") == "claw_hub"

            # And it really happened: staged bytes, ledger lineage.
            staged = paa_home / "skills" / "installed" / "greeter" / "1.0.0"
            assert (staged / "SKILL.md").is_file()
            assert (staged / "main.py").is_file()
            assert await _ledger_count(db, EventType.EXTENSION_INSTALLED) == 1
            assert await _ledger_count(db, EventType.EXTENSION_ACTIVATED) == 1

    async def test_a_gate_refusal_renders_in_place_with_zero_trace(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        hub = _make_hub(
            tmp_path, entrypoint="import os\nos.system('rm -rf /')\n"
        )
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)

            await pilot.press("d")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, InstallSourceModal)
            source = modal.query_one("#install-source", Input)
            source.focus()
            await pilot.pause()
            await pilot.press(*str(hub))
            await pilot.press("enter")
            listing = modal.query_one("#install-found", ListView)
            await _wait_for(
                pilot, lambda: len(listing.children) == 1, "the discovered skill"
            )
            await pilot.press("enter")

            # The AST gate's own refusal, rendered as one — never
            # retried around, never weakened.
            status = screen.query_one("#console-status", Static)
            await _wait_for(
                pilot,
                lambda: "refused greeter" in _static_text(status),
                "the refusal line",
            )
            line = _static_text(status)
            assert line.startswith("✗ refused greeter at the ast_scan gate")
            assert "nothing landed" in line

            # Zero trace: no panel row, no staged bytes, no package row.
            skills = screen.query_one("#console-skills", DataTable)
            assert skills.row_count == 0
            assert not (paa_home / "skills" / "installed" / "greeter").exists()
            packages = await db.fetch_all(
                "SELECT package_name FROM marketplace_packages"
            )
            assert list(packages) == []
            assert await _ledger_count(db, EventType.EXTENSION_INSTALLED) == 0


class TestActivate:
    """``a`` — the extensions table's own two cases, driven by key."""

    async def test_activate_flips_a_staged_row_live(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        hub = _make_hub(tmp_path)
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        # A staged install through the same service the screen drives —
        # the real gates, activation deliberately withheld.
        discovered = await app.console_service.discover(hub)
        report = await app.console_service.install(
            discovered[0], allow_unverified=True, activate=False
        )
        assert report.installed and report.activation is None

        async with app.run_test() as pilot:
            screen = await _open_console(pilot)
            skills = screen.query_one("#console-skills", DataTable)
            await _wait_for(
                pilot, lambda: skills.row_count == 1, "the staged row"
            )
            assert _cell(skills, "greeter", "state") == "staged"

            skills.focus()
            await pilot.pause()
            await pilot.press("a")

            status = screen.query_one("#console-status", Static)
            await _wait_for(
                pilot,
                lambda: "activated greeter v1.0.0" in _static_text(status),
                "the activation's outcome line",
            )
            assert "EXTENSION_ACTIVATED recorded" in _static_text(status)
            await _wait_for(
                pilot,
                lambda: _cell(skills, "greeter", "state") == "live",
                "the row flipping live",
            )
            assert await _ledger_count(db, EventType.EXTENSION_ACTIVATED) == 1


class TestFork:
    """``f`` — the pinned mechanism, driven through its name modal."""

    async def test_fork_seeds_a_loading_overlay_and_lists_it_ok(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)
            table = screen.query_one("#console-pipelines", DataTable)

            # Walk the cursor onto builtin.chat — the table's own row
            # order decides how many downs, not this test.
            target = [key.value for key in table.rows].index("builtin.chat")
            for _ in range(target):
                await pilot.press("down")
            await pilot.pause()

            await pilot.press("f")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, ForkNameModal)
            assert modal.pipeline == "builtin.chat"
            name_input = modal.query_one("#fork-name", Input)
            name_input.focus()
            await pilot.pause()
            await pilot.press(*"my-chat")
            await pilot.press("enter")

            # The worker writes the overlay and dismisses the modal.
            status = screen.query_one("#console-status", Static)
            await _wait_for(
                pilot,
                lambda: "forked →" in _static_text(status),
                "the fork's outcome line",
            )
            assert isinstance(pilot.app.screen, ConsoleScreen)

            # The pinned mechanism: a personal overlay under
            # PAA_HOME/pipelines/overlays/<source>/<name>.toml that the
            # tighten-only loader loads against the real declaration.
            fork_path = (
                paa_home
                / "pipelines"
                / FORKS_DIR_NAME
                / "builtin.chat"
                / "my-chat.toml"
            )
            assert fork_path.is_file()
            chat = PipelineRegistry(
                local_dir=paa_home / "pipelines"
            ).get("builtin.chat")
            assert chat is not None
            load_overlay(fork_path, chat.declaration)  # loads, unmodified

            # The panel lists it with its load verdict, and the outcome
            # line names the command that runs it.
            forks = _static_text(screen.query_one("#console-forks", Static))
            assert "my-chat ← builtin.chat (ok)" in forks
            assert "paa pipeline run builtin.chat --overlay" in _static_text(
                status
            )

    async def test_a_taken_fork_name_is_refused_in_the_open_modal(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        app, _ = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)

            await pilot.press("f")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, ForkNameModal)
            name_input = modal.query_one("#fork-name", Input)
            name_input.focus()
            await pilot.pause()
            await pilot.press(*"taken")
            await pilot.press("enter")
            status = screen.query_one("#console-status", Static)
            await _wait_for(
                pilot,
                lambda: "forked →" in _static_text(status),
                "the first fork's outcome line",
            )

            # The same name again: refused in place — the modal stays
            # open and names the refusal, so the operator fixes the
            # name instead of starting the flow over.
            await pilot.press("f")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, ForkNameModal)
            name_input = modal.query_one("#fork-name", Input)
            name_input.focus()
            await pilot.pause()
            await pilot.press(*"taken")
            await pilot.press("enter")
            help_line = modal.query_one("#fork-help", Static)
            await _wait_for(
                pilot,
                lambda: "already exists" in _static_text(help_line),
                "the taken-name refusal in the modal",
            )
            assert pilot.app.screen is modal


class TestConformanceRun:
    """``c`` — the six-check standard, rendered with per-check verdicts."""

    async def test_conformance_renders_every_check_verdict_on_a_scratch_ledger(
        self, paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        app, wiring = _wiring(ledger, db, paa_home, tmp_path)
        async with app.run_test() as pilot:
            screen = await _open_console(pilot)

            # The session the chat screen opened must be on the ledger
            # before the count is taken, or the comparison below would
            # race its mount worker.
            deadline = time.monotonic() + _TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await pilot.pause()
                if await wiring.service.list_sessions():
                    break
            assert await wiring.service.list_sessions(), (
                "the chat session's lineage never landed on the ledger"
            )
            before = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM system_state_ledger"
            )
            assert before is not None

            # The cursor sits on the registry's first row — the
            # plan-execute-critique pipeline, a runnable with a runner.
            table = screen.query_one("#console-pipelines", DataTable)
            assert table.row_count > 0
            await pilot.press("c")

            await _wait_for(
                pilot,
                lambda: isinstance(pilot.app.screen, ConformanceModal),
                "the conformance report",
            )
            report_screen = pilot.app.screen
            assert isinstance(report_screen, ConformanceModal)

            # The report page is the standard's own renderer: title
            # verdict plus one line per check, each with its own mark.
            title = _static_text(report_screen.query_one("#conf-title", Static))
            assert title == "CONFORMANCE — builtin.plan-execute-critique: GREEN"
            page = _static_text(report_screen.query_one("#conf-page", Static))
            verdicts = [
                line for line in page.splitlines() if line.startswith("  [")
            ]
            assert len(verdicts) == len(CHECK_NAMES)
            for check in CHECK_NAMES:
                assert any(check in line for line in verdicts)
            # And the verdicts differ per check, honestly: the scripted
            # sequence-equality check passes, and the halt-family
            # checks the runner does not exercise read ``-`` (n/a, the
            # reason named) — never a faked green.
            assert "  [  ok] double-run-sequence-equality" in page
            assert "  [   -] divergence-halt" in page

            # The run was a diagnostic: the operator's ledger kept only
            # that operator's work — the scripted check runs went to a
            # scratch database that is closed by now.
            after = await db.fetch_one("SELECT COUNT(*) AS n FROM system_state_ledger")
            assert after is not None
            assert int(after["n"]) == int(before["n"])
