"""The MCP policy proxy — the harness's tool calls, routed through PAA (C7.3).

This is the second half of "one deterministic policy across harnesses".
The first half (C5.3) was PAA as a *client* of foreign MCP servers,
sandboxed; this is PAA as the *server* a harness points its tool calls
at. The harness spawns ``paa record proxy --run-dir <dir>`` as its MCP
server (the run's env carries the dir in ``PAA_RECORD_RUN_DIR``, and
the ready-to-paste stanza in ``PAA_RECORD_PROXY_CMD``), and from then
on every tool call it makes is, in order:

1. **journalled** — a ``tool_call`` event lands in the hash-chained
   flight record before anything is decided, so even a refused call is
   a fact about the run;
2. **decided by the run's policy** — the same
   :class:`~paa.coder.permissions.ToolPermissionPolicy` the coder loop
   uses, configured once by the operator (``--mode`` / ``--allow`` /
   ``--deny`` / ``--ask`` at ``paa record run`` time) and recorded in
   the run's summary, so the record proves what policy was in force
   rather than asking the reader to trust a config that is no longer
   there;
3. **executed under containment** — commands through the sandbox's
   one-shot contract, file tools jailed in-process
   (:mod:`paa.recorder.tools` states the split);
4. **answered** — with the result (or the refusal) and a
   ``tool_result`` / ``policy_denied`` event beside it.

An ``ask`` decision is the one place the proxy cannot honour the
policy's intent: there is no human on a stdio pipe. It resolves to a
**denial**, recorded as ``resolution="denied_unattended"`` with the
raw ``decision="ask"`` kept beside it — the record never hides that a
human was needed and absent, and the error message names the fix (a
pre-allow rule).

No auth token, on purpose. The C4.5 server guards a long-lived
daemon's state across clients, so it demands a token before it will
say anything. This proxy is an ephemeral per-run stdio child: whoever
can spawn it already holds local process authority in the workspace,
and a token here would be theatre — the honest integrity boundary for
what the proxy journalled is the record's hash chain and the attested
bundle's signature, both of which a hostile local writer breaks only
visibly.

Framing, error codes and the protocol version are
:mod:`paa.mcp.protocol`'s, verbatim — one framing codebase serves the
MCP server, the MCP client and this proxy, which is the SPEC's own
rule (W7 C7.6's "one framing kit" pulled forward to every surface that
exists today).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paa.coder.permissions import (
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.core.errors import PaaError
from paa.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PERMISSION_DENIED,
    PROTOCOL_VERSION,
    FrameSink,
    McpError,
    encode_frame,
    error_response,
    read_frame,
    result_response,
)
from paa.recorder.record import RecordError, RunRecord, read_summary
from paa.recorder.tools import TOOL_SPECS, execute_tool, validate_arguments
from paa.sandbox.base import Sandbox

__all__ = ["RecordProxy", "policy_from_summary"]

#: How the proxy renders in the initialize handshake.
PROXY_NAME = "paa-record-proxy"

#: Cap on the journalled rendering of a call's arguments — the record is
#: evidence, not a dump; the executed tool sees the full arguments.
_ARGS_JOURNAL_CAP = 4096


def policy_from_summary(summary: dict[str, Any]) -> ToolPermissionPolicy:
    """Rebuild the run's policy from the recorded summary.

    The summary is the single source of truth for what policy was in
    force — the proxy reads it rather than accepting policy over its own
    command line, so two proxies serving one run cannot disagree about
    the rules, and a reviewer reading the record sees exactly what the
    proxy saw.
    """
    raw = summary.get("policy") or {}
    mode = str(raw.get("mode") or "ask")
    try:
        parsed_mode = PermissionMode(mode)
    except ValueError as exc:
        raise RecordError(
            f"the run's recorded policy mode {mode!r} is not one of "
            f"{[m.value for m in PermissionMode]} — the summary is corrupt"
        ) from exc
    return ToolPermissionPolicy(
        mode=parsed_mode,
        allow=list(raw.get("allow") or []),
        deny=list(raw.get("deny") or []),
        ask=list(raw.get("ask") or []),
    )


class RecordProxy:
    """One run's MCP server over stdio: journal, decide, execute, answer.

    Constructed against a run directory (the summary supplies the
    workspace and the policy) and a sandbox (the executor). One
    instance serves one client connection, exactly like the C4.5
    server; :meth:`serve` frames NDJSON until the client closes the
    pipe.
    """

    def __init__(self, run_dir: Path, sandbox: Sandbox) -> None:
        self._run_dir = Path(run_dir)
        self._sandbox = sandbox
        self._record = RunRecord.open_existing(run_dir)
        summary = read_summary(run_dir)
        self._summary = summary
        self._workspace = Path(str(summary["workspace"]))
        self._policy = policy_from_summary(summary)
        self._initialized = False
        self._calls = 0
        self._denied = 0

    # -- the framing loop ---------------------------------------------------

    async def serve(self, reader: Any, sink: FrameSink) -> None:
        """Serve NDJSON requests from ``reader`` until end of stream.

        One frame in, one reply out, in order — the JSON-RPC 2.0 stdio
        shape, identical to the C4.5 server's loop. On EOF the proxy
        writes its ``proxy_closed`` event: the harness hung up, and the
        record says so.
        """
        if self._record.phase == "final":
            raise RecordError(
                f"{self._run_dir} is already finalised — the proxy serves a "
                "live run, and one that ended has nothing left to mediate"
            )
        while True:
            payload = await read_frame(reader)
            if payload is None:
                self._record.append(
                    "proxy_closed",
                    {"reason": "eof", "tool_calls": self._calls, "denied": self._denied},
                )
                return
            response = await self.handle_payload(payload)
            if response is None:
                continue
            sink.write(encode_frame(response))
            await sink.drain()

    async def handle_payload(self, payload: str) -> dict[str, Any] | None:
        """Parse one raw frame and dispatch it. Parse failures reply ``-32700``."""
        try:
            message: Any = json.loads(payload)
        except json.JSONDecodeError:
            return error_response(None, PARSE_ERROR, "frame is not valid JSON")
        return await self.handle_message(message)

    # -- the request handler -------------------------------------------------

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Route one decoded JSON-RPC message. ``None`` for notifications.

        Total over any input, the C4.5 discipline: a bad frame gets the
        honest protocol answer and the loop continues; only a record
        failure (the chain refusing an append) ends the connection,
        because a proxy that cannot journal must not keep mediating.
        """
        if not isinstance(message, dict):
            return error_response(None, INVALID_REQUEST, "frame is not a JSON object")
        method = message.get("method")
        request_id = message.get("id")
        has_id = "id" in message
        if method is None:
            return error_response(None, INVALID_REQUEST, "request object carries no method")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                return error_response(request_id, INVALID_PARAMS, "params must be an object")
            return None
        if not has_id:
            return None  # notifications are acknowledged by silence
        try:
            result = await self._dispatch(method, params)
        except McpError as exc:
            return error_response(request_id, exc.code, exc.message, exc.details or None)
        except RecordError:
            # The chain refused the append: mediating without journaling
            # would be exactly the quiet unrecorded action this proxy
            # exists to prevent — the honest move is to stop serving.
            raise
        except PaaError as exc:
            from paa.models.base import redact

            return error_response(request_id, PERMISSION_DENIED, redact(str(exc)), None)
        except Exception:  # the loop's total-over-input rule
            return error_response(
                request_id,
                -32603,
                "internal error",
            )
        return result_response(request_id, result)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._handle_initialize(params)
        if not self._initialized:
            raise McpError(
                -32009,
                "handshake not completed: send initialize first",
            )
        if method == "tools/list":
            return {"tools": [dict(spec) for spec in TOOL_SPECS.values()]}
        if method == "tools/call":
            return await self._handle_call(params)
        if method == "ping":
            return {}
        raise McpError(METHOD_NOT_FOUND, f"unknown method {method!r}")

    # -- the methods ----------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """The handshake: pin the version, journal the proxy's arrival.

        The version contract is the C4.5 server's own: the client names
        a revision, the reply carries this proxy's pinned one — the
        client decides what to do about the difference.
        """
        version = params.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise McpError(INVALID_PARAMS, "initialize requires params.protocolVersion")
        self._initialized = True
        self._record.append(
            "proxy_started",
            {
                "proxy": PROXY_NAME,
                "protocol_version": PROTOCOL_VERSION,
                "client_version": version,
                "workspace": str(self._workspace),
                "policy_mode": self._policy.mode.value,
            },
        )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": PROXY_NAME,
                "version": self._summary.get("recorder_version"),
            },
            "instructions": (
                "PAA record proxy: bash, read_file, write_file — every call "
                "journalled in this run's hash-chained record, decided by "
                "the run's policy, executed under containment."
            ),
        }

    async def _handle_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise McpError(INVALID_PARAMS, "tools/call needs a 'name' string")
        if not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "tools/call 'arguments' must be an object")

        # 1. The attempt is a fact before it is a decision.
        self._record.append(
            "tool_call",
            {"tool": name, "arguments": _journal_args(name, arguments), "via": "proxy"},
        )

        # 2. Shape: nonsense cannot be weighed by policy.
        try:
            normalised = validate_arguments(name, arguments)
        except ValueError as exc:
            raise McpError(INVALID_PARAMS, str(exc)) from exc

        # 3. The deterministic policy.
        decision = self._policy.evaluate(name, normalised)
        if decision.outcome is not PermissionOutcome.ALLOW:
            if decision.outcome is PermissionOutcome.ASK:
                resolution = "denied_unattended"
                reason = (
                    "the policy asks for a human on this call and the record "
                    "proxy has no interactive door — pre-allow it with a "
                    "--allow rule on paa record run"
                )
            else:
                resolution = "denied"
                reason = decision.reason
            self._denied += 1
            self._record.append(
                "policy_denied",
                {
                    "tool": name,
                    "decision": decision.outcome.value,
                    "resolution": resolution,
                    "reason": reason,
                    "rule": decision.rule,
                },
            )
            raise McpError(PERMISSION_DENIED, reason, tool=name, resolution=resolution)

        # 4. Execute, journal, answer. The normalised argv rides the
        # tool_result event itself (not just the tool_call before it):
        # the attested bundle's command rows read tool_result events
        # alone, so a row can never mis-pair a call with another
        # concurrent proxy's result.
        self._calls += 1
        execution = await execute_tool(self._sandbox, self._workspace, name, normalised)
        self._record.append(
            "tool_result",
            {
                "tool": name,
                "argv": normalised.get("argv"),
                "policy": decision.reason,
                **execution.to_json(),
            },
        )
        return {
            "content": [{"type": "text", "text": _render_execution(name, execution)}],
            "isError": not execution.ok,
        }


# ---------------------------------------------------------------------------
# Rendering helpers — what rides the frames and the journal
# ---------------------------------------------------------------------------


def _journal_args(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The journalable rendering of a call's arguments.

    ``bash`` argv is joined for the journal exactly the way the policy
    renders it for matching (``git status``, not ``["git", "status"]``)
    — the representation-independence rule — with the raw argv kept
    beside it, capped: the record is evidence, not a dump.
    """
    rendered: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str):
            rendered[key] = _cap(value)
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
            rendered[key] = _cap(text)
    return rendered


def _cap(text: str) -> str:
    if len(text) <= _ARGS_JOURNAL_CAP:
        return text
    return text[:_ARGS_JOURNAL_CAP] + f"… [truncated at {_ARGS_JOURNAL_CAP} chars]"


def _render_execution(tool: str, execution: Any) -> str:
    """The tool result's text, as the harness sees it."""
    if tool == "bash":
        parts: list[str] = []
        for stream in execution.streams:
            if stream.get("text"):
                parts.append(str(stream["text"]))
                if stream.get("truncated"):
                    parts.append(f"… [stdout truncated at {stream.get('bytes')} bytes]")
        if execution.error:
            parts.append(str(execution.error))
        if not parts:
            parts.append(f"(no output; exit {execution.exit_code})")
        return "\n".join(parts)
    if tool == "read_file":
        if execution.error:
            return str(execution.error)
        return str(execution.streams[0]["text"]) if execution.streams else ""
    # write_file
    if execution.error:
        return str(execution.error)
    return f"wrote {execution.bytes_written} bytes to {execution.path}"
