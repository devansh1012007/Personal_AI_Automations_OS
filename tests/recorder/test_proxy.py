"""The MCP policy proxy as a protocol participant (C7.3).

Driven in-process through the same handler the stdio loop calls, over a
real run record and the real subprocess sandbox — so the pinned
behaviours are the protocol's, the policy's and the journal's, not a
mock's. What is pinned: the handshake, the toolset, the
journalled-then-decided-then-executed ordering, the ask→denied
resolution, the deny-wins rule, the jail, and the framing loop itself
over real streams.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from paa.recorder.proxy import RecordProxy
from paa.recorder.record import RunRecord
from paa.sandbox.subprocess_backend import SubprocessSandbox


@pytest.fixture
def sandbox() -> SubprocessSandbox:
    return SubprocessSandbox()


def _record(tmp_path: Path, *, policy: dict) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("kept\n", encoding="utf-8")
    run_dir = tmp_path / "record" / "run1"
    RunRecord.create(
        run_dir,
        run_id="run1",
        harness=("fake-harness",),
        workspace=workspace,
        policy=policy,
        honesty={"mediated": "proxy calls", "observed": "everything else"},
        recorder_version="paa-recorder/test",
    )
    return run_dir


def _make_proxy(run_dir: Path, sandbox: SubprocessSandbox) -> RecordProxy:
    return RecordProxy(run_dir, sandbox)


def _call(proxy: RecordProxy, name: str, arguments: dict) -> dict:
    return asyncio.run(
        proxy.handle_payload(
            json.dumps(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": name, "arguments": arguments}}
            )
        )
    )


def _init(proxy: RecordProxy) -> dict:
    return asyncio.run(
        proxy.handle_payload(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}}
            )
        )
    )


class TestTheHandshake:
    def test_initialize_pins_the_protocol_version_and_journals_the_arrival(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "ask", "allow": [], "deny": [], "ask": []}
        )
        proxy = _make_proxy(run_dir, sandbox)
        reply = _init(proxy)
        assert reply["result"]["protocolVersion"] == "2024-11-05"
        assert reply["result"]["serverInfo"]["name"] == "paa-record-proxy"
        kinds = [e["kind"] for e in proxy._record.events()]
        assert kinds[-1] == "proxy_started"

    def test_a_call_before_the_handshake_is_refused(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "bypass", "allow": [], "deny": [], "ask": []}
        )
        proxy = _make_proxy(run_dir, sandbox)
        reply = _call(proxy, "bash", {"argv": ["true"]})
        assert reply["error"]["code"] == -32009

    def test_a_corrupt_policy_summary_is_refused_loudly(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "nonsense", "allow": [], "deny": [], "ask": []}
        )
        with pytest.raises(Exception, match="nonsense"):
            _make_proxy(run_dir, sandbox)

    def test_a_finalised_run_refuses_to_be_mediated_further(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(tmp_path, policy={"mode": "bypass", "allow": [], "deny": [], "ask": []})
        RunRecord.open_existing(run_dir).finalise()
        proxy = _make_proxy(run_dir, sandbox)
        with pytest.raises(Exception, match="already finalised"):
            asyncio.run(proxy.serve(_fake_reader([]), _null_sink()))


class TestTheToolset:
    def test_tools_list_serves_the_three_v1_tools(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "ask", "allow": [], "deny": [], "ask": []}
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = asyncio.run(
            proxy.handle_payload(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        )
        names = {tool["name"] for tool in reply["result"]["tools"]}
        assert names == {"bash", "read_file", "write_file"}

    def test_an_unknown_tool_is_invalid_params_and_still_journalled(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "bypass", "allow": [], "deny": [], "ask": []}
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "delete_everything", {"path": "/"})
        assert reply["error"]["code"] == -32602
        kinds = [e["kind"] for e in proxy._record.events()]
        assert "tool_call" in kinds  # the attempt is a fact before it is a decision

    def test_a_shapeless_bash_call_is_invalid_params(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path, policy={"mode": "bypass", "allow": [], "deny": [], "ask": []}
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "bash", {"argv": "git status"})
        assert reply["error"]["code"] == -32602


class TestThePolicy:
    def test_an_ask_decision_resolves_to_a_recorded_denial(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        """The unattended ask: the record keeps the raw decision beside it."""
        run_dir = _record(tmp_path, policy={"mode": "ask", "allow": [], "deny": [], "ask": []})
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "bash", {"argv": ["echo", "hi"]})
        assert reply["error"]["code"] == -32002
        assert "denied_unattended" in str(reply["error"].get("data"))
        denials = [e for e in proxy._record.events() if e["kind"] == "policy_denied"]
        assert denials[0]["data"]["decision"] == "ask"
        assert denials[0]["data"]["resolution"] == "denied_unattended"
        # and nothing executed: no tool_result event exists
        assert not [e for e in proxy._record.events() if e["kind"] == "tool_result"]

    def test_an_allow_rule_executes_and_journals_the_result(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "ask", "allow": ["bash(echo *)"], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "bash", {"argv": ["echo", "mediated"]})
        assert reply["result"]["isError"] is False
        assert "mediated" in reply["result"]["content"][0]["text"]
        results = [e for e in proxy._record.events() if e["kind"] == "tool_result"]
        assert len(results) == 1
        data = results[0]["data"]
        assert data["tool"] == "bash"
        assert data["argv"] == ["echo", "mediated"]
        assert data["executed"] == "sandbox"
        assert data["ok"] is True

    def test_a_deny_rule_wins_over_an_allow_rule(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={
                "mode": "ask",
                "allow": ["bash(*)"],
                "deny": ["bash(rm *)"],
                "ask": [],
            },
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "bash", {"argv": ["rm", "-rf", "anything"]})
        assert reply["error"]["code"] == -32002
        denials = [e for e in proxy._record.events() if e["kind"] == "policy_denied"]
        assert denials[0]["data"]["resolution"] == "denied"
        assert not [e for e in proxy._record.events() if e["kind"] == "tool_result"]

    def test_plan_mode_refuses_every_mutating_tool(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "plan", "allow": ["bash(*)"], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        for tool, args in (
            ("bash", {"argv": ["true"]}),
            ("write_file", {"path": "x.txt", "content": "x"}),
        ):
            reply = _call(proxy, tool, args)
            assert reply["error"]["code"] == -32002, tool


class TestContainment:
    def test_a_read_outside_the_jail_is_refused_before_anything_touches_disk(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "bypass", "allow": [], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "read_file", {"path": "/etc/passwd"})
        assert reply["result"]["isError"] is True
        assert "escapes the workspace jail" in reply["result"]["content"][0]["text"]

    def test_a_write_outside_the_jail_is_refused_by_the_jail_not_the_policy(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "bypass", "allow": [], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "write_file", {"path": "../escape.txt", "content": "no"})
        assert reply["result"]["isError"] is True
        assert not (tmp_path / "escape.txt").exists()

    def test_the_oversize_read_is_refused_with_the_count_named(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "bypass", "allow": [], "deny": [], "ask": []},
        )
        workspace = tmp_path / "ws"
        (workspace / "big.txt").write_text("x" * (256 * 1024 + 1), encoding="utf-8")
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "read_file", {"path": "big.txt"})
        assert reply["result"]["isError"] is True
        assert "over the" in reply["result"]["content"][0]["text"]

    def test_a_failed_command_is_a_tool_result_not_a_protocol_error(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        """The MCP convention: a dispatched tool that fails returns
        ``isError=true``; only protocol-level faults take the error path."""
        run_dir = _record(
            tmp_path,
            policy={"mode": "bypass", "allow": [], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        _init(proxy)
        reply = _call(proxy, "bash", {"argv": ["sh", "-c", "exit 3"]})
        assert "error" not in reply
        assert reply["result"]["isError"] is True
        results = [e for e in proxy._record.events() if e["kind"] == "tool_result"]
        assert results[0]["data"]["exit_code"] == 3
        assert results[0]["data"]["ok"] is False


class TestTheFramingLoop:
    def test_serve_frames_ndjson_until_eof_and_writes_proxy_closed(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "ask", "allow": ["bash(echo *)"], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "bash", "arguments": {"argv": ["echo", "framed"]}}},
        ]
        asyncio.run(proxy.serve(_fake_reader(frames), _capture_sink()))

        kinds = [e["kind"] for e in proxy._record.events()]
        assert kinds[-1] == "proxy_closed"
        assert proxy._record.events()[-1]["data"]["reason"] == "eof"
        assert proxy._record.events()[-1]["data"]["tool_calls"] == 1

    def test_a_bad_frame_gets_its_error_reply_and_the_loop_continues(
        self, tmp_path: Path, sandbox: SubprocessSandbox
    ) -> None:
        run_dir = _record(
            tmp_path,
            policy={"mode": "ask", "allow": [], "deny": [], "ask": []},
        )
        proxy = _make_proxy(run_dir, sandbox)
        frames = [
            "not json at all {{{",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        ]
        sink = _capture_sink()
        asyncio.run(proxy.serve(_fake_reader(frames), sink))
        assert sink.frames[0]["error"]["code"] == -32700
        assert sink.frames[1]["result"]["protocolVersion"] == "2024-11-05"
        assert sink.frames[2]["result"] == {}


# -- tiny stream doubles ------------------------------------------------------


class _fake_reader:
    """An asyncio.StreamReader-shaped iterable of frames."""

    def __init__(self, frames: list) -> None:
        self._lines = [
            json.dumps(f) if isinstance(f, dict) else f for f in frames
        ]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode("utf-8")


class _mixed_reader(_fake_reader):
    """The same, for a list that mixes raw strings and dicts."""


class _capture_sink:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None


class _null_sink:
    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None
