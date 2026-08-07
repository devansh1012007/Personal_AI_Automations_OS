"""McpServerAdapter + MCPClient against a fake stdio MCP server.

The fake server is a tiny Python script whose behaviour is chosen by an argv
flag, so every transport failure mode (crash, malformed line, unknown id,
timeout) is a deterministic, separately exercised path rather than a flake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from paa.skills.adapters.mcp import MCPClient, McpServerAdapter

# A fake MCP server. Reads newline-delimited JSON-RPC from stdin; its reply
# framing and failure behaviour are picked by argv[1]. Writes through the raw
# buffer so Windows text-mode never rewrites the Content-Length bodies.
_FAKE_SERVER = r'''
import sys, json, time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

def _write(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()

def send_line(obj):
    _write(json.dumps(obj) + "\n")

def send_cl(obj):
    data = json.dumps(obj)
    _write("Content-Length: %d\r\n\r\n%s" % (len(data.encode("utf-8")), data))

send = send_cl if mode == "cl_read" else send_line

TOOL = {
    "name": "echo",
    "description": "Echo the given message straight back to the caller.",
    "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
    "outputSchema": {"type": "object", "properties": {"echoed": {"type": "string"}},
                     "required": ["echoed"]},
}

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
        args = msg["params"].get("arguments", {})
        if mode == "crash":
            sys.exit(1)
        if mode == "timeout":
            time.sleep(30)
            continue
        if mode == "malformed":
            _write("this line is not valid json\n")
        if mode == "unknown_id":
            send_line({"jsonrpc": "2.0", "id": 987654, "result": {"content": []}})
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text",
                                      "text": json.dumps({"echoed": args.get("message", "")})}],
                         "structuredContent": {"echoed": args.get("message", "")},
                         "isError": False}})
'''


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(_FAKE_SERVER, encoding="utf-8")
    return script


def _command(script: Path, mode: str) -> list[str]:
    return [sys.executable, str(script), mode]


class TestClientHandshake:
    async def test_initialize_list_and_call(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "normal"))
        await client.start()
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
        await client.start()
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
        await client.start()
        try:
            await client.initialize()
            with pytest.raises(ConnectionError):
                await client.call_tool("echo", {"message": "x"}, timeout=10.0)
        finally:
            await client.close()

    async def test_malformed_json_line_is_skipped(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "malformed"))
        await client.start()
        try:
            await client.initialize()
            # The bad line is dropped; the correct reply still resolves the call.
            result = await client.call_tool("echo", {"message": "ok"}, timeout=10.0)
            assert result["structuredContent"] == {"echoed": "ok"}
        finally:
            await client.close()

    async def test_unknown_response_id_is_ignored(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "unknown_id"))
        await client.start()
        try:
            await client.initialize()
            # A reply with an id we never sent is dropped; ours still lands.
            result = await client.call_tool("echo", {"message": "y"}, timeout=10.0)
            assert result["structuredContent"] == {"echoed": "y"}
        finally:
            await client.close()

    async def test_request_timeout_raises_timeout_error(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "timeout"))
        await client.start()
        try:
            await client.initialize()
            with pytest.raises(TimeoutError):
                await client.call_tool("echo", {"message": "z"}, timeout=0.3)
        finally:
            await client.close()

    async def test_call_after_close_raises(self, server_script: Path) -> None:
        client = MCPClient(_command(server_script, "normal"))
        await client.start()
        await client.initialize()
        await client.close()
        with pytest.raises(ConnectionError):
            await client.call_tool("echo", {"message": "x"})


class TestAdapter:
    async def test_discover_maps_tools_to_contracts(self, server_script: Path) -> None:
        adapter = McpServerAdapter(_command(server_script, "normal"))
        contracts = await adapter.discover()
        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.skill_name == "echo"
        assert contract.provider == "mcp_server"
        assert contract.invocation.kind == "mcp_tool"
        assert contract.invocation.target == "echo"

    async def test_invoke_happy_path(self, server_script: Path) -> None:
        adapter = McpServerAdapter(_command(server_script, "normal"))
        contract = (await adapter.discover())[0]
        result = await adapter.invoke(
            contract, {"message": "roundtrip"}, sandbox=None, timeout=10.0, secret_broker=None
        )
        assert result.ok
        assert result.output == {"echoed": "roundtrip"}
        assert contract.validate_output(result.output) == []

    async def test_invoke_reports_crash_as_failure(self, server_script: Path) -> None:
        adapter = McpServerAdapter(_command(server_script, "crash"))
        contract = McpServerAdapter(_command(server_script, "normal"))
        discovered = (await contract.discover())[0]
        result = await adapter.invoke(
            discovered, {"message": "x"}, sandbox=None, timeout=10.0, secret_broker=None
        )
        assert result.ok is False
        assert "connection" in (result.error or "").lower()

    async def test_invoke_reports_timeout_as_failure(self, server_script: Path) -> None:
        adapter = McpServerAdapter(_command(server_script, "timeout"))
        discovered = McpServerAdapter(_command(server_script, "normal"))
        contract = (await discovered.discover())[0]
        result = await adapter.invoke(
            contract, {"message": "x"}, sandbox=None, timeout=0.3, secret_broker=None
        )
        assert result.ok is False
        assert "timed out" in (result.error or "").lower()
