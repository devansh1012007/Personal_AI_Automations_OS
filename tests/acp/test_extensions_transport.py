"""ACP as a chat transport for the extension surface (C8.4, SPEC 2 Wave 8).

The in-process harness the dispatch suite uses, driven one step further:
an installed skill's capability reaches the editor-side session — the
``[capabilities]`` section rides the prompt the ACP-served model sees, and
a turn whose reply opens with the ``@use-skill`` convention dispatches the
real staged entrypoint through the same five-gate-produced bytes, with the
outcome fed back to the model and journalled as TOOL_CALL_* rows.

Because ``session/prompt`` is the one method whose reply the *turn task*
writes (the read loop stays free for ``session/cancel``), the prompt round
runs through the production ``serve`` loop over a fed reader and an
in-memory sink — the same wire path an editor's frames take, not the
``handle_message`` shortcut the synchronous methods use.

This is the smallest **real** round over the ACP door: a real sandbox, a
real ledger, the same five-gate install the chat flow proves. What it
deliberately does not cover: the process boundary
(``tests/acp/test_server_stdio.py`` owns the stdio child, and that child
wires no extension surface — see the file's note) and streaming chunk
shape (the dispatch suite owns it).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from paa.acp.protocol import encode_frame
from paa.chat.extensions import ChatExtensionSurface
from paa.chat.loop import ChatReply
from paa.core.types import EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.marketplace.installer import InstallGate
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.storage.relational.database import Database
from paa.validation.ast_scanner import AstSecurityScanner

# The same fixture skill tests/chat/test_extensions.py installs through
# the five gates: a SKILL.md directory whose entrypoint echoes its
# arguments as one JSON object on stdout.
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


class _AcpDirectiveModel:
    """The editor-side scripted model: ask for the skill, then answer.

    The second reply can only know "hello Grace" if the ACP-served turn
    dispatched the staged entrypoint and fed the result back. Both
    replies stream through ``on_delta`` — ACP's reply text rides the
    ``agent_message_chunk`` stream, not the final result.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            text = '@use-skill greeter\n{"name": "Grace"}'
        else:
            text = "The greeter says: hello Grace"
        await asyncio.sleep(0)  # cooperative: the stream flushes between turns
        if on_delta is not None:
            await on_delta(text)
        return ChatReply(text=text, tokens_used=1)


class _MemSink:
    """The in-memory :class:`FrameSink` the wire path writes into."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None


class TestExtensionSurfaceOverAcp:
    async def test_installed_skill_reaches_and_runs_through_the_acp_door(
        self, tmp_path: Path, db: Database, ledger: LedgerStore, acp
    ) -> None:
        # Install through the real five-gate flow first — the ACP front is
        # a consumer of installed capabilities, never an installer.
        hub = tmp_path / "hub" / "greeter"
        hub.mkdir(parents=True)
        (hub / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
        (hub / "main.py").write_text(_ENTRYPOINT, encoding="utf-8")
        surface = ChatExtensionSurface(
            ledger=ledger,
            db=db,
            home=tmp_path / "home",
            sandbox=SubprocessSandbox(),
            scanner=AstSecurityScanner(),
            permission_mode=PermissionMode.ASK,
            allow_unverified=True,
        )
        discovered = await surface.discover(tmp_path / "hub")
        report = await surface.install(discovered[0])
        assert report.installed
        assert report.install.passed_gates == [
            InstallGate.SIGNATURE,
            InstallGate.CONTENT_HASH,
            InstallGate.AST_SCAN,
            InstallGate.PERMISSIONS,
            InstallGate.SMOKE_TEST,
        ]

        # The ACP session, served with the extension surface bound. The
        # initialize/new rounds go through the synchronous router; the
        # prompt round rides the production serve loop.
        model = _AcpDirectiveModel()
        h = acp(model=model, extensions=surface)
        await h.initialize()
        session_id = await h.new_session()

        reader = asyncio.StreamReader()
        sink = _MemSink()
        serve_task = asyncio.create_task(h.server.serve(reader, sink))
        reader.feed_data(
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": "greet Grace"}],
                    },
                }
            )
        )
        # The turn runs while the read loop is free (the wire's own rule);
        # wait for its reply before EOF tears the connection down.
        for _ in range(500):
            if any(
                frame.get("id") == 30 and "method" not in frame for frame in sink.frames
            ):
                break
            await asyncio.sleep(0.02)
        else:
            await serve_task
            raise AssertionError("the prompt turn never answered")
        reader.feed_eof()
        await serve_task

        # end_turn: the turn completed over the wire. (The reply carries
        # the stop reason per ACP v1; the streamed agent_message_chunk
        # notifications carry the text, and both are in the sink.)
        replies = [
            frame
            for frame in sink.frames
            if frame.get("id") == 30 and "method" not in frame
        ]
        assert len(replies) == 1
        assert replies[0]["result"]["stopReason"] == "end_turn"
        streamed = "".join(
            frame["params"]["update"]["content"]["text"]
            for frame in sink.frames
            if frame.get("method") == "session/update"
        )
        assert "hello Grace" in streamed

        # The prompt the served model saw offered the capability, and the
        # second round carried the skill's result — the same think→act→
        # observe cycle the in-process chat turn runs.
        assert "[capabilities]" in model.prompts[0]
        assert "greeter v1.0.0" in model.prompts[0]
        assert "[skill results]" in model.prompts[1]
        assert "hello Grace" in model.prompts[1]

        # The invocation is on the ledger: the install's audit lineage and
        # the turn's tool-call rows, the same shapes the chat flow writes.
        assert report.lineage is not None
        install_kinds = await h.kinds(report.lineage)
        assert install_kinds == [
            EventType.EXTENSION_INSTALLED.value,
            EventType.EXTENSION_ACTIVATED.value,
        ]
        rows = await db.fetch_all(
            "SELECT event_type, payload FROM system_state_ledger "
            "WHERE event_type LIKE 'TOOL_CALL%' ORDER BY sequence_id"
        )
        assert [row["event_type"] for row in rows] == [
            "TOOL_CALL_PROPOSED",
            "TOOL_CALL_COMPLETED",
        ]
        assert json.loads(rows[0]["payload"])["tool"] == "greeter"
