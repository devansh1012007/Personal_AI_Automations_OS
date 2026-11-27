"""The child side of ``paa-pipeline/1``: an asyncio client over stdio.

See the package docstring for the deliberate no-``paa`` boundary. The
wire is newline-delimited JSON-RPC 2.0 on stdin/stdout, UTF-8, one
message per line; stderr is the child's own free-form logging and never
part of the protocol. Request ids are small integers, monotonic per
direction, and the two directions' id spaces are independent — the same
rule every JSON-RPC stdio transport in this family follows.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TextIO

__all__ = [
    "CANCELLED",
    "INVALID_PARAMS",
    "METHOD_NOT_FOUND",
    "PROTOCOL_VERSION",
    "TOKENS_ENV",
    "PipelineCtx",
    "RpcError",
    "serve",
]

#: The protocol this SDK speaks. Copied, not imported — see the package
#: docstring — and pinned equal to ``paa.pipelines.base.PIPELINE_PROTOCOL``
#: by ``tests/pipelines/test_rpc_host.py``.
PROTOCOL_VERSION = "paa-pipeline/1"

#: The environment variable the host delivers the capability tokens in:
#: one JSON object mapping capability name to opaque token string. The
#: tokens are this run's and no one else's; treat them as secrets that
#: must never reach a log, a result or a summary.
TOKENS_ENV = "PAA_PIPELINE_TOKENS"

#: The pipeline-platform error codes this side can emit or recognise.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
CANCELLED = -32005

_RESULT_STATUSES = frozenset({"completed", "failed", "needs_human"})
_SUMMARY_MAX_CHARS = 500


class RpcError(Exception):
    """The host refused or failed a call; ``code`` is the wire error code."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class PipelineCtx:
    """The facade as the child sees it: typed async calls carrying ``{seq, token}``.

    Every capability method increments the sequence counter and attaches
    the token the host minted for that capability (absent when the run
    was not granted it — the host then refuses the call with ``-32006``,
    which surfaces here as :class:`RpcError`). ``now``/``random``/``progress``
    need no token: they are the always-served determinism aids and the
    heartbeat, deliberately outside the capability grant set.
    """

    def __init__(
        self,
        request: Callable[[str, dict[str, Any]], Awaitable[Any]],
        tokens: Mapping[str, str],
        recorded_calls: int,
        task: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        self._request = request
        self._tokens = dict(tokens)
        #: How many calls the host says were already journalled — the
        #: resume fact from ``pipeline.initialize``. A replaying pipeline
        #: re-issues its calls in the same order and the host serves the
        #: recorded results without re-executing them.
        self.recorded_calls = recorded_calls
        #: The task the host handed ``pipeline.initialize`` — the run's
        #: goal, as a mapping (``{"goal": …}`` in v1). The pipeline reads
        #: its work here and nowhere else: argv is the host's business,
        #: and a child that depends on its own argv is a child the wire
        #: cannot resume.
        self.task: dict[str, Any] = dict(task or {})
        #: The run's typed params (the declaration's ``[params]``, coerced
        #: and tightened host-side before the run ever launched).
        self.params: dict[str, Any] = dict(params or {})
        self._seq = 0

    @property
    def seq(self) -> int:
        """The sequence number of the last issued call (0 before any)."""
        return self._seq

    async def _call(self, method: str, capability: str | None, params: dict[str, Any]) -> Any:
        self._seq += 1
        params["seq"] = self._seq
        if capability is not None:
            params["token"] = self._tokens.get(capability)
        return await self._request(method, params)

    # -- capabilities (token-carrying) --------------------------------------

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> Any:
        """A model call with role, schema and budget."""
        params: dict[str, Any] = {"prompt": prompt, "role": role}
        if schema is not None:
            params["schema"] = dict(schema)
        if budget is not None:
            params["budget"] = budget
        return await self._call("ctx.think", "think", params)

    async def call_agent(self, role: str, message: Mapping[str, Any]) -> Any:
        """Reuse a built-in agent role."""
        return await self._call(
            "ctx.call_agent", "call_agent", {"role": role, "message": dict(message)}
        )

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> Any:
        """A skill or tool call; policy, sandbox and validation apply host-side."""
        return await self._call(
            "ctx.use_skill", "use_skill", {"skill": name, "args": dict(args)}
        )

    async def recall(self, query: str, *, k: int = 10) -> Any:
        """A bounded memory read."""
        return await self._call("ctx.recall", "recall", {"query": query, "k": k})

    async def remember(self, fact: Mapping[str, Any]) -> Any:
        """A memory write, through the host's memory creator."""
        return await self._call("ctx.remember", "remember", {"fact": dict(fact)})

    async def gate(self, payload: Mapping[str, Any]) -> Any:
        """Ask a human; the run parks until the gate is answered."""
        return await self._call("ctx.gate", "gate", {"payload": dict(payload)})

    async def plan_submit(self, steps: list[Mapping[str, Any]]) -> Any:
        """Offer a plan; the host validates schema and size."""
        return await self._call(
            "ctx.plan.submit", "plan_submit", {"steps": [dict(s) for s in steps]}
        )

    async def policy_check(self, plan_sha256: str) -> Any:
        """Request clearance for a plan."""
        return await self._call(
            "ctx.policy.check", "policy_check", {"plan_sha256": plan_sha256}
        )

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> Any:
        """Commit validated changes; requires a valid clearance."""
        return await self._call(
            "ctx.commit",
            "commit",
            {"steps_completed": steps_completed, "tokens_consumed": tokens_consumed},
        )

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> Any:
        """Run another pipeline; the child's budget halves and recursion is guarded."""
        return await self._call(
            "ctx.sub_run", "sub_run", {"name": name, "input": dict(task_input)}
        )

    # -- determinism aids and heartbeat (always served) ----------------------

    async def now(self) -> str:
        """Recorded time, as an ISO-8601 string. The only clock to read."""
        return str(await self._call("ctx.now", None, {}))

    async def random(self) -> float:
        """Recorded, seeded randomness. The only entropy to read."""
        return float(await self._call("ctx.random", None, {}))

    async def progress(self, status: str) -> None:
        """Heartbeat and status text; not journalled, feeds the watchdog."""
        await self._request("ctx.progress", {"status": status})


#: What a pipeline is: one callable handed the ``ctx`` object, returning
#: the final-result mapping (``status``/``summary``/``metrics``/``artifacts``).
#: Stated after the class it names: a type alias is a runtime assignment,
#: so a forward reference here would be a :class:`NameError` the moment the
#: module is imported — ``from __future__ import annotations`` defers only
#: *annotations*, not alias targets.
Pipeline = Callable[[PipelineCtx], Awaitable[Mapping[str, Any] | None]]


def _final_result(raw: Any) -> dict[str, Any]:
    """Fold the pipeline's return value into a valid final result.

    The closed vocabulary is the protocol's, not the pipeline's: a bad
    status word, a missing mapping or an oversized summary degrades to a
    named failure here, on the child, so the host never has to guess what
    the pipeline meant. Artifacts pass through shape-checked — the wire
    carries ``[{"kind", "sha256"}]`` and nothing else — because an
    artifact the host cannot name is an artifact the run cannot attest.
    """
    if not isinstance(raw, Mapping):
        return {"status": "failed", "summary": "pipeline returned no result object"}
    status = raw.get("status")
    if status not in _RESULT_STATUSES:
        return {"status": "failed", "summary": "pipeline result carries no valid status"}
    summary = raw.get("summary")
    text = summary if isinstance(summary, str) else ""
    out: dict[str, Any] = {"status": status, "summary": text[:_SUMMARY_MAX_CHARS]}
    metrics = raw.get("metrics")
    if isinstance(metrics, Mapping):
        out["metrics"] = dict(metrics)
    artifacts = raw.get("artifacts")
    if isinstance(artifacts, list):
        kept: list[dict[str, str]] = []
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            kind = artifact.get("kind")
            sha = artifact.get("sha256")
            if not isinstance(kind, str) or not kind:
                continue
            entry: dict[str, str] = {"kind": kind}
            if isinstance(sha, str) and sha:
                entry["sha256"] = sha
            kept.append(entry)
        if kept:
            out["artifacts"] = kept
    return out


async def serve(
    pipeline: Pipeline,
    *,
    name: str,
    version: str,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Run the child side of the protocol until ``pipeline.shutdown`` (or EOF).

    One call at a time per ``pipeline.run``; a ``pipeline.cancel`` cancels
    the running coroutine and the run request is answered with the
    ``-32005`` error. A crashed pipeline folds to a ``failed`` final
    result naming the exception *type*, never its text — a traceback can
    carry a secret, and the summary is not the place to find out.
    """
    loop = asyncio.get_running_loop()
    reader = stdin if stdin is not None else sys.stdin
    writer = stdout if stdout is not None else sys.stdout
    pending: dict[int, asyncio.Future[Any]] = {}
    next_id = 0

    def reply(message: Mapping[str, Any]) -> None:
        # Total over a dead pipe: once the host is gone there is nobody to
        # answer, and a BrokenPipeError unwinding through a cancelled run
        # would be noise on top of the exit that is already happening.
        try:
            writer.write(json.dumps(dict(message), separators=(",", ":")) + "\n")
            writer.flush()
        except OSError:
            pass

    async def request(method: str, params: dict[str, Any]) -> Any:
        nonlocal next_id
        my_id = next_id
        next_id += 1
        future: asyncio.Future[Any] = loop.create_future()
        pending[my_id] = future
        reply(
            {"jsonrpc": "2.0", "id": my_id, "method": method, "params": params}
        )
        result = await future
        if isinstance(result, Mapping) and "error" in result:
            error = result["error"]
            raise RpcError(
                int(error.get("code", -32603)),
                str(error.get("message", "")),
                error.get("data"),
            )
        return result.get("result") if isinstance(result, Mapping) else None

    async def run_and_answer(request_id: Any, ctx: PipelineCtx) -> None:
        try:
            raw = await pipeline(ctx)
        except asyncio.CancelledError:
            reply(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": CANCELLED, "message": "cancelled"},
                }
            )
            return
        except Exception as exc:  # fold to a named failure, never a traceback
            reply(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "status": "failed",
                        "summary": f"pipeline raised {type(exc).__name__}",
                    },
                }
            )
            return
        reply({"jsonrpc": "2.0", "id": request_id, "result": _final_result(raw)})

    run_task: asyncio.Task[None] | None = None
    ctx: PipelineCtx | None = None
    try:
        while True:
            line = await loop.run_in_executor(None, reader.readline)
            if not line:
                break  # EOF: the host is gone; nothing to answer
            text = line.strip()
            if not text:
                continue
            try:
                message: Any = json.loads(text)
            except json.JSONDecodeError:
                continue  # the protocol's frames are JSON; junk is not ours to answer
            if not isinstance(message, dict) or "id" not in message:
                continue  # v1 defines no host->child notifications
            request_id = message["id"]
            method = message.get("method")
            if method is None:
                # A *reply* to a call this child issued: route it to the
                # awaiting future. Requests always carry "method"; replies
                # carry "result" or "error" and echo the caller's id.
                # Conflating the two would answer a reply with a reply
                # (a -32601 loop that deadlocks the wire), and never
                # resolving the future would hang every ctx call.
                future = pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}
            if method == "pipeline.initialize":
                if params.get("protocol") != PROTOCOL_VERSION:
                    reply(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": INVALID_PARAMS,
                                "message": (
                                    "protocol mismatch: this pipeline speaks "
                                    f"{PROTOCOL_VERSION}"
                                ),
                            },
                        }
                    )
                    continue
                try:
                    tokens: dict[str, str] = json.loads(
                        os.environ.get(TOKENS_ENV, "{}")
                    )
                except json.JSONDecodeError:
                    tokens = {}
                recorded = params.get("resume", {})
                resume_mapping: Mapping[str, Any] = (
                    recorded if isinstance(recorded, Mapping) else {}
                )
                ctx = PipelineCtx(
                    request,
                    tokens,
                    int(resume_mapping.get("recorded_calls", 0)),
                    task=params.get("task") if isinstance(
                        params.get("task"), Mapping
                    ) else None,
                    params=params.get("params") if isinstance(
                        params.get("params"), Mapping
                    ) else None,
                )
                reply(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"ok": True, "name": name, "version": version},
                    }
                )
            elif method == "pipeline.run":
                if ctx is None:
                    # The protocol's order is initialize, then run; a run
                    # before the handshake is a malformed conversation, and
                    # the honest answer is the reserved invalid-request
                    # refusal rather than a crash of the child.
                    reply(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32600,
                                "message": "pipeline.run before pipeline.initialize",
                            },
                        }
                    )
                    continue
                run_task = asyncio.create_task(run_and_answer(request_id, ctx))
            elif method == "pipeline.cancel":
                if run_task is not None and not run_task.done():
                    run_task.cancel()
                reply({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
            elif method == "pipeline.shutdown":
                reply({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}})
                if run_task is not None and not run_task.done():
                    run_task.cancel()
                break
            else:
                reply(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": METHOD_NOT_FOUND,
                            "message": f"unknown method {method!r}",
                        },
                    }
                )
    finally:
        if run_task is not None and not run_task.done():
            run_task.cancel()
        for future in pending.values():
            if not future.done():
                future.set_exception(ConnectionError("host stream closed"))
