"""The RPC pipeline host — ``paa-pipeline/1`` over a contained child's stdio.

P3 of docs/plans/2026-11-21-pipeline-platform-spec.md §5, and the third
user of the frozen protocol (ADR-0055): the in-process facade speaks it as
typed calls, the C8.6 sandboxed runner speaks its result vocabulary, and
this module speaks it **as a wire** — newline-delimited JSON-RPC 2.0 on
the child's stdin/stdout, UTF-8, one message per line, stderr reserved
for the child's free-form logging.

What is reused, and why
-----------------------
* **The framing kit, imported** (``encode_frame`` / ``read_frame`` /
  ``result_response`` / ``error_response`` from :mod:`paa.mcp.protocol`)
  — the C7.6 ACP precedent: a third encoder would be the drift the SPEC 2
  §10 mitigation exists to prevent. The child's raw chunks are fed into a
  bare :class:`asyncio.StreamReader` so the kit's own ``read_frame`` does
  the line reassembly and blank-line skipping; nothing about framing is
  restated here.
* **The sandbox session, not a private subprocess** —
  :meth:`paa.sandbox.base.Sandbox.open` returns the duplex child the C5.2
  contract built for exactly this shape (its docstring names the pipeline
  RPC host as a consumer): the env is the allowlist builder's, the cwd is
  the workspace jail, the wall clock is the session's lifetime deadline,
  stderr is drained by the session's own capped capture (a chatty child
  cannot wedge on a full pipe), and ``close()`` is the **whole-tree kill**
  — SIGTERM with a short grace, then SIGKILL, grandchildren included.
  Cancel's "grace then process-tree kill" is therefore this machinery,
  reused rather than rebuilt; the host adds only the protocol-level
  courtesy (send ``pipeline.cancel`` and wait) before it takes the kill.
* **The call journal and the replay table** — every journalled call is
  PROPOSED before the facade effect and COMPLETED/FAILED after
  (:class:`~paa.pipelines.journal.CallJournal`), and a replaying run is
  served from the record through :class:`~paa.pipelines.replay.ReplayView`
  with divergence, never resync (ADR-0050), exactly as the in-process
  facades do. The journal's method names are the facade's (``think``),
  not the wire's (``ctx.think``): a call's identity is
  transport-independent, which is what makes the frozen protocol's three
  users one population.

The wire's own rules (spec §5), restated as this module's contract:

* the capability tokens travel **out of band** — one JSON object in the
  ``PAA_PIPELINE_TOKENS`` environment variable, granted capabilities only
  — and every call carries ``{seq, token}``; a call whose token is absent
  or wrong is refused ``-32006``. The token is stripped from the params
  before anything is journalled: a fingerprint that included it would
  differ run-to-run (defeating divergence detection) and the token would
  ride the ledger (the one thing ADR-0051 forbids);
* ``ctx.use_skill`` is served but **not** journalled here — the spec's
  §3.2 table sends it through the existing ``TOOL_CALL_*`` vocabulary,
  which is the facade implementation's own journaling, not the
  transport's;
* ``ctx.now`` / ``ctx.random`` are journalled (they are the determinism
  aids — a resumed run must be served its recorded clock and entropy) and
  ``ctx.progress`` is not (the heartbeat is never journalled, and it
  carries no ``seq``);
* host-side budgets: ``max_calls`` and ``max_sub_runs`` are enforced per
  call (``-32001``), ``max_wall_s`` is the session's lifetime clock (an
  exhausted clock kills the tree and fails the run with
  :class:`~paa.core.errors.BudgetExceededError` — the error table's own
  "wall time" row); ``max_tokens`` is *not* enforceable at this layer
  because the transport never sees token counts — the facade's ``think``
  budget owns it, and a driver that needs the guarantee tightens it there;
* the v1 surface is the wire-shaped subset of the facade:
  ``think``, ``use_skill``, ``recall``, ``remember``, ``commit``,
  ``sub_run``, ``plan.submit``, ``now``, ``random``, ``progress``. The
  projection-carrying three — ``call_agent``, ``gate``, ``policy.check``
  — answer ``-32601`` with the reason named, because their in-process
  parameter shapes (``TaskProjection``, ``AgentContext``) are exactly
  what :mod:`paa.pipelines.base` marks "will be reshaped by the RPC
  transport", and reshaping them is the facade-adapter integration's
  work, not the transport's.

The child's stderr is captured by the session (capped, the same bound the
one-shot path applies) and surfaced on the host (:attr:`child_stderr`)
for the run's transcript consumers. It is deliberately **never
journalled**: a child's log can hold anything, the ledger's payloads are
pattern-scanned by the no-secrets conformance check, and "captured to the
transcript" must not become "written where a secret would fail the run".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import structlog

from paa.core.errors import BudgetExceededError, SandboxError
from paa.mcp.protocol import (
    encode_frame,
    error_response,
    read_frame,
    result_response,
    wire_code_for,
)
from paa.pipelines.base import (
    FACADE_RUNTIME_VERSION,
    PIPELINE_PROTOCOL,
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    InvalidCallParamsError,
    PipelineArtifact,
    PipelineCancelledError,
    PipelineDivergedError,
    PipelineError,
    PipelineResult,
    PipelineStatus,
    RunLimits,
)
from paa.pipelines.journal import CallJournal
from paa.pipelines.registry import _parse_runtime_range
from paa.pipelines.replay import ReplayAction, ReplayedCallFailedError, ReplayView
from paa.pipelines.sandboxed import _bounded_summary, _metrics_of
from paa.pipelines.slots import PipelineDeclaration
from paa.sandbox.base import (
    Sandbox,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)

__all__ = [
    "CANCEL_GRACE_SECONDS",
    "HANDSHAKE_TIMEOUT_SECONDS",
    "RUN_TIMEOUT_SECONDS",
    "TOKENS_ENV",
    "RpcCapabilityFacade",
    "RpcPipelineError",
    "RpcPipelineHost",
]

log = structlog.get_logger(__name__)

#: The wall-clock budget one RPC run grants the child when neither the
#: declaration's ``max_wall_s`` nor the constructor names one — the same
#: role and the same number the sandboxed adapter grants an installed run,
#: so "what a run may expect" does not depend on which transport launched it.
RUN_TIMEOUT_SECONDS = 120.0

#: How long the handshake waits for ``pipeline.initialize``'s reply before
#: the child is declared unresponsive. Generous on purpose: a cold-starting
#: pipeline (an interpreter importing its own dependencies) is slow, not
#: broken, and refusing it here would punish exactly the heavyweight
#: out-of-process pipelines the wire exists to serve.
HANDSHAKE_TIMEOUT_SECONDS = 30.0

#: The courtesy window ``cancel`` gives the child to stop itself after
#: ``pipeline.cancel`` before the process-tree kill lands. The spec's own
#: grace rule: a well-behaved child flushes and exits; the rest is SIGKILL.
CANCEL_GRACE_SECONDS = 5.0

#: The environment variable the tokens ride — the child SDK's own constant
#: (``paa_pipeline.TOKENS_ENV``), restated by name because the host must
#: not import the child SDK: the no-``paa`` boundary runs both directions.
TOKENS_ENV = "PAA_PIPELINE_TOKENS"

#: The pipeline-platform wire code the JSON-RPC reserved table does not
#: name: a transport-level fault of the RPC host itself (a child that
#: died, answered out of contract, or never answered). Follows the
#: in-tree convention of extending past the spec's frozen
#: ``-32001..-32008`` run for platform errors (-32009 MCP auth,
#: -32012/-32013/-32014 manifest/selection/sandboxed).
_RPC_TRANSPORT_WIRE_CODE = -32015


class RpcPipelineError(PipelineError):
    """The RPC transport could not drive the pipeline as contracted."""

    wire_code = _RPC_TRANSPORT_WIRE_CODE


def _range_satisfied(range_text: str, version: str) -> bool:
    """Whether ``version`` satisfies a ``>=4.1,<5``-shaped range.

    The clause parser is the registry's own (imported, not restated, so
    the two cannot disagree about what a range *is*); the comparison is
    the tuple comparison every :data:`FACADE_RUNTIME_VERSION` check in
    this tree does. The spec's rule: the host refuses a pipeline whose
    declared range excludes the facade version — at run time, before any
    child is launched, because launching a child the host must immediately
    refuse is a waste of the launch and a lie about the handshake.
    """
    try:
        clauses = _parse_runtime_range(range_text)
        have = tuple(int(part) for part in version.split("."))
    except ValueError:
        return False
    for op, bound in clauses:
        cmp = (have > bound) - (have < bound)
        if op == ">=" and cmp < 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<" and cmp >= 0:
            return False
        if op == "==" and cmp != 0:
            return False
    return True


class RpcCapabilityFacade(Protocol):
    """The wire-shaped subset of the facade the RPC host forwards to.

    A structural subset of
    :class:`~paa.pipelines.base.CapabilityFacade` — every in-process
    facade implementation satisfies it — carrying exactly the operations
    whose parameters and results are JSON-shaped today (the module
    docstring names the three that are not, and why they answer
    ``-32601`` until the facade-adapter integration reshapes them).
    """

    async def think(
        self,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]: ...

    async def use_skill(self, name: str, args: Mapping[str, Any]) -> str: ...

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]: ...

    async def remember(self, fact: Mapping[str, Any]) -> None: ...

    async def submit_plan(self, steps: Sequence[Mapping[str, Any]]) -> None: ...

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None: ...

    async def sub_run(self, name: str, task_input: Mapping[str, Any]) -> PipelineResult: ...

    async def now(self) -> datetime: ...

    async def random(self) -> float: ...

    async def progress(self, status: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _WireMethod:
    """One dispatchable wire method: its journal name, grant, journaling."""

    #: The journal/facade name (``think``). The wire says ``ctx.think``;
    #: the journal has never carried the prefix, and a call's fingerprint
    #: must not depend on which transport issued it.
    method: str
    #: The capability a valid token must name, or ``None`` for the
    #: always-served determinism aids.
    capability: Capability | None
    #: Whether the transport journals this call in ``PIPELINE_CALL_*``.
    #: ``use_skill`` is the spec §3.2 exception: its journaling is the
    #: facade implementation's ``TOOL_CALL_*`` vocabulary, not the
    #: transport's; ``progress`` is never journalled and carries no seq.
    journalled: bool


_WIRE_METHODS: dict[str, _WireMethod] = {
    "ctx.think": _WireMethod("think", Capability.THINK, True),
    "ctx.use_skill": _WireMethod("use_skill", Capability.USE_SKILL, False),
    "ctx.recall": _WireMethod("recall", Capability.RECALL, True),
    "ctx.remember": _WireMethod("remember", Capability.REMEMBER, True),
    "ctx.commit": _WireMethod("commit", Capability.COMMIT, True),
    "ctx.sub_run": _WireMethod("sub_run", Capability.SUB_RUN, True),
    "ctx.plan.submit": _WireMethod("plan_submit", Capability.PLAN_SUBMIT, True),
    "ctx.now": _WireMethod("now", None, True),
    "ctx.random": _WireMethod("random", None, True),
    "ctx.progress": _WireMethod("progress", None, False),
}

#: The wire methods that exist in the vocabulary but are not servable by
#: this transport in v1 — their in-process parameter shapes are what
#: ``base.CapabilityFacade`` marks as the RPC transport's reshaping work.
_RESHAPED_METHODS = frozenset({"ctx.call_agent", "ctx.gate", "ctx.policy.check"})

#: The two params that are the transport's, not the call's: ``seq`` rides
#: the journal row's own key, and ``token`` is the out-of-band secret that
#: must never reach a fingerprint, a shape preview or a payload.
_TRANSPORT_PARAMS = frozenset({"seq", "token"})


def _wire_code(exc: Exception) -> int:
    """The JSON-RPC code for an exception raised while serving a call.

    The pipeline vocabulary's frozen ``wire_code`` attributes win; the
    shared map (:func:`paa.mcp.protocol.wire_code_for`, imported) covers
    the core errors the pipeline table reuses (``-32001``/``-32002``);
    everything else is the reserved internal error with a redacted
    message — the same fallback the MCP server applies to a ``PaaError``
    the table does not name.
    """
    if isinstance(exc, PipelineError):
        code = getattr(exc, "wire_code", None)
        if code is not None:
            return int(code)
    return wire_code_for(exc)


def _call_reason(exc: Exception) -> str:
    """The journalled reason for a failed call: a message or a type name.

    A :class:`PipelineError`'s message is the platform's own vocabulary
    (``capability is not granted to this run``) and names the refusing
    rule, so it rides; anything else is reduced to its type — an
    arbitrary exception's text can echo the child's own words back into
    the ledger, and the observe driver's rule (argv and secrets never in
    a failed-journal reason) applies to a child-supplied prompt too.
    """
    if isinstance(exc, PipelineError):
        return exc.message
    return type(exc).__name__


def _req_str(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidCallParamsError(
            f"the call's {key!r} param must be a non-empty string",
            param=key,
        )
    return value


def _opt_int(params: Mapping[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCallParamsError(
            f"the call's {key!r} param must be an int", param=key
        )
    return value


def _fold_run_result(raw: Any) -> PipelineResult:
    """The ``pipeline.run`` result as a :class:`PipelineResult`.

    The bounds are the sandboxed adapter's own (summary at the journal's
    reason budget, metrics as bounded scalars — imported, not restated,
    so a wire result and a subprocess result fold under the same rules).
    Artifacts pass shape-checked: the wire carries
    ``[{"kind", "sha256"}]`` and the fold keeps exactly that.
    """
    if not isinstance(raw, Mapping):
        return PipelineResult(
            status=PipelineStatus.FAILED,
            summary="pipeline.run carried no result object",
        )
    status = raw.get("status")
    try:
        resolved = PipelineStatus(str(status))
    except ValueError:
        return PipelineResult(
            status=PipelineStatus.FAILED,
            summary="pipeline.run result carries an unknown status; the "
            "vocabulary is closed",
        )
    artifacts: list[PipelineArtifact] = []
    raw_artifacts = raw.get("artifacts")
    if isinstance(raw_artifacts, list):
        for entry in raw_artifacts:
            if not isinstance(entry, Mapping):
                continue
            kind = entry.get("kind")
            sha = entry.get("sha256")
            if not isinstance(kind, str) or not kind:
                continue
            artifacts.append(
                PipelineArtifact(
                    kind=kind, sha256=sha if isinstance(sha, str) else None
                )
            )
    return PipelineResult(
        status=resolved,
        summary=_bounded_summary(raw.get("summary")),
        metrics=_metrics_of(raw.get("metrics")),
        artifacts=artifacts,
    )


class RpcPipelineHost:
    """Drives one pipeline child through the ``paa-pipeline/1`` wire.

    One instance, one run: the constructor takes the launch facts (the
    argv the declaration's entrypoint resolved to, the workspace jail,
    the sandbox backend, the minted grant set, the journal the driver
    minted for this run's lineage) and :meth:`run` performs the whole
    conversation — initialize, run, the calls in between, shutdown. The
    driver owns the run's lifecycle rows (``PIPELINE_RUN_*``), exactly as
    it does for the sandboxed adapter; this host owns the call rows and
    the divergence audit row.
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        journal: CallJournal,
        facade: RpcCapabilityFacade,
        grants: CapabilitySet,
        declaration: PipelineDeclaration,
        command: Sequence[str],
        workspace: Path,
        limits: RunLimits | None = None,
        timeout_s: float | None = RUN_TIMEOUT_SECONDS,
        handshake_timeout_s: float = HANDSHAKE_TIMEOUT_SECONDS,
        cancel_grace_s: float = CANCEL_GRACE_SECONDS,
        memory_mb: int = 256,
    ) -> None:
        if not command or any(not part for part in command):
            raise RpcPipelineError(
                "the launch argv must name an executable and its arguments",
                command=list(command[:3]),
            )
        self._sandbox = sandbox
        self._journal = journal
        self._facade = facade
        self._grants = grants
        self._declaration = declaration
        self._command = tuple(command)
        self._workspace = Path(workspace)
        self._limits = limits if limits is not None else declaration.limits
        self._timeout_s = timeout_s
        self._handshake_timeout_s = handshake_timeout_s
        self._cancel_grace_s = cancel_grace_s
        self._memory_mb = memory_mb
        # -- per-run state (one run per instance; run() sets the rest) --
        self._session: SandboxSession | None = None
        self._reader: asyncio.StreamReader | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._serve_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._run_future: asyncio.Future[dict[str, Any]] | None = None
        self._call_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._next_id = 0
        self._last_seq = 0
        self._calls_made = 0
        self._sub_runs_made = 0
        self._replay: ReplayView | None = None
        self._child_name: str | None = None
        self._child_version: str | None = None
        self._stderr_tail: str | None = None
        self._ran = False

    @property
    def journal(self) -> CallJournal:
        """The run's call journal — the driver's own run rows land on it."""
        return self._journal

    # -- post-run reads ---------------------------------------------------

    @property
    def child_name(self) -> str | None:
        """The name the child's initialize reply claimed, if it answered."""
        return self._child_name

    @property
    def child_version(self) -> str | None:
        """The version the child's initialize reply claimed, if it answered."""
        return self._child_version

    @property
    def child_stderr(self) -> str | None:
        """The child's captured stderr (capped), once the session closed.

        Surface this to the run's transcript consumers; it is deliberately
        not journalled (the module docstring's no-secrets rule).
        """
        return self._stderr_tail

    # -- the run ----------------------------------------------------------

    async def run(
        self,
        goal: str,
        *,
        params: Mapping[str, Any] | None = None,
        replay: ReplayView | None = None,
    ) -> PipelineResult:
        """One run: launch, initialize, ``pipeline.run``, shutdown.

        :raises RpcPipelineError: the declaration's runtime range excludes
            the facade version, or the child broke the handshake contract
            (no reply, an error reply, or an identity that is not the
            declaration's).
        :raises PipelineDivergedError: a replayed call's fingerprint
            differs from the journal; the run stops, the divergence row is
            the audit record, and the child's tree is killed.
        :raises BudgetExceededError: the wall clock ran out (the session
            killed the tree) — the error table's own "wall time" row.
        :raises PipelineCancelledError: :meth:`cancel` stopped the run,
            within the grace period or by the process-tree kill after it.
        """
        if self._ran:
            raise RpcPipelineError("one run per RpcPipelineHost instance")
        self._ran = True

        declared = self._declaration.requires_runtime
        if not _range_satisfied(declared, FACADE_RUNTIME_VERSION):
            raise RpcPipelineError(
                "the declaration's requires_runtime range excludes this "
                f"facade version ({FACADE_RUNTIME_VERSION}); the host refuses "
                "a pipeline it cannot speak for",
                declared_range=declared,
                facade_version=FACADE_RUNTIME_VERSION,
            )
        self._replay = replay
        await self._launch()
        try:
            await self._initialize(goal, params or {})
            reply = await self._await_run_reply()
        except TimeoutError as exc:
            await self._close()
            raise RpcPipelineError(
                "the handshake did not complete", reason=str(exc)
            ) from exc
        except BaseException:
            await self._close()
            raise
        if "error" in reply:
            await self._close()
            return self._run_error(reply)
        result = _fold_run_result(reply.get("result"))
        await self._shutdown()
        return result

    async def _launch(self) -> None:
        """Open the duplex child under the sandbox, tokens in the env."""
        tokens = {}
        for capability in self._grants:
            token = self._grants.token_for(capability)
            if token is not None:
                tokens[capability.value] = token.nonce
        wall = (
            self._limits.max_wall_s
            if self._limits.max_wall_s is not None
            else self._timeout_s
        )
        spec = SandboxSpec(
            command=self._command,
            workspace_path=self._workspace,
            env={TOKENS_ENV: json.dumps(tokens, separators=(",", ":"))},
            timeout_seconds=wall,
            memory_mb=self._memory_mb,
        )
        self._session = await self._sandbox.open(spec)
        # A bare StreamReader as the reassembly buffer: the pump feeds the
        # session's raw chunks in, the framing kit's read_frame drains
        # lines out — the kit owns the framing, this host owns nothing.
        self._reader = asyncio.StreamReader()
        self._pump_task = asyncio.create_task(self._pump())
        self._read_task = asyncio.create_task(self._read_loop())

    # -- the stream --------------------------------------------------------

    async def _pump(self) -> None:
        """Feed the session's stdout chunks into the framing reader."""
        reader = self._reader
        session = self._session
        assert reader is not None and session is not None
        try:
            while True:
                chunk = await session.read()
                if not chunk:
                    break  # EOF: the child closed stdout (crash or exit)
                reader.feed_data(chunk)
        except asyncio.CancelledError:
            raise
        except (SandboxError, OSError) as exc:
            log.warning("pipeline.rpc.read_pump_error", error=str(exc))
        finally:
            reader.feed_eof()

    async def _read_loop(self) -> None:
        """The framing kit's reader over the session, dispatching frames."""
        reader = self._reader
        assert reader is not None
        try:
            while True:
                text = await read_frame(reader)
                if text is None:
                    break
                if text:
                    self._dispatch(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a dead stream must not orphan callers
            log.warning("pipeline.rpc.read_loop_error", error=str(exc))
        finally:
            self._fail_pending()

    def _dispatch(self, text: str) -> None:
        """Route one decoded frame: a reply, or a child request to serve."""
        try:
            message: Any = json.loads(text)
        except json.JSONDecodeError:
            log.warning("pipeline.rpc.malformed_frame", preview=text[:120])
            return
        if not isinstance(message, dict):
            log.warning("pipeline.rpc.non_object_frame")
            return
        if message.get("method") is None:
            message_id = message.get("id")
            future = (
                self._pending.pop(message_id, None)
                if message_id is not None
                else None
            )
            if future is not None and not future.done():
                future.set_result(message)
            elif message_id is not None:
                log.warning("pipeline.rpc.unknown_response_id", id=message_id)
            return
        if "id" in message:
            # A child→host request: served as its own task so the read
            # loop keeps flowing — a long think must not stall the stream
            # that carries the progress heartbeats riding beside it.
            task = asyncio.create_task(self._serve(message))
            self._serve_tasks.add(task)
            task.add_done_callback(self._serve_tasks.discard)

    def _fail_pending(self) -> None:
        """Resolve every outstanding host request as a dead stream."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    ConnectionError("the pipeline child's stream closed")
                )
        self._pending.clear()
        if self._run_future is not None and not self._run_future.done():
            self._run_future.set_exception(
                ConnectionError("the pipeline child exited before answering")
            )

    async def _send(self, message: Mapping[str, Any]) -> None:
        """Write one frame under the write lock (the ACP client's rule)."""
        session = self._session
        if session is None:
            raise ConnectionError("the pipeline child has not been launched")
        async with self._write_lock:
            try:
                await session.write(encode_frame(dict(message)))
            except SandboxError as exc:
                raise ConnectionError(
                    "the pipeline child's pipe is broken"
                ) from exc

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """One host→child request, awaiting its correlated reply."""
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        # Registered before the send: a reply cannot precede its request
        # on a serial stream, and registering first makes that a fact of
        # the code rather than a fact of the scheduling.
        self._pending[message_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": method,
                "params": dict(params),
            }
        )
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise TimeoutError(
                f"the child did not answer {method!r} within {timeout_seconds}s"
            ) from None

    # -- the handshake and the run request ---------------------------------

    async def _initialize(self, goal: str, params: Mapping[str, Any]) -> None:
        """The handshake: pinned protocol, facade version, granted names."""
        limits = {
            key: value
            for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs")
            if (value := getattr(self._limits, key)) is not None
        }
        init_params: dict[str, Any] = {
            "protocol": PIPELINE_PROTOCOL,
            "runtime_version": FACADE_RUNTIME_VERSION,
            "run_id": str(self._journal.run_id),
            "task": {"goal": goal},
            "params": dict(params),
            # Names, never tokens: the token's channel is the env var, and
            # the initialize frame is the one place a lazy implementation
            # would be tempted to inline them.
            "granted": sorted(capability.value for capability in self._grants),
            "limits": limits,
            "resume": {
                "recorded_calls": len(self._replay) if self._replay else 0
            },
        }
        try:
            reply = await self._request(
                "pipeline.initialize",
                init_params,
                timeout_seconds=self._handshake_timeout_s,
            )
        except TimeoutError as exc:
            raise RpcPipelineError(
                "the child did not complete the pipeline.initialize handshake",
                timeout_s=self._handshake_timeout_s,
            ) from exc
        if "error" in reply:
            error = reply["error"]
            raise RpcPipelineError(
                "the child refused the handshake",
                code=error.get("code") if isinstance(error, dict) else None,
                detail=str(error.get("message")) if isinstance(error, dict) else "",
            )
        result = reply.get("result")
        if not isinstance(result, dict) or not result.get("ok"):
            raise RpcPipelineError("the child's initialize reply is not an ok result")
        name = result.get("name")
        version = result.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise RpcPipelineError(
                "the child's initialize reply carries no string name/version",
            )
        if name != self._declaration.name or version != self._declaration.version:
            raise RpcPipelineError(
                "the child that answered is not the pipeline the host launched",
                replied=f"{name}/{version}",
                declared=(
                    f"{self._declaration.name}/{self._declaration.version}"
                ),
            )
        self._child_name = name
        self._child_version = version

    async def _await_run_reply(self) -> dict[str, Any]:
        """Send ``pipeline.run`` and await the answer.

        No request-level timeout: the session's wall clock is the bound
        (an exhausted clock kills the tree, the pump sees EOF, and the
        run future resolves as a dead stream the caller folds into the
        budget refusal).
        """
        self._run_future = asyncio.get_running_loop().create_future()
        message_id = self._next_id
        self._next_id += 1
        self._pending[message_id] = self._run_future
        await self._send(
            {"jsonrpc": "2.0", "id": message_id, "method": "pipeline.run"}
        )
        try:
            reply = await self._run_future
        except ConnectionError:
            return await self._child_died()
        finally:
            self._pending.pop(message_id, None)
        return reply

    async def _child_died(self) -> dict[str, Any]:
        """Fold a dead child into the run's named refusal.

        The close is what collects the facts (exit code, timeout flag,
        stderr); the wall-clock kill is the budget refusal the error table
        names, and every other death is a transport fault.
        """
        result = await self._close()
        if result is not None and (
            result.timed_out or result.killed_reason == "timeout"
        ):
            raise BudgetExceededError(
                "max_wall_s",
                float(self._limits.max_wall_s or self._timeout_s or 0.0),
                0.0,
                detail="the wall clock ran out and the child's tree was killed",
            )
        raise RpcPipelineError(
            "the pipeline child exited before answering pipeline.run",
            exit_code=result.exit_code if result is not None else None,
        )

    def _run_error(self, reply: Mapping[str, Any]) -> PipelineResult:
        """Interpret a ``pipeline.run`` error reply as the run's outcome."""
        error = reply.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == -32005:
            raise PipelineCancelledError(
                "the pipeline stopped itself within the grace period"
            )
        detail = str(error.get("message")) if isinstance(error, dict) else ""
        raise RpcPipelineError(
            "pipeline.run answered an error", code=code, detail=detail
        )

    async def _shutdown(self) -> None:
        """The courtesy close: ``pipeline.shutdown``, then reap the tree."""
        # The child is exiting or gone; either way the close below reaps.
        with contextlib.suppress(TimeoutError, ConnectionError):
            await self._request(
                "pipeline.shutdown", {}, timeout_seconds=self._cancel_grace_s
            )
        await self._close()

    async def _close(self) -> SandboxResult | None:
        """Close the session (whole-tree kill if still live) and drain.

        Idempotent, and it never awaits the task that called it — the
        divergence stop runs *inside* a serve task, and a close that
        waited on its own caller would be a deadlock wearing a timeout.
        """
        async with self._close_lock:
            if self._closed:
                return None
            self._closed = True
            result: SandboxResult | None = None
            session = self._session
            if session is not None:
                result = await session.close()
                self._stderr_tail = result.stderr
            current = asyncio.current_task()
            for task in (self._pump_task, self._read_task, *self._serve_tasks):
                if task is None or task.done() or task is current:
                    continue
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
            return result

    # -- serving the child's calls -----------------------------------------

    async def _serve(self, message: Mapping[str, Any]) -> None:
        """Serve one child→host request: a capability call or a heartbeat."""
        request_id = message.get("id")
        wire_method = str(message.get("method"))
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            spec = _WIRE_METHODS.get(wire_method)
            if spec is None:
                if wire_method in _RESHAPED_METHODS:
                    reply: Mapping[str, Any] = error_response(
                        request_id,
                        -32601,
                        f"{wire_method} is not servable over "
                        f"{PIPELINE_PROTOCOL} yet: its parameter shapes are "
                        "the in-process facade's and are reshaped by the "
                        "facade-adapter integration, not this transport",
                    )
                else:
                    reply = error_response(
                        request_id, -32601, f"unknown method {wire_method!r}"
                    )
                await self._send(reply)
                return
            if spec.method == "progress":
                # The heartbeat: no seq, no journal, no lock — a long
                # think must never stall the heartbeat riding beside it.
                status = _req_str(params, "status")
                await self._facade.progress(status)
                await self._send(result_response(request_id, {"ok": True}))
                return
            await self._serve_call(request_id, spec, params)
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            # The pipe is gone: there is nobody to answer, and an unwind
            # here would be noise on top of the exit already happening
            # (the SDK child's own reply() rule, restated host-side).
            pass
        except Exception as exc:
            code = _wire_code(exc)
            with contextlib.suppress(ConnectionError):
                await self._send(
                    error_response(request_id, code, _call_reason(exc))
                )

    async def _serve_call(
        self, request_id: Any, spec: _WireMethod, params: Mapping[str, Any]
    ) -> None:
        """One seq-carrying call: sequence, replay, journal, budget, grant.

        The lock serialises journalled calls so the ledger's row order is
        the arrival order even against a child that pipelines requests —
        the journal's meaning ("seq N is the Nth call") must not depend on
        the child's scheduling. The spec §3.3 pseudocode's order is kept
        exactly: the PROPOSED row lands before the token, budget and mode
        checks, so a refused call is still a call the record saw.
        """
        async with self._call_lock:
            seq = params.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise InvalidCallParamsError(
                    "the call's 'seq' param must be a positive int", param="seq"
                )
            if seq != self._last_seq + 1:
                # Monotonicity is the journal's addressing: a skipped or
                # repeated seq is a call the record cannot speak for.
                raise InvalidCallParamsError(
                    "the call's 'seq' must be exactly one past the previous "
                    f"call's (expected {self._last_seq + 1}, got {seq})",
                    param="seq",
                )
            self._last_seq = seq
            journal_params = {
                key: value
                for key, value in params.items()
                if key not in _TRANSPORT_PARAMS
            }
            if self._replay is not None:
                decision = self._replay.check(seq, spec.method, journal_params)
                if decision.action is ReplayAction.SERVE:
                    await self._send(result_response(request_id, decision.result))
                    return
                if decision.action is ReplayAction.DIVERGED:
                    await self._journal.run_diverged(
                        seq,
                        decision.expected_fingerprint or "",
                        decision.actual_fingerprint or "",
                    )
                    await self._send(
                        error_response(
                            request_id,
                            PipelineDivergedError.wire_code,
                            "the call sequence diverged from the journal; "
                            "the run stops and never resyncs",
                        )
                    )
                    await self._stop_the_run(
                        PipelineDivergedError(
                            seq,
                            decision.expected_fingerprint or "",
                            decision.actual_fingerprint or "",
                        )
                    )
                    return
                if decision.action is ReplayAction.OUTCOME_UNKNOWN:
                    await self._send(
                        error_response(
                            request_id,
                            -32008,
                            "the recorded outcome of this call cannot be "
                            "classified; the pipeline decides (D-6)",
                        )
                    )
                    return
                if decision.action is ReplayAction.FAIL:
                    await self._send(
                        error_response(
                            request_id,
                            ReplayedCallFailedError.wire_code,
                            "the recorded outcome of this call is a real "
                            f"failure and it stands: {decision.reason}",
                        )
                    )
                    return
                # EXECUTE: a new call, or a pure call whose interrupted
                # outcome is safe to resolve by re-execution. Fall through.
            self._calls_made += 1
            if (
                self._limits.max_calls is not None
                and self._calls_made > self._limits.max_calls
            ):
                raise BudgetExceededError(
                    "max_calls",
                    float(self._limits.max_calls),
                    float(self._calls_made - 1),
                )
            if spec.method == "sub_run":
                self._sub_runs_made += 1
                if (
                    self._limits.max_sub_runs is not None
                    and self._sub_runs_made > self._limits.max_sub_runs
                ):
                    raise BudgetExceededError(
                        "max_sub_runs",
                        float(self._limits.max_sub_runs),
                        float(self._sub_runs_made - 1),
                    )
            if spec.journalled:
                await self._journal.call_proposed(seq, spec.method, journal_params)
            try:
                if spec.capability is not None:
                    self._require_token(spec.capability, params.get("token"))
                result = await self._execute(spec.method, journal_params)
            except asyncio.CancelledError:
                if spec.journalled:
                    await asyncio.shield(
                        self._journal.call_failed(seq, spec.method, "cancelled")
                    )
                raise
            except Exception as exc:
                if spec.journalled:
                    await asyncio.shield(
                        self._journal.call_failed(seq, spec.method, _call_reason(exc))
                    )
                raise
            if spec.journalled:
                await self._journal.call_completed(seq, spec.method, result)
            await self._send(result_response(request_id, result))

    def _require_token(self, capability: Capability, token: Any) -> None:
        """The grant check: the wire token must be this run's minted nonce.

        An ungranted capability has no token in the child's env at all
        (the mint only covers the granted set), so the absent-token and
        forged-token cases are the same refusal — which is the point of
        delivering tokens out of band: there is nothing to guess.
        """
        minted = self._grants.token_for(capability)
        expected = minted.nonce if minted is not None else None
        if not isinstance(token, str) or token != expected:
            raise CapabilityNotGrantedError(
                capability.value, (cap.value for cap in self._grants)
            )

    async def _execute(self, method: str, params: Mapping[str, Any]) -> Any:
        """Dispatch one validated call to the facade, JSON in, JSON out."""
        if method == "think":
            schema = params.get("schema")
            if schema is not None and not isinstance(schema, Mapping):
                raise InvalidCallParamsError(
                    "the call's 'schema' param must be an object", param="schema"
                )
            budget = _opt_int(params, "budget")
            role = params.get("role", "worker")
            if not isinstance(role, str) or not role:
                raise InvalidCallParamsError(
                    "the call's 'role' param must be a non-empty string",
                    param="role",
                )
            return await self._facade.think(
                _req_str(params, "prompt"),
                schema=dict(schema) if schema is not None else None,
                role=role,
                budget=budget,
            )
        if method == "use_skill":
            args = params.get("args")
            if not isinstance(args, Mapping):
                raise InvalidCallParamsError(
                    "the call's 'args' param must be an object", param="args"
                )
            return await self._facade.use_skill(_req_str(params, "skill"), dict(args))
        if method == "recall":
            k = params.get("k", 10)
            if isinstance(k, bool) or not isinstance(k, int) or k < 1:
                raise InvalidCallParamsError(
                    "the call's 'k' param must be a positive int", param="k"
                )
            return await self._facade.recall(_req_str(params, "query"), k=k)
        if method == "remember":
            fact = params.get("fact")
            if not isinstance(fact, Mapping):
                raise InvalidCallParamsError(
                    "the call's 'fact' param must be an object", param="fact"
                )
            await self._facade.remember(dict(fact))
            return None
        if method == "commit":
            steps = _opt_int(params, "steps_completed")
            tokens_consumed = _opt_int(params, "tokens_consumed")
            if steps is None or tokens_consumed is None:
                raise InvalidCallParamsError(
                    "ctx.commit needs int 'steps_completed' and 'tokens_consumed'",
                )
            await self._facade.commit(
                steps_completed=steps, tokens_consumed=tokens_consumed
            )
            return None
        if method == "sub_run":
            task_input = params.get("input")
            if not isinstance(task_input, Mapping):
                raise InvalidCallParamsError(
                    "the call's 'input' param must be an object", param="input"
                )
            child = await self._facade.sub_run(
                _req_str(params, "name"), dict(task_input)
            )
            return {
                "status": child.status.value,
                "summary": child.summary,
                "metrics": dict(child.metrics),
            }
        if method == "plan_submit":
            steps = params.get("steps")
            if not isinstance(steps, list):
                raise InvalidCallParamsError(
                    "the call's 'steps' param must be an array", param="steps"
                )
            await self._facade.submit_plan(steps)
            return None
        if method == "now":
            return (await self._facade.now()).isoformat()
        if method == "random":
            return await self._facade.random()
        raise RpcPipelineError(  # pragma: no cover - the table is closed
            f"no handler for journal method {method!r}"
        )

    # -- cancellation -------------------------------------------------------

    async def cancel(self, reason: str = "cancelled") -> None:
        """Stop the run: the courtesy window, then the process-tree kill.

        Sends ``pipeline.cancel`` and waits one grace period for the child
        to stop itself (the SDK answers ``pipeline.run`` with ``-32005``
        and exits); a child that does not is killed as a whole tree — the
        sandbox session's own SIGTERM-grace-then-SIGKILL — and the run
        resolves as cancelled with the kill named. Safe to call before,
        during or after :meth:`run`; a run that already resolved is a
        no-op.
        """
        if self._session is None or self._closed:
            return
        run_future = self._run_future
        if run_future is not None and run_future.done():
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._cancel_grace_s
        # The grace is spent or the child is gone; the kill decides.
        with contextlib.suppress(TimeoutError, ConnectionError):
            await self._request(
                "pipeline.cancel",
                {"reason": reason},
                timeout_seconds=self._cancel_grace_s,
            )
        if run_future is not None:
            remaining = deadline - loop.time()
            if remaining > 0 and not run_future.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_future), timeout=remaining
                    )
                    return  # the child answered within the grace period
                except TimeoutError:
                    pass
        # The grace is spent: resolve the run as cancelled *before* the
        # kill, so the read loop's EOF handling cannot race this verdict.
        if run_future is not None and not run_future.done():
            run_future.set_exception(
                PipelineCancelledError(
                    "the pipeline did not stop within the grace period; "
                    "its process tree was killed"
                )
            )
        await self._close()

    async def _stop_the_run(self, error: Exception) -> None:
        """Fail the run and kill the tree — the divergence stop."""
        if self._run_future is not None and not self._run_future.done():
            self._run_future.set_exception(error)
        await self._close()
