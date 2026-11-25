"""A fake external ACP agent — the client-front test child (stdlib only).

``paa acp client`` spawns an external ACP agent behind a
:class:`~paa.sandbox.base.SandboxSession` and mediates it. This file is the
smallest honest external agent: a real child process speaking real NDJSON
JSON-RPC over its stdin/stdout, with a ``--scenario`` switch that decides
what it does inside one prompt turn. It imports nothing from paa — an
external agent is not the runtime's code, and a test that shared an
implementation with its counterparty could pass for the wrong reason.

Inbound framing is a reader thread feeding a queue; the main loop pulls from
it, so a ``session/cancel`` can be observed *between* streamed chunks — the
cooperative-cancel property the slow scenario exists to prove. The agent
reports what it observed (permission verdicts, file reads, client errors) as
a final ``session/update`` message chunk whose text is JSON, because its
stdout is a wire, not a display: the protocol itself is the only out-of-band
channel it has.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from queue import Empty, Queue

REPLY_TIMEOUT = 10.0

#: The slow scenario's cadence: how long one poll window waits for a cancel
#: before serving the next tick. The full 200-tick turn must fit inside the
#: client-side reply budget with room to spare — the *second* turn of the
#: cancel test runs to completion uncancelled, and a cadence that makes that
#: turn slower than the test's wait proves nothing about cancellation.
SLOW_POLL_SECONDS = 0.02
SLOW_TICKS = 200


class Agent:
    """One scripted ACP agent over blocking stdio."""

    def __init__(
        self,
        scenario: str,
        workspace: str,
        tool_name: str,
        tool_command: str,
    ) -> None:
        self._scenario = scenario
        self._workspace = workspace
        self._tool_name = tool_name
        self._tool_command = tool_command
        self._inbox: Queue[str | None] = Queue()
        self._next_id = 100
        self._session_id = "fake-agent-session"
        self._cancelled = False

    # -- plumbing ---------------------------------------------------------------

    def start_reader(self) -> None:
        import threading

        def _read() -> None:
            for line in sys.stdin:
                self._inbox.put(line)
            self._inbox.put(None)

        threading.Thread(target=_read, daemon=True).start()

    def send(self, message: dict[str, object]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def result(self, request_id: object, result: dict[str, object]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: object, code: int, message: str) -> None:
        self.send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    def update(self, session_id: str, text: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": "m1",
                        "content": {"type": "text", "text": text},
                    },
                },
            }
        )

    def next_message(self, timeout: float = REPLY_TIMEOUT) -> dict[str, object] | None:
        line = self._inbox.get(timeout=timeout)
        if line is None:
            return None
        decoded: dict[str, object] = json.loads(line)
        return decoded

    def await_reply(self, request_id: object) -> dict[str, object]:
        deadline = time.monotonic() + REPLY_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply to request {request_id!r}")
            message = self.next_message(timeout=remaining)
            if message is None:
                raise TimeoutError(f"stream closed awaiting reply to {request_id!r}")
            if message.get("id") == request_id and "method" not in message:
                return message

    def poll_cancel(self, session_id: str, seconds: float) -> bool:
        """Watch the inbox for ``seconds``; note a cancel for ``session_id``.

        The slow scenario calls this between chunks. Anything else that
        arrives is ignored — the scripted agent asks nothing of its client
        mid-turn, so only a cancel can appear here.
        """
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._cancelled
            try:
                message = self.next_message(timeout=remaining)
            except Empty:
                continue
            if self._notes_cancel(message, session_id):
                self._cancelled = True

    def _notes_cancel(self, message: dict[str, object] | None, session_id: str) -> bool:
        if not isinstance(message, dict):
            return False
        if message.get("method") != "session/cancel":
            return False
        params = message.get("params")
        return isinstance(params, dict) and params.get("sessionId") == session_id

    # -- the conversation ---------------------------------------------------------

    def handle(self, message: dict[str, object]) -> None:
        method = message.get("method")
        if method is None:
            return  # a reply the awaiters already took
        request_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            version = 2 if self._scenario == "badversion" else 1
            self.result(request_id, {"protocolVersion": version, "agentCapabilities": {}})
            return
        if method == "session/new":
            self.result(request_id, {"sessionId": self._session_id})
            return
        if method == "session/prompt":
            self.run_turn(request_id, params)
            return
        if method == "session/cancel":
            if self._notes_cancel(message, str(params.get("sessionId") or "")):
                self._cancelled = True
            return
        self.error(request_id, -32601, f"the fake agent has no method {method!r}")

    def run_turn(self, request_id: object, params: dict[str, object]) -> None:
        session_id = str(params.get("sessionId") or self._session_id)
        # A cancel belongs to the turn in flight: a cancel that landed
        # between turns (or answered the previous one) must not bleed into
        # this turn — the protocol's cancel is "stop the current prompt".
        self._cancelled = False
        scenario = self._scenario
        if scenario == "stream":
            for i in range(4):
                self.update(session_id, f"chunk-{i}")
            self.result(request_id, {"stopReason": "end_turn"})
            return
        if scenario == "slow":
            for i in range(SLOW_TICKS):
                if self.poll_cancel(session_id, SLOW_POLL_SECONDS):
                    self.result(request_id, {"stopReason": "cancelled"})
                    return
                self.update(session_id, f"tick-{i}")
            self.result(request_id, {"stopReason": "end_turn"})
            return
        if scenario == "permission":
            verdict = self.ask_permission(session_id)
            self.update(session_id, json.dumps({"permission": verdict}))
            self.result(request_id, {"stopReason": "end_turn"})
            return
        if scenario == "fs":
            seen = self.read_files(session_id)
            self.update(session_id, json.dumps({"fs": seen}))
            self.result(request_id, {"stopReason": "end_turn"})
            return
        if scenario == "unknown":
            reply = self.request(session_id, "session/load", {"sessionId": "nope"})
            self.update(session_id, json.dumps({"unknown": reply}))
            self.result(request_id, {"stopReason": "end_turn"})
            return
        self.result(request_id, {"stopReason": "end_turn"})

    def request(
        self, session_id: str, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {"sessionId": session_id, **params},
            }
        )
        return self.await_reply(request_id)

    def ask_permission(self, session_id: str) -> dict[str, object]:
        reply = self.request(
            session_id,
            "session/request_permission",
            {
                "toolCall": {
                    "toolCallId": "call-1",
                    "name": self._tool_name,
                    "rawInput": {"command": self._tool_command},
                },
                "options": [
                    {"kind": "allow_once", "optionId": "allow-1"},
                    {"kind": "reject_once", "optionId": "reject-1"},
                ],
            },
        )
        return _shaped(reply)

    def read_files(self, session_id: str) -> dict[str, object]:
        """Two reads: one in-scope file, one protected ``.git`` path."""
        import os

        good = self.request(
            session_id,
            "fs/read_text_file",
            {"path": os.path.join(self._workspace, "notes.txt")},
        )
        bad = self.request(
            session_id,
            "fs/read_text_file",
            {"path": os.path.join(self._workspace, ".git", "config")},
        )
        return {"good": _shaped(good), "git": _shaped(bad)}


def _shaped(reply: dict[str, object]) -> dict[str, object]:
    """One observed reply, trimmed to what the scenario reports."""
    if "error" in reply:
        error = reply["error"]
        assert isinstance(error, dict)
        return {"error": {"code": error.get("code"), "message": error.get("message")}}
    return {"result": reply.get("result")}


def main() -> int:
    parser = argparse.ArgumentParser(description="the fake external ACP agent")
    parser.add_argument(
        "--scenario",
        required=True,
        choices=("stream", "slow", "permission", "fs", "badversion", "unknown"),
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--tool-name", default="bash")
    parser.add_argument(
        "--unnamed-tool",
        action="store_true",
        help="ask with an empty tool name (the unclassifiable call)",
    )
    parser.add_argument("--tool-command", default="curl http://example.invalid/")
    args = parser.parse_args()

    agent = Agent(
        scenario=args.scenario,
        workspace=args.workspace,
        tool_name="" if args.unnamed_tool else args.tool_name,
        tool_command=args.tool_command,
    )
    agent.start_reader()
    while True:
        try:
            message = agent.next_message()
        except Empty:
            return 0  # the client went quiet mid-conversation; exit politely
        if message is None:
            return 0  # EOF: the mediator closed the session
        agent.handle(message)


if __name__ == "__main__":
    raise SystemExit(main())
