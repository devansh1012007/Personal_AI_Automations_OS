"""One headless smoke test for the chat TUI: it mounts and it turns.

The full Pilot suite is its own task; this file pins the two properties every
other chat-TUI test leans on — the app mounts under Textual's test harness
with the offline echo model, and a submitted prompt produces one real turn
(user block, streamed assistant block, session lineage on the ledger). If
either of those breaks, every deeper interaction test would fail for the same
root cause, so one test says so cheaply.
"""

from __future__ import annotations

import time
from pathlib import Path

from paa.chat.loop import EchoChatModel
from paa.chat.session import CHAT_EVENT_TYPES
from paa.config import ChatSettings
from paa.core.types import PermissionMode
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database
from paa.tui.chat.app import ChatApp
from paa.tui.chat.wiring import ChatWiring

_TURN_TIMEOUT_SECONDS = 10.0


async def test_chat_app_mounts_and_processes_one_turn(
    paa_home: Path, db: Database, ledger: LedgerStore, tmp_path: Path
) -> None:
    """Mount headless, submit one prompt, see the echo reply — one turn."""
    wiring = ChatWiring.from_parts(
        ledger=ledger,
        db=db,
        creator=MemoryCreator(db),
        settings=ChatSettings(),
        signals=SignalRepository(db, ContentAddressedStore(tmp_path / "cold")),
        home=paa_home,
        permission_mode=PermissionMode.ASK,
        model=EchoChatModel(),
    )
    app = ChatApp(wiring)
    async with app.run_test() as pilot:
        # The ChatScreen mount worker creates the session; poll for the
        # lineage it writes rather than sleeping past the loop.
        deadline = time.monotonic() + _TURN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await pilot.pause()
            if await wiring.service.list_sessions():
                break
        session_id = app.session_id  # raises if the mount setup never ran

        chat = app.chat_screen
        assert chat is not None
        chat.prompt.focus()
        await pilot.pause()
        await pilot.press(*"hello memory")
        await pilot.press("enter")

        # One echo turn: the reply repeats the message verbatim. Wait for it
        # through the pilot's own pump rather than sleeping past the loop.
        deadline = time.monotonic() + _TURN_TIMEOUT_SECONDS
        messages: list[tuple[str, str]] = []
        while time.monotonic() < deadline:
            await pilot.pause()
            chat_screen = app.chat_screen
            if chat_screen is not None:
                messages = chat_screen.stream.messages()
                if any(
                    role == "assistant" and text.strip() for role, text in messages
                ):
                    break

        assert ("user", "hello memory") in messages
        assert ("assistant", "hello memory") in messages

        # The turn is ledgered, not just rendered: the session lineage has
        # chat event rows (STARTED at least, PROPOSED/COMPLETED for the turn).
        events = await ledger.read_correlation(session_id)
        assert any(event.event_type in CHAT_EVENT_TYPES for event in events)
