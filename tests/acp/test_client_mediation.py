"""The client front: an external agent, contained and mediated (T2).

``AcpClient`` spawns the checked-in fake agent (``fake_agent.py`` — real
child process, real NDJSON, zero paa imports) behind a real
``SubprocessSandbox`` session and mediates the two things an ACP agent can
ask of its client. What is pinned here:

* **containment** — the agent runs in the spawned workspace, its whole tree
  dies with :meth:`AcpClient.close`, and nothing it does escapes the pipe;
* **mediation is deterministic and journalled** — a policy-violating tool
  call is refused *and* leaves ``TOOL_CALL_PROPOSED`` + ``TOOL_CALL_DENIED``
  rows in the ledger; a pre-allowed call is allowed and attested
  ``COMPLETED``; an unattended ``ask`` resolves to a recorded
  ``denied_unattended`` refusal, the recorder proxy's semantics;
* **fs serving is the same jail** — an in-scope read is served, a
  ``.git`` read is refused, both journalled;
* **streaming and cancel** — updates reach the caller's sink while the turn
  runs, and a cancel resolves the turn as ``cancelled`` without killing the
  session or the process.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from paa.acp.client import AcpClient, _select_permission_option
from paa.acp.protocol import AcpError
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.core.types import EventType
from paa.sandbox.subprocess_backend import SubprocessSandbox

from .conftest import HERE

#: The fake agent's streamed chunks in the ``stream`` scenario.
STREAM_CHUNKS = ["chunk-0", "chunk-1", "chunk-2", "chunk-3"]


async def _wait_pid_gone(pid: int, *, timeout_seconds: float = 10.0) -> None:
    """Poll until the pid is dead — bounded, because a tree that never dies
    must fail the test rather than hang it."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() > deadline:
            pytest.fail(f"agent pid {pid} still alive {timeout_seconds}s after close()")
        await asyncio.sleep(0.05)


class MediatedAgent:
    """One spawned fake agent behind the real client adapter."""

    def __init__(self, client: AcpClient, updates: list[dict[str, Any]]) -> None:
        self.client = client
        self.updates = updates

    async def final_report(self) -> dict[str, Any]:
        """The agent's last message chunk parsed as JSON — its observation
        of the mediation, the only out-of-band channel it has."""
        texts = [
            u["update"]["content"]["text"]
            for u in self.updates
            if u["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        report = json.loads(texts[-1])
        assert isinstance(report, dict)
        return report


async def drive(
    ledger: Any,
    tmp_path: Path,
    *,
    scenario: str,
    policy: ToolPermissionPolicy | None = None,
    prompt: str = "do the thing",
    tool_name: str = "bash",
    tool_command: str = "curl http://example.invalid/",
    workspace_files: dict[str, str] | None = None,
) -> MediatedAgent:
    """Spawn the fake agent under the sandbox and drive one full turn."""
    workspace = tmp_path / "agent-ws"
    workspace.mkdir()
    for name, content in (workspace_files or {}).items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    updates: list[dict[str, Any]] = []

    async def on_update(update: dict[str, Any]) -> None:
        updates.append(update)

    # An empty tool name cannot ride in the command line — SandboxSpec
    # refuses empty argv elements (rightly: an argv slot that is the empty
    # string is a quoting bug, not a name) — so the fake agent takes it as a
    # flag instead and the *protocol* carries the empty name, which is where
    # an unclassifiable tool call actually lives.
    command: tuple[str, ...] = (
        sys.executable,
        str(HERE / "fake_agent.py"),
        "--scenario",
        scenario,
        "--workspace",
        str(workspace),
        "--tool-command",
        tool_command,
    )
    command += ("--tool-name", tool_name) if tool_name else ("--unnamed-tool",)
    client = AcpClient(
        command,
        policy=policy or ToolPermissionPolicy(mode=PermissionMode.ASK),
        ledger=ledger,
    )
    sandbox = SubprocessSandbox()
    await client.start(sandbox, workspace=workspace, timeout_seconds=None)
    try:
        await client.initialize()
        session_id = await client.new_session()
        stop = await client.prompt(session_id, prompt, on_update=on_update)
        assert stop == "end_turn", f"the turn stopped early: {stop!r}"
    finally:
        await client.close()
    return MediatedAgent(client, updates)


async def tool_events(ledger: Any, kind: EventType) -> list[Any]:
    """Every tool-call row of one kind, newest first (the client journals a
    fresh lineage per mediated call, so the sweep is the only way in)."""
    from datetime import UTC, datetime

    return await ledger.events_since(
        datetime(1970, 1, 1, tzinfo=UTC), event_types=[kind], descending=True
    )


class TestEndToEnd:
    async def test_handshake_session_and_streamed_updates(
        self, ledger, tmp_path
    ) -> None:
        agent = await drive(ledger, tmp_path, scenario="stream")
        assert agent.client.agent_capabilities == {}
        texts = [
            u["update"]["content"]["text"]
            for u in agent.updates
            if u["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        assert texts == STREAM_CHUNKS
        # Every update names the session the client created with the agent.
        assert {u["sessionId"] for u in agent.updates} == {"fake-agent-session"}

    async def test_close_kills_the_whole_session(self, ledger, tmp_path) -> None:
        """Teardown is the sandbox's whole-tree kill: the pid is gone once
        close() returns, not merely 'reaped eventually'."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        client = AcpClient(
            (
                sys.executable,
                str(HERE / "fake_agent.py"),
                "--scenario",
                "stream",
                "--workspace",
                str(workspace),
            ),
            policy=ToolPermissionPolicy(mode=PermissionMode.ASK),
            ledger=ledger,
        )
        await client.start(SubprocessSandbox(), workspace=workspace, timeout_seconds=None)
        session = client._session
        assert session is not None and session.pid is not None
        pid = session.pid
        result = await client.close()
        assert result is not None
        await _wait_pid_gone(pid)
        # close() is idempotent: a second call is a quiet no-op, not an error.
        await client.close()


class TestPermissionMediation:
    async def test_a_policy_violating_call_is_refused_and_journalled(
        self, ledger, tmp_path
    ) -> None:
        """Mode ASK with no rules: an external agent's bash curl ask is an
        unattended ask — denied, recorded as ``denied_unattended``."""
        agent = await drive(ledger, tmp_path, scenario="permission")
        report = await agent.final_report()
        outcome = report["permission"]["result"]["outcome"]
        assert outcome["outcome"] == "selected"
        assert outcome["optionId"] == "reject-1"

        proposed = await tool_events(ledger, EventType.TOOL_CALL_PROPOSED)
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert proposed and proposed[0].payload["tool"] == "bash"
        assert denied, "the refusal was not journalled"
        assert "denied_unattended" in denied[0].payload["reason"]
        completed = await tool_events(ledger, EventType.TOOL_CALL_COMPLETED)
        assert not completed, "a refused call must not be attested completed"

    async def test_a_pre_allowed_call_is_allowed_and_attested(
        self, ledger, tmp_path
    ) -> None:
        policy = ToolPermissionPolicy(
            mode=PermissionMode.ASK, allow=["bash(git status*)"]
        )
        agent = await drive(
            ledger,
            tmp_path,
            scenario="permission",
            policy=policy,
            tool_command="git status --short",
        )
        report = await agent.final_report()
        outcome = report["permission"]["result"]["outcome"]
        assert outcome == {"outcome": "selected", "optionId": "allow-1"}

        completed = await tool_events(ledger, EventType.TOOL_CALL_COMPLETED)
        assert completed and completed[0].payload["tool"] == "bash"
        assert completed[0].payload["result_sha256"]
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert not denied

    async def test_an_ask_rule_under_bypass_still_resolves_to_a_refusal(
        self, ledger, tmp_path
    ) -> None:
        """Bypass skips prompts, not safety rules: an explicit ask rule
        forces the ask, and an unattended ask is a recorded denial."""
        policy = ToolPermissionPolicy(
            mode=PermissionMode.BYPASS, ask=["bash(curl *)"]
        )
        agent = await drive(ledger, tmp_path, scenario="permission", policy=policy)
        report = await agent.final_report()
        outcome = report["permission"]["result"]["outcome"]
        assert outcome["optionId"] == "reject-1"
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert "denied_unattended" in denied[0].payload["reason"]

    async def test_a_denying_policy_selects_the_reject_option(
        self, ledger, tmp_path
    ) -> None:
        policy = ToolPermissionPolicy(mode=PermissionMode.ASK, deny=["bash(curl *)"])
        agent = await drive(ledger, tmp_path, scenario="permission", policy=policy)
        report = await agent.final_report()
        outcome = report["permission"]["result"]["outcome"]
        assert outcome["optionId"] == "reject-1"
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert "denied by policy" in denied[0].payload["reason"]


class TestOptionSelection:
    """The fail-closed table: never approve through a door the agent did
    not open, never guess when no door exists."""

    def test_an_allow_verdict_prefers_the_allow_option(self) -> None:
        options = [
            {"kind": "reject_once", "optionId": "r"},
            {"kind": "allow_always", "optionId": "a"},
        ]
        assert _select_permission_option(options, allow=True) == ("selected", "a")

    def test_a_deny_verdict_takes_the_reject_option(self) -> None:
        options = [
            {"kind": "allow_once", "optionId": "a"},
            {"kind": "reject_once", "optionId": "r"},
        ]
        assert _select_permission_option(options, allow=False) == ("selected", "r")

    def test_an_allow_verdict_with_no_allow_option_falls_back_to_reject(self) -> None:
        options = [{"kind": "reject_once", "optionId": "r"}]
        assert _select_permission_option(options, allow=True) == ("selected", "r")

    def test_no_usable_option_at_all_is_cancelled(self) -> None:
        assert _select_permission_option([], allow=True) == ("cancelled", None)
        assert _select_permission_option(
            [{"kind": "allow_once", "optionId": "a"}], allow=False
        ) == ("cancelled", None)

    def test_an_option_without_an_id_is_not_selectable(self) -> None:
        options = [{"kind": "allow_once"}, {"kind": "reject_once", "optionId": "r"}]
        assert _select_permission_option(options, allow=True) == ("selected", "r")


class TestFsMediation:
    async def test_in_scope_reads_are_served_and_git_reads_are_refused(
        self, ledger, tmp_path
    ) -> None:
        agent = await drive(
            ledger,
            tmp_path,
            scenario="fs",
            workspace_files={
                "notes.txt": "the agent may read this",
                ".git/config": "[core]\n\trepositoryformatversion = 0\n",
            },
        )
        report = await agent.final_report()
        assert report["fs"]["good"]["result"] == {"content": "the agent may read this"}
        assert report["fs"]["git"]["error"]["code"] == -32602

        completed = await tool_events(ledger, EventType.TOOL_CALL_COMPLETED)
        assert completed and completed[0].payload["tool"] == "fs/read_text_file"
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert denied and denied[0].payload["tool"] == "fs/read_text_file"


class TestClientEnvelope:
    async def test_a_version_mismatching_agent_is_refused_at_wiring_time(
        self, ledger, tmp_path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        client = AcpClient(
            (
                sys.executable,
                str(HERE / "fake_agent.py"),
                "--scenario",
                "badversion",
                "--workspace",
                str(workspace),
            ),
            policy=ToolPermissionPolicy(mode=PermissionMode.ASK),
            ledger=ledger,
        )
        await client.start(SubprocessSandbox(), workspace=workspace, timeout_seconds=None)
        try:
            with pytest.raises(AcpError) as raised:
                await client.initialize()
            assert "speaks ACP major version" in str(raised.value)
        finally:
            await client.close()

    async def test_an_unknown_agent_method_gets_the_honest_error(
        self, ledger, tmp_path
    ) -> None:
        agent = await drive(ledger, tmp_path, scenario="unknown")
        report = await agent.final_report()
        assert report["unknown"]["error"]["code"] == -32601


class TestClientCancel:
    async def test_cancel_reaches_the_agent_and_resolves_the_turn(
        self, ledger, tmp_path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        updates: list[dict[str, Any]] = []
        client = AcpClient(
            (
                sys.executable,
                str(HERE / "fake_agent.py"),
                "--scenario",
                "slow",
                "--workspace",
                str(workspace),
            ),
            policy=ToolPermissionPolicy(mode=PermissionMode.ASK),
            ledger=ledger,
        )
        await client.start(SubprocessSandbox(), workspace=workspace, timeout_seconds=None)
        try:
            await client.initialize()
            session_id = await client.new_session()

            async def on_update(update: dict[str, Any]) -> None:
                updates.append(update)

            turn = asyncio.create_task(
                client.prompt(session_id, "take your time", on_update=on_update)
            )
            deadline = time.monotonic() + 10.0
            while len(updates) < 2:
                assert time.monotonic() < deadline, "the agent streamed no updates"
                await asyncio.sleep(0.05)
            seen_at_cancel = len(updates)
            await client.cancel(session_id)
            stop = await asyncio.wait_for(turn, timeout=10.0)
            assert stop == "cancelled"
            # The agent honoured the cancel: the stream stopped soon after,
            # not 200 ticks later.
            assert len(updates) <= seen_at_cancel + 2

            # The session is still usable for a second, uncancelled turn.
            updates.clear()
            stop = await asyncio.wait_for(
                client.prompt(session_id, "one more", on_update=on_update),
                timeout=10.0,
            )
            assert stop == "end_turn"
        finally:
            result = await client.close()
        assert result is not None


class TestUnattendedPolicyWiring:
    async def test_a_call_with_no_tool_name_fails_closed_without_a_policy_lookup(
        self, ledger, tmp_path
    ) -> None:
        """The unnamed ask never reaches the policy: the refusal is stated
        once, in the journal, without classifying an unclassifiable call."""
        agent = await drive(
            ledger,
            tmp_path,
            scenario="permission",
            policy=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
            tool_name="",
        )
        report = await agent.final_report()
        outcome = report["permission"]["result"]["outcome"]
        assert outcome == {"outcome": "selected", "optionId": "reject-1"}
        denied = await tool_events(ledger, EventType.TOOL_CALL_DENIED)
        assert denied[0].payload["tool"] == "unnamed"
        assert "cannot classify" in denied[0].payload["reason"]
