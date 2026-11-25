"""The ACP server as a real child process — the stdio acceptance child.

Spawned by ``tests/acp/test_server_stdio.py`` with piped stdin/stdout, this
is ``paa acp serve`` minus the runtime: the same ``ACPServer``, the same
production stdio transport (the CLI module's ``_stdin_reader`` and
``_StdoutSink``), and a chat service wired the way ``tests/chat/conftest.py``
wires one (real SQLite, real ledger, real MemoryCreator, deterministic echo
model) — so the tests exercise the process boundary the editor boundary
would, without a model key or a socket.

Two models:

``echo``
    The production :class:`~paa.chat.loop.EchoChatModel` — deterministic,
    streams 12-char chunks with cooperative yields.
``slow``
    The same reply text, but every chunk is delayed by a real
    ``--chunk-delay`` seconds, so a ``session/cancel`` can be aimed at a
    turn that is visibly mid-stream and the test can prove the chunks
    stopped. Slowness is a property of the model seam, not the server: the
    server code under test is identical in both modes.

Every paa import is function-level (the CLI's own idiom) after a
:func:`_bootstrap` that puts ``src/`` on ``sys.path``, so the child imports
paa however the test runner was invoked. Logging is configured to stderr
before anything else runs: unconfigured structlog prints to **stdout**, and
stdout is the wire here (the discipline ``paa.cli.main``'s callback states
for the MCP server).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

#: ChatModel/ChatReply appear in annotations only; ``from __future__ import
#: annotations`` keeps them lazy, so no paa name is needed at module scope.


def _bootstrap() -> None:
    """Put the tree's ``src/`` on ``sys.path`` — the child may be spawned
    without the parent's PYTHONPATH (an installed run, a bare ``python``)."""
    src = Path(__file__).resolve().parents[2] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


class SlowEchoChatModel:
    """The echo model with real delays between streamed chunks.

    Wraps the production echo (instantiated by the caller, after the
    bootstrap) and re-streams its reply in larger chunks separated by real
    sleeps — the property under test is that a cancel *lands mid-stream*,
    and cooperative yields are too fast to aim at.
    """

    def __init__(self, inner: Any, *, chunk_chars: int, delay: float) -> None:
        self._inner = inner
        self._chunk_chars = chunk_chars
        self._delay = delay

    async def reply(self, prompt: str, *, on_delta: Any) -> Any:
        reply = await self._inner.reply(prompt, on_delta=None)
        text = reply.text
        for start in range(0, len(text), self._chunk_chars):
            await asyncio.sleep(self._delay)  # a cancel lands here, not later
            if on_delta is not None:
                await on_delta(text[start : start + self._chunk_chars])
        return reply


async def _main() -> int:
    parser = argparse.ArgumentParser(description="the ACP server test child")
    parser.add_argument("--db", required=True, help="SQLite file for the child's ledger")
    parser.add_argument(
        "--model", choices=("echo", "slow"), default="echo", help="the chat model seam"
    )
    parser.add_argument(
        "--chunk-delay", type=float, default=0.05, help="seconds between chunks (slow model)"
    )
    args = parser.parse_args()
    _bootstrap()

    # stdout is the protocol: diagnostics go to stderr, and structlog's
    # default is stdout — so this runs before any module logs.
    from paa.observability.logging import configure_logging

    configure_logging()

    from paa.acp.server import ACPServer
    from paa.chat.loop import EchoChatModel
    from paa.chat.service import ChatService
    from paa.cli.acp import _stdin_reader, _StdoutSink
    from paa.config import ChatSettings, ContextSettings
    from paa.core.types import PermissionMode
    from paa.ledger.store import LedgerStore
    from paa.memory.creator import MemoryCreator
    from paa.storage.coldlake.cas import ContentAddressedStore
    from paa.storage.coldlake.signals import SignalRepository
    from paa.storage.relational.database import Database

    model: Any
    if args.model == "echo":
        model = EchoChatModel()
    else:
        model = SlowEchoChatModel(
            EchoChatModel(), chunk_chars=24, delay=args.chunk_delay
        )

    # ``signals`` is wired exactly as tests/chat/conftest.py wires it (and as
    # ChatService.from_runtime does in production): the chat loop's
    # signals-None degrade path journals the turn's remember() through a bare
    # payload that MemoryCreator._envelope_from_row cannot load (KeyError
    # 'id') — a latent chat-loop finding recorded in the W7-IMPL-07 report,
    # not this front's contract.
    cold_root = Path(args.db).parent / "cold"
    database = Database(Path(args.db))
    await database.connect()
    try:
        service = ChatService(
            ledger=LedgerStore(database),
            db=database,
            creator=MemoryCreator(database),
            settings=ChatSettings(),
            permission_mode=PermissionMode.ASK,
            context_settings=ContextSettings(),
            model=model,
            signals=SignalRepository(database, ContentAddressedStore(cold_root)),
        )
        await ACPServer(chat=service).serve(await _stdin_reader(), _StdoutSink())
    finally:
        await database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
