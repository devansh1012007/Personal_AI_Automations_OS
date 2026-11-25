"""McpServerAdapter + MCPClient against a fake stdio MCP server.

The fake server is a tiny Python script whose behaviour is chosen by an argv
flag, so every transport failure mode (crash, malformed line, unknown id,
timeout) is a deterministic, separately exercised path rather than a flake.

C5.3: the client spawns the server through a :class:`SandboxSession` and the
adapter gates every tool through ``manifest ∩ mandate ∩ policy``, so this
suite proves the containment facts **from inside the child** — the ``facts``
tool reports the child's own cwd, pid and environment, and the assertions are
on that self-report, not on the parent's belief about it. The denial, secret
and kill/cancel paths the spec row names are exercised alongside. No mocks of
the process layer: the property under test is the kernel's view of the child,
and a mock would test the mock.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.core.errors import PermissionDeniedError, SkillContractError
from paa.core.types import EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.recovery import find_interrupted_calls
from paa.ledger.store import LedgerStore
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.skills.adapters.mcp import MCPClient, McpServerAdapter
from paa.skills.contracts import SkillContract
from paa.skills.secrets import SecretValue

# A fake MCP server. Reads newline-delimited JSON-RPC from stdin; its reply
# framing and failure behaviour are picked by argv[1] (and argv[2], when a
# mode needs a directory to report into). Writes through the raw buffer so
# Windows text-mode never rewrites the Content-Length bodies.
#
# Modes:
#   normal     serves the "echo" tool faithfully;
#   cl_read    as normal, but replies with Content-Length framing;
#   crash      exits 1 on the first tools/call;
#   timeout    sleeps 30s on tools/call (the per-call budget's hang);
#   malformed  emits one invalid JSON line before the real reply;
#   unknown_id emits a reply with an id the client never sent;
#   facts      serves a "facts" tool that reports the child's own view —
#              cwd, pid, environment — and writes probe.txt into its cwd;
#   tree       spawns a grandchild sleeper (pid reported into argv[2]) and
#              hangs — the kill/cancel target with a real tree to reap.
#
# Every tools/call the server *receives* is appended to ``calls.log`` in its
# cwd — the post-mortem record of what actually reached a child. The denial
# tests do not need to read it: their backend is a double whose ``open()``
# fails the test outright, so the witness is stronger than any log — a call
# refused locally leaves no ``calls.log`` row because it leaves no process.
_FAKE_SERVER = r'''
import sys, json, time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
report_dir = sys.argv[2] if len(sys.argv) > 2 else None

def _write(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()

def send_line(obj):
    _write(json.dumps(obj) + "\n")

def send_cl(obj):
    data = json.dumps(obj)
    _write("Content-Length: %d\r\n\r\n%s" % (len(data.encode("utf-8")), data))

send = send_cl if mode == "cl_read" else send_line

if mode == "facts":
    TOOL = {
        "name": "facts",
        "description": "Report this child's own sandbox facts: cwd, pid, env.",
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
    }
else:
    TOOL = {
        "name": "echo",
        "description": "Echo the given message straight back to the caller.",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
        "outputSchema": {"type": "object", "properties": {"echoed": {"type": "string"}},
                         "required": ["echoed"]},
    }

def _facts():
    import os
    facts = {
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "env_keys": sorted(os.environ),
        "ambient_leak": os.environ.get("PAA_TEST_AMBIENT_LEAK"),
        "allowed": os.environ.get("PAA_MCP_ALLOWED"),
        "token": os.environ.get("PAA_TEST_TOOL_TOKEN"),
    }
    try:
        import resource
        facts["rlimit_as"] = list(resource.getrlimit(resource.RLIMIT_AS))
    except ImportError:  # Windows has no resource module
        facts["rlimit_as"] = None
    # The one writable directory is the cwd jail: a write lands there.
    with open("probe.txt", "w", encoding="utf-8") as fh:
        fh.write("written-from-the-child")
    return facts

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
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        with open("calls.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg["params"].get("name", "")) + "\n")
        args = msg["params"].get("arguments", {})
        if mode == "crash":
            sys.exit(1)
        if mode == "timeout":
            time.sleep(30)
            continue
        if mode == "tree":
            import subprocess
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"])
            if report_dir:
                with open(report_dir + "/grandchild.pid", "w", encoding="utf-8") as fh:
                    fh.write(str(grandchild.pid))
            time.sleep(60)
            continue
        if mode == "malformed":
            _write("this line is not valid json\n")
        if mode == "unknown_id":
            send_line({"jsonrpc": "2.0", "id": 987654, "result": {"content": []}})
        payload = _facts() if mode == "facts" else {"echoed": args.get("message", "")}
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text",
                                      "text": json.dumps(payload)}],
                         "structuredContent": payload,
                         "isError": False}})
'''


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    return script


def _command(script: Path, mode: str, *extra: str) -> list[str]:
    return [sys.executable, str(script), mode, *extra]


#: An ambient sentinel the parent plants; the child must never see it.
CANARY_VAR = "PAA_TEST_AMBIENT_LEAK"


# ---------------------------------------------------------------------------
# helpers — the ledger and process probes the new tests share
# ---------------------------------------------------------------------------


async def _tool_rows(ledger: LedgerStore) -> list[LedgerEvent]:
    """Every tool-call audit row in the ledger, in ledger order."""
    return list(
        await ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[
                EventType.TOOL_CALL_PROPOSED,
                EventType.TOOL_CALL_DENIED,
                EventType.TOOL_CALL_COMPLETED,
                EventType.TOOL_CALL_FAILED,
            ],
        )
    )


async def _wait_for_tool_rows(ledger: LedgerStore, count: int, *, budget: float = 5.0) -> None:
    """Poll until at least ``count`` tool-call rows exist, bounded.

    Ledger appends are fast but asynchronous to the caller's next line; the
    assertions below read rows the invoke wrote, so they wait for the rows
    rather than asserting a race nobody agreed to run.
    """
    deadline = time.monotonic() + budget
    rows = await _tool_rows(ledger)
    while len(rows) < count:
        if time.monotonic() > deadline:
            pytest.fail(f"only {len(rows)} tool-call rows after {budget}s")
        await asyncio.sleep(0.05)
        rows = await _tool_rows(ledger)


async def _wait_for_file(path: Path, *, budget: float = 5.0) -> str:
    """Poll until ``path`` exists, then return its text — the fake server's
    async handshake (it announces the grandchild by writing a file)."""
    deadline = time.monotonic() + budget
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path} never appeared after {budget}s")
        await asyncio.sleep(0.05)
    return path.read_text(encoding="utf-8")


async def _wait_until_pid_gone(pid: int, *, budget: float = 10.0) -> None:
    """Poll until the process no longer exists — signal 0 is the probe.

    The kernel is asynchronous about death; asserting immediately would be
    asserting a race. Bounded, because a process that never dies must *fail*
    the test rather than hang it.
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:  # pragma: no cover - exists but is not ours
            pytest.fail(f"pid {pid} exists but is not ours")
        if time.monotonic() > deadline:
            pytest.fail(f"pid {pid} still alive {budget}s after the kill")
        await asyncio.sleep(0.05)


def _echo_contract(**invocation_extra: Any) -> SkillContract:
    """An 'echo' contract built host-side — no server needed to refuse one."""
    invocation: dict[str, Any] = {
        "kind": "mcp_tool",
        "target": "echo",
        "server_command": ("python", "fake_mcp_server.py"),
    }
    invocation.update(invocation_extra)
    return SkillContract.parse(
        {
            "skill_name": "echo",
            "provider": "mcp_server",
            "version": "0.1.0",
            "description": "Echo the given message straight back to the caller.",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_profile": 0.5,
            "required_permissions": ["PERM_SANDBOX_RUN", "PERM_NET_EGRESS"],
            "invocation": invocation,
        }
    )


class _NoSpawnSandbox(Sandbox):
    """A backend that fails the test if anything tries to spawn through it.

    The denial tests hand this to the adapter: the gate must refuse *before*
    the first ``open()``, and an ``open()`` that happens is an AssertionError
    — the loudest way to say the denied call reached the wire anyway.
    """

    @property
    def name(self) -> str:
        return "no-spawn-test-double"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    async def healthcheck(self) -> bool:
        return True

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        raise AssertionError("no workload may run: the call was denied")

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        raise AssertionError("no child may spawn: the call was denied")


class _RecordingSandbox(Sandbox):
    """A transparent backend wrapper that remembers the sessions it opened.

    The adapter owns its client (and the client's session), so the
    kill/cancel tests use this to keep the root pid the post-mortem orphan
    check needs.
    """

    def __init__(self, inner: Sandbox) -> None:
        self._inner = inner
        self.pids: list[int] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def isolation_level(self) -> IsolationLevel:
        return self._inner.isolation_level

    async def healthcheck(self) -> bool:
        return await self._inner.healthcheck()

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return await self._inner.run(spec)

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        session = await self._inner.open(spec)
        if session.pid is not None:
            self.pids.append(session.pid)
        return session


class _FakeBroker:
    """The narrowed ``SecretProvider`` the USA binds: name -> SecretValue."""

    def __init__(self, values: Mapping[str, SecretValue]) -> None:
        self._values = values

    def get_secret(self, name: str) -> SecretValue:
        return self._values[name]


class _TouchFailsBroker:
    """A broker whose ``get_secret`` failing the test IS the assertion.

    The denial-ordering test hands this to the adapter: the policy gate
    must refuse before ``_invocation_env`` ever consults the broker, so any
    consultation that happens is the security claim ("a refused call never
    resolves — never even touches — a secret value") being broken.
    """

    def get_secret(self, name: str) -> SecretValue:
        raise AssertionError("a denied call must never resolve a secret")


# ---------------------------------------------------------------------------
# the transport, now over a sandbox session
# ---------------------------------------------------------------------------


class TestClientHandshake:
    async def test_initialize_list_and_call(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "normal"))
        await client.start(SubprocessSandbox())
        try:
            info = await client.initialize()
            assert info["serverInfo"]["name"] == "fake"
            tools = await client.list_tools()
            assert [t["name"] for t in tools] == ["echo"]
            result = await client.call_tool("echo", {"message": "hi"})
            assert result["structuredContent"] == {"echoed": "hi"}
        finally:
            await client.close()

    async def test_content_length_framing_autodetected_on_read(self, server_script: Path) -> None:
        # Client writes line-delimited; server replies with Content-Length frames.
        client = MCPClient(_command(server_script, "cl_read"))
        await client.start(SubprocessSandbox())
        try:
            await client.initialize()
            result = await client.call_tool("echo", {"message": "framed"})
            assert result["structuredContent"] == {"echoed": "framed"}
        finally:
            await client.close()


class TestClientFailureModes:
    async def test_server_crash_mid_request_raises_connection_error(
        self, server_script: Path
    ) -> None:
        client = MCPClient(_command(server_script, "crash"))
        await client.start(SubprocessSandbox())
        try:
            await client.initialize()
            with pytest.raises(ConnectionError):
                await client.call_tool("echo", {"message": "x"}, timeout=10.0)
        finally:
            await client.close()

    async def test_malformed_json_line_is_skipped(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "malformed"))
        await client.start(SubprocessSandbox())
        try:
            await client.initialize()
            # The bad line is dropped; the correct reply still resolves the call.
            result = await client.call_tool("echo", {"message": "ok"}, timeout=10.0)
            assert result["structuredContent"] == {"echoed": "ok"}
        finally:
            await client.close()

    async def test_unknown_response_id_is_ignored(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "unknown_id"))
        await client.start(SubprocessSandbox())
        try:
            await client.initialize()
            # A reply with an id we never sent is dropped; ours still lands.
            result = await client.call_tool("echo", {"message": "y"}, timeout=10.0)
            assert result["structuredContent"] == {"echoed": "y"}
        finally:
            await client.close()

    async def test_request_timeout_raises_timeout_error(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "timeout"))
        await client.start(SubprocessSandbox())
        try:
            await client.initialize()
            with pytest.raises(TimeoutError):
                await client.call_tool("echo", {"message": "z"}, timeout=0.3)
        finally:
            await client.close()

    async def test_call_after_close_raises(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "normal"))
        await client.start(SubprocessSandbox())
        await client.initialize()
        await client.close()
        with pytest.raises(ConnectionError):
            await client.call_tool("echo", {"message": "x"})


class TestFrameReassembly:
    """``_next_payload`` is chunk-agnostic — the one pure piece of the transport.

    The session hands the client raw chunks, so the message reassembly is the
    client's own job. The process-backed tests above exercise the framing
    paths, but which *chunks* the pipe delivers is timing; these tests feed
    the buffer deterministically, pinning the module docstring's claims: a
    body may span chunks, several messages may share one, and the framing
    autodetects per message.
    """

    def test_a_body_may_span_chunks_and_messages_may_share_one(self) -> None:
        client = MCPClient(("true",))

        # The finest chunking a pipe can deliver: one byte at a time through
        # a whole Content-Length frame. No payload may appear until the
        # body's last byte arrives, then exactly one.
        frame = b'Content-Length: 8\r\n\r\n{"a": 1}'
        for i in range(len(frame) - 1):
            client._buffer += frame[i : i + 1]
            assert client._next_payload() is None, f"payload emerged early at byte {i}"
        client._buffer += frame[-1:]
        assert client._next_payload() == '{"a": 1}'
        assert client._next_payload() is None

        # The coarsest: two line-framed messages and a header-framed third
        # arrive in ONE chunk, and the framing autodetects per message.
        client._buffer += b'{"b": 2}\n{"c": 3}\nContent-Length: 8\r\n\r\n{"d": 4}'
        assert client._next_payload() == '{"b": 2}'
        assert client._next_payload() == '{"c": 3}'
        assert client._next_payload() == '{"d": 4}'
        assert client._next_payload() is None

    def test_extra_header_lines_are_drained_not_parsed(self) -> None:
        client = MCPClient(("true",))
        # The canonical LSP order: Content-Length first, the extras after.
        # (The autodetect contract is a *Content-Length* line that opens the
        # block; a block whose first line is some other header is not
        # recognised as one.) The drain consumes the extras without ever
        # handing them to the dispatcher as payloads.
        client._buffer += b"Content-Length: 8\r\n"
        assert client._next_payload() is None  # the block is open
        client._buffer += b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        assert client._next_payload() is None  # the extra header is drained, not parsed
        client._buffer += b"\r\n"
        assert client._next_payload() is None  # separator consumed, body pending
        client._buffer += b'{"a": 1}'
        assert client._next_payload() == '{"a": 1}'
        assert client._next_payload() is None

    def test_a_bad_content_length_hands_the_dispatcher_an_unparsable_payload(self) -> None:
        client = MCPClient(("true",))
        # The bad header becomes an empty payload, which the dispatcher's
        # malformed-line path skips — one bad frame must not kill the loop.
        client._buffer += b"Content-Length: nan\r\n\r\n"
        assert client._next_payload() == ""
        assert client._next_payload() is None


# ---------------------------------------------------------------------------
# C5.3 — the child runs under the session's containment facts
# ---------------------------------------------------------------------------


class TestSandboxedSpawn:
    async def test_the_child_runs_under_the_sessions_facts(
        self, server_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child's self-report is the witness: cwd, pid, env, writes.

        The ``facts`` tool reports what the child itself sees: its cwd is the
        resolved workspace jail (its one writable directory — the write
        lands there), its pid is the session's root child, and its
        environment is the backend bootstrap set plus the caller's explicit
        allowlist — an ambient sentinel planted in the *parent's* environment
        must be absent, because the session's env is built from empty, never
        inherited. The resource envelope shows up in the close result's
        honest fields: this backend enforces memory through the sampled
        watchdog (Job Object where the platform has one) — there is
        deliberately no ``setrlimit`` deal on the session path, so the
        assertion is on the enforcement the backend actually performs.
        """
        monkeypatch.setenv(CANARY_VAR, "super-secret-do-not-leak")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        client = MCPClient(_command(server_script, "facts"))
        await client.start(
            SubprocessSandbox(), workspace=workspace, env={"PAA_MCP_ALLOWED": "yes"}
        )
        try:
            await client.initialize()
            result = await client.call_tool("facts", {})
            facts = result["structuredContent"]
        finally:
            closed = await client.close()

        assert Path(facts["cwd"]).resolve() == workspace.resolve()
        assert facts["pid"] == client.pid
        # The env allowlist: the deliberate variable arrives, ambient ones do not.
        assert facts["allowed"] == "yes"
        assert facts["ambient_leak"] is None
        keys = facts["env_keys"]
        assert CANARY_VAR not in keys
        assert "PAA_MCP_ALLOWED" in keys
        # A whole-environment splat would be dozens of variables; the
        # bootstrap set plus the allowlist is a handful.
        assert len(keys) < 20, f"child environment is suspiciously large: {sorted(keys)}"
        # The filesystem jail is the cwd: the child's write landed inside it.
        assert (workspace / "probe.txt").read_text(encoding="utf-8") == (
            "written-from-the-child"
        )
        # The teardown is the backend's own classification, not a guess.
        assert closed is not None
        assert closed.backend == "subprocess"
        assert closed.isolation_level is IsolationLevel.PROCESS
        assert closed.memory_enforcement in ("watchdog", "job_object")
        assert closed.killed_reason == "closed"

    async def test_a_scratch_workspace_the_client_owns_is_removed_on_close(
        self, server_script: Path
    ) -> None:
        """No workspace left behind: the default scratch dir is the client's."""
        client = MCPClient(_command(server_script, "normal"))
        await client.start(SubprocessSandbox())
        workspace = client.workspace
        assert workspace is not None and workspace.is_dir()
        await client.initialize()
        await client.close()
        assert workspace.exists() is False


# ---------------------------------------------------------------------------
# C5.3 — the deterministic allowlist and the denial ledger event
# ---------------------------------------------------------------------------


class TestPolicyAllowlist:
    async def test_discover_hides_tools_the_mode_does_not_grant(
        self, server_script: Path
    ) -> None:
        """The catalogue itself is filtered by manifest ∩ mandate ∩ policy.

        Under ASK the echo tool's declared permissions are granted, so the
        tool is exposed; under LOCKDOWN egress is not granted, so the same
        server's tool is not — discovery never surfaces a capability the
        runtime would then refuse to dispatch.
        """
        granted = McpServerAdapter(
            _command(server_script, "normal"), sandbox=SubprocessSandbox()
        )
        assert [c.skill_name for c in await granted.discover()] == ["echo"]

        locked = McpServerAdapter(
            _command(server_script, "normal"),
            sandbox=SubprocessSandbox(),
            permission_mode=PermissionMode.LOCKDOWN,
        )
        assert await locked.discover() == []

    async def test_a_denied_call_is_refused_locally_and_journalled(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        """The denial path: no spawn, a TOOL_CALL_DENIED row, nothing executed.

        The discovering adapter runs under ASK (egress granted); the invoking
        adapter is bound to LOCKDOWN, whose grants exclude PERM_NET_EGRESS.
        The backend is a double whose ``open()`` fails the test — the gate
        must refuse before the first spawn, so a child that was never started
        is the strongest form of "the fake server never received the call".
        The ledger carries the refusal lineage in the MCP server's own shape
        (PROPOSED then DENIED, on a per-call lineage), and the DENIED row is
        an *outcome* the recovery sweep never re-queues — a denied call is
        never retried as though it had been lost.
        """
        discoverer = McpServerAdapter(
            _command(server_script, "normal"), sandbox=SubprocessSandbox()
        )
        contract = (await discoverer.discover())[0]

        refuser = McpServerAdapter(
            _command(server_script, "normal"),
            sandbox=_NoSpawnSandbox(),
            permission_mode=PermissionMode.LOCKDOWN,
            ledger=ledger,
        )
        with pytest.raises(PermissionDeniedError) as exc_info:
            await refuser.invoke(contract, {"message": "must not run"}, timeout=10.0)
        assert "PERM_NET_EGRESS" in exc_info.value.missing

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        kinds = [row.event_type for row in rows]
        assert kinds == [EventType.TOOL_CALL_PROPOSED, EventType.TOOL_CALL_DENIED]
        assert rows[-1].payload["tool"] == "echo"
        assert "PERM_NET_EGRESS" in rows[-1].payload["reason"]
        # The sweep never sees an outcome-less PROPOSED here: the denial is
        # the outcome, so nothing is re-queued as interrupted.
        assert find_interrupted_calls(rows) == []

    async def test_a_mandate_that_omits_the_permission_denies_too(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        """The mandate leg of the intersection tightens the same way.

        An empty mandate allowlist grants nothing, so the intersection is
        empty even under ASK: the call is refused before the spawn, the
        DENIED row names the mandate as the binding leg, and the mode alone
        would have granted what the mandate removed.
        """
        refuser = McpServerAdapter(
            _command(server_script, "normal"),
            sandbox=_NoSpawnSandbox(),
            mandate_permissions=frozenset(),
            ledger=ledger,
        )
        with pytest.raises(PermissionDeniedError):
            await refuser.invoke(_echo_contract(), {"message": "x"}, timeout=10.0)

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        assert [row.event_type for row in rows] == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_DENIED,
        ]
        assert "mandate" in rows[-1].payload["reason"]


# ---------------------------------------------------------------------------
# C5.3 — secrets reach the child only through the proxy
# ---------------------------------------------------------------------------


class TestSecretDelivery:
    async def test_a_secret_reaches_the_child_only_through_the_proxy(
        self, server_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env_allowlist travels the broker, never the parent's environ.

        The same *name* carries a different value in the parent's ambient
        environment than in the broker: the child must see the broker's
        value — proving the value travelled the invocation's deliberate env
        allowlist and not inheritance — while an unrelated ambient sentinel
        stays absent. No ledger is bound to this invocation, so the revealed
        value has nowhere to leak into; the test is the value's only reader.
        """
        monkeypatch.setenv("PAA_TEST_TOOL_TOKEN", "ambient-wrong-value")
        monkeypatch.setenv(CANARY_VAR, "never-inherit-me")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        adapter = McpServerAdapter(
            _command(server_script, "facts"), sandbox=SubprocessSandbox()
        )
        contract = (await adapter.discover())[0]
        data = contract.model_dump(mode="json")
        data["invocation"]["env_allowlist"] = ["PAA_TEST_TOOL_TOKEN"]
        contract = SkillContract.parse(data)

        result = await adapter.invoke(
            contract,
            {},
            sandbox=SubprocessSandbox(),
            timeout=10.0,
            secret_broker=_FakeBroker(
                {"PAA_TEST_TOOL_TOKEN": SecretValue("PAA_TEST_TOOL_TOKEN", "tok-via-broker-42")}
            ),
        )
        assert result.ok
        facts = result.output
        assert facts["token"] == "tok-via-broker-42"
        assert facts["ambient_leak"] is None
        assert CANARY_VAR not in facts["env_keys"]

    async def test_an_env_allowlist_without_a_broker_refuses(self) -> None:
        """A contract that names secrets but no proxy is an unspeakable one.

        Failing loudly here beats a server that starts, mysteriously lacks
        its credential, and fails far away from the cause.
        """
        contract = _echo_contract(env_allowlist=["PAA_TEST_TOOL_TOKEN"])
        adapter = McpServerAdapter(("python", "fake_mcp_server.py"))
        with pytest.raises(SkillContractError, match="secret broker"):
            await adapter.invoke(contract, {}, sandbox=SubprocessSandbox(), timeout=10.0)

    async def test_a_denied_call_never_resolves_a_secret(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        """The gate precedes the secret resolution, not just the spawn.

        The docstring's security claim is an ordering fact: a refused call
        never resolves — never even touches — a secret value, because
        ``_invocation_env`` runs only after the policy gate passes. The
        broker here fails the test if it is ever consulted, and the contract
        names a secret, so the only green outcome is the gate refusing
        first, with the denial row journalled as the outcome.
        """
        contract = _echo_contract(env_allowlist=["PAA_TEST_TOOL_TOKEN"])
        refuser = McpServerAdapter(
            _command(server_script, "normal"),
            sandbox=_NoSpawnSandbox(),
            permission_mode=PermissionMode.LOCKDOWN,
            ledger=ledger,
        )
        with pytest.raises(PermissionDeniedError):
            await refuser.invoke(
                contract,
                {"message": "must not run"},
                timeout=10.0,
                secret_broker=_TouchFailsBroker(),
            )
        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        assert [row.event_type for row in rows] == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_DENIED,
        ]


# ---------------------------------------------------------------------------
# C5.3 — kill and cancel: no orphan, a classified outcome, no repeat
# ---------------------------------------------------------------------------


class TestKillAndCancel:
    async def test_close_mid_conversation_leaves_no_orphan(
        self, server_script: Path
    ) -> None:
        """Closing a live conversation kills the child, classified, promptly.

        The fake server is mid-conversation: it has answered and gone back to
        reading its stdin. ``close()`` ends the whole session — no orphan
        holds the pipe — reports the kill with its honest classification
        (``killed_reason="closed"``: the *caller* ended a live session), and
        returns well inside the 5-second cancellation contract.
        """
        client = MCPClient(_command(server_script, "normal"))
        await client.start(SubprocessSandbox())
        await client.initialize()
        reply = await client.call_tool("echo", {"message": "hi"})
        assert reply["structuredContent"] == {"echoed": "hi"}

        started = time.monotonic()
        result = await client.close()
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"close took {elapsed:.2f}s — the child was not killed promptly"
        assert result is not None
        assert result.killed_reason == "closed"
        assert result.exit_code is not None and result.exit_code < 0
        assert client.pid is not None
        await _wait_until_pid_gone(client.pid)

    async def test_cancel_mid_call_journals_a_classified_outcome_and_kills_the_tree(
        self, server_script: Path, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """A cancelled MCP call is an outcome row, never an unknown to repeat.

        The ``tree`` mode server answers its tool by spawning a grandchild
        sleeper and hanging — a real tree the cancel must reap. The cancel
        lands mid-call: the adapter re-raises CancelledError promptly (the
        5-second budget), the ledger already carries PROPOSED + FAILED — a
        *classified* outcome, so the recovery sweep never sees an
        outcome-less PROPOSED to re-queue, which is the "a retry does not
        repeat the lost effect" property — and both the root child and its
        grandchild are gone: no orphan holds capacity after the ledger says
        the work ended.
        """
        sandbox = _RecordingSandbox(SubprocessSandbox())
        adapter = McpServerAdapter(
            _command(server_script, "tree", str(tmp_path)),
            sandbox=sandbox,
            ledger=ledger,
        )
        contract = _echo_contract()

        task = asyncio.create_task(adapter.invoke(contract, {"message": "hang"}, timeout=30.0))
        try:
            await _wait_for_tool_rows(ledger, 1)  # PROPOSED: before any effect
            grandchild_pid = int(await _wait_for_file(tmp_path / "grandchild.pid"))
        except BaseException:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"cancel took {elapsed:.2f}s, budget is 5s"
        root_pid = sandbox.pids[-1]
        await _wait_until_pid_gone(root_pid)
        await _wait_until_pid_gone(grandchild_pid)

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        kinds = [row.event_type for row in rows]
        assert kinds == [EventType.TOOL_CALL_PROPOSED, EventType.TOOL_CALL_FAILED]
        assert rows[-1].payload["result_sha256"] is None, "a cancel attests no result hash"
        # The sweep only closes PROPOSED rows with no outcome; the FAILED row
        # already present means this cancel is never re-queued as interrupted.
        assert find_interrupted_calls(rows) == []


# ---------------------------------------------------------------------------
# the adapter, end to end
# ---------------------------------------------------------------------------


class TestAdapter:
    async def test_discover_maps_tools_to_contracts(self, server_script: Path) -> None:
        adapter = McpServerAdapter(
            _command(server_script, "normal"), sandbox=SubprocessSandbox()
        )
        contracts = await adapter.discover()
        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.skill_name == "echo"
        assert contract.provider == "mcp_server"
        assert contract.invocation.kind == "mcp_tool"
        assert contract.invocation.target == "echo"

    async def test_invoke_happy_path_journals_completed(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        adapter = McpServerAdapter(
            _command(server_script, "normal"), sandbox=SubprocessSandbox(), ledger=ledger
        )
        contract = (await adapter.discover())[0]
        result = await adapter.invoke(
            contract, {"message": "roundtrip"}, sandbox=None, timeout=10.0, secret_broker=None
        )
        assert result.ok
        assert result.output == {"echoed": "roundtrip"}
        assert contract.validate_output(result.output) == []

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        assert [row.event_type for row in rows] == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_COMPLETED,
        ]
        assert rows[-1].payload["result_sha256"] is not None
        assert find_interrupted_calls(rows) == []

    async def test_invoke_reports_crash_as_failure(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        adapter = McpServerAdapter(
            _command(server_script, "crash"), sandbox=SubprocessSandbox(), ledger=ledger
        )
        contract = (
            await McpServerAdapter(
                _command(server_script, "normal"), sandbox=SubprocessSandbox()
            ).discover()
        )[0]
        result = await adapter.invoke(
            contract, {"message": "x"}, sandbox=None, timeout=10.0, secret_broker=None
        )
        assert result.ok is False
        assert "connection" in (result.error or "").lower()

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        assert [row.event_type for row in rows] == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_FAILED,
        ]
        assert find_interrupted_calls(rows) == []

    async def test_invoke_reports_timeout_as_failure(
        self, server_script: Path, ledger: LedgerStore
    ) -> None:
        adapter = McpServerAdapter(
            _command(server_script, "timeout"), sandbox=SubprocessSandbox(), ledger=ledger
        )
        contract = (
            await McpServerAdapter(
                _command(server_script, "normal"), sandbox=SubprocessSandbox()
            ).discover()
        )[0]
        result = await adapter.invoke(
            contract, {"message": "x"}, sandbox=None, timeout=0.3, secret_broker=None
        )
        assert result.ok is False
        assert "timed out" in (result.error or "").lower()

        await _wait_for_tool_rows(ledger, 2)
        rows = await _tool_rows(ledger)
        assert [row.event_type for row in rows] == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_FAILED,
        ]

    async def test_invoke_without_a_sandbox_refuses(self, server_script: Path) -> None:
        """The claw_hub rule, applied to MCP: no sandbox, no spawn."""
        adapter = McpServerAdapter(_command(server_script, "normal"))
        with pytest.raises(SkillContractError, match="require a sandbox"):
            await adapter.invoke(
                _echo_contract(), {}, sandbox=None, timeout=10.0, secret_broker=None
            )

    async def test_discover_without_a_sandbox_refuses(self, server_script: Path) -> None:
        """Discovery spawns the same child — it needs the same containment."""
        adapter = McpServerAdapter(_command(server_script, "normal"))
        with pytest.raises(SkillContractError, match="requires a sandbox"):
            await adapter.discover()
