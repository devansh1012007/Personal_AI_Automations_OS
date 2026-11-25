"""Fixtures for the ACP suite.

Two harnesses, one per front, both offline:

* the **in-process** server harness — the ``mcp_server`` suite's shape:
  a real SQLite database, a real ledger, a real MemoryCreator and the
  deterministic echo model, wired the way ``ChatService.from_runtime``
  would, with ``ACPServer`` on top and ``handle_message`` as the door;
* the **child-process** harness — ``serve_child.py`` spawned with piped
  stdio, plus the test's half of the wire (a line client with request
  correlation and notification collection), so the streaming and cancel
  acceptance crosses a real process boundary the way an editor's would.

Nothing here reads process settings: the chat service and every facade are
built with explicit settings, so no test depends on the environment or the
``get_settings`` cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from asyncio import transports
from asyncio.subprocess import PIPE, SubprocessStreamProtocol
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from paa.acp.protocol import encode_frame
from paa.acp.server import ACPServer
from paa.chat.loop import ChatModel
from paa.chat.service import ChatService
from paa.config import ChatSettings, ContextSettings
from paa.core.types import PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.memory.creator import MemoryCreator
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.database import Database

HERE = Path(__file__).resolve().parent

PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# The in-process server harness (dispatch, envelopes, fs scoping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcpHarness:
    """Everything one in-process server test needs, wired like the CLI."""

    server: ACPServer
    service: ChatService
    ledger: LedgerStore
    db: Database
    workspace: Path

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, request_id: Any = 1
    ) -> dict[str, Any]:
        """One request/response round trip through the router, id 1."""
        response = await self.server.handle_message(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        assert response is not None, f"{method} produced no response"
        return response

    async def initialize(self, **extra: Any) -> dict[str, Any]:
        """The handshake with the pinned version (plus extra params)."""
        response = await self.call(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "clientInfo": {}, **extra},
        )
        assert "error" not in response, response["error"]
        return response["result"]

    async def new_session(self, cwd: Path | None = None) -> str:
        """A session bound to the harness workspace (or the given cwd)."""
        response = await self.call("session/new", {"cwd": str(cwd or self.workspace)})
        assert "error" not in response, response["error"]
        session_id = response["result"]["sessionId"]
        assert isinstance(session_id, str)
        return session_id

    async def events(self, correlation_id: Any) -> list[LedgerEvent]:
        """One lineage, in ledger order."""
        return await self.ledger.read_correlation(correlation_id)

    async def kinds(self, correlation_id: Any) -> list[str]:
        """One lineage's event-type values, in ledger order."""
        return [event.event_type.value for event in await self.events(correlation_id)]


@pytest.fixture
def acp(tmp_path: Path, db: Database, ledger: LedgerStore) -> Callable[..., AcpHarness]:
    """Build an in-process server harness; the model seam is overridable."""

    def build(*, model: ChatModel | None = None) -> AcpHarness:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        service = ChatService(
            ledger=ledger,
            db=db,
            creator=MemoryCreator(db),
            settings=ChatSettings(),
            permission_mode=PermissionMode.ASK,
            context_settings=ContextSettings(),
            model=model,
            signals=SignalRepository(db, ContentAddressedStore(tmp_path / "cold")),
        )
        return AcpHarness(
            server=ACPServer(chat=service),
            service=service,
            ledger=ledger,
            db=db,
            workspace=workspace,
        )

    return build


# ---------------------------------------------------------------------------
# The child-process harness (streaming and cancel over real pipes)
# ---------------------------------------------------------------------------


@dataclass
class ServeChild:
    """The server as a real child, plus the test's half of its wire."""

    process: asyncio.subprocess.Process
    transport: transports.BaseTransport
    db_path: Path
    drain_task: asyncio.Task[None] | None = None
    notifications: list[dict[str, Any]] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    _next_id: int = 1

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(encode_frame(message))
        await self.process.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: float = 10.0
    ) -> dict[str, Any]:
        """One request, answered in wire order — notifications read on the
        way are collected, never dropped."""
        request_id = self._next_id
        self._next_id += 1
        await self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        while True:
            message = await self.read_frame(timeout_seconds=timeout_seconds)
            if message.get("id") == request_id and "method" not in message:
                return message
            self.notifications.append(message)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def read_frame(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """One decoded frame from the child, within a bounded wait."""
        assert self.process.stdout is not None
        line = await asyncio.wait_for(
            self.process.stdout.readline(), timeout=timeout_seconds
        )
        if not line:
            raise AssertionError(
                "the server child closed stdout; stderr: " + "".join(self.stderr_lines[-10:])
            )
        message: dict[str, Any] = json.loads(line)
        return message

    async def initialize(self) -> dict[str, Any]:
        reply = await self.request(
            "initialize", {"protocolVersion": PROTOCOL_VERSION, "clientInfo": {}}
        )
        assert "error" not in reply, reply["error"]
        return reply["result"]

    async def close_stdin_and_wait(self, *, timeout_seconds: float = 10.0) -> int:
        """EOF: close the pipe and await the child's clean exit."""
        assert self.process.stdin is not None
        self.process.stdin.close()
        return await asyncio.wait_for(self.process.wait(), timeout=timeout_seconds)

    async def kill(self) -> None:
        """The finally-path teardown: never leave a child (or its pipes).

        Idempotent and safe after a graceful exit: the kill only fires when
        the child is still alive, and the transport close only matters when
        the pipes are still open — but it must happen *here*, while the test
        loop still runs. The subprocess transport's ``__del__`` calls back
        into the event loop, so a transport left for the garbage collector
        after loop close turns clean teardown into an unraisable
        ``RuntimeError: Event loop is closed`` on an otherwise green run.
        """
        if self.process.returncode is None:
            self.process.kill()
            await asyncio.wait_for(self.process.wait(), timeout=10.0)
        if self.drain_task is not None:
            self.drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.drain_task
        self.transport.close()


ServeFactory = Callable[..., Awaitable[ServeChild]]


@pytest.fixture
async def spawn_serve(tmp_path: Path) -> AsyncIterator[ServeFactory]:
    """Spawn ``serve_child.py`` with piped stdio.

    Every child spawned through this fixture is torn down here — kill, reap,
    close the pipes — so a test that fails before its own graceful EOF path
    cannot leak a process or an open transport onto the next run.
    """
    children: list[ServeChild] = []

    async def spawn(*, model: str = "echo", chunk_delay: float = 0.05) -> ServeChild:
        db_path = tmp_path / f"child-{model}-{chunk_delay}-{len(children)}.db"
        # ``asyncio.create_subprocess_exec`` spelled out once: the loop-level
        # call underneath it is identical, and keeping the transport it
        # returns is the only *public* handle for closing the child's pipes
        # while this loop still runs — the transport's ``__del__`` calls back
        # into the loop, so a transport left to the garbage collector after
        # loop close is an unraisable "Event loop is closed" on a green run.
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_exec(
            lambda: SubprocessStreamProtocol(limit=64 * 1024, loop=loop),
            sys.executable,
            str(HERE / "serve_child.py"),
            "--db",
            str(db_path),
            "--model",
            model,
            "--chunk-delay",
            str(chunk_delay),
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
        )
        process = asyncio.subprocess.Process(transport, protocol, loop)
        child = ServeChild(process=process, transport=transport, db_path=db_path)

        async def drain_stderr() -> None:
            assert process.stderr is not None
            async for raw in process.stderr:
                child.stderr_lines.append(raw.decode("utf-8", errors="replace"))

        child.drain_task = asyncio.create_task(drain_stderr())
        children.append(child)
        return child

    try:
        yield spawn
    finally:
        for child in children:
            await child.kill()


async def read_child_ledger(db_path: Path, correlation_id: Any) -> list[LedgerEvent]:
    """One lineage out of a finished child's ledger file.

    The child is dead by the time this runs, so the SQLite file is ours to
    open — the test reads back exactly what the process journaled.
    """
    database = Database(db_path)
    await database.connect()
    try:
        return await LedgerStore(database).read_correlation(correlation_id)
    finally:
        await database.close()
