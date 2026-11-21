"""Fixtures for the chat suite.

One harness per test, wired the way :meth:`paa.chat.service.ChatService.from_runtime`
wires it minus the runtime: a real SQLite database, a real ledger, a real
MemoryCreator, and a real cold-lake SignalRepository over a local
ContentAddressedStore. Chat is exercised end to end, offline — the model is
the ``echo`` seam, so replies are deterministic and the two-turn recall proof
in ``test_loop`` is byte-stable.

Nothing here reads process settings: the service and every facade are built
with explicit ``ContextSettings``, so no test depends on the environment or
the ``get_settings`` cache.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from paa.chat.loop import ChatModel, ChatTurnFacade
from paa.chat.service import ChatService
from paa.chat.session import ChatSessionJournal
from paa.config import ChatSettings, ContextSettings
from paa.core.types import PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database


@dataclass(frozen=True, slots=True)
class ChatHarness:
    """Everything one chat test needs, wired like the service wires it."""

    service: ChatService
    ledger: LedgerStore
    db: Database
    signals: SignalRepository
    settings: ChatSettings
    context_settings: ContextSettings
    creator: MemoryCreator

    async def events(self, session_id: Any) -> list[LedgerEvent]:
        """One session's lineage, in ledger order — the fold's input."""
        return await self.ledger.read_correlation(session_id)

    async def kinds(self, session_id: Any) -> list[str]:
        """One session's event-type values, in ledger order."""
        return [e.event_type.value for e in await self.events(session_id)]

    def facade_for(
        self,
        session_id: Any,
        turn_id: str = "facade-turn",
        *,
        settings: ChatSettings | None = None,
    ) -> ChatTurnFacade:
        """A host-minted facade, for capability-level tests (fence, recall)."""
        return ChatTurnFacade(
            session_id=session_id,
            turn_id=turn_id,
            journal=ChatSessionJournal(self.ledger, session_id),
            ledger=self.ledger,
            model=self.service._model,
            creator=self.creator,
            facts=self.service._facts,
            db=self.db,
            settings=settings if settings is not None else self.settings,
            permission_mode=PermissionMode.ASK,
            signals=self.signals,
            context_settings=self.context_settings,
        )


@pytest.fixture
def chat(
    tmp_path: Path, db: Database, ledger: LedgerStore
) -> Callable[..., ChatHarness]:
    """Build a chat harness; every argument is overridable per test."""

    def build(
        *,
        settings: ChatSettings | None = None,
        model: ChatModel | None = None,
        creator: MemoryCreator | None = None,
        signals: SignalRepository | None = None,
    ) -> ChatHarness:
        resolved_settings = settings if settings is not None else ChatSettings()
        context_settings = ContextSettings()
        resolved_creator = creator if creator is not None else MemoryCreator(db)
        resolved_signals = (
            signals
            if signals is not None
            else SignalRepository(db, ContentAddressedStore(tmp_path / "cold"))
        )
        service = ChatService(
            ledger=ledger,
            db=db,
            creator=resolved_creator,
            settings=resolved_settings,
            permission_mode=PermissionMode.ASK,
            context_settings=context_settings,
            model=model,
            signals=resolved_signals,
        )
        return ChatHarness(
            service=service,
            ledger=ledger,
            db=db,
            signals=resolved_signals,
            settings=resolved_settings,
            context_settings=context_settings,
            creator=resolved_creator,
        )

    return build
