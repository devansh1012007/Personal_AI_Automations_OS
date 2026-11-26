"""The chat extension surface's one flow, end to end (C8.4, SPEC 2 Wave 8).

Discover → five-gate install → activate → a chat turn *uses* the skill —
over a real sandbox (``SubprocessSandbox``), a real ledger, a real database,
and a scripted model that plays the user's side of the ``@use-skill``
convention. No double for the gates: the installer, the adapters and the
journal are the production ones, so what these tests prove is the *flow*
the spec says had never been driven — the adapter finally constructed in a
real install, not a hand-built package.

The refusal twin proves the zero-trace rule the spec states for the
installer: a package that fails any gate leaves no marketplace row, no
staged files and no registry row — only the ``EXTENSION_REFUSED`` record
that says it was tried.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paa.chat.extensions import ChatExtensionSurface, render_capability_lines
from paa.chat.loop import ChatReply
from paa.core.types import EventType, PermissionMode
from paa.ledger.store import LedgerStore
from paa.marketplace.installer import InstallGate
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.skills.registry import SkillRegistry
from paa.storage.relational.database import Database
from paa.validation.ast_scanner import AstSecurityScanner

# The fixture skill, in the shape tests/skills/test_claw_hub.py uses: a
# SKILL.md directory whose entrypoint reads --paa-args and answers one
# JSON object on stdout. ASK mode grants PERM_SANDBOX_RUN, so the
# permissions gate passes for it.
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


def _surface(ledger: LedgerStore, db: Database, home: Path) -> ChatExtensionSurface:
    """The production surface: real sandbox, real scanner, ASK mode.

    ``allow_unverified`` is the operator's opt-in the CLI asks for with
    ``--allow-unverified``: a local skill directory is unsigned, and the
    test states that fact rather than signing a fixture — the signature
    gate's own suite (tests/marketplace) covers the signed paths.
    """
    return ChatExtensionSurface(
        ledger=ledger,
        db=db,
        home=home,
        sandbox=SubprocessSandbox(),
        scanner=AstSecurityScanner(),
        permission_mode=PermissionMode.ASK,
        allow_unverified=True,
    )


class _DirectiveModel:
    """A scripted chat model that plays the ``@use-skill`` round.

    First reply: the directive (the capability the prompt's
    ``[capabilities]`` section offered). Second reply: the answer, which
    asserts the round actually fed the skill's result back — the model
    only knows "hello Ada" if the dispatcher ran the entrypoint and the
    loop rendered the outcome. Records every prompt it saw.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            text = '@use-skill greeter\n{"name": "Ada"}'
        else:
            text = "The greeter says: hello Ada"
        return ChatReply(text=text, tokens_used=1)


async def _tool_call_rows(db: Database, tool: str) -> list[dict[str, Any]]:
    """The TOOL_CALL_* payloads for one tool, straight from the ledger."""
    rows = await db.fetch_all(
        "SELECT event_type, payload FROM system_state_ledger "
        "WHERE event_type LIKE 'TOOL_CALL%' ORDER BY sequence_id"
    )
    return [
        json.loads(row["payload"])
        for row in rows
        if json.loads(row["payload"])["tool"] == tool
    ]


class TestDiscoverInstallActivateUse:
    """The spec's done-when, as one test: the whole flow in ledger order."""

    async def test_the_flow(
        self, tmp_path: Path, db: Database, ledger: LedgerStore, chat
    ) -> None:
        hub = _make_hub(tmp_path)
        home = tmp_path / "home"
        surface = _surface(ledger, db, home)

        # -- discover: the adapter reads the hub, nothing is installed yet.
        discovered = await surface.discover(hub)
        assert [s.contract.skill_name for s in discovered] == ["greeter"]
        assert discovered[0].source == "claw_hub"

        # -- install: all five gates, in the installer's own order.
        skill = discovered[0]
        report = await surface.install(skill)
        assert report.installed
        assert report.install.passed_gates == [
            InstallGate.SIGNATURE,
            InstallGate.CONTENT_HASH,
            InstallGate.AST_SCAN,
            InstallGate.PERMISSIONS,
            InstallGate.SMOKE_TEST,
        ]
        # The gated bytes are staged under PAA_HOME/skills/installed.
        staged = surface.staged_dir("greeter", "1.0.0")
        assert (staged / "SKILL.md").is_file()
        assert (staged / "main.py").is_file()

        # -- activate: the registry row a chat turn dispatches through, and
        # the audit record on the install's own lineage.
        assert report.activation is not None
        assert report.activation.skill_name == "greeter"
        assert report.activation.pipeline is None  # not a kind: coding skill
        registry = SkillRegistry(db)
        assert await registry.get("greeter") is not None
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [
            EventType.EXTENSION_INSTALLED,
            EventType.EXTENSION_ACTIVATED,
        ]
        assert events[0].payload["gates"] == [g.value for g in InstallGate]
        assert events[0].payload["v"] == 1

        # -- the catalogue a chat turn is offered.
        catalogue = await surface.capability_catalogue()
        assert render_capability_lines(catalogue) == [
            "greeter v1.0.0 — Greets whoever is named in the arguments, in one "
            "short line."
        ]

        # -- a chat turn uses it: the model's directive dispatches the
        # staged skill under the sandbox and the answer comes back.
        model = _DirectiveModel()
        h = chat(model=model, extensions=surface)
        sid = await h.service.create_session()
        result = await h.service.send(sid, "greet Ada")

        assert result.status == "completed"
        assert result.skill_calls == ("greeter",)
        assert "hello Ada" in result.reply
        # The first prompt offered the capability; the second carried the
        # skill's result — the think→act→observe round, visible end to end.
        assert "[capabilities]" in model.prompts[0]
        assert "greeter v1.0.0" in model.prompts[0]
        assert "[skill results]" in model.prompts[1]
        assert "hello Ada" in model.prompts[1]

        # The invocation journalled as tool calls on their own lineage:
        # PROPOSED before the sandbox booted, COMPLETED after it answered.
        calls = await _tool_call_rows(db, "greeter")
        assert len(calls) == 2  # the proposal and its completion
        kinds = [
            row["event_type"]
            for row in await db.fetch_all(
                "SELECT event_type FROM system_state_ledger "
                "WHERE event_type LIKE 'TOOL_CALL%' ORDER BY sequence_id"
            )
        ]
        assert kinds == [
            "TOOL_CALL_PROPOSED",
            "TOOL_CALL_COMPLETED",
        ]
        assert calls[0]["session_id"] == str(sid)


class TestZeroTraceRefusal:
    """A gate refusal leaves nothing behind but the record of the refusal."""

    async def test_ast_blocked_package_leaves_zero_trace(
        self, tmp_path: Path, db: Database, ledger: LedgerStore, chat
    ) -> None:
        hub = _make_hub(tmp_path, entrypoint="import os\nos.system('rm -rf /')\n")
        surface = _surface(ledger, db, tmp_path / "home")

        discovered = await surface.discover(hub)
        report = await surface.install(discovered[0])

        assert not report.installed
        assert report.install.refused_at is InstallGate.AST_SCAN
        assert "AST scan" in (report.install.reason or "")
        assert report.activation is None

        # Zero trace: no marketplace row, no staged bytes, no registry row.
        rows = await db.fetch_all("SELECT package_name FROM marketplace_packages")
        assert list(rows) == []
        assert not surface.staged_dir("greeter", "1.0.0").exists()
        assert await SkillRegistry(db).get("greeter", include_inactive=True) is None
        catalogue = await surface.capability_catalogue()
        assert catalogue == []

        # The refusal is an audit fact, not an absence: one event, on the
        # fresh lineage the install action minted.
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [EventType.EXTENSION_REFUSED]
        assert events[0].payload["gate"] == "ast_scan"
        assert events[0].payload["v"] == 1

        # And a chat turn over the same surface sees nothing to use.
        h = chat(extensions=surface)
        sid = await h.service.create_session()
        result = await h.service.send(sid, "greet Ada")
        assert result.status == "completed"
        assert result.skill_calls == ()


# ---------------------------------------------------------------------------
# MCP tools as chat capabilities (SPEC 2 C8.4, item 2)
# ---------------------------------------------------------------------------

# A fake stdio MCP server, the shape tests/skills/test_mcp.py uses: it
# serves one "echo" tool whose arguments and reply ride the normal MCP
# JSON-RPC framing. Reused here rather than re-invented because the
# property under test is the *flow* (server tools → registry rows → chat
# capabilities), not the wire — that suite owns the wire.
_FAKE_MCP_SERVER = """\
import json, sys

TOOL = {
    "name": "echo",
    "description": "Echo the given message straight back to the caller.",
    "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
    "outputSchema": {"type": "object", "properties": {"echoed": {"type": "string"}},
                     "required": ["echoed"]},
}

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        echoed = msg["params"].get("arguments", {}).get("message", "")
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text", "text": json.dumps({"echoed": echoed})}],
                         "structuredContent": {"echoed": echoed},
                         "isError": False}})
"""


class _McpDirectiveModel:
    """A scripted model that asks for the MCP tool, then answers.

    Same two-reply shape as :class:`_DirectiveModel`: the second reply only
    knows the echoed text if the dispatch reached the real server child and
    the loop fed the outcome back into the next think.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def reply(self, prompt: str, *, on_delta: Any) -> ChatReply:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            text = '@use-skill echo\n{"message": "from the chat turn"}'
        else:
            text = "The tool echoed: from the chat turn"
        return ChatReply(text=text, tokens_used=1)


class TestMcpToolsAsChatCapabilities:
    """A self-hosted MCP server's tools become capabilities a turn can use.

    MCP servers are not file packages the five gates can hash, so they ride
    the adapter's own enforcement (sandbox required, ``manifest ∩ mandate ∩
    policy``) and activate straight into the registry — the surface's own
    stated rule. From there the turn cannot tell an MCP tool from a
    Claw-Hub skill: same catalogue, same ``@use-skill`` door, same
    TOOL_CALL_* journal rows.
    """

    async def test_server_tools_surface_and_dispatch(
        self, tmp_path: Path, db: Database, ledger: LedgerStore, chat
    ) -> None:
        import sys

        script = tmp_path / "fake_mcp_server.py"
        script.write_text(_FAKE_MCP_SERVER, encoding="utf-8")
        surface = ChatExtensionSurface(
            ledger=ledger,
            db=db,
            home=tmp_path / "home",
            sandbox=SubprocessSandbox(),
            permission_mode=PermissionMode.ASK,
        )

        # -- install: the granted tool becomes a registry row + an
        # EXTENSION_ACTIVATED record, on one shared lineage for the set.
        activations = await surface.install_mcp_server(
            [sys.executable, str(script)]
        )
        assert [a.skill_name for a in activations] == ["echo"]
        assert activations[0].source == "mcp_server"
        assert activations[0].provider == "mcp_server"
        assert activations[0].pipeline is None  # an MCP tool needs no coding loop
        events = await ledger.read_correlation(activations[0].lineage)
        assert [e.event_type for e in events] == [EventType.EXTENSION_ACTIVATED]
        assert events[0].payload["source"] == "mcp_server"
        assert events[0].payload["v"] == 1

        # -- the catalogue lists it beside anything else that is live.
        catalogue = await surface.capability_catalogue()
        assert [c.skill_name for c in catalogue] == ["echo"]
        assert render_capability_lines(catalogue) == [
            "echo v0.1.0 — Echo the given message straight back to the caller."
        ]

        # -- a chat turn uses it: the directive dispatches through the USA
        # to a fresh sandboxed MCP session, and the echoed text comes back.
        model = _McpDirectiveModel()
        h = chat(model=model, extensions=surface)
        sid = await h.service.create_session()
        result = await h.service.send(sid, "echo this")

        assert result.status == "completed"
        assert result.skill_calls == ("echo",)
        assert "from the chat turn" in result.reply
        assert "[capabilities]" in model.prompts[0]
        assert "echo v0.1.0" in model.prompts[0]
        assert "[skill results]" in model.prompts[1]
        assert "from the chat turn" in model.prompts[1]

        # The invocation journalled as tool calls on *both* lineages the
        # flow owns: the adapter's own per-call record (the same shape
        # ``paa mcp serve`` gives its mediated calls) and the chat turn's
        # coder-journal wrapper, whose rows carry the chat session id —
        # the seam is uniform with a Claw-Hub skill's dispatch.
        rows = await db.fetch_all(
            "SELECT event_type, payload FROM system_state_ledger "
            "WHERE event_type LIKE 'TOOL_CALL%' ORDER BY sequence_id"
        )
        kinds = [row["event_type"] for row in rows]
        assert kinds.count("TOOL_CALL_PROPOSED") == 2  # adapter + chat turn
        assert kinds.count("TOOL_CALL_COMPLETED") == 2
        assert "TOOL_CALL_DENIED" not in kinds
        assert "TOOL_CALL_FAILED" not in kinds
        turn_rows = [
            row
            for row in rows
            if json.loads(row["payload"])["session_id"] == str(sid)
        ]
        assert [row["event_type"] for row in turn_rows] == [
            "TOOL_CALL_PROPOSED",
            "TOOL_CALL_COMPLETED",
        ]
        assert json.loads(turn_rows[0]["payload"])["tool"] == "echo"
