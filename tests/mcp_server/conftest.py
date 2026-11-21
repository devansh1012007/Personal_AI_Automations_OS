"""Fixtures for the MCP-server suite.

One harness per test, wired like :meth:`paa.mcp.server.MCPServer.from_runtime`
wires it minus the runtime — the same shape ``tests/chat/conftest.py`` uses,
plus the server itself. Everything is real (SQLite database, ledger,
MemoryCreator, cold-lake SignalRepository over a local CAS); the model is the
deterministic ``echo`` seam, so ``chat_send`` and ``context_bundle`` are
byte-stable.

Nothing here reads process settings: chat/context settings are explicit, the
MCP settings block is constructed, and the token (when a test wants one) is
passed explicitly — no environment, no ``get_settings`` cache.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from paa.chat.service import ChatService
from paa.config import ChatSettings, ContextSettings, MCPSettings
from paa.core.types import EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.mcp.server import MCPServer
from paa.memory.creator import MemoryCreator
from paa.pipelines.base import Capability
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database

PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True, slots=True)
class McpHarness:
    """Everything one MCP test needs, wired like the CLI wires it."""

    server: MCPServer
    service: ChatService
    ledger: LedgerStore
    db: Database
    signals: SignalRepository
    chat_settings: ChatSettings

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request/response round trip through the handler, id 1."""
        response = await self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        )
        assert response is not None, f"{method} produced no response"
        return response

    async def initialize(self, **extra: Any) -> dict[str, Any]:
        """Handshake with the pinned protocol version (plus extra params)."""
        response = await self.call(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {}, **extra},
        )
        assert "error" not in response, response["error"]
        return response["result"]

    async def events(self, correlation_id: Any) -> list[LedgerEvent]:
        """One lineage, in ledger order."""
        return await self.ledger.read_correlation(correlation_id)

    async def kinds(self, correlation_id: Any) -> list[str]:
        """One lineage's event-type values, in ledger order."""
        return [event.event_type.value for event in await self.events(correlation_id)]

    async def tool_call_lineage(self) -> Any:
        """The correlation id of the most recent tool-call audit lineage."""
        from datetime import UTC, datetime

        events = await self.ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.TOOL_CALL_PROPOSED],
            descending=True,
            limit=1,
        )
        assert events, "no TOOL_CALL_PROPOSED row was written"
        return events[0].correlation_id

    async def refusal_lineages(self) -> list[LedgerEvent]:
        """Every POLICY_BLOCKED refusal row (auth refusals land there)."""
        from datetime import UTC, datetime

        return await self.ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.POLICY_BLOCKED],
            descending=True,
        )


@pytest.fixture
def mcp(
    tmp_path: Path, paa_home: Path, db: Database, ledger: LedgerStore
) -> Callable[..., McpHarness]:
    """Build an MCP harness under an isolated PAA_HOME; args overridable per test."""

    def build(
        *,
        chat_settings: ChatSettings | None = None,
        context_settings: ContextSettings | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        mcp_settings: MCPSettings | None = None,
        expected_token: str | None = None,
        mandate_capabilities: frozenset[Capability] | None = None,
        signals: SignalRepository | None = None,
    ) -> McpHarness:
        resolved_chat = chat_settings if chat_settings is not None else ChatSettings()
        resolved_context = context_settings if context_settings is not None else ContextSettings()
        resolved_signals = (
            signals
            if signals is not None
            else SignalRepository(db, ContentAddressedStore(tmp_path / "cold"))
        )
        creator = MemoryCreator(db)
        service = ChatService(
            ledger=ledger,
            db=db,
            creator=creator,
            settings=resolved_chat,
            permission_mode=permission_mode,
            context_settings=resolved_context,
            signals=resolved_signals,
        )
        server = MCPServer(
            chat=service,
            ledger=ledger,
            db=db,
            creator=creator,
            signals=resolved_signals,
            chat_settings=resolved_chat,
            context_settings=resolved_context,
            permission_mode=permission_mode,
            mcp_settings=mcp_settings if mcp_settings is not None else MCPSettings(),
            expected_token=expected_token,
            mandate_capabilities=mandate_capabilities,
        )
        return McpHarness(
            server=server,
            service=service,
            ledger=ledger,
            db=db,
            signals=resolved_signals,
            chat_settings=resolved_chat,
        )

    return build


def error_code(response: dict[str, Any]) -> int:
    """The JSON-RPC code of an error response, asserting the shape once."""
    assert "error" in response, f"expected an error object, got {response}"
    return response["error"]["code"]


def structured(result: dict[str, Any]) -> dict[str, Any]:
    """The structured payload of a tools/call result, asserting shape once."""
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    # The text block carries the same payload for 2024-11-05 clients.
    assert json.loads(result["content"][0]["text"]) == payload
    return payload
